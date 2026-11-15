from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
import sqlite3
from threading import Barrier

import pytest

from app.services.announcement_state import AnnouncementState, AnnouncementStateStore
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase


@pytest.fixture
def state_parts(tmp_path):
    path = tmp_path / "announcement-flags.sqlite3"
    database = SQLiteDatabase(path)
    yield database, AnnouncementStateStore(database), path
    database.close()


def test_default_announcement_state_is_unread_and_unstarred_and_frozen():
    state = AnnouncementState()
    assert state.is_read is False
    assert state.is_starred is False
    with pytest.raises(FrozenInstanceError):
        state.is_read = True


def test_no_rows_until_a_local_state_is_changed(state_parts):
    _, store, _ = state_parts
    assert store.get_states() == {}
    assert store.get_states().get((1, 10), AnnouncementState()) == AnnouncementState()


def test_flags_are_independent_and_both_can_be_cleared(state_parts):
    _, store, _ = state_parts
    assert store.set_read(1, 10, True) == AnnouncementState(True, False)
    assert store.set_starred(1, 10, True) == AnnouncementState(True, True)
    assert store.set_read(1, 10, False) == AnnouncementState(False, True)
    assert store.set_starred(1, 10, False) == AnnouncementState(False, False)
    assert store.get_states() == {(1, 10): AnnouncementState()}


def test_starred_first_does_not_mark_an_announcement_read(state_parts):
    _, store, _ = state_parts
    assert store.set_starred(1, 10, True) == AnnouncementState(False, True)
    assert store.set_read(1, 10, False) == AnnouncementState(False, True)
    assert store.set_read(1, 10, True) == AnnouncementState(True, True)
    assert store.set_starred(1, 10, False) == AnnouncementState(True, False)


def test_same_announcement_id_in_different_courses_has_independent_state(state_parts):
    _, store, _ = state_parts
    store.set_read(1, 10, True)
    store.set_starred(2, 10, True)
    store.set_starred(1, 11, True)
    assert store.get_states() == {
        (1, 10): AnnouncementState(True, False),
        (2, 10): AnnouncementState(False, True),
        (1, 11): AnnouncementState(False, True),
    }


def test_saved_flags_survive_reopening_database(tmp_path):
    path = tmp_path / "reopened-flags.sqlite3"
    database = SQLiteDatabase(path)
    try:
        store = AnnouncementStateStore(database)
        store.set_read(31, 50, True)
        store.set_starred(31, 50, True)
        store.set_starred(31, 51, True)
    finally:
        database.close()

    reopened = SQLiteDatabase(path)
    try:
        assert AnnouncementStateStore(reopened).get_states() == {
            (31, 50): AnnouncementState(True, True),
            (31, 51): AnnouncementState(False, True),
        }
    finally:
        reopened.close()


def test_clear_cache_keeps_announcement_state_and_other_user_data(state_parts):
    database, store, _ = state_parts
    cache = Cache(database)
    cache.set("announcements", [{"id": 10, "course_id": 1}])
    database.set_preference("test.display", "compact")
    database.record_reminder_sent(10, "1_day", datetime.now(UTC))
    store.set_read(1, 10, True)
    store.set_starred(1, 10, True)

    assert cache.clear() == 1
    assert cache.get("announcements") is None
    assert store.get_states() == {(1, 10): AnnouncementState(True, True)}
    assert database.get_preference("test.display") == "compact"
    assert database.reminder_was_sent(10, "1_day")


