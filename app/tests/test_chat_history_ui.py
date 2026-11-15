"""Persistent, isolated study conversations exercised through the real widgets."""

from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QInputDialog

from app.models.assignment import Assignment
from app.models.course import Course
from app.services.ai_service import AIAnswer, AIServiceError
from app.services.chat_history import ChatHistoryStore
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.ai_chat import AIChatPage, CachedCanvasReadOnlySource


class RecordingService:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.answer = "这是已保存的回答。"

    def ask(self, question, **kwargs):
        self.calls.append((question, deepcopy(kwargs)))
        if self.failure is not None:
            raise self.failure
        return AIAnswer(self.answer, ("Canvas → source",), False)


@pytest.fixture(autouse=True)
def isolated_canvas_cache():
    session_cache.clear()
    yield
    session_cache.clear()


@pytest.fixture
def database():
    value = SQLiteDatabase(":memory:")
    yield value
    value.close()


def make_page(qtbot, database, service=None):
    service = service or RecordingService()
    page = AIChatPage(service, database=database)
    qtbot.addWidget(page)
    page.resize(1000, 720)
    page.show()
    QApplication.processEvents()
    return page, service


def send(qtbot, page, question):
    page.question_input.setText(question)
    page.ask_button.click()
    qtbot.waitUntil(lambda: getattr(page, "_thread", None) is None, timeout=10_000)
    QApplication.processEvents()


def choose(combo, value):
    index = combo.findData(value)
    assert index >= 0, f"Missing combo identity {value!r}"
    combo.setCurrentIndex(index)
    QApplication.processEvents()


def message_field(message, name):
    return message[name] if isinstance(message, dict) else getattr(message, name)


def text_dialog(monkeypatch, value, *, accepted=True):
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: (value, accepted))


def add_group(page, monkeypatch, name):
    text_dialog(monkeypatch, name)
    page.new_group_button.click()
    QApplication.processEvents()
    return page.group_combo.currentData()


def new_chat(page):
    page.new_chat_button.click()
    QApplication.processEvents()


def test_questions_and_answers_survive_page_recreation(qtbot, database):
    page, service = make_page(qtbot, database)
    send(qtbot, page, "请记住我关于作业的这个问题。")
    identity = page._current_conversation_id
    assert identity is not None
    assert len(service.calls) == 1
    reopened, fresh_service = make_page(qtbot, database)
    assert reopened._current_conversation_id == identity
    text = reopened.chat_view.toPlainText()
    assert "请记住我关于作业的这个问题。" in text
    assert service.answer in text
    assert fresh_service.calls == []


def test_history_survives_closing_and_reopening_database(qtbot, tmp_path):
    path = tmp_path / "history.sqlite3"
    database = SQLiteDatabase(path)
    page, _service = make_page(qtbot, database)
    send(qtbot, page, "下次启动仍然需要看到的提问")
    identity = page._current_conversation_id
    page.close()
    database.close()
    reopened_database = SQLiteDatabase(path)
    try:
        reopened, _ = make_page(qtbot, reopened_database)
        assert reopened._current_conversation_id == identity
        assert "下次启动仍然需要看到的提问" in reopened.chat_view.toPlainText()
        assert "这是已保存的回答" in reopened.chat_view.toPlainText()
    finally:
        reopened_database.close()


def test_refresh_does_not_clear_selected_conversation(qtbot, database):
    page, _ = make_page(qtbot, database)
    send(qtbot, page, "刷新页面不能清除我问过的问题")
    identity = page._current_conversation_id
    before = page.chat_view.toPlainText()
    page.refresh()
    assert page._current_conversation_id == identity
    assert page.chat_view.toPlainText() == before


