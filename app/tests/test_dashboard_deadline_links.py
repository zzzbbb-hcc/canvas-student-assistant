"""Dashboard statistics lead to the matching cross-course assignment/quiz list."""

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import Qt

from app.models.assignment import Assignment
from app.services.deadline_service import DeadlineService
from app.services.notification_service import NotificationService, ReminderPreferences
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.assignments import AssignmentsPage
from app.ui.dashboard import DashboardPage
from app.ui.deadline_center import DeadlineCenterPage
from app.ui.main_window import MainWindow


NOW = datetime(2026, 10, 14, 1, tzinfo=timezone.utc).astimezone()
STAT_PERIODS = [
    ("Due Today", "today"),
    ("Due Tomorrow", "tomorrow"),
    ("Due This Week", "week"),
    ("Overdue", "overdue"),
]


def task(item_id, due=None, *, course=1, state="unsubmitted", engine=None):
    return Assignment(
        assignment_id=item_id, course_id=course,
        course_name=f"Course {course}", course_code=f"COMP{course}",
        title=f"{'Quiz' if engine else 'Assignment'} {item_id}",
        due_at=due, submission_status=state,
        quiz_id=abs(item_id) + 100 if engine else None, quiz_engine=engine,
    )


@pytest.fixture
def study_items():
    midnight = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    sunday = midnight + timedelta(days=6 - midnight.weekday(), hours=23, minutes=59, seconds=59)
    return [
        task(1, NOW + timedelta(hours=1), course=1),
        task(2, NOW + timedelta(hours=2), course=2, engine="classic"),
        task(3, NOW + timedelta(hours=3), course=3, state=None, engine="new"),
        task(4, NOW + timedelta(minutes=30), course=3, state="submitted"),
        task(5, midnight + timedelta(days=1, hours=10), course=2),
        task(6, (midnight + timedelta(days=1, hours=11)).astimezone(timezone.utc),
             course=1, engine="classic"),
        task(7, sunday, course=3, engine="new"),
        task(8, sunday + timedelta(seconds=1), course=1),
        task(9, course=2, engine="classic"),
        task(10, NOW - timedelta(hours=1), course=3, engine="classic"),
        task(11, NOW - timedelta(days=50), course=2, state=None, engine="new"),
        task(12, NOW - timedelta(days=50), course=1, state="submitted"),
        task(13, NOW, course=1),
        task(14, NOW - timedelta(microseconds=1), course=1),
    ]


EXPECTED = {
    "today": {1, 2, 3, 13},
    "tomorrow": {5, 6},
    "week": {1, 2, 3, 5, 6, 7, 13},
    "overdue": {10, 11, 14},
}


def visible_ids(page):
    return {deadline.assignment.assignment_id for deadline in page._visible_deadlines}


@pytest.fixture
def linked_window(qtbot, monkeypatch, study_items):
    class Store:
        def has_canvas_token(self):
            return False

        def has_ai_api_key(self):
            return False

        def get_canvas_token(self):
            pytest.fail("Dashboard links must not read real credentials during tests")

    class FrozenDeadlineService(DeadlineService):
        def __init__(self, now=None):
            super().__init__(NOW if now is None else now)

    session_cache.clear()
    session_cache.set("assignments", study_items)
    monkeypatch.setattr("app.ui.main_window.CredentialStore", Store)
    monkeypatch.setattr("app.ui.main_window.SQLiteDatabase", lambda: SQLiteDatabase(":memory:"))
    monkeypatch.setattr("app.ui.main_window.NotificationService", lambda database: NotificationService(
        database, preferences=ReminderPreferences(enabled=False)))
    monkeypatch.setattr("app.ui.dashboard.DeadlineService", FrozenDeadlineService)
    monkeypatch.setattr("app.ui.assignments.DeadlineService", FrozenDeadlineService)
    monkeypatch.setattr("app.ui.deadline_center.DeadlineService", FrozenDeadlineService, raising=False)
    monkeypatch.setattr(DashboardPage, "refresh", lambda self, **_kwargs: self._show(
        session_cache.get("assignments", [])))
    refreshes = []

    def refresh_without_network(page, *_args, **_kwargs):
        refreshes.append((page, getattr(page, "period_filter", None).currentData()
                          if hasattr(page, "period_filter") else None, visible_ids(page)))

    monkeypatch.setattr(AssignmentsPage, "refresh", refresh_without_network)
    window = MainWindow()
    window._reminder_timer.stop()
    qtbot.addWidget(window)
    window.resize(1100, 720)
    window.show()
    qtbot.wait(20)
    dashboard = next(window.pages.widget(index) for index in range(window.pages.count())
                     if type(window.pages.widget(index)) is DashboardPage)
    deadlines = next(window.pages.widget(index) for index in range(window.pages.count())
                     if type(window.pages.widget(index)) is DeadlineCenterPage)
    yield window, dashboard, deadlines, refreshes
    window.close()
    window._database.close()
    session_cache.clear()


