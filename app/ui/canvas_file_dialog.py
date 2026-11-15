"""Read-only Canvas file previews and explicit, atomic downloads."""

from __future__ import annotations

import re

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSaveFile, QThread, Qt, Slot
from PySide6.QtGui import QImage
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton,
    QStackedWidget, QTextBrowser, QVBoxLayout,
)

from app.services.canvas_client import CanvasClient, CanvasClientError
from app.services.file_preview import extract_file_text, preview_kind
from app.ui.canvas_worker import CanvasWorker
from app.ui.theme import PALETTE, application_stylesheet, reader_stylesheet, theme_font


MAX_FILE_BYTES = 25_000_000
_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³"
}


def safe_download_filename(name: object, fallback: str = "canvas-file") -> str:
    """Return a single filename that can safely be suggested on Windows."""
    if not isinstance(name, str) or "://" in name:
        return fallback
    basename = name.replace("\\", "/").rsplit("/", 1)[-1]
    basename = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", basename).strip(" .")
    if not basename:
        return fallback
    # Windows reserves device names even when followed by an extension.
    if basename.partition(".")[0].rstrip(" .").upper() in _WINDOWS_DEVICES:
        basename = "_" + basename
    if len(basename) > 180:
        stem, dot, extension = basename.rpartition(".")
        if dot and stem and len(extension) < 20:
            basename = stem[:179 - len(extension)] + "." + extension
        else:
            basename = basename[:180]
    return basename.rstrip(" .") or fallback


def _preview_metadata(metadata: dict, file_id: int, size: int) -> dict:
    """Whitelist preview fields, excluding Canvas URLs and signed URLs."""
    name = metadata.get("display_name") or metadata.get("filename")
    mime = metadata.get("content-type")
    return {
        "display_name": safe_download_filename(name, f"canvas-file-{file_id}"),
        "content-type": mime.split(";", 1)[0].strip().lower() if isinstance(mime, str) else "",
        "size": size,
    }


def load_canvas_file(file_id: int, credential_store) -> tuple[dict, bytes]:
    """Fetch in the worker thread; retain no download URL in the result."""
    if not isinstance(file_id, int) or isinstance(file_id, bool) or file_id <= 0:
        raise CanvasClientError("This Canvas file is unavailable.")
    token = credential_store.get_canvas_token()
    if not token:
        raise CanvasClientError("No Canvas token is stored.")
    with CanvasClient(token) as client:
        metadata, content = client.get_canvas_file_content(file_id, max_bytes=MAX_FILE_BYTES)
    return _preview_metadata(metadata, file_id, len(content)), content


