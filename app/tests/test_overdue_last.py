"""Overdue work follows dated non-overdue and undated work in every flat view."""

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import Qt

from app.models.assignment import Assignment
from app.services.deadline_service import DeadlineService
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.deadline_center import DeadlineCenterPage
from app.ui.quizzes import QuizzesPage


NOW = datetime(2026, 10, 16, 2, 30, tzinfo=timezone.utc)


def task(task_id, due=None, state="unsubmitted", *, course=1, **kwargs):
    return Assignment(
        assignment_id=task_id, course_id=course,
        course_name=f"Course {course}", course_code=f"COMP{course}",
        title=f"Task {task_id}", due_at=due, submission_status=state,
        **kwargs,
    )


def ids(deadlines):
    return [deadline.assignment.assignment_id for deadline in deadlines]


def test_future_dates_then_undated_then_all_overdue_dates():
    service = DeadlineService(NOW)
    deadlines = service.deadlines([
        task(6, NOW - timedelta(hours=1)),
        task(3),
        task(2, NOW + timedelta(days=7)),
        task(5, NOW - timedelta(days=30)),
        task(4, state="submitted"),
        task(1, NOW + timedelta(minutes=5)),
    ])

    assert ids(deadlines) == [1, 2, 3, 4, 5, 6]
    assert [d.status for d in deadlines[-2:]] == ["overdue", "overdue"]
    assert deadlines[-2].category == "Past"  # Old overdue work is not omitted.
    assert deadlines[-1].countdown.startswith("OVERDUE")


def test_due_now_is_not_overdue_but_one_microsecond_earlier_is():
    service = DeadlineService(NOW)
    deadlines = service.deadlines([
        task(3, NOW - timedelta(microseconds=1)),
        task(2),
        task(1, NOW),
    ])

    assert ids(deadlines) == [1, 2, 3]
    assert deadlines[0].status == "today"
    assert deadlines[-1].status == "overdue"
    assert ids(service.upcoming_unsubmitted([d.assignment for d in deadlines])) == [1]


def test_absolute_time_not_displayed_hour_determines_overdue_and_sorting():
    service = DeadlineService(NOW)
    sydney = timezone(timedelta(hours=11))
    new_york = timezone(timedelta(hours=-4))
    deadlines = service.deadlines([
        task(5, (NOW - timedelta(hours=1)).astimezone(sydney)),
        task(2, (NOW + timedelta(hours=1)).astimezone(new_york)),
        task(4, (NOW - timedelta(hours=2)).astimezone(new_york)),
        task(3),
        task(1, NOW.astimezone(sydney)),
    ])

    assert ids(deadlines) == [1, 2, 3, 4, 5]
    assert [d.status for d in deadlines[-2:]] == ["overdue", "overdue"]
    assert deadlines[0].due_local.timestamp() == NOW.timestamp()


@pytest.mark.parametrize("state, metadata", [
    ("submitted", {}),
    ("pending_review", {}),
    ("graded", {"submission_attempt": 1}),
    ("graded", {"submission_submitted_at": NOW - timedelta(days=2)}),
])
def test_completed_past_due_work_is_not_misclassified_as_overdue(state, metadata):
    service = DeadlineService(NOW)
    deadlines = service.deadlines([
        task(4, NOW - timedelta(days=3)),
        task(3),
        task(2, NOW + timedelta(days=1)),
        task(1, NOW - timedelta(days=4), state, **metadata),
    ])

    assert ids(deadlines) == [1, 2, 3, 4]
    assert deadlines[0].status == "completed"
    assert deadlines[0].countdown == "Submitted"


def test_unknown_missing_and_graded_without_attempt_remain_overdue():
    service = DeadlineService(NOW)
    deadlines = service.deadlines([
        task(5, NOW - timedelta(hours=1), "graded"),
        task(4, NOW - timedelta(hours=2), "submitted", submission_missing=True),
        task(3, NOW - timedelta(hours=3), None),
        task(2),
        task(1, NOW + timedelta(hours=1)),
    ])

    assert ids(deadlines) == [1, 2, 3, 4, 5]
    assert all(d.status == "overdue" for d in deadlines[-3:])
    assert all(not d.assignment.is_submitted for d in deadlines[-3:])
    assert deadlines[-2].assignment.submission_label == "Missing / Not submitted"


