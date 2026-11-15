"""Local, de-duplicated deadline notifications.

The first release checks while the desktop app is running.  It does not create
scheduled tasks, services, cloud jobs, or place credentials on command lines.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.models.assignment import Assignment
from app.models.reminder import ReminderEvent, ReminderType
from app.storage.database import SQLiteDatabase


class NotificationUnavailable(RuntimeError):
    pass


class NotificationBackend(Protocol):
    """Tiny abstraction so reminder behaviour is testable without Windows UI."""

    def send(
        self,
        title: str,
        message: str,
        *,
        on_click: Callable[[], None] | None = None,
    ) -> None: ...


class MemoryNotificationBackend:
    """In-memory backend intended for tests and development diagnostics."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, Callable[[], None] | None]] = []

    def send(
        self,
        title: str,
        message: str,
        *,
        on_click: Callable[[], None] | None = None,
    ) -> None:
        self.messages.append((title, message, on_click))


class QtSystemTrayNotificationBackend:
    """Use Qt's Windows system-tray notification bridge while the app runs."""

    def __init__(self) -> None:
        from PySide6.QtWidgets import QApplication, QStyle, QSystemTrayIcon

        app = QApplication.instance()
        if app is None:
            raise NotificationUnavailable("The app must be running to show notifications.")
        if not QSystemTrayIcon.isSystemTrayAvailable():
            raise NotificationUnavailable("Windows notifications are unavailable.")
        icon = app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self._tray = QSystemTrayIcon(icon)
        self._tray.setToolTip("Student Assistant")
        self._click_action: Callable[[], None] | None = None
        self._tray.messageClicked.connect(self._message_clicked)
        self._tray.show()

    def _message_clicked(self) -> None:
        action, self._click_action = self._click_action, None
        if action is not None:
            action()

    def send(
        self,
        title: str,
        message: str,
        *,
        on_click: Callable[[], None] | None = None,
    ) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        if not QSystemTrayIcon.supportsMessages():
            raise NotificationUnavailable("Windows notifications are unavailable.")
        self._click_action = on_click
        self._tray.showMessage(
            title,
            message,
            QSystemTrayIcon.MessageIcon.Information,
            10_000,
        )


