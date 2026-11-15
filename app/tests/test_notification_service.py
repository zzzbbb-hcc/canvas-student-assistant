from datetime import UTC, datetime, timedelta

import pytest

from app.models.assignment import Assignment
from app.models.reminder import ReminderType
from app.services.notification_service import (
    MemoryNotificationBackend,
    NotificationService,
    ReminderPreferences,
)
from app.storage.database import SQLiteDatabase


NOW = datetime(2026, 9, 20, 10, tzinfo=UTC)


def assignment(
    assignment_id: int,
    remaining: timedelta,
    *,
    submitted: bool = False,
    due: bool = True,
) -> Assignment:
    return Assignment(
        assignment_id=assignment_id,
        course_id=7,
        course_name="Data Science",
        course_code="DATA1000",
        title=f"Assignment {assignment_id}",
        due_at=NOW + remaining if due else None,
        submission_status="submitted" if submitted else "unsubmitted",
    )


@pytest.fixture
def notification_parts(tmp_path):
    database = SQLiteDatabase(tmp_path / "student-assistant.sqlite3")
    backend = MemoryNotificationBackend()
    service = NotificationService(database, backend, clock=lambda: NOW)
    yield database, backend, service
    database.close()


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (timedelta(days=6), ReminderType.SEVEN_DAYS),
        (timedelta(days=2), ReminderType.THREE_DAYS),
        (timedelta(hours=20), ReminderType.ONE_DAY),
        (timedelta(hours=5), ReminderType.SIX_HOURS),
        (timedelta(minutes=-5), ReminderType.OVERDUE),
    ],
)
def test_each_reminder_window_and_overdue_once(notification_parts, remaining, expected):
    database, backend, service = notification_parts
    item = assignment(hash(expected.value) % 100_000, remaining)

    events = service.check_assignments([item])
    assert [event.reminder_type for event in events] == [expected]
    assert len(backend.messages) == 1
    assert database.reminder_was_sent(item.assignment_id, expected.value)

    assert service.check_assignments([item]) == []
    assert len(backend.messages) == 1


def test_submitted_and_no_deadline_are_skipped_by_default(notification_parts):
    _, backend, service = notification_parts
    assert service.check_assignments(
        [assignment(1, timedelta(hours=5), submitted=True), assignment(2, timedelta(), due=False)]
    ) == []
    assert backend.messages == []


def test_notify_after_submitted_can_be_enabled(notification_parts):
    _, backend, service = notification_parts
    service.update_preferences(notify_after_submitted=True)
    events = service.check_assignments([assignment(3, timedelta(hours=5), submitted=True)])
    assert [event.reminder_type for event in events] == [ReminderType.SIX_HOURS]
    assert len(backend.messages) == 1


def test_disabled_rule_and_global_switch(notification_parts):
    _, backend, service = notification_parts
    service.update_preferences(six_hours=False)
    assert service.check_assignments([assignment(4, timedelta(hours=5))]) == []
    service.update_preferences(enabled=False, six_hours=True)
    assert service.check_assignments([assignment(5, timedelta(hours=5))]) == []
    assert backend.messages == []


def test_preferences_round_trip_through_non_sensitive_sqlite(notification_parts):
    database, _, service = notification_parts
    expected = service.update_preferences(
        enabled=False, seven_days=False, overdue=False, notify_after_submitted=True
    )
    restored = NotificationService(
        database, MemoryNotificationBackend(), clock=lambda: NOW
    ).preferences
    assert restored == expected


def test_backend_failure_does_not_permanently_consume_reminder(tmp_path):
    class FailingBackend:
        def send(self, title, message, *, on_click=None):
            raise RuntimeError("notification unavailable")

    database = SQLiteDatabase(tmp_path / "student-assistant.sqlite3")
    service = NotificationService(database, FailingBackend(), clock=lambda: NOW)
    item = assignment(8, timedelta(hours=5))
    with pytest.raises(RuntimeError, match="notification unavailable"):
        service.check_assignments([item])
    assert not database.reminder_was_sent(8, ReminderType.SIX_HOURS.value)
    database.close()


def test_test_notification_uses_backend_without_history(notification_parts):
    database, backend, service = notification_parts
    service.test_notification()
    assert backend.messages[0][:2] == (
        "Student Assistant",
        "Test notification — deadline reminders are enabled.",
    )
    assert not database.reminder_was_sent(1, ReminderType.SIX_HOURS.value)