class CanvasFileDialog(QDialog):
    def __init__(self, file_id: int, credential_store, parent=None) -> None:
        super().__init__(parent)
        self.file_id = file_id
        self._store = credential_store
        self._thread: QThread | None = None
        self._worker: CanvasWorker | None = None
        self._pending_result: int | None = None
        self._content: bytes | None = None
        self._filename = f"canvas-file-{file_id}"
        self._last_render_thread = None
        self.pdf_document = None
        self.pdf_buffer = None
        self.pdf_bytes = None

        self.setObjectName("canvasFileDialog")
        self.setFont(theme_font())
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("Canvas file")
        self.setMinimumSize(800, 560)
        screen = QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.resize(min(1320, int(area.width() * 0.90)), min(900, int(area.height() * 0.90)))
        else:
            self.resize(1200, 820)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        self.heading = QLabel("Canvas file")
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setWordWrap(True)
        self.heading.setStyleSheet(f"font-size: 20px; font-weight: 700; color: {PALETTE.text};")
        layout.addWidget(self.heading)
        self.status = QLabel()
        self.status.setObjectName("canvasFileStatus")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.viewer = QTextBrowser()
        self.viewer.setObjectName("canvasFilePreview")
        self.viewer.setReadOnly(True)
        self.viewer.setOpenExternalLinks(False)
        self.viewer.setOpenLinks(False)
        self.viewer.setFont(theme_font(15))
        self.viewer.document().setDefaultFont(theme_font(15))
        self.viewer.setStyleSheet(reader_stylesheet(font_size=15) + "QTextBrowser { padding: 10px; }")
        self.pdf_view = QPdfView()
        self.pdf_view.setStyleSheet(f"QPdfView {{ background: {PALETTE.surface_alt}; border: 1px solid {PALETTE.border}; border-radius: 14px; }}")
        self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
        self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.preview_stack = QStackedWidget()
        self.preview_stack.addWidget(self.viewer)
        self.preview_stack.addWidget(self.pdf_view)
        layout.addWidget(self.preview_stack, 1)
        actions = QHBoxLayout()
        self.retry_button = QPushButton("Retry")
        self.retry_button.setObjectName("canvasFileRetryButton")
        self.retry_button.clicked.connect(self._load)
        actions.addWidget(self.retry_button)
        actions.addStretch()
        self.save_button = QPushButton("Download / Save As…")
        self.save_button.setObjectName("canvasFileSaveButton")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_as)
        actions.addWidget(self.save_button)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self._load()

    @Slot()
    def _load(self) -> None:
        # A thread that has stopped may still have queued GUI cleanup to run.
        if self._thread is not None or self._pending_result is not None:
            return
        self.status.setText("Loading Canvas file…")
        self.viewer.setPlainText("Loading a read-only preview…")
        self.preview_stack.setCurrentWidget(self.viewer)
        self.retry_button.hide()
        self.retry_button.setEnabled(False)
        self.save_button.setEnabled(False)
        thread = QThread(self)
        worker = CanvasWorker(lambda: load_canvas_file(self.file_id, self._store))
        self._thread = thread
        self._worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._loaded)
        worker.failed.connect(self._failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._load_finished)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    @Slot(object)
    def _loaded(self, result) -> None:
        if self._pending_result is not None:
            return
        self._last_render_thread = QThread.currentThread()
        try:
            metadata, content = result
            if not isinstance(metadata, dict) or not isinstance(content, (bytes, bytearray)):
                raise ValueError
            if len(content) > MAX_FILE_BYTES:
                self._failed("Canvas file is too large to preview safely.")
                return
            metadata = _preview_metadata(metadata, self.file_id, len(content))
            self._content = bytes(content)
            self._filename = metadata["display_name"]
            self.heading.setText(self._filename)
            self.setWindowTitle(self._filename)
            self.status.setText(f"File loaded · {len(content):,} bytes")
            self._render_preview(metadata, self._content)
        except Exception:
            self._failed("This Canvas file could not be previewed. Please retry.")

    @Slot(str)
    def _failed(self, message: str) -> None:
        if self._pending_result is not None:
            return
        self._content = None
        self.status.setText(message)
        self.viewer.setPlainText("The file could not be loaded. Select Retry to try again.")
        self.preview_stack.setCurrentWidget(self.viewer)
        self.retry_button.show()
        self.save_button.setEnabled(False)

    @Slot()
    def _load_finished(self) -> None:
        self._thread = None
        self._worker = None
        if self._pending_result is not None:
            result = self._pending_result
            self._pending_result = None
            super().done(result)
            return
        self.retry_button.setEnabled(True)
        self.save_button.setEnabled(self._content is not None)
        self.close_button.setEnabled(True)

    def _clear_pdf(self) -> None:
        self.pdf_view.setDocument(None)
        if self.pdf_document is not None:
            # close() must precede releasing the buffer and its backing bytes.
            self.pdf_document.close()
            self.pdf_document.deleteLater()
        self.pdf_document = None
        self.pdf_buffer = None
        self.pdf_bytes = None

    def _render_preview(self, metadata: dict, content: bytes) -> None:
        self._clear_pdf()
        kind = preview_kind(metadata)
        if kind == "pdf":
            self.pdf_bytes = QByteArray(content)
            self.pdf_document = QPdfDocument(self.pdf_view)
            self.pdf_buffer = QBuffer(self.pdf_document)
            # QBuffer owns this implicitly shared copy, so it cannot outlive an
            # external QByteArray pointer during deferred QObject deletion.
            self.pdf_buffer.setData(self.pdf_bytes)
            self.pdf_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            self.pdf_document.statusChanged.connect(self._pdf_status_changed)
            self.pdf_view.setDocument(self.pdf_document)
            self.preview_stack.setCurrentWidget(self.pdf_view)
            self.pdf_document.load(self.pdf_buffer)
            return
        self.preview_stack.setCurrentWidget(self.viewer)
        if kind == "image":
            image = QImage.fromData(content)
            if image.isNull():
                self.viewer.setPlainText("This image could not be previewed. You can still save the file.")
                return
            available_width = max(600, self.viewer.viewport().width() - 50)
            if image.width() > available_width or image.height() > 1600:
                image = image.scaled(
                    available_width, 1600, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            self.viewer.clear()
            self.viewer.textCursor().insertImage(image)
            return
        if kind in {"docx", "pptx", "xlsx", "text"}:
            try:
                text = extract_file_text(kind, content)[:500_000]
            except Exception:
                text = "This file could not be previewed. You can still save the file."
            self.viewer.setPlainText(text or "This file contains no readable text.")
            return
        self.viewer.setPlainText("A preview is unavailable for this file type. Use Download / Save As to save it.")

    @Slot(QPdfDocument.Status)
    def _pdf_status_changed(self, status) -> None:
        if status == QPdfDocument.Status.Error and self._pending_result is None:
            self.viewer.setPlainText("This PDF could not be previewed. You can still save the file.")
            self.preview_stack.setCurrentWidget(self.viewer)

    @Slot()
    def save_as(self) -> None:
        if self._content is None or self._thread is not None or self._pending_result is not None:
            return
        # Native QFileDialog supplies its normal overwrite confirmation. Do not
        # use DontConfirmOverwrite or bypass this picker with a guessed path.
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Save Canvas file", self._filename, "All files (*)"
        )
        if not path:
            return
        target = QSaveFile(path)
        # Keep atomic replacement mandatory, including on filesystems where a
        # direct write might otherwise be used as a fallback.
        target.setDirectWriteFallback(False)
        if not target.open(QIODevice.OpenModeFlag.WriteOnly):
            self.status.setText("Could not save this file. Choose another location and try again.")
            return
        if target.write(self._content) != len(self._content):
            target.cancelWriting()
            self.status.setText("Could not save this file. Choose another location and try again.")
            return
        if not target.commit():
            self.status.setText("Could not save this file. Choose another location and try again.")
            return
        self.status.setText("File saved.")

    def done(self, result: int) -> None:
        if self._thread is not None:
            if self._pending_result is None:
                self._pending_result = result
            self.status.setText("Waiting for the current Canvas request to finish…")
            self.retry_button.setEnabled(False)
            self.save_button.setEnabled(False)
            self.close_button.setEnabled(False)
            return
        super().done(result)

    @Slot()
    def accept(self) -> None:
        self.done(QDialog.DialogCode.Accepted)

    @Slot()
    def reject(self) -> None:
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event) -> None:
        if self._thread is not None:
            event.ignore()
            self.reject()
            return
        super().closeEvent(event)
