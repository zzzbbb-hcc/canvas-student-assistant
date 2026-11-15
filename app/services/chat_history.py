"""Durable local conversations, independent of Canvas and online AI providers.

Questions are saved before requesting an answer, so failed/unanswered requests
can be retained. This store never contacts a service or keyring. It strips known
credential shapes before passing any text to SQLite; ordinary text is preserved
verbatim. The database is local per-user SQLite, not encrypted storage.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
import sqlite3
from urllib.parse import unquote_plus
from uuid import uuid4

from app.security.log_filter import CredentialLogFilter
from app.security.sanitizer import sanitize_for_ai
from app.storage.database import SQLiteDatabase, assert_non_sensitive


MAX_GROUP_NAME = 200
MAX_CONVERSATION_TITLE = 300
MAX_MESSAGE_LENGTH = 128_000
MAX_SOURCES = 512
MAX_SOURCE_LENGTH = 2_048

_SECRET_KEY = (
    r"(?:access[\s_-]*token|refresh[\s_-]*token|(?:canvas[\s_-]*)?pat|"
    r"(?:canvas[\s_-]*|ed[\s_-]*|ai[\s_-]*)?token|authorization|"
    r"proxy[\s_-]*authorization|set[\s_-]*cookie|cookies?|session(?:[\s_-]*id)?|"
    r"password|(?:ai[\s_-]*|x[\s_-]*)?api[\s_-]*key|secret)"
)
_SECRET_HEADER = re.compile(
    rf"(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key)\s*:\s*)[^\r\n]+"
)
_INLINE_AUTH = re.compile(
    r"(?i)\b((?:proxy[\s_-]*)?authorization\s*[:=]\s*)"
    r"(?:[\"']?(?:bearer|basic)\s+[^\s,;\r\n\"']+[\"']?)"
)
_SECRET_VALUE = re.compile(
    rf"(?i)(?<![\w])({_SECRET_KEY})([\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\r\n&#<>]+)"
)
_AUTH_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+[a-z0-9._~+/=-]+")
_RAW_KEY = re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{8,}\b")
_SENTINEL = re.compile(r"SUPER_SECRET_[^\s\"'<>]*")
_URL_USERINFO = re.compile(r"(?i)(https?://)[^\s/@]+@")
_URL_PARAMETER = re.compile(r"([?&#])([^\s?&#=]+)=([^\s&#]*)")
_URL_SECRET_KEYS = frozenset({
    "access_token", "refresh_token", "token", "canvas_pat", "pat", "ed_token",
    "ai_api_key", "api_key", "apikey", "authorization", "auth", "cookie", "cookies",
    "session", "session_id", "password", "secret", "x_api_key",
    "verifier", "signature", "sig", "x_amz_signature", "x_amz_credential",
    "x_amz_security_token", "x_goog_signature",
})


def redact_chat_text(value: str) -> str:
    """Remove credential-shaped spans without discarding a legitimate question.

    No heuristic can recognize arbitrary unlabelled secrets; the UI must still
    warn against pasting credentials. This handles copied headers, explicit
    key/value fields (including URL query/fragment tokens), raw OpenAI key
    shapes and the project's fail-closed security test sentinel.
    """

    if not isinstance(value, str):
        raise ValueError("Chat text must be a string.")
    # Hide already handled spans while applying the shared sanitizers: their
    # diagnostic-oriented patterns otherwise swallow unrelated URL parameters
    # after a token. Placeholder identity is random to prevent user text from
    # colliding with a placeholder and being replaced on restoration.
    replacements: dict[str, str] = {}
    nonce = uuid4().hex

    def remember(redacted: str) -> str:
        key = f"__CHATSAFE_{nonce}_{len(replacements)}__"
        replacements[key] = redacted
        return key

    def parameter(match: re.Match[str]) -> str:
        key = re.sub(r"[^a-z0-9]+", "_", unquote_plus(match[2]).casefold()).strip("_")
        if key in _URL_SECRET_KEYS:
            return remember(match[1] + match[2] + "=[REDACTED]")
        return match[0]

    safe = _SECRET_HEADER.sub(lambda m: remember(m[1] + "[REDACTED]"), value)
    safe = _INLINE_AUTH.sub(lambda m: remember(m[1] + "[REDACTED]"), safe)
    safe = _AUTH_VALUE.sub(lambda m: remember("[REDACTED]"), safe)
    safe = _URL_PARAMETER.sub(parameter, safe)
    safe = _SECRET_VALUE.sub(lambda m: remember(m[1] + m[2] + "[REDACTED]"), safe)
    safe = _URL_USERINFO.sub(lambda m: remember(m[1] + "[REDACTED]@"), safe)
    safe = _RAW_KEY.sub(lambda m: remember("[REDACTED]"), safe)
    safe = _SENTINEL.sub(lambda m: remember("[REDACTED]"), safe)
    # Keep these shared defences in the at-rest boundary as well as AI payloads.
    safe = sanitize_for_ai(safe, remove_personal_data=False)
    safe = CredentialLogFilter._redact(safe)
    assert isinstance(safe, str)
    for placeholder, redacted in replacements.items():
        safe = safe.replace(placeholder, redacted)
    assert_non_sensitive(safe)
    return safe


@dataclass(frozen=True, slots=True)
class ChatGroup:
    id: str
    name: str
    course_id: int | None = None


@dataclass(frozen=True, slots=True)
class ChatConversation:
    id: str
    group_id: str
    title: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ChatMessage:
    id: int
    conversation_id: str
    role: str
    content: str
    sources: tuple[str, ...]
    created_at: datetime


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _identifier(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 128
            or value != value.strip() or any(ord(char) < 32 for char in value)):
        raise ValueError("Invalid chat identity.")
    return value


def _text(value: str, limit: int, *, strip: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Chat text cannot be empty.")
    if len(value) > limit:
        raise ValueError("Chat text exceeds the supported length.")
    # NUL is not a meaningful chat character and causes misleading SQL/string
    # inspection in some tools. Reject it, rather than silently dropping it.
    if "\x00" in value:
        raise ValueError("Chat text contains an unsupported character.")
    safe = redact_chat_text(value.strip() if strip else value)
    if len(safe) > limit:
        raise ValueError("Redacted chat text exceeds the supported length.")
    return safe


def _group(row: sqlite3.Row) -> ChatGroup:
    return ChatGroup(row["id"], row["name"], row["course_id"])


def _conversation(row: sqlite3.Row) -> ChatConversation:
    return ChatConversation(
        row["id"], row["group_id"], row["title"],
        datetime.fromisoformat(row["created_at"]), datetime.fromisoformat(row["updated_at"]),
    )


class ChatHistoryStore:
    """Append-only message retention with renameable groups and conversations.

    Uses the database gateway's lock/transaction so a message and its updated
    conversation timestamp either both commit or neither commits. There is
    deliberately no delete API, and cache clearing does not touch these tables.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        with database._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO chat_groups (id, name, course_id, created_at) VALUES (?, ?, ?, ?)",
                ("general", "未分组", None, _now()),
            )

    def list_groups(self) -> list[ChatGroup]:
        with self.database._lock:
            rows = self.database._connection.execute(
                "SELECT id, name, course_id FROM chat_groups "
                "ORDER BY CASE WHEN id = 'general' THEN 0 ELSE 1 END, created_at, id"
            ).fetchall()
        return [_group(row) for row in rows]

    def ensure_course_groups(self, courses: Iterable[object]) -> list[ChatGroup]:
        # Validate every row before a transaction: malformed later courses must
        # not leave half the requested groups added.
        prepared: list[tuple[str, str, int, str]] = []
        timestamp = _now()
        for course in courses:
            course_id = course.get("id") if isinstance(course, Mapping) else getattr(course, "id", None)
            name = course.get("name") if isinstance(course, Mapping) else getattr(course, "name", None)
            if isinstance(course_id, bool) or not isinstance(course_id, int) or not 0 < course_id < 2**63:
                raise ValueError("Course IDs must be positive integers.")
            prepared.append((f"course:{course_id}", _text(name, MAX_GROUP_NAME, strip=True), course_id, timestamp))
        with self.database._transaction() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO chat_groups (id, name, course_id, created_at) VALUES (?, ?, ?, ?)",
                prepared,
            )
        return self.list_groups()

    def create_group(self, name: str) -> ChatGroup:
        group = ChatGroup(f"group:{uuid4().hex}", _text(name, MAX_GROUP_NAME, strip=True))
        with self.database._transaction() as connection:
            connection.execute(
                "INSERT INTO chat_groups (id, name, course_id, created_at) VALUES (?, ?, ?, ?)",
                (group.id, group.name, None, _now()),
            )
        return group

    def rename_group(self, group_id: str, name: str) -> ChatGroup:
        group_id, name = _identifier(group_id), _text(name, MAX_GROUP_NAME, strip=True)
        with self.database._transaction() as connection:
            row = connection.execute(
                "UPDATE chat_groups SET name = ? WHERE id = ? RETURNING id, name, course_id",
                (name, group_id),
            ).fetchone()
            if row is None:
                raise ValueError("Chat group does not exist.")
        return _group(row)

    def create_conversation(self, group_id: str, title: str = "新对话") -> ChatConversation:
        group_id, title = _identifier(group_id), _text(title, MAX_CONVERSATION_TITLE, strip=True)
        timestamp = _now()
        with self.database._transaction() as connection:
            if connection.execute("SELECT 1 FROM chat_groups WHERE id = ?", (group_id,)).fetchone() is None:
                raise ValueError("Chat group does not exist.")
            row = connection.execute(
                "INSERT INTO chat_conversations (id, group_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) RETURNING *",
                (f"chat:{uuid4().hex}", group_id, title, timestamp, timestamp),
            ).fetchone()
        return _conversation(row)

    def rename_conversation(self, conversation_id: str, title: str) -> ChatConversation:
        conversation_id, title = _identifier(conversation_id), _text(title, MAX_CONVERSATION_TITLE, strip=True)
        with self.database._transaction() as connection:
            row = connection.execute(
                "UPDATE chat_conversations SET title = ?, updated_at = ? WHERE id = ? RETURNING *",
                (title, _now(), conversation_id),
            ).fetchone()
            if row is None:
                raise ValueError("Chat conversation does not exist.")
        return _conversation(row)

    def move_conversation(self, conversation_id: str, group_id: str) -> ChatConversation:
        conversation_id, group_id = _identifier(conversation_id), _identifier(group_id)
        with self.database._transaction() as connection:
            if connection.execute("SELECT 1 FROM chat_groups WHERE id = ?", (group_id,)).fetchone() is None:
                raise ValueError("Chat group does not exist.")
            row = connection.execute(
                "UPDATE chat_conversations SET group_id = ?, updated_at = ? WHERE id = ? RETURNING *",
                (group_id, _now(), conversation_id),
            ).fetchone()
            if row is None:
                raise ValueError("Chat conversation does not exist.")
        return _conversation(row)

    def list_conversations(self, group_id: str | None = None) -> list[ChatConversation]:
        if group_id is not None:
            group_id = _identifier(group_id)
        with self.database._lock:
            if group_id is None:
                rows = self.database._connection.execute(
                    "SELECT * FROM chat_conversations ORDER BY updated_at DESC, id"
                ).fetchall()
            else:
                rows = self.database._connection.execute(
                    "SELECT * FROM chat_conversations WHERE group_id = ? ORDER BY updated_at DESC, id", (group_id,)
                ).fetchall()
        return [_conversation(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> ChatConversation | None:
        conversation_id = _identifier(conversation_id)
        with self.database._lock:
            row = self.database._connection.execute(
                "SELECT * FROM chat_conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return _conversation(row) if row is not None else None

    def list_messages(self, conversation_id: str) -> list[ChatMessage]:
        conversation_id = _identifier(conversation_id)
        with self.database._lock:
            rows = self.database._connection.execute(
                "SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)
            ).fetchall()
        return [ChatMessage(
            row["id"], row["conversation_id"], row["role"], row["content"],
            tuple(json.loads(row["sources_json"])), datetime.fromisoformat(row["created_at"]),
        ) for row in rows]

    def append_message(
        self, conversation_id: str, role: str, content: str, sources: Iterable[str] = (),
    ) -> ChatMessage:
        conversation_id = _identifier(conversation_id)
        if not isinstance(role, str) or role not in {"user", "assistant", "error"}:
            raise ValueError("Invalid chat message role.")
        content = _text(content, MAX_MESSAGE_LENGTH)
        if isinstance(sources, (str, bytes, Mapping)):
            raise ValueError("Chat sources must be a sequence of strings.")
        try:
            source_iterator = iter(sources)
        except TypeError:
            raise ValueError("Chat sources must be a sequence of strings.") from None
        prepared_sources: list[str] = []
        for source in source_iterator:
            if len(prepared_sources) >= MAX_SOURCES:
                raise ValueError("Too many chat sources.")
            prepared_sources.append(_text(source, MAX_SOURCE_LENGTH))
        encoded = json.dumps(prepared_sources, ensure_ascii=False, separators=(",", ":"))
        with self.database._transaction() as connection:
            timestamp = _now()
            cursor = connection.execute(
                "UPDATE chat_conversations SET updated_at = ? WHERE id = ?", (timestamp, conversation_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Chat conversation does not exist.")
            cursor = connection.execute(
                "INSERT INTO chat_messages (conversation_id, role, content, sources_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)", (conversation_id, role, content, encoded, timestamp),
            )
            message_id = cursor.lastrowid
        return ChatMessage(message_id, conversation_id, role, content, tuple(prepared_sources), datetime.fromisoformat(timestamp))