def test_new_conversations_are_independent_and_do_not_leak_context(qtbot, database):
    page, service = make_page(qtbot, database)
    send(qtbot, page, "First conversation private question")
    first = page._current_conversation_id
    new_chat(page)
    assert "First conversation private question" not in page.chat_view.toPlainText()
    send(qtbot, page, "Second conversation independent question")
    second = page._current_conversation_id
    assert first != second
    assert not service.calls[-1][1].get("history")
    choose(page.conversation_combo, first)
    assert "First conversation private question" in page.chat_view.toPlainText()
    assert "Second conversation independent question" not in page.chat_view.toPlainText()
    send(qtbot, page, "Follow-up only to the first conversation")
    context = repr(service.calls[-1][1].get("history"))
    assert "First conversation private question" in context
    assert "Second conversation independent question" not in context
    choose(page.conversation_combo, second)
    assert "Follow-up only to the first conversation" not in page.chat_view.toPlainText()


def test_follow_up_receives_previous_user_and_assistant_not_current_question_twice(qtbot, database):
    page, service = make_page(qtbot, database)
    send(qtbot, page, "Initial question")
    send(qtbot, page, "Unique follow-up question")
    history = service.calls[-1][1]["history"]
    roles = [message_field(item, "role") for item in history]
    assert roles == ["user", "assistant"]
    assert message_field(history[0], "content") == "Initial question"
    assert message_field(history[1], "content") == service.answer
    assert "Unique follow-up question" not in repr(history)


def test_course_groups_scope_questions_and_keep_their_own_conversations(qtbot, database):
    session_cache.set("courses", [
        Course(id=10, name="Data Science", course_code="COMP5310"),
        Course(id=20, name="Statistics", course_code="STAT5002"),
    ])
    page, service = make_page(qtbot, database)
    science = "course:10"
    choose(page.group_combo, science)
    send(qtbot, page, "Data-science-only question")
    first = page._current_conversation_id
    assert service.calls[-1][1]["course_id"] == 10
    choose(page.group_combo, "course:20")
    assert "Data-science-only question" not in page.chat_view.toPlainText()
    send(qtbot, page, "Statistics-only question")
    assert service.calls[-1][1]["course_id"] == 20
    assert not service.calls[-1][1].get("history")
    choose(page.group_combo, science)
    assert page._current_conversation_id == first
    assert "Data-science-only question" in page.chat_view.toPlainText()
    assert "Statistics-only question" not in page.chat_view.toPlainText()


def test_assignment_only_startup_creates_course_groups_and_scopes_questions(qtbot, database):
    session_cache.set("assignments", [
        Assignment(assignment_id=101, course_id=10, course_name="Project Management",
                   course_code="PMGT5891", title="Group report"),
        Assignment(assignment_id=102, course_id=10, course_name="Project Management",
                   course_code="PMGT5891", title="Individual reflection"),
        Assignment(assignment_id=201, course_id=20, course_name="Data Science",
                   course_code="COMP5310", title="Analysis report"),
    ])
    assert session_cache.get("courses") is None
    page, service = make_page(qtbot, database)
    assert [page.group_combo.itemData(index) for index in range(page.group_combo.count())] == [
        "general", "course:10", "course:20",
    ]
    choose(page.group_combo, "course:10")
    assert page.group_combo.currentText() == "Project Management"
    send(qtbot, page, "这门课程的报告问题")
    assert service.calls[-1][1]["course_id"] == 10
    assert ChatHistoryStore(database).list_messages(page._current_conversation_id)[0].content == "这门课程的报告问题"