def test_equal_instants_and_undated_ties_remain_stable_in_each_sort_bucket():
    future = NOW + timedelta(hours=1)
    past = NOW - timedelta(hours=1)
    tasks = [task(7, past), task(4), task(2, future), task(6, past, None),
             task(3, state=None), task(1, future, "submitted"), task(5, past)]
    service = DeadlineService(NOW)

    assert ids(service.deadlines(tasks)) == [2, 1, 4, 3, 7, 6, 5]
    assert ids(service.deadlines(reversed(tasks))) == [1, 2, 3, 4, 5, 6, 7]


def test_upcoming_dashboard_limit_still_contains_only_earliest_future_work():
    tasks = [task(1, NOW - timedelta(days=1)), task(2),
             task(3, NOW + timedelta(hours=1), "submitted"),
             task(6, NOW + timedelta(hours=3)),
             task(5, NOW + timedelta(hours=2), None), task(4, NOW)]
    assert ids(DeadlineService(NOW).upcoming_unsubmitted(tasks, limit=2)) == [4, 5]


@pytest.fixture
def cache():
    session_cache.clear()
    database = SQLiteDatabase(":memory:")
    yield Cache(database)
    database.close()
    session_cache.clear()


@pytest.fixture
def fixed_ui_clock(monkeypatch):
    monkeypatch.setattr("app.ui.assignments.DeadlineService", lambda: DeadlineService(NOW))


def ui_tasks():
    # Both flat pages must retain submitted and unavailable-status information.
    entries = [
        task(1, NOW - timedelta(days=2)),
        task(2, NOW + timedelta(hours=1), course=2),
        task(3),
        task(4, NOW + timedelta(hours=2), None),
        task(5, NOW - timedelta(days=1), None),
        task(6, NOW - timedelta(days=3), "submitted"),
        task(7, NOW + timedelta(hours=3), "submitted", course=2),
        task(8, state=None),
    ]
    return [entry.model_copy(update={"quiz_id": 100 + entry.assignment_id, "quiz_engine": "classic"})
            for entry in entries]


def visible_ids(page):
    return ids(page._visible_deadlines)


@pytest.mark.parametrize("page_class", [DeadlineCenterPage, QuizzesPage])
@pytest.mark.parametrize("status, expected", [
    ("pending", [2, 4, 3, 8, 1, 5]),
    ("all", [6, 2, 4, 7, 3, 8, 1, 5]),
    ("submitted", [6, 7]),
    ("unknown", [4, 8, 5]),
])
def test_flat_pages_keep_overdue_last_through_submission_filters(
        qtbot, cache, fixed_ui_clock, page_class, status, expected):
    page = page_class(credential_store=object(), cache=cache)
    qtbot.addWidget(page)
    page._show(ui_tasks())
    page.submission_filter.setCurrentIndex(page.submission_filter.findData(status))

    assert visible_ids(page) == expected
    assert [page.assignment_list.item(row).data(Qt.ItemDataRole.UserRole).assignment.assignment_id
            for row in range(page.assignment_list.count())] == expected
    assert len(expected) == len(set(expected))


@pytest.mark.parametrize("page_class", [DeadlineCenterPage, QuizzesPage])
def test_course_filter_and_refresh_preserve_selection_but_recalculate_overdue_order(
        qtbot, cache, fixed_ui_clock, page_class):
    page = page_class(credential_store=object(), cache=cache)
    qtbot.addWidget(page)
    entries = ui_tasks()
    page._show(entries)
    page.submission_filter.setCurrentIndex(page.submission_filter.findData("unknown"))
    page.course_filter.setCurrentIndex(page.course_filter.findData(1))
    assert visible_ids(page) == [4, 8, 5]

    # A refreshed due-date extension moves an overdue item back into dated work.
    refreshed = [entry.model_copy(update={"due_at": NOW + timedelta(minutes=1)})
                 if entry.assignment_id == 5 else entry for entry in entries]
    page._show(refreshed)
    assert page.course_filter.currentData() == 1
    assert page.submission_filter.currentData() == "unknown"
    assert visible_ids(page) == [5, 4, 8]
    assert all(deadline.status != "overdue" for deadline in page._visible_deadlines)