@pytest.mark.parametrize("name,period", STAT_PERIODS)
def test_statistic_mouse_click_navigates_to_exact_mixed_pending_list(
        qtbot, linked_window, study_items, name, period):
    window, dashboard, deadlines, refreshes = linked_window
    # A previous Deadline view must not silently hide another course or a quiz.
    deadlines._show(study_items)
    deadlines.course_filter.setCurrentIndex(deadlines.course_filter.findData(1))
    deadlines.submission_filter.setCurrentIndex(deadlines.submission_filter.findData("submitted"))
    assert visible_ids(deadlines) == {12}
    assert int(dashboard.stats[name].text()) == len(EXPECTED[period])

    with qtbot.waitSignal(dashboard.deadline_category_requested) as emitted:
        qtbot.mouseClick(dashboard.stat_buttons[name], Qt.MouseButton.LeftButton)

    assert emitted.args == [period]
    assert window.pages.currentWidget() is deadlines
    assert window.navigation.item(window.navigation.currentRow()).text() == "Deadlines"
    assert deadlines.period_filter.currentData() == period
    assert deadlines.course_filter.currentData() is None
    assert deadlines.submission_filter.currentData() == "pending"
    assert visible_ids(deadlines) == EXPECTED[period]
    assert len(deadlines._visible_deadlines) == int(dashboard.stats[name].text())
    assert all(not item.assignment.is_submitted for item in deadlines._visible_deadlines)
    # The existing refresh path sees the cached result already on screen.
    assert refreshes[-1] == (deadlines, period, EXPECTED[period])


@pytest.mark.parametrize("name,period", STAT_PERIODS)
def test_statistic_supports_keyboard_activation(qtbot, linked_window, name, period):
    window, dashboard, deadlines, _refreshes = linked_window
    button = dashboard.stat_buttons[name]
    assert button.focusPolicy() != Qt.FocusPolicy.NoFocus
    button.setFocus()
    with qtbot.waitSignal(dashboard.deadline_category_requested) as emitted:
        qtbot.keyClick(button, Qt.Key.Key_Space)
    assert emitted.args == [period]
    assert window.pages.currentWidget() is deadlines
    assert visible_ids(deadlines) == EXPECTED[period]


@pytest.mark.parametrize("name,period", STAT_PERIODS)
def test_period_filter_survives_refreshed_course_data(qtbot, linked_window, study_items, name, period):
    _window, dashboard, deadlines, _refreshes = linked_window
    qtbot.mouseClick(dashboard.stat_buttons[name], Qt.MouseButton.LeftButton)
    additional = {
        "today": task(100, NOW + timedelta(minutes=1), course=99, engine="new"),
        "tomorrow": task(100, (NOW + timedelta(days=1)).replace(hour=12), course=99, engine="classic"),
        "week": task(100, NOW + timedelta(hours=1), course=99),
        "overdue": task(100, NOW - timedelta(days=100), course=99, engine="new"),
    }[period]
    deadlines._show([*study_items, additional])
    assert deadlines.period_filter.currentData() == period
    assert deadlines.course_filter.currentData() is None
    assert deadlines.submission_filter.currentData() == "pending"
    assert visible_ids(deadlines) == EXPECTED[period] | {100}


