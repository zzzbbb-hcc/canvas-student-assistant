from PySide6.QtCore import QThread
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.models.course import Course
from app.services.canvas_client import CanvasClient, CanvasClientError
from app.services.credential_store import CredentialStore
from app.storage.cache import Cache
from app.ui.canvas_worker import CanvasWorker, is_thread_running
from app.ui.cards import CardList
from app.ui.theme import PALETTE as P
from app.ui.course_detail import CourseDetailDialog
from app.storage.session_cache import session_cache


class CoursesPage(QWidget):
    def __init__(self, credential_store=None, *, cache=None):
        super().__init__(); self._credential_store = credential_store or CredentialStore(); self._thread = None; self._worker = None; self._courses = []; self._cache = cache or Cache()
        layout = QVBoxLayout(self); layout.setContentsMargins(20, 16, 20, 14); layout.setSpacing(8)
        title = QLabel("My Courses"); title.setObjectName("pageTitle"); title.setToolTip("Your current Canvas enrolments · Double-click a course to explore its content")
        actions = QHBoxLayout(); self.refresh_button = QPushButton("Refresh Courses"); self.open_button = QPushButton("Open Selected Course"); self.open_button.setObjectName("secondaryButton"); self.open_button.setEnabled(False)
        actions.setSpacing(8); actions.addWidget(title); actions.addStretch(); actions.addWidget(self.refresh_button); actions.addWidget(self.open_button)
        self.status_label = QLabel("Refresh to load your active courses."); self.status_label.setObjectName("statusText")
        self.course_list = CardList(compact=True); self.course_list.currentRowChanged.connect(lambda row: self.open_button.setEnabled(row >= 0)); self.course_list.itemDoubleClicked.connect(lambda _: self.open_selected_course())
        self.refresh_button.clicked.connect(lambda: self.refresh_courses(refresh=True)); self.open_button.clicked.connect(self.open_selected_course)
        layout.addLayout(actions); layout.addWidget(self.status_label); layout.addWidget(self.course_list, 1)

    def _load_courses(self, refresh=False):
        def fetch():
            token = self._credential_store.get_canvas_token()
            if token is None: raise CanvasClientError("No Canvas token is stored.")
            with CanvasClient(token) as client: return client.get_courses()
        raw = self._cache.get_or_fetch("courses", fetch, refresh=refresh)
        return [item if isinstance(item, Course) else Course.model_validate(item) for item in raw]

    def refresh_courses(self, refresh=True):
        if is_thread_running(self._thread): return
        cached = session_cache.get("courses")
        if cached is None and not self._courses:
            raw = self._cache.get("courses", default=[], allow_expired=True)
            cached = [item if isinstance(item, Course) else Course.model_validate(item) for item in raw]
        if cached is not None and not self._courses:
            self._show_courses(cached)
        self.refresh_button.setEnabled(False); self.status_label.setText("Showing current view · checking Canvas for updates…" if self._courses else "Loading courses…")
        self._thread = QThread(self); self._worker = CanvasWorker(lambda: self._load_courses(refresh)); self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run); self._worker.succeeded.connect(self._show_courses); self._worker.failed.connect(self.status_label.setText)
        self._worker.finished.connect(self._thread.quit); self._worker.finished.connect(self._worker.deleteLater); self._thread.finished.connect(self._thread.deleteLater); self._thread.finished.connect(lambda: self.refresh_button.setEnabled(True)); self._thread.start()

    def _show_courses(self, courses):
        self._courses = list(courses); self.course_list.clear()
        session_cache.set("courses", self._courses)
        colors = [P.accent, P.success, P.warning, P.danger]
        for index, course in enumerate(self._courses):
            self.course_list.add_card(course.name, course.course_code or "Course code unavailable", accent=colors[index % len(colors)])
        self.status_label.setText(f"{len(self._courses)} active course(s) · Double-click to explore course content")

    def open_selected_course(self):
        row = self.course_list.currentRow()
        if 0 <= row < len(self._courses):
            CourseDetailDialog(
                self._courses[row],
                self._credential_store,
                self,
                cache=self._cache,
            ).exec()
