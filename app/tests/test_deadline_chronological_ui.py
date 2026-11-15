from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from app.models.assignment import Assignment
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.ui.assignments import AssignmentsPage
from app.ui.deadline_center import DeadlineCenterPage
from app.ui.deadline_widgets import format_due


@pytest.fixture
def cache():
    database = SQLiteDatabase(":memory:")
    yield Cache(database)
    database.close()


def task(task_id, course_id, due, state="unsubmitted", *, course_code=None):
    return Assignment(
        assignment_id=task_id,
        course_id=course_id,
        course_name=f"Course {course_id}",
        course_code=f"COMP{course_id}" if course_code is None else course_code,
        title=f"Assignment {task_id}",
        submission_status=state,
        due_at=due,
    )


def deadline_page(qtbot, cache, assignments):
    page = DeadlineCenterPage(credential_store=object(), cache=cache)
    qtbot.addWidget(page)
    page._show(assignments)
    return page


def card_deadlines(page):
    return [
        page.assignment_list.item(row).data(Qt.ItemDataRole.UserRole)
        for row in range(page.assignment_list.count())
        if page.assignment_list.item(row).data(Qt.ItemDataRole.UserRole) is not None
    ]


def card_ids(page):
    return [deadline.assignment.assignment_id for deadline in card_deadlines(page)]


def select_status(page, value):
    index = page.submission_filter.findData(value)
    assert index >= 0
    page.submission_filter.setCurrentIndex(index)


def test_deadlines_mix_courses_in_chronological_order_with_overdue_last(qtbot, cache):
    page = deadline_page(qtbot, cache, [
        task(4, 1, None),
        task(3, 1, datetime(2090, 6, 3, 8, 15, tzinfo=timezone.utc)),
        task(1, 3, datetime(2010, 6, 1, 8, 15, tzinfo=timezone.utc)),
        task(2, 2, datetime(2090, 6, 2, 8, 15, tzinfo=timezone.utc)),
    ])

    assert card_ids(page) == [2, 3, 4, 1]
    assert [deadline.assignment.assignment_id for deadline in page._visible_deadlines] == [2, 3, 4, 1]
    assert page.assignment_list.count() == 4
    for row in range(page.assignment_list.count()):
        item = page.assignment_list.item(row)
        assert item.data(Qt.ItemDataRole.UserRole) is not None
        assert item.flags() & Qt.ItemFlag.ItemIsSelectable
        assert page.assignment_list.itemWidget(item).objectName() == "contentCard"


@pytest.mark.parametrize("status, expected", [
    ("pending", [2, 3, 5, 6]),
    ("submitted", [1, 4]),
    ("unknown", [2, 5]),
    ("all", [1, 2, 3, 4, 5, 6]),
])
def test_submission_filters_keep_date_order_instead_of_status_groups(qtbot, cache, status, expected):
    start = datetime(2090, 6, 1, 8, 15, tzinfo=timezone.utc)
    assignments = [
        task(6, 1, None),
        task(5, 3, start + timedelta(days=4), None),
        task(4, 2, start + timedelta(days=3), "submitted"),
        task(3, 1, start + timedelta(days=2)),
        task(2, 2, start + timedelta(days=1), None),
        task(1, 3, start, "submitted"),
    ]
    page = deadline_page(qtbot, cache, assignments)

    assert page.submission_filter.currentData() == "pending"
    select_status(page, status)
    assert card_ids(page) == expected
    assert page.assignment_list.count() == len(expected)


def test_deadlines_compare_absolute_instants_not_displayed_clock_or_course(qtbot, cache):
    page = deadline_page(qtbot, cache, [
        task(2, 1, datetime(2090, 6, 2, 0, 0, tzinfo=timezone.utc)),
        task(3, 2, datetime(2090, 6, 1, 19, 30, tzinfo=timezone(timedelta(hours=-5)))),
        task(1, 3, datetime(2090, 6, 2, 10, 0, tzinfo=timezone(timedelta(hours=11)))),
    ])

    # The local-looking 10:00 is 23:00 UTC on the previous day; 19:30 -05
    # is 00:30 UTC on the next day.
    assert card_ids(page) == [1, 2, 3]


def test_equal_due_dates_and_missing_due_dates_keep_input_order_across_statuses(qtbot, cache):
    due = datetime(2090, 6, 1, 8, 15, tzinfo=timezone.utc)
    assignments = [
        task(6, 1, None, "submitted"),
        task(3, 3, due, "submitted"),
        task(5, 2, None, None),
        task(1, 2, due, None),
        task(4, 3, None),
        task(2, 1, due),
    ]
    page = deadline_page(qtbot, cache, assignments)
    select_status(page, "all")

    assert card_ids(page) == [3, 1, 2, 6, 5, 4]
    page._show(list(reversed(assignments)))
    assert card_ids(page) == [2, 1, 3, 4, 5, 6]
    assert page.submission_filter.currentData() == "all"


def test_course_filter_and_refresh_preserve_chronology_and_selected_status(qtbot, cache):
    start = datetime(2090, 6, 1, 8, 15, tzinfo=timezone.utc)
    assignments = [
        task(4, 2, None),
        task(3, 2, start + timedelta(days=2)),
        task(2, 1, start + timedelta(days=1)),
        task(1, 2, start, "submitted"),
    ]
    page = deadline_page(qtbot, cache, assignments)
    select_status(page, "all")
    page.course_filter.setCurrentIndex(page.course_filter.findData(2))
    assert card_ids(page) == [1, 3, 4]

    page._show(assignments)
    assert page.course_filter.currentData() == 2
    assert page.submission_filter.currentData() == "all"
    assert card_ids(page) == [1, 3, 4]
    page.course_filter.setCurrentIndex(page.course_filter.findData(None))
    assert card_ids(page) == [1, 2, 3, 4]


def test_each_deadline_card_shows_course_and_exact_due_time_without_headers(qtbot, cache):
    due = datetime(2090, 6, 1, 8, 15, tzinfo=timezone.utc)
    page = deadline_page(qtbot, cache, [
        task(1, 1, due),
        task(2, 2, due + timedelta(minutes=17), course_code=""),
        task(3, 3, None),
    ])
    page.resize(1000, 800)
    page.show()
    qtbot.wait(30)

    assert page.assignment_list.count() == 3
    for row in range(page.assignment_list.count()):
        item = page.assignment_list.item(row)
        deadline = item.data(Qt.ItemDataRole.UserRole)
        card = page.assignment_list.itemWidget(item)
        assert deadline is not None
        labels = card.findChildren(QLabel)
        visible_text = "\n".join(label.text() for label in labels if label.isVisible())
        assignment = deadline.assignment
        assert assignment.title in visible_text
        assert (assignment.course_code or assignment.course_name) in visible_text
        assert format_due(deadline) in visible_text
        assert assignment.submission_label in visible_text


def test_assignments_page_retains_course_and_submission_grouping(qtbot, cache):
    start = datetime(2090, 6, 1, 8, 15, tzinfo=timezone.utc)
    page = AssignmentsPage(credential_store=object(), cache=cache)
    qtbot.addWidget(page)
    page._show([
        task(4, 2, start),
        task(2, 1, start + timedelta(days=1), "submitted"),
        task(3, 1, start + timedelta(days=2), None),
        task(1, 1, start + timedelta(days=3)),
    ])

    assert card_ids(page) == [1, 3, 2, 4]
    assert page.assignment_list.count() > len(card_deadlines(page))
