"""Compact dialog chrome must not shrink the actual reading text."""

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QLabel

from app.models.assignment import Assignment
from app.models.course import Course
from app.services.deadline_service import DeadlineService
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.canvas_file_dialog import CanvasFileDialog
from app.ui.course_detail import CourseDetailDialog
from app.ui.deadline_widgets import AssignmentDetailDialog, format_due
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
    qtbot.addWidget(window)
    yield window
    window._database.close()
    session_cache.clear()


def assert_visible_unclipped(label):
    assert label.isVisible()
    assert label.parentWidget().rect().contains(label.geometry())
    if label.hasHeightForWidth():
        assert label.height() >= label.heightForWidth(label.width())


def assignment(index=1, **updates):
    fields = {
        "assignment_id": index, "course_id": 5310,
        "course_name": "Principles of Data Science", "course_code": "COMP5310",
        "title": f"Assignment {index}: Data Science report",
        "description": "<p>Explain the dataset, methods and results in your report.</p>" * 12,
        "due_at": datetime.now(timezone.utc) + timedelta(days=index),
        "submission_status": "unsubmitted",
    }
    fields.update(updates)
    return Assignment(**fields)


def test_course_assignment_tab_is_compact_without_hiding_deadlines(qtbot, monkeypatch, styled_window):
    monkeypatch.setattr(CourseDetailDialog, "_load", lambda self: None)
    course = Course(id=5310, name="Principles of Data Science", course_code="COMP5310")
    dialog = CourseDetailDialog(course, object(), styled_window, cache=styled_window._cache)
    qtbot.addWidget(dialog)
    tasks = [assignment(index) for index in range(1, 13)]
    dialog._populate((tasks, [], [], [], []))
    dialog.resize(1000, 680)
    dialog.tabs.setCurrentWidget(dialog.assignment_list)
    dialog.show()
    qtbot.wait(30)
    dialog.assignment_list._resize_cards()
    dialog.assignment_list.doItemsLayout()
    qtbot.wait(30)

    assert dialog._thread is None
    assert dialog.assignment_list.compact
    assert dialog.assignment_list.count() == len(tasks)
    visible = [dialog.assignment_list.item(index) for index in range(len(tasks))
               if dialog.assignment_list.viewport().rect().contains(
                   dialog.assignment_list.visualItemRect(dialog.assignment_list.item(index)))]
    assert len(visible) >= 4
    first = dialog.assignment_list.item(0)
    deadline = first.data(Qt.ItemDataRole.UserRole)
    card = dialog.assignment_list.itemWidget(first)
    assert deadline.assignment.assignment_id == 1
    assert format_due(deadline) in card.findChild(QLabel, "cardSubtitle").text()
    assert card.findChild(QLabel, "submissionBadge").text() == "Not submitted"
    for name in ("courseHeading", "courseCode", "courseStatus"):
        assert_visible_unclipped(dialog.findChild(QLabel, name))


def test_module_rows_are_dense_but_long_titles_are_not_elided(qtbot, monkeypatch, styled_window):
    monkeypatch.setattr(CourseDetailDialog, "_load", lambda self: None)
    dialog = CourseDetailDialog(Course(id=5310, name="Data Science", course_code="COMP5310"),
                                object(), styled_window, cache=styled_window._cache)
    qtbot.addWidget(dialog)
    long_title = "Week 4 lecture: introduction to exploratory analysis and practical modelling techniques"
    items = [{"id": index, "title": long_title if index == 1 else f"Lecture material {index}",
              "type": "Page", "page_url": f"lecture-{index}"} for index in range(1, 21)]
    dialog._populate(([], [], [], [({"id": 41, "name": "Learning materials"}, items)], []))
    dialog.resize(1000, 680)
    # Select by the public tab label rather than relying on splitter nesting.
    dialog.tabs.setCurrentIndex(next(index for index in range(dialog.tabs.count())
                                    if dialog.tabs.tabText(index) == "Modules"))
    dialog.module_tree.expandAll()
    dialog.show()
    qtbot.wait(50)

    tree = dialog.module_tree
    child = tree.topLevelItem(0).child(0)
    assert tree.textElideMode() == Qt.TextElideMode.ElideNone
    assert tree.header().sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert child.text(0) == long_title
    assert child.toolTip(0) == long_title
    assert 20 <= tree.visualItemRect(child).height() <= 32
    assert tree.columnWidth(0) >= tree.fontMetrics().horizontalAdvance(long_title)
    assert tree.horizontalScrollBar().maximum() > 0
    # Long names deliberately extend horizontally and remain accessible by
    # scrolling; density concerns fully visible rows on the vertical axis.
    row_rects = [tree.visualItemRect(tree.topLevelItem(0).child(index)) for index in range(20)]
    fully_visible = sum(rect.top() >= 0 and rect.bottom() < tree.viewport().height() for rect in row_rects)
    assert fully_visible >= 10
    assert dialog.module_viewer.font().pixelSize() == 16
    assert dialog.module_viewer.viewport().height() >= 350