def test_authoritative_courses_take_precedence_and_assignment_courses_are_deduplicated():
    courses = [
        Course(id=10, name="Authoritative course name", course_code="PMGT5891"),
        {"id": 30, "name": "Course without cached assignments", "course_code": "STAT5002"},
        {"id": 10, "name": "Duplicate stale course name", "course_code": "OLD"},
    ]
    assignments = [
        Assignment(assignment_id=101, course_id=10, course_name="Stale assignment course name",
                   course_code="STALE", title="Old cached assignment"),
        {"course_id": 20, "course_name": " Assignment-derived course ", "course_code": "COMP5310", "token": "not-course-metadata"},
        SimpleNamespace(course_id=20, course_name="Duplicate assignment course", course_code="DUPLICATE"),
    ]
    before_courses, before_assignments = deepcopy(courses), deepcopy(assignments)
    session_cache.set("courses", courses)
    session_cache.set("assignments", assignments)
    result = CachedCanvasReadOnlySource.get_courses()
    assert result == [
        {"id": 10, "name": "Authoritative course name", "course_code": "PMGT5891"},
        {"id": 30, "name": "Course without cached assignments", "course_code": "STAT5002"},
        {"id": 20, "name": "Assignment-derived course", "course_code": "COMP5310"},
    ]
    assert courses == before_courses
    assert assignments == before_assignments
    assert all(set(course) == {"id", "name", "course_code"} for course in result)


def test_renamed_assignment_derived_group_and_history_survive_refresh_and_later_course_sync(qtbot, database, monkeypatch):
    session_cache.set("assignments", [
        Assignment(assignment_id=101, course_id=10, course_name="Project Management",
                   course_code="PMGT5891", title="Group report"),
    ])
    page, _service = make_page(qtbot, database)
    choose(page.group_combo, "course:10")
    send(qtbot, page, "从启动缓存恢复的课程问题需要一直保留")
    identity = page._current_conversation_id
    text_dialog(monkeypatch, "我的 5891 报告")
    page.rename_group_button.click()
    text_dialog(monkeypatch, "小组报告准备")
    page.rename_chat_button.click()
    before = page.chat_view.toPlainText()
    page.refresh()
    assert page.group_combo.currentData() == "course:10"
    assert page.group_combo.currentText() == "我的 5891 报告"
    assert page._current_conversation_id == identity
    assert page.chat_view.toPlainText() == before
    session_cache.set("courses", [
        Course(id=10, name="Updated authoritative course name", course_code="PMGT5891"),
        Course(id=20, name="New course from Canvas", course_code="COMP5310"),
    ])
    page.refresh()
    assert page.group_combo.currentText() == "我的 5891 报告"
    assert page._current_conversation_id == identity
    assert page.conversation_combo.currentText() == "小组报告准备"
    assert page.chat_view.toPlainText() == before
    assert page.group_combo.findData("course:20") >= 0
    reopened, _ = make_page(qtbot, database)
    assert reopened.group_combo.currentText() == "我的 5891 报告"
    assert reopened._current_conversation_id == identity
    assert reopened.chat_view.toPlainText() == before


def test_invalid_assignment_course_entries_are_skipped_without_hiding_valid_groups(qtbot, database):
    invalid = [
        object(), {}, None,
        {"course_id": True, "course_name": "Boolean ID"},
        {"course_id": False, "course_name": "Boolean ID"},
        {"course_id": 0, "course_name": "Zero ID"},
        {"course_id": -1, "course_name": "Negative ID"},
        {"course_id": 2**63, "course_name": "Overflow ID"},
        {"course_id": "10", "course_name": "String ID"},
        {"course_id": 10.0, "course_name": "Float ID"},
        {"course_id": 11, "course_name": ""},
        {"course_id": 12, "course_name": "   ", "course_code": "NO_NAME_FALLBACK"},
        {"course_id": 13, "course_name": None},
        {"course_id": 14, "course_name": ["Not text"]},
    ]
    valid = {"course_id": 20, "course_name": "Valid cached course", "course_code": "COMP5310"}
    session_cache.set("assignments", invalid + [valid])
    assert CachedCanvasReadOnlySource.get_courses() == [
        {"id": 20, "name": "Valid cached course", "course_code": "COMP5310"},
    ]
    page, _service = make_page(qtbot, database)
    assert [page.group_combo.itemData(index) for index in range(page.group_combo.count())] == [
        "general", "course:20",
    ]
    assert page.ask_button.isEnabled()


