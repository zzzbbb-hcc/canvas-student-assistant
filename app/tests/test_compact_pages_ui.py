"""All main list pages share a dense layout without dropping task details."""

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from app.models.assignment import Assignment
from app.models.course import Course
from app.services.deadline_service import DeadlineService
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.assignments import AssignmentsPage
from app.ui.courses import CoursesPage
from app.ui.dashboard import DashboardPage
from app.ui.deadline_center import DeadlineCenterPage
from app.ui.deadline_widgets import format_due
from app.ui.main_window import MainWindow


BASE_TIME = datetime(2026, 11, 1, 2, 37, tzinfo=timezone.utc)


@pytest.fixture
def styled_window(qtbot, monkeypatch):
    # List ordering in these fixtures assumes the dated work is upcoming.
    monkeypatch.setattr("app.ui.assignments.DeadlineService",
                        lambda: DeadlineService(now=BASE_TIME - timedelta(days=2)))
    class Store:
        def has_canvas_token(self):
            return False

        def has_ai_api_key(self):
            return False

    # No operating-system credentials, on-disk cache, reminder delivery, or
    # worker requests are used when exercising the actual MainWindow QSS.
    session_cache.clear()
    monkeypatch.setattr("app.ui.main_window.CredentialStore", Store)
    monkeypatch.setattr("app.ui.main_window.SQLiteDatabase", lambda: SQLiteDatabase(":memory:"))
    monkeypatch.setattr(MainWindow, "_refresh_current_page", lambda *_args: None)
    window = MainWindow()
    window._reminder_timer.stop()
    for index in range(window.pages.count()):
        page = window.pages.widget(index)
        if hasattr(page, "_notification_service"):
            page._notification_service = None
    qtbot.addWidget(window)
    window.resize(1100, 720)
    yield window
    window._database.close()
    session_cache.clear()


def task(task_id, *, course_id=1, state="unsubmitted", due=BASE_TIME, title=None):
    return Assignment(
        assignment_id=task_id,
        course_id=course_id,
        course_name=f"Course {course_id}",
        course_code=f"COMP{course_id}",
        title=title or f"Assignment {task_id}",
        submission_status=state,
        due_at=due,
    )


def get_page(window, page_type):
    # DeadlineCenterPage is an AssignmentsPage subclass; match the exact page.
    return next(window.pages.widget(index) for index in range(window.pages.count())
                if type(window.pages.widget(index)) is page_type)


def show_page(qtbot, window, page, listing):
    window.pages.setCurrentWidget(page)
    window.show()
    qtbot.wait(30)
    listing._resize_cards()
    listing.doItemsLayout()
    qtbot.wait(30)


def items_with_deadlines(page):
    return [page.assignment_list.item(row) for row in range(page.assignment_list.count())
            if page.assignment_list.item(row).data(Qt.ItemDataRole.UserRole) is not None]


def visible_ids(page):
    return [item.data(Qt.ItemDataRole.UserRole).assignment.assignment_id
            for item in items_with_deadlines(page)]


def fully_visible(listing, items):
    return [item for item in items
            if listing.viewport().rect().contains(listing.visualItemRect(item))]


def assert_labels_fit(card, *names):
    for name in names:
        label = card.findChild(QLabel, name)
        assert label is not None and label.isVisible(), name
        # QLabel.geometry is relative to its card: layout must allocate enough
        # height for wrapped text rather than relying on clipping or elision.
        assert card.rect().contains(label.geometry()), name
        if label.hasHeightForWidth():
            assert label.height() >= label.heightForWidth(label.width()), name
    assert card.height() >= card.layout().totalHeightForWidth(card.width())


