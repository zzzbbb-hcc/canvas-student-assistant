from PySide6.QtCore import QSize, QTimer, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QListWidget, QMainWindow, QStackedWidget, QVBoxLayout, QWidget

from app.ui.ai_chat import AIChatPage
from app.ui.app_icon import application_icon
from app.ui.announcements import AnnouncementsPage
from app.ui.assignments import AssignmentsPage
from app.ui.courses import CoursesPage
from app.ui.dashboard import DashboardPage
from app.ui.deadline_center import DeadlineCenterPage
from app.ui.quizzes import QuizzesPage
from app.ui.settings_page import SettingsPage
from app.ui.doodle_icons import navigation_icon
from app.ui.theme import application_stylesheet, theme_font, theme_palette
from app.services.credential_store import CredentialStore
from app.services.notification_service import NotificationService
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Student Assistant")
        self.setWindowIcon(application_icon())
        self.setFont(theme_font())
        self.setPalette(theme_palette(self.palette()))
        self.resize(1100, 720)
        self._credential_store = CredentialStore()
        self._database = SQLiteDatabase()
        self._cache = Cache(self._database)
        self._notification_service = NotificationService(self._database)
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setFixedWidth(196)
        self.navigation.setIconSize(QSize(20, 20))
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pages = QStackedWidget()
        for label, page in [
            ("Dashboard", DashboardPage(self._credential_store, cache=self._cache, notification_service=self._notification_service)),
            ("Courses", CoursesPage(self._credential_store, cache=self._cache)),
            ("Assignments", AssignmentsPage(self._credential_store, cache=self._cache, notification_service=self._notification_service)),
            ("Quizzes", QuizzesPage(self._credential_store, cache=self._cache, notification_service=self._notification_service)),
            ("Deadlines", DeadlineCenterPage(self._credential_store, cache=self._cache, notification_service=self._notification_service)),
            ("公告", AnnouncementsPage(self._credential_store, cache=self._cache)),
            ("AI Assistant", AIChatPage(database=self._database, credential_store=self._credential_store)),
            ("Settings", SettingsPage(
                self._credential_store,
                cache=self._cache,
                notification_service=self._notification_service,
            )),
        ]:
            self.navigation.addItem(label)
            icon_key = {"Dashboard": "dashboard", "Courses": "courses", "Assignments": "assignments",
                        "Quizzes": "quizzes", "Deadlines": "deadlines", "公告": "announcements",
                        "AI Assistant": "assistant", "Settings": "settings"}[label]
            self.navigation.item(self.navigation.count() - 1).setIcon(navigation_icon(icon_key))
            self.pages.addWidget(page)
        self._dashboard_page = next(self.pages.widget(index) for index in range(self.pages.count())
                                    if isinstance(self.pages.widget(index), DashboardPage))
        self._deadline_page = next(self.pages.widget(index) for index in range(self.pages.count())
                                   if isinstance(self.pages.widget(index), DeadlineCenterPage))
        self._dashboard_page.deadline_category_requested.connect(self._open_dashboard_deadlines)
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        self.navigation.currentRowChanged.connect(self._refresh_current_page)
        self._initial_refresh_done = False
        sidebar = QFrame(); sidebar.setObjectName("studySidebar"); sidebar.setFixedWidth(196)
        sidebar_layout = QVBoxLayout(sidebar); sidebar_layout.setContentsMargins(0, 0, 0, 0); sidebar_layout.setSpacing(0)
        brand = QFrame(); brand_layout = QHBoxLayout(brand); brand_layout.setContentsMargins(18, 19, 10, 12); brand_layout.setSpacing(8)
        brand_icon = QLabel(); brand_icon.setPixmap(application_icon().pixmap(32, 32)); brand_icon.setFixedSize(32, 32)
        brand_layout.addWidget(brand_icon)
        brand_text = QVBoxLayout(); brand_text.setSpacing(1)
        brand_title = QLabel("学习小屋"); brand_title.setObjectName("brandTitle")
        brand_subtitle = QLabel("STUDENT ASSISTANT"); brand_subtitle.setObjectName("brandSubtitle")
        brand_text.addWidget(brand_title); brand_text.addWidget(brand_subtitle); brand_layout.addLayout(brand_text)
        sidebar_layout.addWidget(brand); sidebar_layout.addWidget(self.navigation, 1)
        sidebar_note = QLabel("今天也在慢慢进步"); sidebar_note.setObjectName("sidebarNote")
        sidebar_layout.addWidget(sidebar_note)
        layout.addWidget(sidebar)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(container)
        self._reminder_timer = QTimer(self)
        self._reminder_timer.setInterval(15 * 60 * 1000)
        self._reminder_timer.timeout.connect(self._check_cached_reminders)
        self._reminder_timer.start()
        self.setStyleSheet(application_stylesheet())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._initial_refresh_done:
            self._initial_refresh_done = True
            self._refresh_current_page(self.navigation.currentRow())

    def _refresh_current_page(self, index: int) -> None:
        if not (0 <= index < self.pages.count()):
            return
        page = self.pages.widget(index)
        refresh = getattr(page, "refresh_courses", None) or getattr(page, "refresh", None)
        if callable(refresh):
            refresh()

    def _check_cached_reminders(self) -> None:
        assignments = session_cache.get("assignments", [])
        if not assignments:
            return
        try:
            self._notification_service.check_assignments(assignments)
        except Exception:
            # A disabled/unavailable Windows notification backend must not
            # interrupt the running application.
            return

    def _open_dashboard_deadlines(self, period: str) -> None:
        assignments = session_cache.get("assignments")
        if assignments is not None:
            self._deadline_page._show(assignments)
        self._deadline_page.set_period_filter(period)
        self.navigation.setCurrentRow(self.pages.indexOf(self._deadline_page))

    def closeEvent(self, event) -> None:
        running = []
        for index in range(self.pages.count()):
            page = self.pages.widget(index)
            for name in ("_thread", "_connection_thread", "_preview_thread"):
                thread = getattr(page, name, None)
                try:
                    if thread is not None and (thread.isRunning() or name in {"_thread", "_preview_thread", "_connection_thread"}):
                        running.append(thread)
                except RuntimeError:
                    continue
        if running:
            self.setWindowTitle("Student Assistant — finishing current Canvas request…")
            QTimer.singleShot(200, self.close)
            event.ignore()
            return
        super().closeEvent(event)
