from threading import Event

from PySide6.QtCore import QUrl, Qt

from app.models.assignment import Assignment
from app.models.course import Course
from app.security.html_sanitizer import safe_canvas_html
from app.services.canvas_client import CanvasClientError
from app.services.deadline_service import DeadlineService
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.ui.assignments import AssignmentsPage
from app.ui.course_detail import CourseDetailDialog
from app.ui.dashboard import DashboardPage
from app.ui.deadline_widgets import AssignmentDetailDialog


def assignment(description=None):
    return Assignment(
        assignment_id=7, course_id=4, course_name="Example Course",
        title="Assignment materials", submission_status="unsubmitted",
        description=description,
    )


def test_file_links_are_internal_ids_and_do_not_retain_authenticated_url():
    html = '<p>Read <a href="/courses/4/files/42/download?verifier=private-value">brief.pdf</a></p><a href="https://other.example/file">external</a>'
    safe = safe_canvas_html(html, preserve_file_links=True)
    assert '<a href="canvas-file:42">brief.pdf</a>' in safe
    assert "private-value" not in safe
    assert "https://" not in safe
    assert "external" in safe
    assert "href" not in safe_canvas_html(html)


def test_cached_assignment_fetches_full_instructions_and_materials(qtbot, monkeypatch):
    fresh = assignment('<p>Latest instructions</p><a href="/courses/4/files/42?wrap=1">brief.pdf</a>')
    monkeypatch.setattr(AssignmentDetailDialog, "_fetch_assignment", lambda self: fresh)
    dialog = AssignmentDetailDialog(DeadlineService().build(assignment()), credential_store=object())
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=3000)
    assert "Latest instructions" in dialog.description.toPlainText()
    assert dialog.file_list.count() == 1
    assert dialog.file_list.item(0).text() == "brief.pdf"
    assert dialog.file_list.item(0).data(Qt.ItemDataRole.UserRole) == 42
    assert dialog.open_file_button.isEnabled()


def test_failed_refresh_retains_available_instructions_and_can_retry(qtbot, monkeypatch):
    def fail(self):
        raise CanvasClientError("Canvas connection timed out.")
    monkeypatch.setattr(AssignmentDetailDialog, "_fetch_assignment", fail)
    dialog = AssignmentDetailDialog(DeadlineService().build(assignment("<p>Existing text</p>")), credential_store=object())
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=3000)
    assert "Existing text" in dialog.description.toPlainText()
    assert dialog.status_label.text() == "Canvas connection timed out."
    assert dialog.refresh_button.isEnabled()


def test_only_detected_canvas_files_open_preview(qtbot, monkeypatch):
    opened = []
    class FileDialog:
        def __init__(self, file_id, store, parent):
            opened.append(file_id)
        def exec(self):
            pass
    monkeypatch.setattr("app.ui.canvas_file_dialog.CanvasFileDialog", FileDialog)
    dialog = AssignmentDetailDialog(DeadlineService().build(assignment('<a href="/files/42">brief.pdf</a>')))
    qtbot.addWidget(dialog)
    dialog._store = object()
    dialog._open_link(QUrl("https://evil.example/files/42"))
    dialog._open_link(QUrl("canvas-file:99"))
    assert opened == []
    dialog._open_link(QUrl("canvas-file:42"))
    assert opened == [42]


def test_escape_waits_for_worker_before_releasing_dialog(qtbot, monkeypatch):
    started, release = Event(), Event()
    def slow_fetch(self):
        started.set()
        release.wait(timeout=5)
        return assignment("<p>Loaded</p>")
    monkeypatch.setattr(AssignmentDetailDialog, "_fetch_assignment", slow_fetch)
    dialog = AssignmentDetailDialog(DeadlineService().build(assignment()), credential_store=object())
    qtbot.addWidget(dialog); dialog.show()
    try:
        qtbot.waitUntil(started.is_set, timeout=2000)
        dialog.reject()
        assert dialog.isVisible()
        assert dialog._pending_result is not None
        release.set()
        qtbot.waitUntil(lambda: dialog._thread is None and not dialog.isVisible(), timeout=3000)
    finally:
        release.set()
        qtbot.waitUntil(lambda: dialog._thread is None, timeout=3000)


def test_single_card_click_opens_assignment_with_credentials(qtbot, monkeypatch):
    opened = []
    class Detail:
        def __init__(self, deadline, parent, *, credential_store):
            opened.append((deadline.assignment.assignment_id, credential_store))
        def exec(self):
            pass
    monkeypatch.setattr("app.ui.assignments.AssignmentDetailDialog", Detail)
    database = SQLiteDatabase(":memory:")
    try:
        store = object()
        page = AssignmentsPage(store, cache=Cache(database)); qtbot.addWidget(page)
        page._show([assignment()]); page.resize(1000, 750); page.show()
        qtbot.wait(20)
        item = next(page.assignment_list.item(row) for row in range(page.assignment_list.count()) if page.assignment_list.item(row).data(Qt.ItemDataRole.UserRole) is not None)
        page.assignment_list.scrollToItem(item)
        qtbot.mouseClick(page.assignment_list.viewport(), Qt.MouseButton.LeftButton, pos=page.assignment_list.visualItemRect(item).center())
        assert opened == [(7, store)]
    finally:
        database.close()


def test_dashboard_and_course_assignment_cards_open_same_detail(qtbot, monkeypatch):
    from datetime import datetime, timedelta
    opened = []
    class Detail:
        def __init__(self, deadline, parent, *, credential_store):
            opened.append((deadline.assignment.assignment_id, credential_store))
        def exec(self):
            pass
    monkeypatch.setattr("app.ui.dashboard.AssignmentDetailDialog", Detail)
    monkeypatch.setattr("app.ui.course_detail.AssignmentDetailDialog", Detail)
    monkeypatch.setattr(CourseDetailDialog, "_load", lambda self: None)
    database = SQLiteDatabase(":memory:")
    try:
        store = object(); cache = Cache(database)
        item = assignment(); item.due_at = datetime.now().astimezone() + timedelta(days=2)
        dashboard = DashboardPage(store, cache=cache); qtbot.addWidget(dashboard)
        dashboard._show([item]); dashboard.upcoming_list.itemClicked.emit(dashboard.upcoming_list.item(0))
        course = CourseDetailDialog(Course(id=4, name="Course", course_code="TEST"), store, cache=cache)
        qtbot.addWidget(course); course._populate(([item], [], [], [], []))
        course.assignment_list.itemClicked.emit(course.assignment_list.item(0))
        assert opened == [(7, store), (7, store)]
    finally:
        database.close()