def test_existing_schema_is_extended_without_losing_preferences_or_cache(tmp_path):
    path = tmp_path / "existing-app.sqlite3"
    # Simulate a database created before announcement state existed.
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE preferences (
                preference_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO preferences VALUES
                ('test.display', '"compact"', '2026-10-16T01:00:00+00:00');
            CREATE TABLE cache_entries (
                category TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (category, cache_key)
            );
            INSERT INTO cache_entries VALUES
                ('courses', 'default', '[{"id":1}]',
                 '2026-10-16T01:00:00+00:00', '2026-10-17T01:00:00+00:00');
            """
        )
        connection.commit()
    finally:
        connection.close()

    database = SQLiteDatabase(path)
    try:
        store = AnnouncementStateStore(database)
        assert store.get_states() == {}
        assert database.get_preference("test.display") == "compact"
        assert database.get_cache_entry("courses", "default").payload == [{"id": 1}]
        assert store.set_starred(1, 10, True) == AnnouncementState(False, True)
    finally:
        database.close()


def test_state_table_contains_only_numeric_identity_flags_and_timestamp(state_parts):
    database, store, path = state_parts
    store.set_read(1, 10, True)
    store.set_starred(1, 10, True)
    connection = sqlite3.connect(path)
    try:
        columns = connection.execute("PRAGMA table_info(announcement_states)").fetchall()
        assert [row[1] for row in columns] == [
            "course_id", "announcement_id", "is_read", "is_starred", "updated_at"
        ]
        assert [row[5] for row in columns] == [1, 2, 0, 0, 0]
        row = connection.execute("SELECT * FROM announcement_states").fetchone()
        assert row[:4] == (1, 10, 1, 1)
        assert datetime.fromisoformat(row[4]).tzinfo is not None
    finally:
        connection.close()


def test_bulk_get_uses_one_query_and_returns_a_detached_snapshot(state_parts):
    database, store, _ = state_parts
    for announcement_id in range(1, 101):
        store.set_read(1, announcement_id, True)
    traced = []
    database._connection.set_trace_callback(traced.append)
    try:
        states = store.get_states()
    finally:
        database._connection.set_trace_callback(None)
    assert len(states) == 100
    assert len(traced) == 1
    assert traced[0].strip().upper().startswith("SELECT ")
    states.clear()
    assert len(store.get_states()) == 100


@pytest.mark.parametrize("bad_id", [True, False, 0, -1, 1.0, "1", None, 2**63])
@pytest.mark.parametrize("field", ["course_id", "announcement_id"])
def test_invalid_identifiers_are_rejected_without_rows(state_parts, bad_id, field):
    _, store, _ = state_parts
    ids = {"course_id": 1, "announcement_id": 10}
    ids[field] = bad_id
    for method in (store.set_read, store.set_starred):
        with pytest.raises(ValueError, match="positive integers"):
            method(ids["course_id"], ids["announcement_id"], True)
    assert store.get_states() == {}


@pytest.mark.parametrize("bad_flag", [1, 0, "true", None, [], {}])
def test_non_boolean_flags_are_rejected_without_rows(state_parts, bad_flag):
    _, store, _ = state_parts
    for method in (store.set_read, store.set_starred):
        with pytest.raises(ValueError, match="booleans"):
            method(1, 10, bad_flag)
    assert store.get_states() == {}


def test_largest_sqlite_integer_id_is_supported(state_parts):
    _, store, _ = state_parts
    identity = (2**63 - 1, 2**63 - 1)
    assert store.set_read(*identity, True) == AnnouncementState(True, False)
    assert store.get_states() == {identity: AnnouncementState(True, False)}


def test_concurrent_column_updates_do_not_overwrite_other_flag(state_parts):
    _, store, _ = state_parts
    barrier = Barrier(2)

    def change_read():
        for announcement_id in range(1, 101):
            barrier.wait(timeout=5)
            store.set_read(1, announcement_id, True)

    def change_starred():
        for announcement_id in range(1, 101):
            barrier.wait(timeout=5)
            store.set_starred(1, announcement_id, True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(change_read), executor.submit(change_starred)]
        for future in futures:
            future.result(timeout=15)
    assert store.get_states() == {
        (1, announcement_id): AnnouncementState(True, True)
        for announcement_id in range(1, 101)
    }


def test_two_database_connections_preserve_independent_column_changes(state_parts):
    _, store, path = state_parts
    other_database = SQLiteDatabase(path)
    other = AnnouncementStateStore(other_database)
    barrier = Barrier(2)

    def set_read():
        barrier.wait(timeout=5)
        return store.set_read(1, 10, True)

    def set_starred():
        barrier.wait(timeout=5)
        return other.set_starred(1, 10, True)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(set_read), executor.submit(set_starred)]
            for future in futures:
                future.result(timeout=15)
        assert store.get_states() == other.get_states() == {
            (1, 10): AnnouncementState(True, True)
        }
    finally:
        other_database.close()
