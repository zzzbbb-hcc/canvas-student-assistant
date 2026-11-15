from __future__ import annotations

from threading import Event

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QThread, Qt
from PySide6.QtGui import QImage, QPainter, QPdfWriter
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from app.services.canvas_client import CanvasClientError
from app.ui import canvas_file_dialog
from app.ui.canvas_file_dialog import CanvasFileDialog, safe_download_filename


def loaded_dialog(qtbot, monkeypatch, metadata=None, content=b"Course notes"):
    monkeypatch.setattr(
        canvas_file_dialog, "load_canvas_file",
        lambda file_id, store: (metadata or {"display_name": "notes.txt"}, content),
    )
    dialog = CanvasFileDialog(17, object())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=3000)
    return dialog


@pytest.mark.parametrize("name, expected", [
    (r"..\folder\notes?.txt", "notes_.txt"),
    ("../../lecture.pdf", "lecture.pdf"),
    ("NUL.txt", "_NUL.txt"),
    ("con .txt", "_con .txt"),
    ("LPT1", "_LPT1"),
    ("lecture.pdf... ", "lecture.pdf"),
    (". ", "canvas-file"),
    ("https://example.test/file?signature=private", "canvas-file"),
])
def test_safe_filename_removes_paths_and_windows_special_names(name, expected):
    assert safe_download_filename(name) == expected


def test_download_writes_fetched_bytes_to_selected_path(qtbot, monkeypatch, tmp_path):
    secret_url = "https://files.example.test/notes?signature=do-not-show"
    dialog = loaded_dialog(qtbot, monkeypatch, {
        "display_name": r"..\NUL.txt", "url": secret_url,
    })
    output = tmp_path / "download.txt"
    output.write_bytes(b"Old content")
    pickers = []

    def choose_path(*args, **kwargs):
        pickers.append((args, kwargs))
        return str(output), "All files (*)"

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_path)
    dialog.save_button.click()
    assert output.read_bytes() == b"Course notes"
    assert pickers[0][0][2] == "_NUL.txt"
    assert "options" not in pickers[0][1]  # Native overwrite confirmation stays enabled.
    assert dialog.status.text() == "File saved."
    assert secret_url not in dialog.viewer.toPlainText() + dialog.status.text() + dialog.heading.text()
    assert all(secret_url != value for value in dialog.__dict__.values())
    assert dialog._last_render_thread is QApplication.instance().thread()


def test_cancelled_save_does_not_write(qtbot, monkeypatch, tmp_path):
    dialog = loaded_dialog(qtbot, monkeypatch)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    dialog.save_as()
    assert list(tmp_path.iterdir()) == []
    assert dialog.status.text().startswith("File loaded")


def test_invalid_file_has_retry_and_recovers(qtbot, monkeypatch):
    calls = []

    def fetch(file_id, store):
        calls.append(QThread.currentThread())
        if len(calls) == 1:
            raise CanvasClientError("This Canvas file is unavailable.")
        return {"display_name": "notes.txt"}, b"Recovered notes"

    monkeypatch.setattr(canvas_file_dialog, "load_canvas_file", fetch)
    dialog = CanvasFileDialog(17, object())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=3000)
    assert "unavailable" in dialog.status.text()
    assert dialog.retry_button.isVisible()
    assert dialog.retry_button.isEnabled()
    assert not dialog.save_button.isEnabled()
    dialog.retry_button.click()
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=3000)
    assert dialog.viewer.toPlainText() == "Recovered notes"
    assert dialog.save_button.isEnabled()
    assert all(thread is not QApplication.instance().thread() for thread in calls)


@pytest.mark.parametrize("action, result", [
    ("accept", QDialog.DialogCode.Accepted),
    ("reject", QDialog.DialogCode.Rejected),
    ("close", QDialog.DialogCode.Rejected),
    ("done", QDialog.DialogCode.Accepted),
    ("escape", QDialog.DialogCode.Rejected),
])
def test_dialog_waits_for_worker_before_finishing(qtbot, monkeypatch, action, result):
    entered = Event()
    release = Event()

    def fetch(file_id, store):
        entered.set()
        release.wait(timeout=3)
        return {"display_name": "notes.txt"}, b"Notes"

    monkeypatch.setattr(canvas_file_dialog, "load_canvas_file", fetch)
    dialog = CanvasFileDialog(17, object())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(entered.is_set, timeout=2000)
    thread = dialog._thread
    try:
        if action == "done":
            dialog.done(result)
        elif action == "escape":
            qtbot.keyClick(dialog, Qt.Key.Key_Escape)
        else:
            getattr(dialog, action)()
        assert dialog.isVisible()
        assert dialog._thread is thread and thread.isRunning()
        assert not dialog.close_button.isEnabled()
    finally:
        release.set()
        qtbot.waitUntil(lambda: dialog._thread is None, timeout=3000)
    assert not dialog.isVisible()
    assert dialog.result() == result


def test_pdf_keeps_readable_buffer_and_uses_large_multipage_view(qtbot, monkeypatch):
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QPdfWriter(buffer)
    painter = QPainter(writer)
    painter.drawText(100, 100, "First page")
    writer.newPage()
    painter.drawText(100, 100, "Second page")
    painter.end()
    del writer
    buffer.close()
    dialog = loaded_dialog(qtbot, monkeypatch, {"display_name": "lecture.pdf"}, bytes(data))
    qtbot.waitUntil(lambda: dialog.pdf_document.status() == QPdfDocument.Status.Ready, timeout=3000)
    assert dialog.pdf_document.pageCount() == 2
    assert dialog.pdf_buffer.isOpen()
    assert bytes(dialog.pdf_bytes) == bytes(data)
    assert bytes(dialog.pdf_buffer.data()) == bytes(data)
    assert dialog.preview_stack.currentWidget() is dialog.pdf_view
    assert dialog.pdf_view.pageMode() == QPdfView.PageMode.MultiPage
    assert dialog.pdf_view.zoomMode() == QPdfView.ZoomMode.FitToWidth


def test_image_is_inserted_and_text_is_not_interpreted_as_html(qtbot, monkeypatch):
    image = QImage(40, 30, QImage.Format.Format_RGB32)
    image.fill(0x6699CC)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    image_dialog = loaded_dialog(qtbot, monkeypatch, {"content-type": "image/png"}, bytes(data))
    assert "\ufffc" in image_dialog.viewer.toPlainText()
    text_dialog = loaded_dialog(qtbot, monkeypatch, content=b"<script>plain text</script>")
    assert text_dialog.viewer.toPlainText() == "<script>plain text</script>"
    assert text_dialog.viewer.isReadOnly()


def test_loader_uses_canvas_client_and_discards_download_url(monkeypatch):
    calls = []

    class Store:
        def get_canvas_token(self):
            return "test-only-token"

    class Client:
        def __init__(self, token):
            calls.append(token)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_canvas_file_content(self, file_id, max_bytes):
            calls.append((file_id, max_bytes))
            return {"display_name": "notes.txt", "url": "https://private.test/?signature=secret"}, b"Notes"

    monkeypatch.setattr(canvas_file_dialog, "CanvasClient", Client)
    metadata, content = canvas_file_dialog.load_canvas_file(17, Store())
    assert calls == ["test-only-token", (17, 25_000_000)]
    assert metadata == {"display_name": "notes.txt", "content-type": "", "size": 5}
    assert content == b"Notes"


def test_invalid_file_id_fails_without_reading_credentials():
    with pytest.raises(CanvasClientError, match="unavailable"):
        canvas_file_dialog.load_canvas_file(0, object())
