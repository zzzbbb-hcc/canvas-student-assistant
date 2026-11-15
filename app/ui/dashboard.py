from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from app.services.assignment_loader import cached_assignments, load_assignments
from app.services.credential_store import CredentialStore
from app.services.deadline_service import DeadlineService
from app.ui.canvas_worker import CanvasWorker, is_thread_running
from app.ui.cards import CardList
from app.ui.doodle_icons import navigation_icon
from app.ui.theme import PALETTE as P, warning_stylesheet
from app.ui.deadline_widgets import AssignmentDetailDialog, format_due
from app.storage.session_cache import session_cache
from app.storage.cache import Cache


class _StatCardButton(QPushButton):
    """Include the stacked labels when Qt calculates a button's size."""

    def sizeHint(self):
        hint = super().sizeHint()
        return hint.expandedTo(self.layout().sizeHint()) if self.layout() else hint

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return hint.expandedTo(self.layout().minimumSize()) if self.layout() else hint


class DashboardPage(QWidget):
    deadline_category_requested = Signal(str)
    _stat_periods = {
        "Due Today": "today",
        "Due Tomorrow": "tomorrow",
        "Due This Week": "week",
        "Overdue": "overdue",
    }

    def __init__(self, credential_store=None, *, cache=None, notification_service=None):
        super().__init__(); self._store = credential_store or CredentialStore(); self._thread = None; self._worker = None; self._cache = cache or Cache(); self._notification_service = notification_service
        layout = QVBoxLayout(self); layout.setContentsMargins(20, 16, 20, 14); layout.setSpacing(8)
        title = QLabel("Dashboard"); title.setObjectName("pageTitle"); title.setToolTip("A calm view of what needs your attention")
        self.refresh_button = QPushButton("Refresh Dashboard"); self.refresh_button.clicked.connect(lambda: self.refresh(refresh=True))
        header = QHBoxLayout(); header.setSpacing(8); header.addWidget(title); header.addStretch(); header.addWidget(self.refresh_button); layout.addLayout(header)
        grid = QGridLayout(); grid.setSpacing(8); self.stats = {}; self.stat_buttons = {}
        for col, (name, color) in enumerate([
            ("Due Today", P.warning),
            ("Due Tomorrow", P.accent),
            ("Due This Week", P.success),
            ("Overdue", P.danger),
        ]):
            card = _StatCardButton(); card.setObjectName("statCard")
            card.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card.setAccessibleName(name)
            card.setToolTip(f"{name}: view unsubmitted assignments and quizzes in Deadlines.")
            card.clicked.connect(lambda _checked=False, period=self._stat_periods[name]: self.deadline_category_requested.emit(period))
            box = QVBoxLayout(card); box.setContentsMargins(12, 7, 12, 7); box.setSpacing(2)
            number = QLabel("0"); number.setObjectName("statNumber"); number.setStyleSheet(f"color:{color};")
            label = QLabel(name); label.setObjectName("statLabel"); label.setWordWrap(True)
            doodle = QLabel(); doodle.setPixmap(navigation_icon("deadlines" if name != "Due This Week" else "assignments").pixmap(20, 20)); doodle.setFixedSize(20, 20)
            number_row = QHBoxLayout(); number_row.setContentsMargins(0, 0, 0, 0); number_row.addWidget(number); number_row.addStretch(); number_row.addWidget(doodle)
            box.addLayout(number_row); box.addWidget(label)
            for child in (number, label, doodle):
                child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            grid.addWidget(card, 0, col)
            self.stats[name] = number; self.stat_buttons[name] = card
        layout.addLayout(grid)
        self.quiz_coverage_label = QLabel(); self.quiz_coverage_label.setObjectName("quizCoverageWarning")
        self.quiz_coverage_label.setTextFormat(Qt.TextFormat.PlainText); self.quiz_coverage_label.setWordWrap(True)
        self.quiz_coverage_label.setStyleSheet(warning_stylesheet())
        self.quiz_coverage_label.setVisible(False); layout.addWidget(self.quiz_coverage_label)
        self.next_deadline_card = QFrame(); self.next_deadline_card.setObjectName("contentCard")
        next_layout = QVBoxLayout(self.next_deadline_card); next_layout.setContentsMargins(12, 7, 12, 7); next_layout.setSpacing(2)
        next_label = QLabel("Next deadline"); next_label.setObjectName("cardMeta")
        self.next_deadline_title = QLabel("No upcoming unsubmitted deadlines"); self.next_deadline_title.setObjectName("cardTitle"); self.next_deadline_title.setWordWrap(True)
        self.next_deadline_detail = QLabel("Canvas due_at will appear here when available."); self.next_deadline_detail.setObjectName("cardSubtitle"); self.next_deadline_detail.setWordWrap(True)
        self.next_deadline_title.setStyleSheet("font-size: 15px; font-weight: 600;"); self.next_deadline_detail.setStyleSheet("font-size: 13px;")
        next_layout.addWidget(next_label); next_layout.addWidget(self.next_deadline_title); next_layout.addWidget(self.next_deadline_detail)
        layout.addWidget(self.next_deadline_card)
        section = QLabel("Upcoming deadlines"); section.setObjectName("sectionTitle"); layout.addWidget(section)
        self.upcoming_list = CardList(compact=True); self.upcoming_list.itemClicked.connect(self._open_assignment); layout.addWidget(self.upcoming_list, 1); self.status_label = QLabel("Refresh to load Canvas deadlines."); self.status_label.setObjectName("statusText"); layout.addWidget(self.status_label)

    def refresh(self, refresh=True):
        if is_thread_running(self._thread): return
        cached = session_cache.get("assignments")
        if cached is None:
            cached = cached_assignments(self._cache)
        if cached is not None:
            self._show(cached)
        self.refresh_button.setEnabled(False); self.status_label.setText("Showing current view · checking Canvas for updates…" if cached is not None else "Loading deadlines…")
        self._thread = QThread(self); self._worker = CanvasWorker(lambda: load_assignments(self._store, refresh=refresh, cache=self._cache)); self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run); self._worker.succeeded.connect(self._show); self._worker.failed.connect(self.status_label.setText)
        self._worker.finished.connect(self._thread.quit); self._worker.finished.connect(self._worker.deleteLater); self._thread.finished.connect(self._thread.deleteLater); self._thread.finished.connect(lambda: self.refresh_button.setEnabled(True)); self._thread.start()

    def _show(self, assignments):
        service = DeadlineService(); deadlines = service.deadlines(assignments); upcoming = service.upcoming_unsubmitted(assignments, 5)
        warnings = session_cache.get("quiz_coverage_warnings", [])
        warning_count = len(warnings) if isinstance(warnings, list) else 0
        self.quiz_coverage_label.setText("部分测验信息未能完整读取，截止时间列表可能不完整。请到 Quizzes 页面查看读取提示。" if warning_count else "")
        self.quiz_coverage_label.setVisible(bool(warning_count))
        counts = {name: sum(service.matches_period(deadline, period) for deadline in deadlines)
                  for name, period in self._stat_periods.items()}
        for name, count in counts.items(): self.stats[name].setText(str(count))
        if upcoming:
            next_deadline = upcoming[0]
            self.next_deadline_title.setText(next_deadline.assignment.title)
            course = next_deadline.assignment.course_code or next_deadline.assignment.course_name
            self.next_deadline_detail.setText(f"{course}  ·  {format_due(next_deadline)}  ·  {next_deadline.countdown}")
        else:
            self.next_deadline_title.setText("No upcoming unsubmitted deadlines")
            self.next_deadline_detail.setText("Canvas due_at will appear here when available.")
        self.upcoming_list.clear()
        for d in upcoming:
            meta = f"{format_due(d)}  ·  {d.countdown}"
            if d.assignment.is_quiz:
                meta = f"Quiz · {meta}"
            item = self.upcoming_list.add_card(d.assignment.title, d.assignment.course_code or d.assignment.course_name, meta, "UPCOMING", P.accent)
            item.setData(Qt.ItemDataRole.UserRole, d)
        self.status_label.setText(f"Showing {len(upcoming)} upcoming unsubmitted assignment / quiz item(s)")
        if self._notification_service is not None:
            try:
                self._notification_service.check_assignments(assignments)
            except Exception:
                pass

    def _open_assignment(self, item):
        deadline = item.data(Qt.ItemDataRole.UserRole)
        if deadline is not None:
            AssignmentDetailDialog(deadline, self, credential_store=self._store).exec()
