from __future__ import annotations

from html import escape

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRegularExpression, Qt, QThread, Slot
from PySide6.QtGui import QImage, QTextCursor
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QApplication, QDialog, QHeaderView, QHBoxLayout, QLabel, QListWidget, QPushButton, QSplitter,
    QStackedWidget, QTabWidget, QTextBrowser, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from app.services.canvas_client import CanvasClient, CanvasClientError
from app.services.deadline_service import DeadlineService
from app.services.file_preview import extract_file_text, preview_kind
from app.security.html_sanitizer import canvas_image_slots, safe_canvas_html
from app.storage.cache import Cache
from app.storage.database import SensitiveDataError
from app.ui.canvas_worker import CanvasWorker, is_thread_running
from app.ui.cards import CardList
from app.ui.deadline_widgets import AssignmentDetailDialog, format_due
from app.ui.doodle_icons import navigation_icon
from app.ui.theme import PALETTE, application_stylesheet, reader_stylesheet, theme_font
from app.storage.session_cache import session_cache


class CourseDetailDialog(QDialog):
    def __init__(self, course, credential_store, parent=None, *, cache=None):
        super().__init__(parent); self.course = course; self.store = credential_store; self._cache = cache or Cache(); self._thread = None; self._worker = None; self._content_thread = None; self._content_worker = None; self._content_target = None; self._last_render_thread = None; self._announcements = []
        self.setFont(theme_font())
        self.setObjectName("courseDialog"); self.setWindowTitle(course.name); self.setMinimumSize(1000, 680)
        screen = QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry(); self.resize(min(1450, int(area.width() * 0.92)), min(920, int(area.height() * 0.90)))
        else: self.resize(1300, 820)
        layout = QVBoxLayout(self); layout.setContentsMargins(18, 14, 18, 14); layout.setSpacing(6)
        heading = QLabel(course.name); heading.setObjectName("courseHeading"); heading.setWordWrap(True); layout.addWidget(heading)
        code = QLabel(course.course_code); code.setObjectName("courseCode"); layout.addWidget(code)
        self.status = QLabel("Loading read-only course content…"); self.status.setObjectName("courseStatus"); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        self.overview = QTextBrowser(); self.overview.setHtml(f"<h3>Overview</h3><p><b>Course:</b> {course.name}</p><p><b>Code:</b> {course.course_code or 'Not provided'}</p>"); self.tabs.addTab(self.overview, "Overview")
        self.assignment_list = CardList(compact=True); self.assignment_list.itemClicked.connect(self._open_assignment); self.tabs.addTab(self.assignment_list, "Assignments")
        announcement_page = QWidget(); announcement_layout = QHBoxLayout(announcement_page); announcement_layout.setContentsMargins(6, 6, 6, 6); splitter = QSplitter(); self.announcement_list = QListWidget(); self.announcement_body = QTextBrowser(); self.announcement_body.setOpenExternalLinks(False); splitter.addWidget(self.announcement_list); splitter.addWidget(self.announcement_body); announcement_layout.addWidget(splitter); self.announcement_list.currentRowChanged.connect(self._show_announcement); self.tabs.addTab(announcement_page, "Announcements")
        pages_page = QWidget(); pages_layout = QHBoxLayout(pages_page); pages_layout.setContentsMargins(6, 6, 6, 6); pages_splitter = QSplitter()
        self.pages_list = QListWidget(); self.pages_viewer = QTextBrowser(); self.pages_viewer.setOpenExternalLinks(False)
        self.pages_viewer.setHtml("<h3>Pages</h3><p>Select a Canvas page to read it inside the app.</p>")
        pages_splitter.addWidget(self.pages_list); pages_splitter.addWidget(self.pages_viewer); pages_splitter.setSizes([280, 580]); pages_layout.addWidget(pages_splitter)
        self.pages_list.currentRowChanged.connect(self._show_page); self.tabs.addTab(pages_page, "Pages")
        module_page = QWidget(); module_layout = QVBoxLayout(module_page); module_layout.setContentsMargins(6, 6, 6, 6); module_layout.setSpacing(6)
        module_actions = QHBoxLayout(); module_actions.addStretch(); self.expand_preview_button = QPushButton("Expand Preview"); self.expand_preview_button.setObjectName("previewButton"); self.expand_preview_button.clicked.connect(self._toggle_module_tree); module_actions.addWidget(self.expand_preview_button); module_layout.addLayout(module_actions)
        module_splitter = QSplitter(); self.module_splitter = module_splitter; module_splitter.setObjectName("softSplitter")
        self.module_tree = QTreeWidget(); self.module_tree.setHeaderLabels(["Module / Item", "Type", "State"])
        self.module_tree.setObjectName("moduleTree"); self.module_tree.setAlternatingRowColors(True); self.module_tree.setIndentation(18)
        self.module_tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.module_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.module_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.module_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.module_tree.itemClicked.connect(self._show_module_item)
        self.module_viewer = QTextBrowser(); self.module_viewer.setObjectName("moduleViewer"); self.module_viewer.setOpenExternalLinks(False)
        self.module_viewer.setHtml("<h3>Module content</h3><p>Select an item to read it inside the app.</p>")
        self.pdf_document = None; self.pdf_view = QPdfView()
        self.pdf_view.setStyleSheet(f"QPdfView {{ background: {PALETTE.surface_alt}; border: 1px solid {PALETTE.border}; border-radius: 14px; }}")
        self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage); self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.pdf_bytes = None; self.pdf_buffer = None
        self.module_stack = QStackedWidget(); self.module_stack.addWidget(self.module_viewer); self.module_stack.addWidget(self.pdf_view)
        module_splitter.addWidget(self.module_tree); module_splitter.addWidget(self.module_stack); module_splitter.setStretchFactor(0, 0); module_splitter.setStretchFactor(1, 1); module_splitter.setSizes([300, 1050])
        module_layout.addWidget(module_splitter); self.tabs.addTab(module_page, "Modules")
        for index, icon_key in enumerate(("dashboard", "assignments", "announcements", "courses", "courses")):
            self.tabs.setTabIcon(index, navigation_icon(icon_key))
        self.close_button = QPushButton("Close"); self.close_button.setObjectName("dialogCloseButton"); self.close_button.setMinimumWidth(110); self.close_button.setEnabled(False); self.close_button.clicked.connect(self.accept); layout.addWidget(self.close_button, alignment=Qt.AlignmentFlag.AlignRight)
        self.setStyleSheet(application_stylesheet() + f"""
            QDialog#courseDialog {{ background: {PALETTE.background}; }}
            QLabel#courseHeading {{ color: {PALETTE.text}; font-size: 22px; font-weight: 700; }}
            QLabel#courseCode {{ color: {PALETTE.text_secondary}; font-size: 13px; }}
            QLabel#courseStatus {{ color: {PALETTE.text_secondary}; background: {PALETTE.surface_alt}; border-radius: 9px; padding: 5px 8px; font-size: 12px; }}
            QTreeWidget#moduleTree {{ background: {PALETTE.surface}; alternate-background-color: {PALETTE.surface_alt}; border: 1px solid {PALETTE.border}; border-radius: 14px; padding: 4px; outline: none; }}
            QTreeWidget#moduleTree::item {{ min-height: 24px; border-radius: 7px; padding: 2px; color: {PALETTE.text}; font-size: 14px; }}
            QTreeWidget#moduleTree::item:hover {{ background: {PALETTE.surface_alt}; }}
            QTreeWidget#moduleTree::item:selected {{ background: {PALETTE.selection}; color: {PALETTE.selection_text}; }}
            QHeaderView::section {{ background: {PALETTE.surface_alt}; color: {PALETTE.text_secondary}; border: none; border-bottom: 1px solid {PALETTE.border}; padding: 5px; font-weight: 600; }}
            QSplitter#softSplitter::handle {{ background: {PALETTE.border}; width: 4px; margin: 6px 3px; border-radius: 2px; }}
            QPushButton#dialogCloseButton {{ background: {PALETTE.surface_alt}; color: {PALETTE.text_secondary}; border: 1px solid {PALETTE.border}; padding: 6px 16px; }}
            QPushButton#dialogCloseButton:hover {{ background: {PALETTE.selection}; }}
            QPushButton#previewButton {{ background: {PALETTE.surface_alt}; color: {PALETTE.text_secondary}; border: 1px solid {PALETTE.border}; padding: 5px 10px; }}
            QPushButton#previewButton:hover {{ background: {PALETTE.selection}; }}
        """)
        for reader in (self.overview, self.announcement_body, self.pages_viewer, self.module_viewer):
            reader.setFont(theme_font(16))
            reader.document().setDefaultFont(theme_font(16))
            reader.setStyleSheet(reader_stylesheet(font_size=16))
        cached = session_cache.get(f"course_content:{self.course.id}")
        if cached is not None:
            self._populate(cached)
            self.status.setText(self.status.text() + " Checking Canvas for updates…")
        self._load()

    def _toggle_module_tree(self):
        visible = self.module_tree.isVisible()
        self.module_tree.setVisible(not visible)
        self.expand_preview_button.setText("Show Course List" if visible else "Expand Preview")
        if not visible:
            self.module_splitter.setSizes([300, 1050])

    def _fetch(self):
        token = self.store.get_canvas_token()
        if token is None: raise CanvasClientError("No Canvas token is stored.")
        with CanvasClient(token) as client:
            assignments = client.get_assignments(self.course.id, self.course)
            unavailable = []
            def optional(label, operation):
                try:
                    return operation()
                except CanvasClientError:
                    unavailable.append(label)
                    return []
            announcement_key = f"course:{self.course.id}"
            try:
                announcements = [
                    self._cacheable_announcement(item)
                    for item in client.get_announcements(self.course.id)
                ]
                try:
                    self._cache.set("announcements", announcements, key=announcement_key)
                except SensitiveDataError:
                    # Announcement bodies are arbitrary lecturer content. If a
                    # body resembles a credential, show it for this session but
                    # fail closed instead of persisting it.
                    pass
            except CanvasClientError:
                announcements = self._cache.get(
                    "announcements",
                    key=announcement_key,
                    default=[],
                    allow_expired=True,
                )
                unavailable.append(
                    "Announcements (showing cached copy)"
                    if announcements
                    else "Announcements"
                )
            pages = optional("Pages", lambda: client.get_pages(self.course.id))
            modules = optional("Modules", lambda: client.get_modules(self.course.id))
            module_data = []
            for module in modules:
                items = optional(f"Module: {module.get('name') or 'Untitled'}", lambda module=module: client.get_module_items(self.course.id, module["id"])) if module.get("id") is not None else []
                module_data.append((module, items))
            result = assignments, announcements, pages, module_data, unavailable
            session_cache.set(f"course_content:{self.course.id}", result)
            return result

    @staticmethod
    def _cacheable_announcement(item):
        """Keep useful announcement text without persisting embedded URLs."""

        author = item.get("author") or {}
        return {
            "id": item.get("id"),
            "title": item.get("title"),
            "message": safe_canvas_html(item.get("message"), "No announcement content."),
            "posted_at": item.get("posted_at"),
            "author": {"display_name": author.get("display_name")},
        }

    def _load(self):
        self._thread = QThread(self); self._worker = CanvasWorker(self._fetch); self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run); self._worker.succeeded.connect(self._populate); self._worker.failed.connect(self.status.setText)
        self._worker.finished.connect(self._thread.quit); self._worker.finished.connect(self._worker.deleteLater); self._thread.finished.connect(self._thread.deleteLater); self._thread.finished.connect(lambda: self.close_button.setEnabled(True)); self._thread.start()

    def closeEvent(self, event):
        if is_thread_running(self._thread) or is_thread_running(self._content_thread):
            self.status.setText("Waiting for the current Canvas request to finish…")
            event.ignore()
            return
        super().closeEvent(event)

    def _populate(self, result):
        assignments, self._announcements, self._pages, modules, unavailable = result; deadlines = DeadlineService().deadlines(assignments)
        session_cache.set(f"announcements:{self.course.id}", self._announcements)
        session_cache.set(f"modules:{self.course.id}", [module for module, _items in modules])
        for module, items in modules:
            if module.get("id") is not None:
                session_cache.set(f"module_items:{self.course.id}:{module['id']}", items)
        self.assignment_list.clear()
        for d in deadlines:
            status = d.assignment.submission_label
            item = self.assignment_list.add_card(d.assignment.title, format_due(d), d.countdown, "" if d.assignment.is_submitted else d.category.upper(), PALETTE.success if d.assignment.is_submitted else PALETTE.accent, status=status, status_color=PALETTE.success if d.assignment.is_submitted else PALETTE.warning)
            item.setData(Qt.ItemDataRole.UserRole, d)
        self.announcement_list.clear()
        for announcement in self._announcements:
            author = (announcement.get("author") or {}).get("display_name") or "Unknown author"; date = announcement.get("posted_at") or "Date unavailable"
            self.announcement_list.addItem(f"{announcement.get('title') or 'Untitled announcement'}\n{author} · {date}")
        self.pages_list.clear()
        for page in self._pages:
            self.pages_list.addItem(page.get("title") or page.get("url") or "Untitled page")
        self.module_tree.clear()
        for module, items in modules:
            state = "Unpublished" if module.get("published") is False else "Locked" if module.get("state") == "locked" else "Available"
            module_title = module.get("name") or "Untitled module"
            parent = QTreeWidgetItem([module_title, "Module", state]); parent.setToolTip(0, module_title)
            for item in items:
                item_state = "Unpublished" if item.get("published") is False else "Locked" if item.get("locked_for_user") else "Available"
                item_title = item.get("title") or "Missing content"
                child = QTreeWidgetItem([item_title, item.get("type") or "Unknown", item_state]); child.setToolTip(0, item_title); child.setData(0, Qt.ItemDataRole.UserRole, item); parent.addChild(child)
            self.module_tree.addTopLevelItem(parent)
        summary = f"Loaded {len(assignments)} assignments, {len(self._announcements)} announcements, {len(self._pages)} pages and {len(modules)} modules."
        if unavailable:
            summary += " Unavailable for this course: " + ", ".join(unavailable) + "."
        self.status.setText(summary)
        if "Pages" in unavailable:
            self.pages_viewer.setHtml("<h3>Pages unavailable</h3><p>This Canvas course does not expose the Pages API. Other course content remains available.</p>")

    def _open_assignment(self, item):
        deadline = item.data(Qt.ItemDataRole.UserRole)
        if deadline is not None:
            AssignmentDetailDialog(deadline, self, credential_store=self.store).exec()

    def _show_announcement(self, row):
        if 0 <= row < len(self._announcements):
            item = self._announcements[row]; self.announcement_body.setHtml(safe_canvas_html(item.get("message"), "No announcement content."))

    def _show_page(self, row):
        if not (0 <= row < len(getattr(self, "_pages", []))): return
        page = self._pages[row]
        self._load_content({"type": "Page", "title": page.get("title"), "page_url": page.get("url")}, self.pages_viewer)

    def _fetch_module_content(self, item):
        item_type = item.get("type") or "Unknown"; content_id = item.get("content_id")
        token = self.store.get_canvas_token()
        if token is None: raise CanvasClientError("No Canvas token is stored.")
        with CanvasClient(token) as client:
            if item_type == "Page" and (item.get("page_url") or item.get("url")):
                data = client.get_page(self.course.id, item.get("page_url") or item["url"])
                return item_type, self._attach_images(client, data, "body")
            if item_type == "Assignment" and content_id is not None:
                data = client.get_assignment(self.course.id, content_id)
                payload = data.model_copy(); payload.__dict__["_images"] = self._download_images(client, data.description)
                return item_type, payload
            if item_type == "Discussion" and content_id is not None:
                return item_type, self._attach_images(client, client.get_discussion_topic(self.course.id, content_id), "message")
            if item_type == "Quiz" and content_id is not None:
                return item_type, self._attach_images(client, client.get_quiz(self.course.id, content_id), "description")
            if item_type == "File" and content_id is not None:
                metadata, content = client.get_canvas_file_content(content_id)
                metadata = dict(metadata); metadata["_file_bytes"] = content
                return item_type, metadata
            return item_type, item

    def _download_images(self, client, html):
        images = []
        for slot, source in canvas_image_slots(html, CanvasClient.BASE_URL):
            try: images.append((slot, client.get_canvas_image(source)))
            except CanvasClientError: continue
        return images

    def _attach_images(self, client, data, field):
        data = dict(data); data["_images"] = self._download_images(client, data.get(field)); return data

    def _show_module_item(self, tree_item, _column):
        item = tree_item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(item, dict): return
        self._load_content(item, self.module_viewer)

    def _load_content(self, item, target_viewer):
        if is_thread_running(self._content_thread): return
        self.close_button.setEnabled(False)
        if target_viewer is self.module_viewer: self.module_stack.setCurrentWidget(self.module_viewer)
        target_viewer.setHtml("<p>Loading Canvas content…</p>")
        thread = QThread(self); self._content_thread = thread; self._content_worker = CanvasWorker(lambda: self._fetch_module_content(item)); self._content_worker.moveToThread(thread)
        self._content_target = target_viewer
        thread.started.connect(self._content_worker.run); self._content_worker.succeeded.connect(self._content_succeeded)
        self._content_worker.failed.connect(self._content_failed)
        self._content_worker.finished.connect(thread.quit); self._content_worker.finished.connect(self._content_worker.deleteLater)
        thread.finished.connect(lambda: self._content_finished(thread)); thread.finished.connect(thread.deleteLater); thread.start()

    def _content_finished(self, thread):
        if self._content_thread is thread:
            self._content_thread = None
            self._content_worker = None
        self.close_button.setEnabled(True)

    @Slot(object)
    def _content_succeeded(self, result):
        self._last_render_thread = QThread.currentThread()
        if self._content_target is not None:
            self._render_content(result, self._content_target)

    @Slot(str)
    def _content_failed(self, message):
        if self._content_target is not None:
            self._content_target.setHtml(f"<p>{escape(message)}</p>")

    def _render_module_content(self, result):
        self._render_content(result, self.module_viewer)

    def _render_content(self, result, target_viewer):
        item_type, data = result
        if item_type == "Page":
            html = f"<h2>{escape(data.get('title') or 'Untitled page')}</h2>{safe_canvas_html(data.get('body'), 'No page content.', preserve_image_slots=True)}"
        elif item_type == "Assignment":
            due = DeadlineService().build(data); html = f"<h2>{escape(data.title)}</h2><p><b>Due:</b> {escape(format_due(due))}</p>{safe_canvas_html(data.description, 'No assignment description.', preserve_image_slots=True)}"
        elif item_type == "Discussion":
            html = f"<h2>{escape(data.get('title') or 'Untitled discussion')}</h2>{safe_canvas_html(data.get('message'), 'No discussion content.', preserve_image_slots=True)}"
        elif item_type == "Quiz":
            html = f"<h2>{escape(data.get('title') or 'Untitled quiz')}</h2>{safe_canvas_html(data.get('description'), 'No quiz description.', preserve_image_slots=True)}"
        elif item_type == "File":
            self._render_file(data, target_viewer)
            return
        elif item_type == "ExternalUrl":
            html = f"<h2>{escape(data.get('title') or 'External resource')}</h2><p>This is an external resource. For safety it is not opened automatically.</p><p>{escape(data.get('external_url') or 'URL unavailable')}</p>"
        else:
            html = f"<h2>{escape(data.get('title') or item_type)}</h2><p>No additional readable content is available for this item type.</p>"
        target_viewer.setHtml(html)
        images = data.__dict__.get("_images", []) if item_type == "Assignment" else data.get("_images", [])
        for slot, raw in images:
            image = QImage.fromData(raw)
            if image.isNull(): continue
            available_width = max(500, target_viewer.viewport().width() - 50)
            if image.width() > available_width or image.height() > 700:
                image = image.scaled(available_width, 700, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            cursor = target_viewer.document().find(f"CANVAS_IMAGE_SLOT_{slot:04d}")
            if not cursor.isNull(): cursor.insertImage(image)
        marker = QRegularExpression(r"CANVAS_IMAGE_SLOT_\d{4}")
        cursor = target_viewer.document().find(marker)
        while not cursor.isNull():
            cursor.insertText("[Image unavailable]")
            cursor = target_viewer.document().find(marker, cursor)

    def _render_file(self, data, target_viewer):
        raw = data.get("_file_bytes") or b""; kind = preview_kind(data)
        name = escape(data.get("display_name") or data.get("filename") or "Unnamed file")
        if kind == "pdf" and target_viewer is self.module_viewer:
            self.pdf_view.setDocument(None)
            if self.pdf_document is not None:
                self.pdf_document.close(); self.pdf_document.deleteLater()
            self.pdf_bytes = QByteArray(raw)
            self.pdf_document = QPdfDocument(self.pdf_view)
            self.pdf_buffer = QBuffer(self.pdf_bytes, self.pdf_document); self.pdf_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            self.pdf_view.setDocument(self.pdf_document); self.pdf_document.load(self.pdf_buffer); self.module_stack.setCurrentWidget(self.pdf_view)
            return
        self.module_stack.setCurrentWidget(self.module_viewer)
        if kind == "image":
            image = QImage.fromData(raw)
            if image.isNull():
                target_viewer.setHtml(f"<h2>{name}</h2><p>This image format could not be decoded.</p>"); return
            if image.width() > 720 or image.height() > 520:
                image = image.scaled(720, 520, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            target_viewer.clear(); cursor = target_viewer.textCursor(); cursor.insertHtml(f"<h2>{name}</h2>"); cursor.insertBlock(); cursor.insertImage(image); target_viewer.setTextCursor(cursor); return
        if kind in {"docx", "pptx", "xlsx", "text"}:
            text = extract_file_text(kind, raw)[:500_000]
            target_viewer.setHtml(f"<h2>{name}</h2><pre>{escape(text)}</pre>"); return
        content_type = escape(data.get("content-type") or "Unknown"); size = data.get("size")
        target_viewer.setHtml(f"<h2>{name}</h2><p><b>Type:</b> {content_type}</p><p><b>Size:</b> {size if size is not None else 'Unknown'} bytes</p><p>This file type does not have a safe in-app preview yet.</p>")