def test_custom_group_and_conversation_can_be_renamed_without_losing_messages(qtbot, database, monkeypatch):
    page, service = make_page(qtbot, database)
    group = add_group(page, monkeypatch, "考试复习")
    send(qtbot, page, "复习分组中的历史问题")
    conversation = page._current_conversation_id
    assert service.calls[-1][1]["course_id"] is None
    text_dialog(monkeypatch, "期末复习")
    page.rename_group_button.click()
    assert page.group_combo.currentData() == group
    assert "期末复习" in page.group_combo.currentText()
    text_dialog(monkeypatch, "关于第二周的提问")
    page.rename_chat_button.click()
    assert page._current_conversation_id == conversation
    assert "关于第二周的提问" in page.conversation_combo.currentText()
    reopened, _ = make_page(qtbot, database)
    assert reopened.group_combo.currentData() == group
    assert "期末复习" in reopened.group_combo.currentText()
    assert "关于第二周的提问" in reopened.conversation_combo.currentText()
    assert "复习分组中的历史问题" in reopened.chat_view.toPlainText()


def test_failed_answer_preserves_the_question_and_can_be_followed_up(qtbot, database):
    page, service = make_page(qtbot, database)
    service.failure = AIServiceError("The AI provider could not complete the request.")
    send(qtbot, page, "这个问题即使回答失败也要保留")
    identity = page._current_conversation_id
    assert "这个问题即使回答失败也要保留" in page.chat_view.toPlainText()
    assert page.question_input.text() == ""
    reopened, _ = make_page(qtbot, database)
    assert reopened._current_conversation_id == identity
    assert "这个问题即使回答失败也要保留" in reopened.chat_view.toPlainText()
    service.failure = None
    send(qtbot, page, "继续刚才的问题")
    history = service.calls[-1][1]["history"]
    assert [message_field(item, "role") for item in history] == ["user"]
    assert message_field(history[0], "content") == "这个问题即使回答失败也要保留"


def test_html_is_rendered_as_literal_conversation_text(qtbot, database):
    page, service = make_page(qtbot, database)
    question = '<script>alert("question")</script><img src="https://example.invalid/pixel">'
    service.answer = '<b>not markup</b><a href="https://example.invalid/answer">literal link</a>'
    send(qtbot, page, question)
    plain = page.chat_view.toPlainText()
    assert question in plain
    assert service.answer in plain
    html = page.chat_view.toHtml()
    assert '<img src="https://example.invalid/pixel"' not in html
    assert '<a href="https://example.invalid/answer"' not in html
    reopened, _ = make_page(qtbot, database)
    assert question in reopened.chat_view.toPlainText()
    assert service.answer in reopened.chat_view.toPlainText()


def test_cancel_rename_and_create_leave_existing_selection_and_history(qtbot, database, monkeypatch):
    page, _ = make_page(qtbot, database)
    send(qtbot, page, "不应因取消对话框而丢失")
    identity = page._current_conversation_id
    before = page.chat_view.toPlainText()
    labels = [page.group_combo.itemText(i) for i in range(page.group_combo.count())]
    text_dialog(monkeypatch, "不采用", accepted=False)
    page.new_group_button.click()
    page.rename_group_button.click()
    page.rename_chat_button.click()
    assert page._current_conversation_id == identity
    assert page.chat_view.toPlainText() == before
    assert [page.group_combo.itemText(i) for i in range(page.group_combo.count())] == labels


def test_failed_question_save_keeps_composer_and_does_not_call_service(qtbot, database, monkeypatch):
    store = ChatHistoryStore(database)
    service = RecordingService()
    page = AIChatPage(service, database=database, history_store=store)
    qtbot.addWidget(page)

    def cannot_save(*_args, **_kwargs):
        raise OSError("Private storage failure detail must not reach the user")

    monkeypatch.setattr(store, "append_message", cannot_save)
    send(qtbot, page, "不能保存时不要清空这个问题")
    assert page.question_input.text() == "不能保存时不要清空这个问题"
    assert service.calls == []
    assert "Private storage failure detail" not in page.status_label.text()
    assert page.status_label.text().strip()