class ReminderPreferences(BaseModel):
    """User-controllable rules; defaults match the Phase 7 specification."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    seven_days: bool = True
    three_days: bool = True
    one_day: bool = True
    six_hours: bool = True
    overdue: bool = True
    notify_after_submitted: bool = False


_PREFERENCE_KEYS = {
    "enabled": "reminders.enabled",
    "seven_days": "reminders.7_days",
    "three_days": "reminders.3_days",
    "one_day": "reminders.1_day",
    "six_hours": "reminders.6_hours",
    "overdue": "reminders.overdue",
    "notify_after_submitted": "reminders.notify_after_submitted",
}


class ReminderPreferenceStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def load(self) -> ReminderPreferences:
        defaults = ReminderPreferences()
        values = {
            name: bool(
                self.database.get_preference(key, getattr(defaults, name))
            )
            for name, key in _PREFERENCE_KEYS.items()
        }
        return ReminderPreferences(**values)

    def save(self, preferences: ReminderPreferences) -> None:
        for name, key in _PREFERENCE_KEYS.items():
            self.database.set_preference(key, getattr(preferences, name))


_WINDOWS: tuple[tuple[ReminderType, timedelta, timedelta, str], ...] = (
    (ReminderType.SIX_HOURS, timedelta(0), timedelta(hours=6), "six_hours"),
    (ReminderType.ONE_DAY, timedelta(hours=6), timedelta(days=1), "one_day"),
    (ReminderType.THREE_DAYS, timedelta(days=1), timedelta(days=3), "three_days"),
    (ReminderType.SEVEN_DAYS, timedelta(days=3), timedelta(days=7), "seven_days"),
)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Deadline reminders require timezone-aware due dates.")
    return value.astimezone(UTC)


def _due_text(value: datetime) -> str:
    local = value.astimezone()
    day = local.strftime("%a, %d %b")
    clock = local.strftime("%I:%M %p").lstrip("0")
    return f"{day} at {clock}"


class NotificationService:
    """Evaluate assignments and emit each configured reminder at most once."""

    def __init__(
        self,
        database: SQLiteDatabase | None = None,
        backend: NotificationBackend | None = None,
        *,
        preferences: ReminderPreferences | None = None,
        clock: Callable[[], datetime] | None = None,
        assignment_click_handler: Callable[[int], None] | None = None,
    ) -> None:
        self.database = database or SQLiteDatabase()
        self.preference_store = ReminderPreferenceStore(self.database)
        self.preferences = preferences or self.preference_store.load()
        self._backend = backend
        self._clock = clock or (lambda: datetime.now(UTC))
        self._assignment_click_handler = assignment_click_handler

    @property
    def backend(self) -> NotificationBackend:
        if self._backend is None:
            self._backend = QtSystemTrayNotificationBackend()
        return self._backend

    def update_preferences(self, **changes: bool) -> ReminderPreferences:
        unknown = set(changes) - set(_PREFERENCE_KEYS)
        if unknown:
            raise ValueError("Unknown reminder preference.")
        self.preferences = self.preferences.model_copy(update=changes)
        self.preference_store.save(self.preferences)
        return self.preferences

    def _now(self) -> datetime:
        return _aware_utc(self._clock())

    def _type_for(self, remaining: timedelta) -> ReminderType | None:
        if remaining <= timedelta(0):
            return ReminderType.OVERDUE if self.preferences.overdue else None
        for reminder_type, lower, upper, preference_name in _WINDOWS:
            if lower < remaining <= upper and getattr(self.preferences, preference_name):
                return reminder_type
        return None

    def _message(self, reminder_type: ReminderType, due_at: datetime) -> str:
        due = _due_text(due_at)
        if reminder_type is ReminderType.OVERDUE:
            return f"Assignment overdue · Was due {due}"
        labels = {
            ReminderType.SEVEN_DAYS: "Due within 7 days",
            ReminderType.THREE_DAYS: "Due within 3 days",
            ReminderType.ONE_DAY: "Due within 1 day",
            ReminderType.SIX_HOURS: "Due within 6 hours",
        }
        return f"{labels[reminder_type]} · {due}"

    def check_assignments(self, assignments: Iterable[Assignment]) -> list[ReminderEvent]:
        """Show eligible local notifications and return those successfully sent."""

        if not self.preferences.enabled:
            return []
        now = self._now()
        sent: list[ReminderEvent] = []
        for assignment in assignments:
            if assignment.due_at is None or not assignment.published:
                continue
            if assignment.is_submitted and not self.preferences.notify_after_submitted:
                continue
            try:
                due_at = _aware_utc(assignment.due_at)
            except ValueError:
                # Canvas deadlines should always be timezone-aware. An invalid
                # record is ignored rather than producing a fabricated reminder.
                continue
            reminder_type = self._type_for(due_at - now)
            if reminder_type is None:
                continue
            # Reserve first to prevent concurrent app-running checks from both
            # showing the same notification. Failed delivery rolls it back.
            if not self.database.record_reminder_sent(
                assignment.assignment_id, reminder_type.value, now
            ):
                continue
            title = f"{assignment.course_code or assignment.course_name} — {assignment.title}"
            message = self._message(reminder_type, due_at)
            callback = None
            if self._assignment_click_handler is not None:
                callback = lambda assignment_id=assignment.assignment_id: (
                    self._assignment_click_handler(assignment_id)
                )
            try:
                self.backend.send(title[:160], message[:320], on_click=callback)
            except Exception:
                self.database.delete_reminder_record(
                    assignment.assignment_id, reminder_type.value
                )
                raise
            sent.append(
                ReminderEvent(
                    assignment_id=assignment.assignment_id,
                    reminder_type=reminder_type,
                    title=title[:160],
                    message=message[:320],
                    due_at=due_at,
                    sent_at=now,
                )
            )
        return sent

    def test_notification(self) -> None:
        """Exercise the configured backend without recording reminder history."""

        self.backend.send(
            "Student Assistant",
            "Test notification — deadline reminders are enabled.",
        )