@pytest.mark.parametrize("page_type, minimum", [(AssignmentsPage, 4), (DeadlineCenterPage, 5)])
def test_compact_task_pages_fit_more_complete_cards(qtbot, styled_window, page_type, minimum):
    session_cache.set("quiz_coverage_warnings", [f"Course {index}: quiz metadata unavailable."
                                                for index in range(20)])
    page = get_page(styled_window, page_type)
    page._show([task(index, due=BASE_TIME + timedelta(days=index)) for index in range(1, 13)])
    show_page(qtbot, styled_window, page, page.assignment_list)

    assert page.compact_layout and page.assignment_list.compact
    assert len(fully_visible(page.assignment_list, items_with_deadlines(page))) >= minimum
    assert visible_ids(page) == list(range(1, 13))
    assert page.assignment_list.horizontalScrollBar().maximum() == 0
    assert page.refresh_button.isVisible()
    assert page.course_filter.isVisible() and page.submission_filter.isVisible()
    assert page.summary_label.isVisible()
    assert page.quiz_coverage_label.isVisible() and page.quiz_coverage_button.isVisible()


def test_compact_courses_fit_six_complete_rows_and_keep_names_and_codes(qtbot, styled_window):
    page = get_page(styled_window, CoursesPage)
    courses = [Course(id=index, name=f"Principles of Course {index}", course_code=f"COMP{index}")
               for index in range(1, 13)]
    page._show_courses(courses)
    show_page(qtbot, styled_window, page, page.course_list)

    items = [page.course_list.item(row) for row in range(page.course_list.count())]
    assert page.course_list.compact
    assert len(fully_visible(page.course_list, items)) >= 6
    for item, course in zip(items, courses):
        card = page.course_list.itemWidget(item)
        assert card.findChild(QLabel, "cardTitle").text() == course.name
        assert card.findChild(QLabel, "cardSubtitle").text() == course.course_code
    page.course_list.setCurrentRow(3)
    assert page.open_button.isEnabled()
    assert page._courses[page.course_list.currentRow()].id == 4


def test_compact_dashboard_fits_three_upcoming_cards_and_retains_summary(qtbot, styled_window, monkeypatch):
    monkeypatch.setattr("app.ui.dashboard.DeadlineService", lambda: DeadlineService(now=BASE_TIME))
    session_cache.set("quiz_coverage_warnings", ["Quiz metadata unavailable."])
    page = get_page(styled_window, DashboardPage)
    tasks = [task(index, due=BASE_TIME + timedelta(hours=index)) for index in range(1, 9)]
    page._show(list(reversed(tasks)))
    show_page(qtbot, styled_window, page, page.upcoming_list)

    listing = page.upcoming_list
    items = [listing.item(row) for row in range(listing.count())]
    assert listing.compact
    assert len(fully_visible(listing, items)) >= 3
    assert [item.data(Qt.ItemDataRole.UserRole).assignment.assignment_id for item in items] == [1, 2, 3, 4, 5]
    assert all(label.isVisible() for label in page.stats.values())
    assert page.next_deadline_card.isVisible()
    assert page.quiz_coverage_label.isVisible()
    assert page.next_deadline_title.text() == "Assignment 1"
    assert format_due(items[0].data(Qt.ItemDataRole.UserRole)) in page.next_deadline_detail.text()


@pytest.mark.parametrize("page_type", [AssignmentsPage, DeadlineCenterPage])
@pytest.mark.parametrize("state", ["unsubmitted", "submitted", None])
def test_styled_dense_tasks_expand_for_long_titles_dates_and_status(qtbot, styled_window, page_type, state):
    page = get_page(styled_window, page_type)
    assignment = task(1, state=state, title="Long assignment title with detailed sections and requirements " * 5)
    page._show([assignment])
    page.submission_filter.setCurrentIndex(page.submission_filter.findData("all"))
    show_page(qtbot, styled_window, page, page.assignment_list)

    item = items_with_deadlines(page)[0]
    card = page.assignment_list.itemWidget(item)
    assert_labels_fit(card, "cardTitle", "cardSubtitle", "cardMeta", "submissionBadge")
    assert card.findChild(QLabel, "cardTitle").text() == assignment.title
    assert format_due(item.data(Qt.ItemDataRole.UserRole)) in card.findChild(QLabel, "cardSubtitle").text()
    assert card.findChild(QLabel, "submissionBadge").text() == assignment.submission_label
    assert assignment.title in item.toolTip()


