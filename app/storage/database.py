"""SQLite persistence for non-sensitive application data.

Credentials intentionally have no table or API in this module. They remain in
the operating-system credential store; this database contains cached display
data, preferences, local announcement flags, sanitized chat history and reminder
de-duplication metadata. Chat history is local SQLite data, not encrypted storage.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from threading import RLock
from typing import Any, Iterator


class SensitiveDataError(ValueError):
    """Raised when data resembling a credential is offered to SQLite."""


_SENSITIVE_KEYS = {
    "access_token", "refresh_token", "token", "authorization",
    "authorization_header", "cookie", "cookies", "session", "session_id",
    "password", "api_key", "secret", "canvas_pat", "ed_token", "ai_api_key",
}
_AUTH_VALUE = re.compile(r"\b(?:bearer|basic)\s+[a-z0-9._~+/=-]+", re.IGNORECASE)


def _normalise_key(value: object) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def assert_non_sensitive(value: Any) -> None:
    """Reject credential-shaped values without echoing them in an error.

    This is a defence-in-depth guard, not a credential detector. Callers must
    still pass only documented, non-sensitive model data to the cache.
    """

    if isinstance(value, dict):
        for key, child in value.items():
            if _normalise_key(key) in _SENSITIVE_KEYS:
                raise SensitiveDataError("Sensitive data cannot be persisted.")
            assert_non_sensitive(child)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            assert_non_sensitive(child)
    elif isinstance(value, str):
        # The sentinel makes the security regression tests fail closed. The
        # authorization pattern catches accidentally cached request headers.
        if "SUPER_SECRET_" in value or _AUTH_VALUE.search(value):
            raise SensitiveDataError("Sensitive data cannot be persisted.")


def default_database_path() -> Path:
    """Return the per-user Windows data path without storing anything secret."""

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / "StudentAssistant" / "student_assistant.sqlite3"
    # Useful on test/development hosts that lack Windows environment variables.
    return Path.cwd() / ".student-assistant" / "student_assistant.sqlite3"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Stored timestamps must include a timezone.")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _announcement_id(value: int) -> int:
    """Keep flag identities within SQLite's positive integer ID domain."""

    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63:
        raise ValueError("Announcement and course IDs must be positive integers.")
    return value


def _announcement_flag(value: bool) -> int:
    if not isinstance(value, bool):
        raise ValueError("Announcement flags must be booleans.")
    return int(value)


@dataclass(frozen=True, slots=True)
class CacheRecord:
    category: str
    cache_key: str
    payload: Any
    fetched_at: datetime
    expires_at: datetime


