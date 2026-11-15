from datetime import datetime, timedelta, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel
import pytest

from app.models.assignment import Assignment
from app.services.deadline_service import DeadlineService
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.ui.assignments import AssignmentsPage
from app.ui.deadline_center import DeadlineCenterPage
from app.ui.deadline_widgets import AssignmentDetailDialog, format_due


def task(task_id, course_id=1, state="unsubmitted", *, due=None):
    return Assignment(
        assignment_id=task_id,
        course_id=course_id,
        course_name=f"Course {course_id}",
        course_code=f"COMP{course_id}",
        title=f"Assignment {task_id}",
        submission_status=state,
        due_at=due,
    )


@pytest.fixture
def cache(tmp_path):
    database = SQLiteDatabase(tmp_path / "assignments.sqlite3")
    yield Cache(database)
    database.close()


def visible_tasks(page):
    return [d.assignment.assignment_id for d in page._visible_deadlines]


def card_items(page):
    return [page.assignment_list.item(row) for row in range(page.assignment_list.count())
            if page.assignment_list.item(row).data(Qt.ItemDataRole.UserRole) is not None]


def test_assignments_group_by_course_and_submission_keep_all_tasks(qtbot, cache):
    page = AssignmentsPage(cache=cache); qtbot.addWidget(page)
    page._show([task(4, 2, "submitted"), task(3, 1, None), task(2, 1, "submitted"), task(1)])

    assert visible_tasks(page) == [1, 3, 2, 4]
    assert len(card_items(page)) == 4
    badges = [page.assignment_list.itemWidget(item).findChild(QLabel, "submissionBadge").text()
              for item in card_items(page)]
    assert badges == ["Not submitted", "Submission status unavailable", "Submitted", "Submitted"]
    assert page.course_filter.count() == 3


def test_course_and_submission_filters_combine_and_survive_refresh(qtbot, cache):
    page = AssignmentsPage(cache=cache); qtbot.addWidget(page)
    tasks = [task(1), task(2, 1, "submitted"), task(3, 2, "submitted")]
    page._show(tasks)
    page.course_filter.setCurrentIndex(page.course_filter.findData(1))
    page.submission_filter.setCurrentIndex(page.submission_filter.findData("submitted"))
    assert visible_tasks(page) == [2]
    page._show(tasks)
    assert page.course_filter.currentData() == 1
    assert page.submission_filter.currentData() == "submitted"
    assert visible_tasks(page) == [2]


def test_deadlines_default_to_do_but_submitted_remain_accessible(qtbot, cache):
    page = DeadlineCenterPage(cache=cache); qtbot.addWidget(page)
    page._show([task(1), task(2, state="submitted"), task(3, state=None)])
    assert visible_tasks(page) == [1, 3]
    page.submission_filter.setCurrentIndex(page.submission_filter.findData("submitted"))
    assert visible_tasks(page) == [2]
    page.submission_filter.setCurrentIndex(page.submission_filter.findData("all"))
    assert visible_tasks(page) == [1, 2, 3]


def test_deadlines_sorted_within_course_and_submission_group(qtbot, cache):
    page = AssignmentsPage(cache=cache); qtbot.addWidget(page)
    now = datetime.now().astimezone()
    page._show([task(2, due=now + timedelta(days=2)), task(3), task(1, due=now + timedelta(hours=1))])
    assert visible_tasks(page) == [1, 2, 3]


def test_group_headers_cannot_open_wrong_assignment_after_filtering(qtbot, cache, monkeypatch):
    opened = []
    class Detail:
        def __init__(self, deadline, _parent, **_kwargs):
            opened.append(deadline.assignment.assignment_id)
        def exec(self):
            pass
    monkeypatch.setattr("app.ui.assignments.AssignmentDetailDialog", Detail)
    page = AssignmentsPage(cache=cache); qtbot.addWidget(page)
    page._show([task(1), task(2, state="submitted")])
    page._open_detail(0)
    assert opened == []
    page.submission_filter.setCurrentIndex(page.submission_filter.findData("submitted"))
    page._open_item_detail(card_items(page)[0])
    assert opened == [2]


def test_exact_due_time_and_detail_submission_status(qtbot):
    due = datetime(2026, 10, 20, 12, 37, tzinfo=timezone.utc)
    assignment = task(1, state="submitted", due=due)
    deadline = DeadlineService().build(assignment)
    local = due.astimezone()
    text = format_due(deadline)
    assert f"{local:%d %b %Y}" in text
    assert f"{local:%H:%M}" in text
    assert "UTC" in text
    assert format_due(DeadlineService().build(task(2))) == "No deadline"
    dialog = AssignmentDetailDialog(deadline); qtbot.addWidget(dialog)
    assert dialog.findChild(QLabel, "detailSubmissionStatus").text() == "Submission: Submitted"


def test_long_titles_do_not_clip_submission_badge_or_due_time(qtbot, cache):
    page = AssignmentsPage(cache=cache); qtbot.addWidget(page)
    assignment = task(1, state="submitted", due=datetime(2026, 10, 20, 12, 37, tzinfo=timezone.utc))
    assignment.title = "A long assignment title with multiple sections and detailed instructions " * 3
    page.resize(760, 750); page._show([assignment]); page.show()
    qtbot.wait(30)
    item = card_items(page)[0]
    card = page.assignment_list.itemWidget(item)
    badge = card.findChild(QLabel, "submissionBadge")
    due = card.findChild(QLabel, "cardSubtitle")
    assert card.rect().contains(badge.geometry().bottomRight())
    assert card.rect().contains(due.geometry().bottomRight())
    assert card.height() >= card.layout().totalHeightForWidth(card.width())
