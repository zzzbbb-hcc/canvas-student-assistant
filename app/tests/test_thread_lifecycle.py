from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QThread
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from app.ui.canvas_worker import is_thread_running
from app.models.course import Course
from app.ui.course_detail import CourseDetailDialog
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidgetItem


def test_deleted_qthread_is_treated_as_not_running(qtbot):
    thread = QThread()
    thread.start()
    assert is_thread_running(thread)
    thread.quit()
    qtbot.waitUntil(thread.isFinished, timeout=2000)
    thread.deleteLater()
    qtbot.wait(10)
    assert not is_thread_running(thread)


def test_module_items_can_be_loaded_repeatedly(qtbot, monkeypatch):
    monkeypatch.setattr(CourseDetailDialog, "_load", lambda self: None)
    dialog = CourseDetailDialog(Course(id=1, name="Course", course_code="CODE"), object())
    qtbot.addWidget(dialog)
    monkeypatch.setattr(dialog, "_fetch_module_content", lambda item: ("ExternalUrl", item))
    item = QTreeWidgetItem(["Resource", "ExternalUrl", "Available"])
    item.setData(0, Qt.ItemDataRole.UserRole, {"title": "Resource", "type": "ExternalUrl", "external_url": "https://example.invalid"})
    dialog._show_module_item(item, 0)
    qtbot.waitUntil(lambda: dialog.close_button.isEnabled(), timeout=2000)
    qtbot.wait(10)
    dialog._show_module_item(item, 0)
    qtbot.waitUntil(lambda: dialog.close_button.isEnabled(), timeout=2000)
    assert "Resource" in dialog.module_viewer.toPlainText()
    assert dialog._last_render_thread is QApplication.instance().thread()


def test_canvas_image_is_inserted_at_original_text_position(qtbot, monkeypatch):
    monkeypatch.setattr(CourseDetailDialog, "_load", lambda self: None)
    dialog = CourseDetailDialog(Course(id=2, name="Course", course_code="CODE"), object())
    qtbot.addWidget(dialog)
    image = QImage(40, 30, QImage.Format.Format_RGB32)
    image.fill(0x6699CC)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    payload = {
        "title": "Inline image",
        "body": '<p>Before image</p><img src="/image.png"><p>After image</p>',
        "_images": [(0, bytes(data))],
    }
    dialog._render_content(("Page", payload), dialog.module_viewer)
    text = dialog.module_viewer.toPlainText()
    assert text.index("Before image") < text.index("\ufffc") < text.index("After image")