@pytest.mark.parametrize("size", [(900, 620), (1000, 680)])
def test_assignment_detail_keeps_readable_bilingual_body_and_metadata(qtbot, monkeypatch, styled_window, size):
    def forbidden_worker(*_args, **_kwargs):
        pytest.fail("Layout-only assignment construction must not start a worker")

    monkeypatch.setattr(AssignmentDetailDialog, "_start_job", forbidden_worker)
    task = assignment()
    dialog = AssignmentDetailDialog(DeadlineService().build(task), styled_window)
    qtbot.addWidget(dialog)
    dialog.resize(*size)
    dialog.show()
    qtbot.wait(50)

    assert dialog._thread is None
    assert not dialog.refresh_button.isEnabled()
    assert dialog.title_label.text() == task.title
    assert dialog.course_label.text() == "COMP5310"
    assert dialog.submission_label.text() == "Submission: Not submitted"
    assert format_due(dialog.deadline) in dialog.due_label.text()
    for label in (dialog.title_label, dialog.course_label, dialog.submission_label, dialog.due_label):
        assert_visible_unclipped(label)
    assert dialog.description.font().pixelSize() == 16
    assert dialog.chinese_description.font().pixelSize() == 16
    assert "Explain the dataset" in dialog.description.toPlainText()
    assert dialog.description.viewport().width() >= 400
    assert dialog.description.viewport().height() >= 80
    assert dialog.chinese_description.viewport().height() >= 80
    assert dialog.description.viewport().height() + dialog.chinese_description.viewport().height() >= 220
    assert dialog.rect().contains(dialog.description.mapTo(dialog, dialog.description.rect().bottomRight()))
    assert dialog.rect().contains(dialog.chinese_description.mapTo(dialog, dialog.chinese_description.rect().bottomRight()))


def test_assignment_long_heading_wraps_without_reducing_reading_font(qtbot, monkeypatch, styled_window):
    monkeypatch.setattr(AssignmentDetailDialog, "_start_job", lambda *_args: pytest.fail("Unexpected worker"))
    task = assignment(title="Final report on data preparation, exploratory analysis and model evaluation " * 2,
                      submission_status="submitted",
                      submission_submitted_at=datetime.now(timezone.utc))
    dialog = AssignmentDetailDialog(DeadlineService().build(task), styled_window)
    qtbot.addWidget(dialog)
    dialog.resize(1000, 680)
    dialog.show()
    qtbot.wait(50)

    for label in (dialog.title_label, dialog.course_label, dialog.submission_label,
                  dialog.due_label, dialog.submitted_label):
        assert_visible_unclipped(label)
    assert dialog.title_label.wordWrap()
    assert dialog.title_label.height() > dialog.title_label.fontMetrics().height()
    assert "Submitted" in dialog.submission_label.text()
    assert dialog.description.font().pixelSize() == 16
    assert dialog.chinese_description.font().pixelSize() == 16
    assert dialog.description.viewport().height() >= 70
    assert dialog.chinese_description.viewport().height() >= 70


@pytest.mark.parametrize("filename", ["Assignment guide.txt", "A detailed assignment guide and supporting lecture notes " * 2 + ".txt"])
def test_file_dialog_keeps_large_readable_preview_at_minimum_size(qtbot, monkeypatch, styled_window, filename):
    monkeypatch.setattr(CanvasFileDialog, "_load", lambda self: None)
    dialog = CanvasFileDialog(41, object(), styled_window)
    qtbot.addWidget(dialog)
    body = b"Introduction to the assignment.\n" * 50
    dialog._loaded(({"display_name": filename, "content-type": "text/plain"}, body))
    dialog._load_finished()
    dialog.resize(800, 560)
    dialog.show()
    qtbot.wait(50)

    assert dialog._thread is None
    assert dialog.preview_stack.currentWidget() is dialog.viewer
    assert dialog.viewer.font().pixelSize() == 15
    assert dialog.viewer.viewport().width() >= 700
    assert dialog.viewer.viewport().height() >= 350
    assert "Introduction to the assignment." in dialog.viewer.toPlainText()
    assert_visible_unclipped(dialog.heading)
    assert_visible_unclipped(dialog.status)
    assert dialog.rect().contains(dialog.preview_stack.geometry())
    assert dialog.save_button.isVisible() and dialog.save_button.isEnabled()
    assert dialog.close_button.isVisible()
