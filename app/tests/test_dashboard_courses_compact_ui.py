"""Overview and course lists keep their information in a denser layout."""

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from app.models.assignment import Assignment
from app.models.course import Course
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.courses import CoursesPage
from app.ui.dashboard import DashboardPage
from app.ui.main_window import MainWindow


@pytest.fixture
def styled_window(qtbot, monkeypatch):
    class Store:
        def has_canvas_token(self):
            return False

        def has_ai_api_key(self):
            return False

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


def show_page(qtbot, window, page_type):
    page = next(window.pages.widget(index) for index in range(window.pages.count())
                if isinstance(window.pages.widget(index), page_type))
    window.pages.setCurrentWidget(page)
    window.show()
    qtbot.wait(30)
    return page


def settle_cards(qtbot, listing):
    qtbot.wait(30)
    listing._resize_cards()
    listing.doItemsLayout()
    qtbot.wait(30)


def fully_visible_count(listing):
    return sum(listing.viewport().rect().contains(listing.visualItemRect(listing.item(row)))
               for row in range(listing.count()))


def test_courses_fit_more_rows_and_keep_course_names_and_codes(qtbot, styled_window):
    page = show_page(qtbot, styled_window, CoursesPage)
    courses = [Course(id=index, name=f"Principles of Data Science {index}", course_code=f"COMP{index}")
               for index in range(1, 13)]
    page._show_courses(courses)
    settle_cards(qtbot, page.course_list)

    assert page.course_list.compact
    assert fully_visible_count(page.course_list) >= 7
    assert "Double-click" in page.status_label.text()
    for index, course in enumerate(courses):
        card = page.course_list.itemWidget(page.course_list.item(index))
        assert card.findChild(QLabel, "cardTitle").text() == course.name
        assert card.findChild(QLabel, "cardSubtitle").text() == course.course_code
        assert card.findChild(QLabel, "cardMeta") is None
    title = page.findChild(QLabel, "pageTitle")
    assert abs(title.geometry().center().y() - page.refresh_button.geometry().center().y()) <= 2
    assert abs(title.geometry().center().y() - page.open_button.geometry().center().y()) <= 2


def test_long_course_title_wraps_without_clipping_and_selection_opens_same_course(qtbot, styled_window, monkeypatch):
    page = show_page(qtbot, styled_window, CoursesPage)
    course = Course(id=3733, name="A very detailed course name with multiple sections " * 6,
                    course_code="COMP5310 Principles of Data Science")
    page._show_courses([course])
    settle_cards(qtbot, page.course_list)
    item = page.course_list.item(0)
    card = page.course_list.itemWidget(item)
    for name in ("cardTitle", "cardSubtitle"):
        label = card.findChild(QLabel, name)
        assert label.wordWrap()
        assert card.rect().contains(label.geometry())
        assert label.height() >= label.heightForWidth(label.width())
    assert course.name in item.toolTip()
    assert course.course_code in item.toolTip()

    opened = []

    class Detail:
        def __init__(self, selected_course, _store, _parent, *, cache):
            opened.append(selected_course)

        def exec(self):
            pass

    monkeypatch.setattr("app.ui.courses.CourseDetailDialog", Detail)
    assert not page.open_button.isEnabled()
    page.course_list.setCurrentRow(0)
    assert page.open_button.isEnabled()
    qtbot.mouseClick(page.open_button, Qt.MouseButton.LeftButton)
    assert opened == [course]


def test_dashboard_keeps_summary_next_deadline_and_compact_clickable_cards(qtbot, styled_window, monkeypatch):
    page = show_page(qtbot, styled_window, DashboardPage)
    now = datetime.now(timezone.utc)
    tasks = [Assignment(assignment_id=index, course_id=1, course_name="Course", course_code="COMP5310",
                        title=f"Upcoming task {index}", due_at=now + timedelta(days=index),
                        submission_status="unsubmitted") for index in range(1, 7)]
    page._show(tasks)
    settle_cards(qtbot, page.upcoming_list)

    assert page.upcoming_list.compact
    assert page.upcoming_list.count() == 5
    assert fully_visible_count(page.upcoming_list) >= 4
    assert page.next_deadline_title.text() == "Upcoming task 1"
    assert "COMP5310" in page.next_deadline_detail.text()
    assert len(page.stats) == 4
    title = page.findChild(QLabel, "pageTitle")
    assert abs(title.geometry().center().y() - page.refresh_button.geometry().center().y()) <= 2
    assert page.next_deadline_card.layout().contentsMargins().top() == 7

    opened = []

    class Detail:
        def __init__(self, deadline, _parent, *, credential_store):
            opened.append(deadline.assignment.assignment_id)

        def exec(self):
            pass

    monkeypatch.setattr("app.ui.dashboard.AssignmentDetailDialog", Detail)
    first = page.upcoming_list.item(0)
    qtbot.mouseClick(page.upcoming_list.viewport(), Qt.MouseButton.LeftButton,
                    pos=page.upcoming_list.visualItemRect(first).center())
    assert opened == [1]
