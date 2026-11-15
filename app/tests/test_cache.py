from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from app.models.course import Course
from app.storage.cache import CACHE_TTLS, Cache, CacheCategory
from app.storage.database import SensitiveDataError, SQLiteDatabase
import app.storage.database as database_module


FAKE_SECRET = "_".join(("SUPER", "SECRET", "CANVAS", "TOKEN", "123"))


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def cache_parts(tmp_path):
    database = SQLiteDatabase(tmp_path / "student-assistant.sqlite3")
    clock = Clock(datetime(2026, 9, 20, 10, tzinfo=UTC))
    cache = Cache(database, clock=clock)
    yield database, cache, clock
    database.close()


def test_documented_cache_ttls_are_fixed():
    assert CACHE_TTLS == {
        "courses": timedelta(hours=24),
        "assignments": timedelta(hours=1),
        "announcements": timedelta(minutes=30),
        "ed_discussion": timedelta(minutes=15),
        "ed_lessons": timedelta(hours=24),
    }


def test_cache_serializes_models_and_expires_at_category_ttl(cache_parts):
    _, cache, clock = cache_parts
    course = Course(id=42, name="Data Science", course_code="DATA1000")
    entry = cache.set(CacheCategory.COURSES, [course])

    assert cache.get("courses") == [
        {"id": 42, "name": "Data Science", "course_code": "DATA1000"}
    ]
    assert entry.expires_at - entry.fetched_at == timedelta(hours=24)

    clock.now += timedelta(hours=24)
    assert cache.get("courses") is None
    assert not cache.is_fresh("courses")


def test_refresh_bypasses_fresh_cache(cache_parts):
    _, cache, _ = cache_parts
    calls = []

    assert cache.get_or_fetch("assignments", lambda: calls.append(1) or ["first"]) == ["first"]
    assert cache.get_or_fetch("assignments", lambda: calls.append(2) or ["second"]) == ["first"]
    assert cache.get_or_fetch(
        "assignments", lambda: calls.append(3) or ["fresh"], refresh=True
    ) == ["fresh"]
    assert calls == [1, 3]


def test_clear_cache_preserves_preferences_and_reminder_history(cache_parts):
    database, cache, clock = cache_parts
    cache.set("announcements", [{"title": "Welcome"}], key="course:1")
    database.set_preference("reminders.enabled", False, now=clock.now)
    database.record_reminder_sent(123, "1_day", clock.now)

    assert cache.clear() == 1
    assert cache.get("announcements", key="course:1") is None
    assert database.get_preference("reminders.enabled") is False
    assert database.reminder_was_sent(123, "1_day")


@pytest.mark.parametrize(
    "payload",
    [
        {"token": "not-written"},
        {"nested": {"authorization": "not-written"}},
        {"content": "Bearer not-written"},
        {"content": FAKE_SECRET},
    ],
)
def test_sensitive_values_are_rejected_without_entering_sqlite(tmp_path, payload):
    path = tmp_path / "secure.sqlite3"
    database = SQLiteDatabase(path)
    cache = Cache(database)

    with pytest.raises(SensitiveDataError, match="Sensitive data cannot be persisted"):
        cache.set("courses", payload)

    database.close()
    on_disk = b"".join(item.read_bytes() for item in tmp_path.iterdir() if item.is_file())
    assert b"not-written" not in on_disk
    assert FAKE_SECRET.encode() not in on_disk


def test_default_database_path_falls_back_when_profile_is_not_writable(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked" / "student.sqlite3"
    fallback = tmp_path / "fallback" / "student.sqlite3"
    original_open = SQLiteDatabase._open

    monkeypatch.setattr(database_module, "default_database_path", lambda: blocked)
    monkeypatch.setattr(database_module.tempfile, "gettempdir", lambda: str(tmp_path / "fallback-root"))

    def controlled_open(path):
        if path == blocked:
            raise PermissionError("profile is locked")
        return original_open(path)

    monkeypatch.setattr(SQLiteDatabase, "_open", staticmethod(controlled_open))
    database = SQLiteDatabase()
    try:
        assert database.path.parent == tmp_path / "fallback-root" / "StudentAssistant"
        database.set_preference("reminders.enabled", True)
        assert database.get_preference("reminders.enabled") is True
    finally:
        database.close()


def test_default_database_path_falls_back_when_wal_initialisation_fails(tmp_path, monkeypatch):
    primary = tmp_path / "profile" / "student.sqlite3"
    fallback_root = tmp_path / "fallback-root"
    original_initialise = SQLiteDatabase._initialise_schema
    attempts = []

    monkeypatch.setattr(database_module, "default_database_path", lambda: primary)
    monkeypatch.setattr(database_module.tempfile, "gettempdir", lambda: str(fallback_root))

    def fail_primary_then_initialise(self):
        attempts.append(self.path)
        if self.path == primary:
            raise sqlite3.OperationalError("unable to create WAL")
        original_initialise(self)

    monkeypatch.setattr(SQLiteDatabase, "_initialise_schema", fail_primary_then_initialise)
    database = SQLiteDatabase()
    try:
        assert attempts == [
            primary,
            fallback_root / "StudentAssistant" / "student_assistant.sqlite3",
        ]
        assert database.path == attempts[-1]
        database.set_preference("reminders.enabled", True)
        assert database.get_preference("reminders.enabled") is True
    finally:
        database.close()
