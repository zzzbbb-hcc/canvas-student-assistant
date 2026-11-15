from PySide6.QtCore import QRegularExpression, QThread, Qt, QUrl, Slot
from PySide6.QtGui import QImage, QTextDocument
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QSplitter, QTextBrowser,
    QVBoxLayout, QWidget,
)

from app.security.canvas_resources import canvas_file_links
from app.security.html_sanitizer import canvas_image_slots, safe_canvas_html
from app.services.canvas_client import CanvasClient, CanvasClientError
from app.services.deadline_service import DeadlineService
from app.services.offline_translation import offline_translation
from app.storage.session_cache import session_cache
from app.ui.canvas_worker import CanvasWorker
from app.ui.theme import PALETTE, application_stylesheet, reader_stylesheet, theme_font

def format_local_time(value):
    local = value.astimezone()
    offset = local.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}" if offset else ""
    zone = local.tzname() or "Local time"
    return f"{local:%d %b %Y} · {local:%H:%M} · {zone} (UTC{offset})"


def format_due(deadline):
    return "No deadline" if deadline.due_local is None else format_local_time(deadline.due_local)

class AssignmentDetailDialog(QDialog):
    def __init__(self, deadline, parent=None, *, credential_store=None, translator=None):
        super().__init__(parent)
        self.setObjectName("assignmentDetailDialog")
        self.setFont(theme_font())
        self.setStyleSheet(application_stylesheet())
        self.deadline = deadline
        self._store = credential_store
        self._thread = None
        self._worker = None
        self._phase = None
        self._pending_result = None
        self._image_sources = []
        self._file_ids = set()
        self._translator = translator if translator is not None else offline_translation
        self._translation_source = ""
        self._translated_source = None
        self._translation_requested = None
        self.setMinimumSize(900, 620)
        screen = QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.resize(min(1350, int(area.width() * 0.92)), min(900, int(area.height() * 0.90)))
        else:
            self.resize(1200, 800)
        layout = QVBoxLayout(self); layout.setContentsMargins(16, 14, 16, 14); layout.setSpacing(6)
        self.course_label = QLabel(); self.course_label.setTextFormat(Qt.TextFormat.PlainText); self.course_label.setWordWrap(True)
        self.title_label = QLabel(); self.title_label.setTextFormat(Qt.TextFormat.PlainText); self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet(f"font-size: 22px; font-weight: 700; color: {PALETTE.text};")
        self.due_label = QLabel(); self.due_label.setWordWrap(True)
        self.submission_label = QLabel(); self.submission_label.setObjectName("detailSubmissionStatus"); self.submission_label.setWordWrap(True)
        self.submitted_label = QLabel(); self.submitted_label.setWordWrap(True)
        self.quiz_info_label = QLabel(); self.quiz_info_label.setObjectName("quizInfo")
        self.quiz_info_label.setTextFormat(Qt.TextFormat.PlainText); self.quiz_info_label.setWordWrap(True)
        layout.addWidget(self.title_label)
        identity = QHBoxLayout(); identity.addWidget(self.course_label, 1); identity.addWidget(self.submission_label)
        layout.addLayout(identity)
        for label in (self.due_label, self.submitted_label):
            layout.addWidget(label)
        layout.addWidget(self.quiz_info_label)
        actions = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh assignment")
        self.refresh_button.setEnabled(self._store is not None)
        self.refresh_button.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_button)
        self.translate_button = QPushButton("重新翻译介绍 / Retry translation")
        self.translate_button.setObjectName("assignmentTranslateButton")
        self.translate_button.clicked.connect(self.translate_introduction)
        actions.addWidget(self.translate_button); actions.addStretch(); layout.addLayout(actions)
        self.status_label = QLabel(); self.status_label.setWordWrap(True); self.status_label.setObjectName("statusText")
        layout.addWidget(self.status_label)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        introduction = QWidget()
        introduction_layout = QVBoxLayout(introduction); introduction_layout.setContentsMargins(0, 0, 0, 0); introduction_layout.setSpacing(6)
        self.introduction_heading = QLabel("作业介绍 · Assignment introduction")
        self.introduction_heading.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {PALETTE.text};")
        introduction_layout.addWidget(self.introduction_heading)
        intro_splitter = QSplitter(Qt.Orientation.Vertical)
        original = QWidget(); original_layout = QVBoxLayout(original); original_layout.setContentsMargins(0, 0, 0, 0); original_layout.setSpacing(4)
        original_layout.addWidget(QLabel("English · Canvas 原文"))
        self.description = QTextBrowser(); self.description.setObjectName("assignmentDescription")
        self.description.setOpenLinks(False); self.description.setOpenExternalLinks(False)
        self.description.anchorClicked.connect(self._open_link)
        self.description.setFont(theme_font(16))
        self.description.document().setDefaultFont(theme_font(16))
        self.description.setStyleSheet(reader_stylesheet(font_size=16))
        original_layout.addWidget(self.description, 1)
        intro_splitter.addWidget(original)
        chinese = QWidget(); chinese_layout = QVBoxLayout(chinese); chinese_layout.setContentsMargins(0, 4, 0, 0); chinese_layout.setSpacing(4)
        chinese_layout.addWidget(QLabel("简体中文 · 本机离线译文"))
        self.translation_status = QLabel(); self.translation_status.setObjectName("assignmentTranslationStatus")
        self.translation_status.setWordWrap(True); chinese_layout.addWidget(self.translation_status)
        self.chinese_description = QTextBrowser(); self.chinese_description.setObjectName("assignmentChineseDescription")
        self.chinese_description.setOpenLinks(False); self.chinese_description.setOpenExternalLinks(False)
        self.chinese_description.setFont(theme_font(16))
        self.chinese_description.document().setDefaultFont(theme_font(16))
        self.chinese_description.setStyleSheet(reader_stylesheet(background=PALETTE.peach, font_size=16))
        chinese_layout.addWidget(self.chinese_description, 1)
        intro_splitter.addWidget(chinese); intro_splitter.setSizes([340, 340])
        intro_splitter.setChildrenCollapsible(False)
        introduction_layout.addWidget(intro_splitter, 1)
        self.content_notice = QLabel("中文仅供阅读参考，作业要求以 Canvas 原文为准。这里只翻译作业介绍，不读取附件。")
        self.content_notice.setWordWrap(True); self.content_notice.setStyleSheet(f"color: {PALETTE.text_muted}; font-size: 12px;")
        introduction_layout.addWidget(self.content_notice)
        splitter.addWidget(introduction)
        materials = QWidget(); materials.setMinimumWidth(230)
        materials_layout = QVBoxLayout(materials); materials_layout.setContentsMargins(6, 0, 0, 0); materials_layout.setSpacing(6)
        self.materials_heading = QLabel("Assignment materials"); self.materials_heading.setStyleSheet("font-size: 16px; font-weight: 700;")
        materials_layout.addWidget(self.materials_heading)
        hint = QLabel("Click a file to preview it, then choose Download / Save As."); hint.setWordWrap(True); materials_layout.addWidget(hint)
        self.file_list = QListWidget(); self.file_list.setObjectName("assignmentMaterials")
        self.file_list.setWordWrap(True); self.file_list.itemClicked.connect(self._open_file_item)
        self.file_list.currentRowChanged.connect(lambda row: self.open_file_button.setEnabled(row >= 0 and self._store is not None))
        materials_layout.addWidget(self.file_list, 1)
        self.empty_materials = QLabel(); self.empty_materials.setWordWrap(True); materials_layout.addWidget(self.empty_materials)
        self.open_file_button = QPushButton("Open / Download selected file")
        self.open_file_button.setEnabled(False)
        self.open_file_button.clicked.connect(lambda: self._open_file_item(self.file_list.currentItem()))
        materials_layout.addWidget(self.open_file_button)
        splitter.addWidget(materials); splitter.setSizes([900, 280]); splitter.setStretchFactor(0, 1)
        layout.addWidget(splitter, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self._render_assignment(deadline.assignment)
        if self._store is not None:
            self.refresh()
        elif translator is not None:
            self._start_translation()

    def _render_assignment(self, assignment):
        self.deadline = DeadlineService().build(assignment)
        self.setWindowTitle(assignment.title)
        self.course_label.setText(assignment.course_code or assignment.course_name)
        self.title_label.setText(assignment.title)
        self.due_label.setText(f"Due: {format_due(self.deadline)} · {self.deadline.countdown}")
        self.submission_label.setText(f"Submission: {assignment.submission_label}")
        status_color = PALETTE.success if assignment.is_submitted else PALETTE.warning
        status_background = PALETTE.success_bg if assignment.is_submitted else PALETTE.warning_bg
        self.submission_label.setStyleSheet(
            f"font-weight: 700; color: {status_color}; background: {status_background}; "
            "border-radius: 8px; padding: 3px 8px;"
        )
        self.submitted_label.setText(f"Submitted: {format_local_time(assignment.submission_submitted_at)}" if assignment.submission_submitted_at else "")
        self.submitted_label.setVisible(assignment.submission_submitted_at is not None)
        self.introduction_heading.setText("测验介绍 · Quiz introduction" if assignment.is_quiz else "作业介绍 · Assignment introduction")
        self.content_notice.setText(
            "中文仅供阅读参考，要求和时间以 Canvas 原文为准。这里只读测验介绍，不读取题目、开始测验或提交答案。"
            if assignment.is_quiz else "中文仅供阅读参考，作业要求以 Canvas 原文为准。这里只翻译作业介绍，不读取附件。"
        )
        quiz_info = []
        if assignment.is_quiz:
            quiz_info.append("New Quiz" if assignment.quiz_engine == "new" else "Classic Quiz")
            if assignment.quiz_time_limit is not None:
                quiz_info.append(f"作答时限 / Time limit: {assignment.quiz_time_limit:g} min")
            if assignment.unlock_at is not None:
                quiz_info.append(f"开放 / Opens: {format_local_time(assignment.unlock_at)}")
            if assignment.lock_at is not None:
                quiz_info.append(f"关闭 / Closes: {format_local_time(assignment.lock_at)} (not the deadline)")
            if assignment.quiz_locked:
                quiz_info.append("当前已锁定 / Locked for you")
        self.quiz_info_label.setText(" · ".join(quiz_info)); self.quiz_info_label.setVisible(bool(quiz_info))
        self.description.setHtml(safe_canvas_html(
            assignment.description,
            "No quiz introduction is available from Canvas with your current access."
            if assignment.is_quiz else "No description provided by Canvas.",
            preserve_image_slots=self._store is not None, preserve_file_links=True,
        ))
        # Extract display text on the GUI thread; never pass Qt objects, URLs,
        # credentials, or attachment contents to the local translation worker.
        import re
        text_document = QTextDocument()
        text_document.setHtml(safe_canvas_html(assignment.description, "", preserve_image_slots=True))
        source = re.sub(r"CANVAS_IMAGE_SLOT_\d{4}", "", text_document.toPlainText()).strip()
        if source != self._translation_source or self._translated_source is None:
            self._translation_source = source
            self._translated_source = None
            self._translation_requested = source or None
            self.chinese_description.clear()
            self.translation_status.setText("正在准备本机离线翻译…" if source else "当前没有可翻译的测验介绍。" if assignment.is_quiz else "Canvas 未提供可翻译的作业介绍。")
        self.translate_button.setEnabled(bool(source) and self._thread is None)
        self._image_sources = canvas_image_slots(assignment.description, CanvasClient.BASE_URL)
        links = canvas_file_links(assignment.description)
        self._file_ids = {link.file_id for link in links}
        self.file_list.clear()
        for link in links:
            item = QListWidgetItem(link.title)
            item.setData(Qt.ItemDataRole.UserRole, link.file_id)
            item.setToolTip(link.title)
            self.file_list.addItem(item)
        self.materials_heading.setText(f"Assignment materials ({len(links)})")
        self.empty_materials.setText("No linked Canvas files in this assignment." if not links else "")
        self.file_list.setCurrentRow(0 if links else -1)

    def _fetch_assignment(self):
        token = self._store.get_canvas_token()
        if not token:
            raise CanvasClientError("No Canvas token is stored.")
        assignment = self.deadline.assignment
        with CanvasClient(token) as client:
            if assignment.is_quiz and assignment.assignment_id < 0 and assignment.quiz_id is not None:
                return client.get_quiz_assignment(assignment.course_id, assignment.quiz_id, engine=assignment.quiz_engine or "classic")
            fresh = client.get_assignment(assignment.course_id, assignment.assignment_id)
            if assignment.is_quiz:
                metadata = assignment
                if not fresh.description and assignment.quiz_id is not None:
                    try:
                        metadata = client.get_quiz_assignment(
                            assignment.course_id, assignment.quiz_id,
                            engine=assignment.quiz_engine or "classic",
                        )
                    except CanvasClientError:
                        # A student may read assignment deadlines but lack
                        # the separate Quiz API permission. Keep available
                        # discovery instructions, never start an attempt.
                        pass
                # The assignment endpoint is authoritative for the student's
                # due/submission state; keep quiz-only metadata from discovery.
                updates = {
                    name: getattr(metadata, name) for name in (
                        "quiz_id", "quiz_engine", "quiz_type", "quiz_time_limit", "quiz_locked", "quiz_assignment_id"
                    ) if getattr(fresh, name, None) is None or (
                        name == "quiz_locked" and name not in fresh.model_fields_set
                    )
                }
                if not fresh.description and metadata.description:
                    updates["description"] = metadata.description
                fresh = fresh.model_copy(update=updates)
            return fresh

    def _fetch_images(self):
        token = self._store.get_canvas_token()
        if not token:
            return []
        images = []
        with CanvasClient(token, timeout=5.0) as client:
            for slot, url in self._image_sources:
                try:
                    images.append((slot, client.get_canvas_image(url)))
                except CanvasClientError:
                    continue
        return images

    def refresh(self):
        if self._store is None or self._thread is not None:
            return
        self.status_label.setText("Loading the latest assignment instructions and materials…")
        self._start_job("assignment", self._fetch_assignment)

    def _start_job(self, phase, operation):
        self._phase = phase
        self.refresh_button.setEnabled(False)
        self.translate_button.setEnabled(False)
        self._thread = QThread(self)
        self._worker = CanvasWorker(operation); self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.succeeded.connect(self._job_succeeded)
        self._worker.failed.connect(self._job_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._job_finished)
        self._thread.start()

    @Slot(object)
    def _job_succeeded(self, result):
        if self._pending_result is not None:
            return
        if self._phase == "assignment":
            self._render_assignment(result)
            cached = session_cache.get("assignments")
            if cached is not None:
                session_cache.set("assignments", [result if (item.course_id, item.assignment_id) == (result.course_id, result.assignment_id) else item for item in cached])
            self.status_label.setText("Assignment updated. Click a linked file or use Assignment materials to preview and download.")
        elif self._phase == "images":
            self._insert_images(result)
        elif self._phase == "translation":
            source, translated = result
            if source == self._translation_source:
                self.chinese_description.setPlainText(translated)
                self._translated_source = source
                self.translation_status.setText("离线翻译完成 · 正文未发送给第三方")

    @Slot(str)
    def _job_failed(self, message):
        if self._phase == "translation":
            self.translation_status.setText("离线翻译暂不可用，可点击重新翻译；英文原文不受影响。")
            return
        self._image_sources = []
        self.status_label.setText(message)
        self._insert_images([])

    @Slot()
    def _job_finished(self):
        # finished can precede native thread-local cleanup. Join that tail
        # before a chained image/translation job reuses native allocations.
        thread = self._thread
        if thread is not None:
            thread.wait()
            thread.deleteLater()
        self._thread = None
        self._worker = None
        if self._pending_result is not None:
            result, self._pending_result = self._pending_result, None
            super().done(result)
            return
        if self._phase == "assignment" and self._image_sources:
            self._start_job("images", self._fetch_images)
        elif self._translation_requested:
            self._start_translation()
        else:
            self.refresh_button.setEnabled(self._store is not None)
            self.translate_button.setEnabled(bool(self._translation_source))

    def _start_translation(self):
        if self._thread is not None or self._pending_result is not None:
            return
        source = self._translation_requested
        if not source:
            return
        self._translation_requested = None
        self.translation_status.setText("正在本机翻译作业介绍，首次加载模型可能稍慢…")
        self._start_job("translation", lambda: (source, self._translator.translate(source)))

    def translate_introduction(self):
        if self._thread is not None or not self._translation_source:
            return
        self._translation_requested = self._translation_source
        self._start_translation()

    def _insert_images(self, images):
        for slot, raw in images:
            image = QImage.fromData(raw)
            if image.isNull():
                continue
            width = max(400, self.description.viewport().width() - 50)
            if image.width() > width or image.height() > 1200:
                image = image.scaled(width, 1200, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            cursor = self.description.document().find(f"CANVAS_IMAGE_SLOT_{slot:04d}")
            if not cursor.isNull():
                cursor.insertImage(image)
        marker = QRegularExpression(r"CANVAS_IMAGE_SLOT_\d{4}")
        cursor = self.description.document().find(marker)
        while not cursor.isNull():
            cursor.insertText("[Image unavailable]")
            cursor = self.description.document().find(marker, cursor)

    def _open_link(self, url: QUrl):
        if url.scheme() == "canvas-file" and url.path().isdigit():
            self._open_file(int(url.path()))

    def _open_file_item(self, item):
        if item is not None:
            self._open_file(item.data(Qt.ItemDataRole.UserRole))

    def _open_file(self, file_id):
        if file_id not in self._file_ids or self._store is None:
            return
        from app.ui.canvas_file_dialog import CanvasFileDialog
        CanvasFileDialog(file_id, self._store, self).exec()

    def done(self, result):
        if self._thread is not None:
            self._pending_result = result
            self.status_label.setText("Finishing the current Canvas request before closing…")
            return
        super().done(result)

    @Slot()
    def accept(self):
        self.done(QDialog.DialogCode.Accepted)

    @Slot()
    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        if self._thread is not None:
            self._pending_result = QDialog.DialogCode.Rejected
            self.status_label.setText("Finishing the current Canvas request before closing…")
            event.ignore()
            return
        super().closeEvent(event)
