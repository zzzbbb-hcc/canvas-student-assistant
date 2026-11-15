from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
import sqlite3

import pytest

from app.models.course import Course
from app.services.chat_history import (
    ChatConversation, ChatGroup, ChatHistoryStore, ChatMessage,
    MAX_CONVERSATION_TITLE, MAX_GROUP_NAME, MAX_MESSAGE_LENGTH,
    MAX_SOURCE_LENGTH, MAX_SOURCES, redact_chat_text,
)
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase


@pytest.fixture
def chat_parts(tmp_path):
    path = tmp_path / "history.sqlite3"
    database = SQLiteDatabase(path)
    yield database, ChatHistoryStore(database), path
    database.close()


def test_default_group_is_initialized_once_and_models_are_frozen(chat_parts):
    database, store, _ = chat_parts
    assert store.list_groups() == [ChatGroup("general", "未分组")]
    assert ChatHistoryStore(database).list_groups() == store.list_groups()
    group = store.create_group("复习")
    conversation = store.create_conversation(group.id)
    message = store.append_message(conversation.id, "user", "问题")
    for model, name, replacement in (
        (group, "name", "其他"), (conversation, "title", "其他"), (message, "content", "其他"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(model, name, replacement)


def test_saved_questions_answers_sources_and_errors_survive_reopen(chat_parts):
    _, store, path = chat_parts
    course = Course(id=12, name="数据科学")
    store.ensure_course_groups([course])
    group = store.create_group("考试复习")
    conversation = store.create_conversation("course:12", "每周问答")
    original_question = "  我之前问过的问题：\n第一部分，A + B？\n第二部分：123。  "
    user = store.append_message(conversation.id, "user", original_question)
    answer = store.append_message(conversation.id, "assistant", "回答\n不改变格式", ("Canvas → 课题 A", "Canvas → 模块 B"))
    second = store.append_message(conversation.id, "user", "没有回答的问题？")
    error = store.append_message(conversation.id, "error", "暂时无法回答，请重试。")
    # Moving and renaming never replaces or deletes the historical transcript.
    store.rename_group(group.id, "自定义名称")
    store.move_conversation(conversation.id, group.id)
    renamed = store.rename_conversation(conversation.id, "原来的问答")
    reopened = SQLiteDatabase(path)
    try:
        other = ChatHistoryStore(reopened)
        assert other.get_conversation(conversation.id) == renamed
        assert other.list_messages(conversation.id) == [user, answer, second, error]
        assert other.list_messages(conversation.id)[0].content == original_question
        assert other.list_messages(conversation.id)[1].sources == ("Canvas → 课题 A", "Canvas → 模块 B")
        assert group.id in [item.id for item in other.list_groups() if item.name == "自定义名称"]
    finally:
        reopened.close()


def test_unanswered_question_remains_without_any_assistant_message(chat_parts):
    _, store, _ = chat_parts
    conversation = store.create_conversation("general")
    question = store.append_message(conversation.id, "user", "以后接着回答这个问题")
    assert store.list_messages(conversation.id) == [question]


def test_course_groups_have_stable_ids_preserve_names_and_do_not_delete_old_courses(chat_parts):
    _, store, _ = chat_parts
    store.ensure_course_groups([Course(id=1, name="课程 A"), {"id": 2, "name": "课程 B"}])
    store.rename_group("course:1", "自己改的名字")
    store.ensure_course_groups([Course(id=1, name="老师改了课程名字"), Course(id=3, name="课程 C")])
    assert {group.id: group.name for group in store.list_groups()} == {
        "general": "未分组", "course:1": "自己改的名字", "course:2": "课程 B", "course:3": "课程 C",
    }


def test_course_group_batch_validates_atomically(chat_parts):
    _, store, _ = chat_parts
    with pytest.raises(ValueError):
        store.ensure_course_groups([Course(id=1, name="课程 A"), {"id": -2, "name": "坏数据"}])
    assert store.list_groups() == [ChatGroup("general", "未分组")]


def test_cache_clear_preserves_all_chat_data_and_metadata(chat_parts):
    database, store, _ = chat_parts
    Cache(database).set("courses", [{"id": 1}])
    group = store.create_group("持久分组")
    conversation = store.create_conversation(group.id, "持久标题")
    question = store.append_message(conversation.id, "user", "持久问题")
    answer = store.append_message(conversation.id, "assistant", "持久回答", ("Canvas → 来源",))
    assert database.clear_cache() == 1
    assert store.list_groups() == [ChatGroup("general", "未分组"), group]
    assert store.get_conversation(conversation.id).title == "持久标题"
    assert store.list_messages(conversation.id) == [question, answer]


def test_legacy_database_migrates_without_losing_existing_content(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE preferences (preference_key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO preferences VALUES ('display', '"compact"', '2026-10-16T01:00:00+00:00');
        """)
    database = SQLiteDatabase(path)
    try:
        store = ChatHistoryStore(database)
        assert database.get_preference("display") == "compact"
        assert store.create_conversation("general").title == "新对话"
    finally:
        database.close()


def test_chat_queries_are_parameterized_and_ids_do_not_collide(chat_parts):
    _, store, _ = chat_parts
    group = store.create_group("O'Reilly; DROP TABLE chat_messages;--")
    conversations = [store.create_conversation(group.id, "引号 ' 和 \" 不影响保存") for _ in range(5)]
    assert len({item.id for item in conversations}) == 5
    for conversation in conversations:
        store.append_message(conversation.id, "user", "'; DELETE FROM chat_messages; --")
    assert len(store.list_conversations(group.id)) == 5
    assert store.get_conversation("' OR 1=1 --") is None
    assert store.list_messages("' OR 1=1 --") == []
    assert sum(len(store.list_messages(item.id)) for item in conversations) == 5


def test_conversation_sort_and_message_order_are_deterministic(chat_parts, monkeypatch):
    _, store, _ = chat_parts
    import app.services.chat_history as history
    monkeypatch.setattr(history, "_now", lambda: "2026-10-16T01:00:00.000000+00:00")
    first = store.create_conversation("general", "一")
    second = store.create_conversation("general", "二")
    assert [item.id for item in store.list_conversations()] == sorted([first.id, second.id])
    monkeypatch.setattr(history, "_now", lambda: "2026-10-16T02:00:00.000000+00:00")
    messages = [store.append_message(first.id, role, role) for role in ("user", "assistant", "error")]
    assert store.list_conversations()[0].id == first.id
    assert store.list_messages(first.id) == messages
    assert all(item.created_at.tzinfo == UTC for item in messages)


def test_failed_message_insert_rolls_back_conversation_timestamp(chat_parts):
    database, store, _ = chat_parts
    conversation = store.create_conversation("general")
    with database._transaction() as connection:
        connection.execute("CREATE TRIGGER reject_chat BEFORE INSERT ON chat_messages BEGIN SELECT RAISE(ABORT, 'test rejection'); END")
    with pytest.raises(sqlite3.IntegrityError, match="test rejection"):
        store.append_message(conversation.id, "assistant", "不能保存的回答")
    assert store.get_conversation(conversation.id) == conversation
    assert store.list_messages(conversation.id) == []


def test_foreign_keys_prevent_orphan_messages_and_moves(chat_parts):
    database, store, _ = chat_parts
    conversation = store.create_conversation("general")
    with pytest.raises(ValueError):
        store.append_message("missing", "user", "问题")
    with pytest.raises(ValueError):
        store.move_conversation(conversation.id, "missing")
    with pytest.raises(ValueError):
        store.create_conversation("missing")
    assert store.get_conversation(conversation.id) == conversation
    assert database._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        with database._transaction() as connection:
            connection.execute("INSERT INTO chat_messages (conversation_id, role, content, sources_json, created_at) VALUES (?, ?, ?, ?, ?)",
                               ("missing", "user", "问题", "[]", datetime.now(UTC).isoformat()))


@pytest.mark.parametrize("bad", ["", " ", None, 12, "a\x00b"])
def test_empty_or_invalid_text_is_rejected_without_partial_writes(chat_parts, bad):
    _, store, _ = chat_parts
    conversation = store.create_conversation("general")
    with pytest.raises(ValueError):
        store.create_group(bad)
    with pytest.raises(ValueError):
        store.rename_conversation(conversation.id, bad)
    with pytest.raises(ValueError):
        store.append_message(conversation.id, "user", bad)
    assert store.get_conversation(conversation.id) == conversation
    assert store.list_messages(conversation.id) == []


@pytest.mark.parametrize("bad", ["", " ", None, 1, True, "x\n", "x" * 129])
def test_invalid_id_fields_are_rejected(chat_parts, bad):
    _, store, _ = chat_parts
    for action in (
        lambda: store.create_conversation(bad), lambda: store.list_messages(bad),
        lambda: store.get_conversation(bad), lambda: store.rename_group(bad, "名字"),
        lambda: store.list_conversations(bad) if bad is not None else store.move_conversation("missing", bad),
    ):
        with pytest.raises(ValueError):
            action()


@pytest.mark.parametrize("bad_role", ["system", "tool", "USER", "", None, 1, []])
def test_invalid_roles_do_not_touch_conversation(chat_parts, bad_role):
    _, store, _ = chat_parts
    conversation = store.create_conversation("general")
    with pytest.raises(ValueError):
        store.append_message(conversation.id, bad_role, "问题")
    assert store.get_conversation(conversation.id) == conversation
    assert store.list_messages(conversation.id) == []


def test_bounds_reject_rather_than_silently_truncate(chat_parts):
    _, store, _ = chat_parts
    group = store.create_group("组" * MAX_GROUP_NAME)
    conversation = store.create_conversation(group.id, "题" * MAX_CONVERSATION_TITLE)
    content = "问" * MAX_MESSAGE_LENGTH
    assert store.append_message(conversation.id, "user", content).content == content
    for action in (
        lambda: store.create_group("组" * (MAX_GROUP_NAME + 1)),
        lambda: store.rename_conversation(conversation.id, "题" * (MAX_CONVERSATION_TITLE + 1)),
        lambda: store.append_message(conversation.id, "user", "问" * (MAX_MESSAGE_LENGTH + 1)),
        lambda: store.append_message(conversation.id, "assistant", "回答", ["源"] * (MAX_SOURCES + 1)),
        lambda: store.append_message(conversation.id, "assistant", "回答", ["源" * (MAX_SOURCE_LENGTH + 1)]),
    ):
        with pytest.raises(ValueError):
            action()
    assert len(store.list_messages(conversation.id)) == 1


@pytest.mark.parametrize("sources", ["not a sequence", b"bytes", {"source": "value"}, [1], [""], None])
def test_invalid_sources_fail_before_insert(chat_parts, sources):
    _, store, _ = chat_parts
    conversation = store.create_conversation("general")
    with pytest.raises(ValueError):
        store.append_message(conversation.id, "assistant", "回答", sources)
    assert store.list_messages(conversation.id) == []


@pytest.mark.parametrize("raw,secret", [
    ("Authorization: Bearer abcdefghijklm\n请解释这个公式。", "abcdefghijklm"),
    ("检查 authorization=Bearer abcdefghijklm，然后解释。", "abcdefghijklm"),
    ('{"Authorization": "Bearer abcdefghijklm", "question": "解释公式"}', "abcdefghijklm"),
    ("我的 API_KEY='abcdefghi'，问题是这个公式。", "abcdefghi"),
    ("Cookie: session=abc123; secure=xyz\n继续问问题", "abc123"),
    ("https://canvas.example/test?access_token=abcdefghi&course=12", "abcdefghi"),
    ("https://canvas.example/test#refresh_token=abcdefghi&course=12", "abcdefghi"),
    ("https://canvas.example/test?access%5Ftoken=abcdefghi&course=12", "abcdefghi"),
    ("https://person:abcdefghi@canvas.example/test", "abcdefghi"),
    ("raw sk-proj-abcdefghijklmnopqrs question", "abcdefghijklmnopqrs"),
    ("raw SUPER_SECRET_CHAT_SENTINEL and ordinary question", "SUPER_SECRET_CHAT_SENTINEL"),
    ("canvas_pat=abcdefghi 后面是我的正常问题", "abcdefghi"),
    ("session_id:abcdefghi 请回答问题", "abcdefghi"),
])
def test_credentials_are_redacted_before_binding_and_never_reach_database_bytes(chat_parts, raw, secret):
    database, store, path = chat_parts
    group = store.create_group(raw)
    conversation = store.create_conversation(group.id, raw)
    message = store.append_message(conversation.id, "user", raw, (raw,))
    assert secret not in group.name
    assert secret not in conversation.title
    assert secret not in message.content
    assert secret not in message.sources[0]
    assert "[REDACTED]" in message.content
    # Inspect while the connection is open so WAL is covered too. Credentials
    # must not merely be deleted after reaching a durable journal.
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if candidate.exists():
            assert secret.encode() not in candidate.read_bytes()
    assert database._connection.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 1


def test_redaction_does_not_change_ordinary_question_format():
    question = "  这是我的问题：\n公式 Token(A) = A / B。\n请解释 student_id 的含义，而不是我的学号。\n链接 https://example.org/a?course=12  "
    assert redact_chat_text(question) == question


def test_secret_url_parameter_does_not_discard_non_secret_course_or_question():
    original = "https://example.org/a?access%5Ftoken=abcdefghi&course=12\n请解释课件第二页？"
    assert redact_chat_text(original) == "https://example.org/a?access%5Ftoken=[REDACTED]&course=12\n请解释课件第二页？"


def test_many_concurrent_messages_are_neither_lost_nor_duplicated(chat_parts):
    _, store, _ = chat_parts
    conversation = store.create_conversation("general")
    with ThreadPoolExecutor(max_workers=4) as executor:
        messages = list(executor.map(lambda number: store.append_message(conversation.id, "user", f"问题 {number}"), range(40)))
    stored = store.list_messages(conversation.id)
    assert len(stored) == 40
    assert {item.id for item in stored} == {item.id for item in messages}
    assert {item.content for item in stored} == {f"问题 {number}" for number in range(40)}


def test_methods_return_documented_model_types(chat_parts):
    _, store, _ = chat_parts
    assert isinstance(store.rename_group("general", "其他"), ChatGroup)
    conversation = store.create_conversation("general")
    assert isinstance(conversation, ChatConversation)
    assert isinstance(store.rename_conversation(conversation.id, "换名"), ChatConversation)
    assert isinstance(store.move_conversation(conversation.id, "general"), ChatConversation)
    assert isinstance(store.append_message(conversation.id, "assistant", "你好"), ChatMessage)