def test_all_dates_restores_undated_and_every_pending_time_range(qtbot, linked_window):
    _window, dashboard, deadlines, _refreshes = linked_window
    qtbot.mouseClick(dashboard.stat_buttons["Due This Week"], Qt.MouseButton.LeftButton)
    deadlines.period_filter.setCurrentIndex(deadlines.period_filter.findData("all"))
    assert deadlines.period_filter.currentData() == "all"
    assert visible_ids(deadlines) == set(range(1, 15)) - {4, 12}


def test_zero_count_statistic_still_opens_an_empty_filtered_view(qtbot, linked_window):
    window, dashboard, deadlines, _refreshes = linked_window
    session_cache.set("assignments", [])
    dashboard._show([])
    assert dashboard.stats["Due Today"].text() == "0"
    assert dashboard.stat_buttons["Due Today"].isEnabled()
    qtbot.mouseClick(dashboard.stat_buttons["Due Today"], Qt.MouseButton.LeftButton)
    assert window.pages.currentWidget() is deadlines
    assert deadlines.period_filter.currentData() == "today"
    assert visible_ids(deadlines) == set()


def test_linked_quiz_and_assignment_details_share_the_original_credential_store(
        qtbot, linked_window, monkeypatch):
    window, dashboard, deadlines, _refreshes = linked_window
    opened = []

    class Detail:
        def __init__(self, deadline, parent, *, credential_store):
            opened.append((deadline.assignment.assignment_id, parent, credential_store))

        def exec(self):
            pass

    monkeypatch.setattr("app.ui.assignments.AssignmentDetailDialog", Detail)
    qtbot.mouseClick(dashboard.stat_buttons["Due Today"], Qt.MouseButton.LeftButton)
    qtbot.wait(20)
    for row in range(deadlines.assignment_list.count()):
        item = deadlines.assignment_list.item(row)
        deadline = item.data(Qt.ItemDataRole.UserRole)
        if deadline is not None:
            deadlines.assignment_list.scrollToItem(item)
            qtbot.mouseClick(deadlines.assignment_list.viewport(), Qt.MouseButton.LeftButton,
                             pos=deadlines.assignment_list.visualItemRect(item).center())
    assert {item_id for item_id, _parent, _store in opened} == EXPECTED["today"]
    assert all(parent is deadlines and store is window._credential_store
               for _item_id, parent, store in opened)


@pytest.mark.parametrize("period,expected", [
    ("today", {1, 2}), ("tomorrow", {4}), ("week", {1, 2, 4, 5}), ("overdue", {3, 7}),
])
def test_common_period_rules_respect_now_local_dates_sunday_and_submission(period, expected):
    service = DeadlineService(NOW)
    midnight = service.now.replace(hour=0, minute=0, second=0, microsecond=0)
    sunday_end = midnight + timedelta(days=6 - midnight.weekday(), hours=23, minutes=59, seconds=59)
    items = [
        task(1, service.now),
        task(2, service.now + timedelta(seconds=1), state=None, engine="new"),
        task(3, service.now - timedelta(microseconds=1), engine="classic"),
        task(4, (midnight + timedelta(days=1, hours=12)).astimezone(timezone.utc)),
        task(5, sunday_end, engine="classic"),
        task(6, sunday_end + timedelta(seconds=1), engine="new"),
        task(7, service.now - timedelta(days=100), state=None),
        task(8, service.now + timedelta(seconds=1), state="submitted"),
        task(9, service.now - timedelta(days=100), state="submitted"),
        task(10),
    ]
    matched = {deadline.assignment.assignment_id for deadline in service.deadlines(items)
               if service.matches_period(deadline, period)}
    assert matched == expected