def test_credential_shaped_question_and_answer_are_redacted_before_persisting(qtbot, database):
    page, service = make_page(qtbot, database)
    service.answer = "请勿保存 api_key=example-answer-credential-secretvalue"
    send(qtbot, page, "请解释这行 password=example-user-credential-secretvalue")
    identity = page._current_conversation_id
    messages = ChatHistoryStore(database).list_messages(identity)
    assert [item.role for item in messages] == ["user", "assistant"]
    combined = "\n".join(item.content for item in messages)
    assert "example-user-credential-secretvalue" not in combined
    assert "example-answer-credential-secretvalue" not in combined
    assert "[REDACTED]" in combined
    assert "example-user-credential-secretvalue" not in page.chat_view.toPlainText()
    assert "example-answer-credential-secretvalue" not in page.chat_view.toPlainText()
    assert "example-user-credential-secretvalue" not in repr(service.calls)


def test_move_conversation_keeps_history_and_changes_future_course_scope(qtbot, database, monkeypatch):
    session_cache.set("courses", [Course(id=10, name="Data Science", course_code="COMP5310")])
    page, service = make_page(qtbot, database)
    target = add_group(page, monkeypatch, "小组项目")
    choose(page.group_combo, "course:10")
    send(qtbot, page, "移到自定义分组后仍需保留")
    identity = page._current_conversation_id
    before = ChatHistoryStore(database).list_messages(identity)

    def pick_group(*args, **kwargs):
        labels = args[3] if len(args) > 3 else kwargs["items"]
        choices = [text for text in labels if "小组项目" in text]
        assert len(choices) == 1
        return choices[0], True

    monkeypatch.setattr(QInputDialog, "getItem", pick_group)
    page.move_chat_button.click()
    assert page.group_combo.currentData() == target
    assert page._current_conversation_id == identity
    store = ChatHistoryStore(database)
    assert store.get_conversation(identity).group_id == target
    assert store.list_messages(identity) == before
    send(qtbot, page, "移动后继续提问")
    assert service.calls[-1][1]["course_id"] is None
    assert "移到自定义分组后仍需保留" in repr(service.calls[-1][1]["history"])


def test_clear_cached_canvas_data_does_not_delete_chat(qtbot, database):
    page, _ = make_page(qtbot, database)
    send(qtbot, page, "清除课程缓存后仍需保留的对话")
    identity = page._current_conversation_id
    database.clear_cache()
    session_cache.clear()
    page.refresh()
    reopened, _ = make_page(qtbot, database)
    assert reopened._current_conversation_id == identity
    assert "清除课程缓存后仍需保留的对话" in reopened.chat_view.toPlainText()


def test_last_selected_group_and_older_conversation_are_restored(qtbot, database, monkeypatch):
    page, _ = make_page(qtbot, database)
    group = add_group(page, monkeypatch, "自定义课程笔记")
    send(qtbot, page, "稍早的对话")
    earlier = page._current_conversation_id
    new_chat(page)
    send(qtbot, page, "较新的对话")
    assert page._current_conversation_id != earlier
    choose(page.conversation_combo, earlier)
    reopened, _ = make_page(qtbot, database)
    assert reopened._current_group_id == group
    assert reopened._current_conversation_id == earlier
    assert "稍早的对话" in reopened.chat_view.toPlainText()
    assert "较新的对话" not in reopened.chat_view.toPlainText()


def test_repeated_new_chat_and_empty_send_do_not_create_empty_history(qtbot, database):
    page, service = make_page(qtbot, database)
    store = ChatHistoryStore(database)
    for _ in range(3):
        new_chat(page)
        send(qtbot, page, "   ")
    assert store.list_conversations() == []
    assert service.calls == []