def test_styled_dense_course_long_name_and_code_do_not_clip(qtbot, styled_window):
    page = get_page(styled_window, CoursesPage)
    course = Course(id=1, name="A complete long course title covering several study areas " * 5,
                    course_code="COMP5310 / COMP9123 extended course code " * 3)
    page._show_courses([course])
    show_page(qtbot, styled_window, page, page.course_list)

    item = page.course_list.item(0)
    card = page.course_list.itemWidget(item)
    assert_labels_fit(card, "cardTitle", "cardSubtitle")
    assert card.findChild(QLabel, "cardTitle").text() == course.name
    assert card.findChild(QLabel, "cardSubtitle").text() == course.course_code


def test_styled_dense_dashboard_long_title_retains_exact_due(qtbot, styled_window, monkeypatch):
    monkeypatch.setattr("app.ui.dashboard.DeadlineService", lambda: DeadlineService(now=BASE_TIME))
    page = get_page(styled_window, DashboardPage)
    assignment = task(1, due=BASE_TIME + timedelta(hours=1),
                      title="A long upcoming assignment with several detailed sections " * 4)
    page._show([assignment])
    show_page(qtbot, styled_window, page, page.upcoming_list)

    item = page.upcoming_list.item(0)
    card = page.upcoming_list.itemWidget(item)
    assert_labels_fit(card, "cardTitle", "cardSubtitle", "cardMeta")
    assert format_due(item.data(Qt.ItemDataRole.UserRole)) in card.findChild(QLabel, "cardMeta").text()
    assert page.next_deadline_title.height() >= page.next_deadline_title.heightForWidth(page.next_deadline_title.width())


def test_compact_task_pages_preserve_grouping_filtering_order_and_clicked_identity(qtbot, styled_window, monkeypatch):
    tasks = [task(4, course_id=2, due=None),
             task(3, state="submitted", due=BASE_TIME + timedelta(days=1)),
             task(2, course_id=2, state=None, due=BASE_TIME),
             task(1, due=BASE_TIME + timedelta(days=2))]
    assignments = get_page(styled_window, AssignmentsPage)
    deadlines = get_page(styled_window, DeadlineCenterPage)
    assignments._show(tasks)
    assert visible_ids(assignments) == [1, 3, 4, 2]
    # Course and status headings remain non-selectable and have no deadline.
    headers = [assignments.assignment_list.item(row) for row in range(assignments.assignment_list.count())
               if assignments.assignment_list.item(row).data(Qt.ItemDataRole.UserRole) is None]
    assert headers and all(not item.flags() & Qt.ItemFlag.ItemIsSelectable for item in headers)
    assignments.course_filter.setCurrentIndex(assignments.course_filter.findData(2))
    assignments.submission_filter.setCurrentIndex(assignments.submission_filter.findData("unknown"))
    assignments._show(tasks)
    assert visible_ids(assignments) == [2]

    deadlines._show(tasks)
    assert visible_ids(deadlines) == [2, 1, 4]
    deadlines.submission_filter.setCurrentIndex(deadlines.submission_filter.findData("all"))
    assert visible_ids(deadlines) == [2, 3, 1, 4]
    deadlines.course_filter.setCurrentIndex(deadlines.course_filter.findData(2))
    deadlines._show(tasks)
    assert visible_ids(deadlines) == [2, 4]

    opened = []

    class Detail:
        def __init__(self, deadline, _parent, *, credential_store):
            opened.append(deadline.assignment.assignment_id)

        def exec(self):
            pass

    monkeypatch.setattr("app.ui.assignments.AssignmentDetailDialog", Detail)
    show_page(qtbot, styled_window, deadlines, deadlines.assignment_list)
    item = items_with_deadlines(deadlines)[0]
    qtbot.mouseClick(deadlines.assignment_list.viewport(), Qt.MouseButton.LeftButton,
                    pos=deadlines.assignment_list.visualItemRect(item).center())
    assert opened == [2]