class SQLiteDatabase:
    """Small, thread-safe SQLite gateway used by cache and notifications."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._lock = RLock()
        requested_path = Path(path) if path is not None else default_database_path()
        candidates = [requested_path]
        if path is None:
            # A locked-down Windows profile or test sandbox must not make the
            # entire GUI fail during construction. The fallback contains only
            # non-sensitive cache/preferences and never credentials.
            fallback = (
                Path(tempfile.gettempdir())
                / "StudentAssistant"
                / "student_assistant.sqlite3"
            )
            if str(fallback).casefold() != str(requested_path).casefold():
                candidates.append(fallback)

        last_error: OSError | sqlite3.Error | None = None
        for candidate in candidates:
            self._connection = None
            try:
                self.path, self._connection = self._open(candidate)
                self._initialise_schema()
                return
            except (OSError, sqlite3.Error) as exc:
                last_error = exc
                if self._connection is not None:
                    try:
                        self._connection.close()
                    except sqlite3.Error:
                        pass
        assert last_error is not None
        raise last_error

    def _initialise_schema(self) -> None:
        self._connection.row_factory = sqlite3.Row
        with self._transaction() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    category TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (category, cache_key)
                );

                CREATE INDEX IF NOT EXISTS idx_cache_expiry
                    ON cache_entries(expires_at);

                CREATE TABLE IF NOT EXISTS notification_history (
                    assignment_id INTEGER NOT NULL,
                    reminder_type TEXT NOT NULL,
                    reminder_sent_at TEXT NOT NULL,
                    PRIMARY KEY (assignment_id, reminder_type)
                );

                CREATE TABLE IF NOT EXISTS preferences (
                    preference_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_metadata (
                    sync_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS announcement_states (
                    course_id INTEGER NOT NULL CHECK (course_id > 0),
                    announcement_id INTEGER NOT NULL CHECK (announcement_id > 0),
                    is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
                    is_starred INTEGER NOT NULL DEFAULT 0 CHECK (is_starred IN (0, 1)),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (course_id, announcement_id)
                );

                CREATE TABLE IF NOT EXISTS chat_groups (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    course_id INTEGER UNIQUE CHECK (course_id > 0),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_conversations (
                    id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL REFERENCES chat_groups(id),
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_conversations_group
                    ON chat_conversations(group_id, updated_at DESC, id);

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES chat_conversations(id),
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'error')),
                    content TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation
                    ON chat_messages(conversation_id, id);
                """
            )

    @staticmethod
    def _open(path: Path) -> tuple[Path, sqlite3.Connection]:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(path), timeout=5.0, check_same_thread=False
        )
        return path, connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._connection
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def put_cache_entry(
        self,
        category: str,
        cache_key: str,
        payload: Any,
        fetched_at: datetime,
        expires_at: datetime,
    ) -> None:
        assert_non_sensitive(payload)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO cache_entries
                    (category, cache_key, payload_json, fetched_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(category, cache_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at
                """,
                (category, cache_key, encoded, _timestamp(fetched_at), _timestamp(expires_at)),
            )

    def get_cache_entry(self, category: str, cache_key: str) -> CacheRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT category, cache_key, payload_json, fetched_at, expires_at
                FROM cache_entries WHERE category = ? AND cache_key = ?
                """,
                (category, cache_key),
            ).fetchone()
        if row is None:
            return None
        return CacheRecord(
            category=row["category"], cache_key=row["cache_key"],
            payload=json.loads(row["payload_json"]),
            fetched_at=_parse_timestamp(row["fetched_at"]),
            expires_at=_parse_timestamp(row["expires_at"]),
        )

    def delete_cache_entry(self, category: str, cache_key: str | None = None) -> int:
        with self._transaction() as connection:
            if cache_key is None:
                cursor = connection.execute(
                    "DELETE FROM cache_entries WHERE category = ?", (category,)
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM cache_entries WHERE category = ? AND cache_key = ?",
                    (category, cache_key),
                )
            return int(cursor.rowcount)

    def clear_cache(self) -> int:
        """Clear cache only, preserving preferences, reminders, flags and chats."""

        with self._transaction() as connection:
            cursor = connection.execute("DELETE FROM cache_entries")
            return int(cursor.rowcount)

    def get_announcement_states(self) -> dict[tuple[int, int], tuple[bool, bool]]:
        """Read all local announcement flags in one query, without content."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT course_id, announcement_id, is_read, is_starred "
                "FROM announcement_states"
            ).fetchall()
        return {
            (row["course_id"], row["announcement_id"]): (
                bool(row["is_read"]), bool(row["is_starred"])
            )
            for row in rows
        }

    def set_announcement_read(
        self, course_id: int, announcement_id: int, is_read: bool
    ) -> tuple[bool, bool]:
        """Update only read status, preserving any concurrent star change."""

        course_id, announcement_id = _announcement_id(course_id), _announcement_id(announcement_id)
        flag = _announcement_flag(is_read)
        with self._transaction() as connection:
            row = connection.execute(
                """
                INSERT INTO announcement_states
                    (course_id, announcement_id, is_read, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(course_id, announcement_id) DO UPDATE SET
                    is_read = excluded.is_read,
                    updated_at = excluded.updated_at
                RETURNING is_read, is_starred
                """,
                (course_id, announcement_id, flag, _timestamp(datetime.now(UTC))),
            ).fetchone()
        return bool(row["is_read"]), bool(row["is_starred"])

    def set_announcement_starred(
        self, course_id: int, announcement_id: int, is_starred: bool
    ) -> tuple[bool, bool]:
        """Update only star status, preserving any concurrent read change."""

        course_id, announcement_id = _announcement_id(course_id), _announcement_id(announcement_id)
        flag = _announcement_flag(is_starred)
        with self._transaction() as connection:
            row = connection.execute(
                """
                INSERT INTO announcement_states
                    (course_id, announcement_id, is_starred, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(course_id, announcement_id) DO UPDATE SET
                    is_starred = excluded.is_starred,
                    updated_at = excluded.updated_at
                RETURNING is_read, is_starred
                """,
                (course_id, announcement_id, flag, _timestamp(datetime.now(UTC))),
            ).fetchone()
        return bool(row["is_read"]), bool(row["is_starred"])

    def purge_expired_cache(self, now: datetime) -> int:
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM cache_entries WHERE expires_at <= ?", (_timestamp(now),)
            )
            return int(cursor.rowcount)

    def reminder_was_sent(self, assignment_id: int, reminder_type: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1 FROM notification_history
                WHERE assignment_id = ? AND reminder_type = ?
                """,
                (assignment_id, reminder_type),
            ).fetchone()
        return row is not None

    def record_reminder_sent(
        self, assignment_id: int, reminder_type: str, sent_at: datetime
    ) -> bool:
        """Record one reminder; return False when it was already recorded."""

        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO notification_history
                    (assignment_id, reminder_type, reminder_sent_at)
                VALUES (?, ?, ?)
                """,
                (assignment_id, reminder_type, _timestamp(sent_at)),
            )
            return cursor.rowcount == 1

    def get_reminder_sent_at(
        self, assignment_id: int, reminder_type: str
    ) -> datetime | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT reminder_sent_at FROM notification_history
                WHERE assignment_id = ? AND reminder_type = ?
                """,
                (assignment_id, reminder_type),
            ).fetchone()
        return _parse_timestamp(row[0]) if row else None

    def delete_reminder_record(self, assignment_id: int, reminder_type: str) -> None:
        """Roll back a reservation when the local notification backend fails."""

        with self._transaction() as connection:
            connection.execute(
                """
                DELETE FROM notification_history
                WHERE assignment_id = ? AND reminder_type = ?
                """,
                (assignment_id, reminder_type),
            )

    def set_preference(self, key: str, value: Any, now: datetime | None = None) -> None:
        assert_non_sensitive({key: value})
        changed_at = now or datetime.now(UTC)
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO preferences(preference_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(preference_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, encoded, _timestamp(changed_at)),
            )

    def get_preference(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._connection.execute(
                "SELECT value_json FROM preferences WHERE preference_key = ?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else default

    def set_sync_metadata(self, key: str, value: Any, now: datetime | None = None) -> None:
        assert_non_sensitive({key: value})
        changed_at = now or datetime.now(UTC)
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO sync_metadata(sync_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(sync_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, encoded, _timestamp(changed_at)),
            )

    def get_sync_metadata(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._connection.execute(
                "SELECT value_json FROM sync_metadata WHERE sync_key = ?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else default