def test_old_questions_remain_visible_beyond_provider_context_window(qtbot, database):
    store = ChatHistoryStore(database)
    conversation = store.create_conversation("general", "完整提问历史")
    for number in range(45):
        store.append_message(conversation.id, "user", f"History question {number:03d}")
        store.append_message(conversation.id, "assistant", f"History answer {number:03d}")
    page, _ = make_page(qtbot, database)
    text = page.chat_view.toPlainText()
    assert "History question 000" in text
    assert "History answer 000" in text
    assert "History question 044" in text
    assert "History answer 044" in text
    page.refresh()
    assert page.chat_view.toPlainText() == text


def test_unexpected_service_failure_does_not_lose_question_or_expose_diagnostics(qtbot, database):
    page, service = make_page(qtbot, database)
    service.failure = RuntimeError("PRIVATE_BACKEND_DIAGNOSTIC")
    send(qtbot, page, "即使后端发生意外也要保留我的问题")
    assert "即使后端发生意外也要保留我的问题" in page.chat_view.toPlainText()
    assert "PRIVATE_BACKEND_DIAGNOSTIC" not in page.chat_view.toPlainText()
    assert "PRIVATE_BACKEND_DIAGNOSTIC" not in page.status_label.text()
    messages = ChatHistoryStore(database).list_messages(page._current_conversation_id)
    assert messages[0].role == "user"
    assert messages[0].content == "即使后端发生意外也要保留我的问题"


def test_async_answer_keeps_event_loop_responsive_and_prevents_cross_chat_switch(qtbot, database):
    class WaitingService(RecordingService):
        def __init__(self):
            super().__init__()
            self.started = Event()
            self.release = Event()

        def ask(self, question, **kwargs):
            self.started.set()
            assert self.release.wait(5), "Test did not release the fake service"
            return super().ask(question, **kwargs)

    service = WaitingService()
    page, _ = make_page(qtbot, database, service)
    ticks = []
    page.question_input.setText("等待网络时也必须保留")
    try:
        page.ask_button.click()
        qtbot.waitUntil(service.started.is_set, timeout=1_000)
        QTimer.singleShot(0, lambda: ticks.append(True))
        qtbot.waitUntil(lambda: bool(ticks), timeout=1_000)
        assert not page.ask_button.isEnabled()
        assert not page.group_combo.isEnabled()
        assert not page.conversation_combo.isEnabled()
        assert not page.new_chat_button.isEnabled()
        identity = page._current_conversation_id
        assert ChatHistoryStore(database).list_messages(identity)[0].content == "等待网络时也必须保留"
        page.refresh()
        assert page._current_conversation_id == identity
        assert "等待网络时也必须保留" in page.chat_view.toPlainText()
    finally:
        service.release.set()
        qtbot.waitUntil(lambda: getattr(page, "_thread", None) is None, timeout=10_000)
    assert page.ask_button.isEnabled()
    assert page.group_combo.isEnabled()
    assert page._current_conversation_id == identity
    assert service.answer in page.chat_view.toPlainText()


def test_closing_while_answer_runs_waits_for_worker_and_keeps_saved_messages(qtbot, database):
    class DeferredService(RecordingService):
        def __init__(self):
            super().__init__()
            self.started = Event()
            self.release = Event()

        def ask(self, question, **kwargs):
            self.started.set()
            assert self.release.wait(5), "Test did not release the fake service"
            return super().ask(question, **kwargs)

    service = DeferredService()
    page, _ = make_page(qtbot, database, service)
    page.question_input.setText("关闭过程中也不能丢失的问题")
    try:
        page.ask_button.click()
        qtbot.waitUntil(service.started.is_set, timeout=1_000)
        identity = page._current_conversation_id
        assert page.close() is False
        assert page.isVisible()
    finally:
        service.release.set()
        qtbot.waitUntil(lambda: getattr(page, "_thread", None) is None, timeout=10_000)
    qtbot.waitUntil(lambda: not page.isVisible(), timeout=1_000)
    messages = ChatHistoryStore(database).list_messages(identity)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "关闭过程中也不能丢失的问题"
