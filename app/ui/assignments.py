from collections import defaultdict

from PySide6.QtCore import QSignalBlocker, QThread, Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget
from app.services.assignment_loader import cached_assignments, load_assignments
from app.services.credential_store import CredentialStore
from app.services.deadline_service import DeadlineService
from app.ui.canvas_worker import CanvasWorker, is_thread_running
from app.ui.cards import CardList
from app.ui.theme import PALETTE as P, warning_stylesheet
from app.ui.deadline_widgets import AssignmentDetailDialog, format_due, format_local_time
from app.storage.session_cache import session_cache
from app.storage.cache import Cache

class AssignmentsPage(QWidget):
    page_name = "Assignments"
    refresh_text = "Refresh Assignments"
    default_submission_filter = "all"
    group_by_course = True
    compact_layout = True
    page_subtitle = "Grouped by course and submission status · Overdue last within each group · Exact local due times"
    item_label = "assignment(s)"
    empty_title = "No assignments in this view"
    instruction_text = "Click an assignment to read its instructions and download materials."

    def __init__(self, credential_store=None, *, cache=None, notification_service=None):
        super().__init__()
        self._store = credential_store or CredentialStore()
        self._deadlines = []
        self._deadline_service = DeadlineService()
        self._visible_deadlines = []
        self._thread = None
        self._worker = None
        self._cache = cache or Cache()
        self._notification_service = notification_service
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 14) if self.compact_layout else layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(8 if self.compact_layout else 14)
        title = QLabel(self.page_name); title.setObjectName("pageTitle")
        subtitle = QLabel(self.page_subtitle, self)
        subtitle.setObjectName("pageSubtitle"); subtitle.setWordWrap(True)
        actions = QHBoxLayout()
        self.header_actions = actions
        self.refresh_button = QPushButton(self.refresh_text)
        if self.compact_layout:
            title.setStyleSheet(f"font-size: 24px; font-weight: 600; color: {P.text};")
            title.setToolTip(self.page_subtitle)
            actions.addWidget(title); actions.addStretch(); actions.addWidget(self.refresh_button)
            subtitle.setVisible(False)
            self.setStyleSheet("QPushButton { padding: 5px 10px; } QComboBox { padding: 5px 8px; } QLabel#statusText { padding: 0; font-size: 12px; }")
        else:
            actions.addWidget(self.refresh_button); actions.addStretch()
        filters = QHBoxLayout()
        filters.addWidget(QLabel("Course"))
        self.course_filter = QComboBox(); self.course_filter.setObjectName("courseFilter")
        self.course_filter.addItem("All courses", None)
        self.course_filter.setMinimumWidth(200)
        filters.addWidget(self.course_filter, 1)
        filters.addWidget(QLabel("Submission"))
        self.submission_filter = QComboBox(); self.submission_filter.setObjectName("submissionFilter")
        for label, value in [
            ("All statuses", "all"),
            ("To do · Not submitted / Unknown", "pending"),
            ("Submitted", "submitted"),
            ("Status unavailable", "unknown"),
        ]:
            self.submission_filter.addItem(label, value)
        self.submission_filter.setCurrentIndex(self.submission_filter.findData(self.default_submission_filter))
        filters.addWidget(self.submission_filter)
        self.summary_label = QLabel("No assignments loaded."); self.summary_label.setObjectName("statusText")
        self.summary_label.setWordWrap(True)
        self.status_label = QLabel("Refresh to load assignments."); self.status_label.setObjectName("statusText")
        self.status_label.setWordWrap(True)
        self.quiz_coverage_label = QLabel(); self.quiz_coverage_label.setObjectName("quizCoverageWarning")
        self.quiz_coverage_label.setTextFormat(Qt.TextFormat.PlainText); self.quiz_coverage_label.setWordWrap(True)
        self.quiz_coverage_label.setStyleSheet(warning_stylesheet(compact=self.compact_layout))
        self.quiz_coverage_label.setVisible(False)
        self.quiz_coverage_button = QPushButton("查看读取提示 / Coverage details")
        self.quiz_coverage_button.setObjectName("secondaryButton"); self.quiz_coverage_button.setVisible(False)
        self.quiz_coverage_button.clicked.connect(self._show_quiz_coverage)
        self._quiz_coverage_warnings = []
        self.assignment_list = CardList(compact=self.compact_layout)
        self.assignment_list.itemClicked.connect(self._open_item_detail)
        self.refresh_button.clicked.connect(lambda: self.refresh(refresh=True))
        self.course_filter.currentIndexChanged.connect(self._render_groups)
        self.submission_filter.currentIndexChanged.connect(self._render_groups)
        if self.compact_layout:
            layout.addLayout(actions)
            layout.addLayout(filters)
            summary = QHBoxLayout(); summary.addWidget(self.summary_label, 1); summary.addWidget(self.quiz_coverage_button)
            layout.addLayout(summary)
            layout.addWidget(self.quiz_coverage_label)
        else:
            layout.addWidget(title); layout.addWidget(subtitle); layout.addLayout(actions)
            layout.addLayout(filters); layout.addWidget(self.summary_label)
            layout.addWidget(self.quiz_coverage_label); layout.addWidget(self.quiz_coverage_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.assignment_list, 1); layout.addWidget(self.status_label)

    def refresh(self, refresh=True):
        if is_thread_running(self._thread): return
        cached = session_cache.get("assignments")
        if cached is None:
            cached = cached_assignments(self._cache)
        if cached is not None:
            self._show(cached)
        self.refresh_button.setEnabled(False); self.status_label.setText("Showing current view · checking Canvas for updates…" if self._deadlines else "Loading assignments…")
        self._thread = QThread(self); self._worker = CanvasWorker(lambda: load_assignments(self._store, refresh=refresh, cache=self._cache)); self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run); self._worker.succeeded.connect(self._show); self._worker.failed.connect(self.status_label.setText)
        self._worker.finished.connect(self._thread.quit); self._worker.finished.connect(self._worker.deleteLater); self._thread.finished.connect(self._thread.deleteLater); self._thread.finished.connect(lambda: self.refresh_button.setEnabled(True)); self._thread.start()

    def _show(self, assignments):
        assignments = list(assignments)
        self._deadline_service = DeadlineService()
        self._deadlines = self._deadline_service.deadlines(assignments)
        self._update_course_filter()
        self._render_groups()
        self.status_label.setText(self.instruction_text)
        self._update_quiz_coverage()
        if self._notification_service is not None:
            try:
                self._notification_service.check_assignments(assignments)
            except Exception:
                # Notification availability must never prevent display.
                pass

    def _update_quiz_coverage(self):
        warnings = session_cache.get("quiz_coverage_warnings", [])
        self._quiz_coverage_warnings = [item for item in warnings if isinstance(item, str)] if isinstance(warnings, list) else []
        count = len(self._quiz_coverage_warnings)
        warning_text = f"{count} 项测验来源暂不可读，列表可能不完整；点“读取提示”查看受影响课程。" if self.compact_layout else f"测验读取有 {count} 项提示：部分课程或测验接口无法完整读取，当前列表可能不完整。已有测验和作业仍保留。"
        self.quiz_coverage_label.setText(warning_text if count else "")
        if self.compact_layout:
            self.quiz_coverage_button.setText(f"读取提示 ({count})")
        self.quiz_coverage_label.setVisible(bool(count)); self.quiz_coverage_button.setVisible(bool(count))

    def _show_quiz_coverage(self):
        if not self._quiz_coverage_warnings:
            return
        message = QMessageBox(self)
        message.setWindowTitle("Canvas 测验读取提示")
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setText(f"有 {len(self._quiz_coverage_warnings)} 项测验读取提示。已显示可读取的内容，列表完整性尚无法确认。")
        message.setInformativeText("展开详情可查看受影响的课程和读取来源。App 不会绕过课程访问权限。")
        message.setDetailedText("\n\n".join(self._quiz_coverage_warnings))
        message.setStandardButtons(QMessageBox.StandardButton.Close)
        message.exec()

    def _update_course_filter(self):
        selected = self.course_filter.currentData()
        courses = {d.assignment.course_id: d.assignment for d in self._deadlines}
        with QSignalBlocker(self.course_filter):
            self.course_filter.clear(); self.course_filter.addItem("All courses", None)
            for course_id, assignment in sorted(courses.items(), key=lambda pair: (pair[1].course_code.casefold(), pair[1].course_name.casefold(), pair[0])):
                name = f"{assignment.course_code} · {assignment.course_name}" if assignment.course_code else assignment.course_name
                self.course_filter.addItem(name, course_id)
            index = self.course_filter.findData(selected)
            self.course_filter.setCurrentIndex(max(0, index))

    @staticmethod
    def _submission_group(assignment):
        if assignment.is_submitted:
            return "submitted"
        if assignment.submission_status is None and assignment.submission_missing is not True:
            return "unknown"
        return "unsubmitted"

    def _deadlines_in_view(self):
        return self._deadlines

    def _render_groups(self, *_args):
        course_id = self.course_filter.currentData()
        status_filter = self.submission_filter.currentData()
        groups = defaultdict(lambda: defaultdict(list))
        filtered_deadlines = []
        selected_course = [d for d in self._deadlines_in_view() if course_id is None or d.assignment.course_id == course_id]
        submitted_count = sum(d.assignment.is_submitted for d in selected_course)
        self._visible_deadlines = []
        for deadline in selected_course:
            group = self._submission_group(deadline.assignment)
            if status_filter == "pending" and group == "submitted":
                continue
            if status_filter in {"submitted", "unknown"} and group != status_filter:
                continue
            filtered_deadlines.append(deadline)
            groups[deadline.assignment.course_id][group].append(deadline)
        self.assignment_list.clear()
        if not self.group_by_course:
            # Preserve the service's cross-course order: dated work first,
            # undated work next, overdue last. Filtering must not regroup it.
            for deadline in filtered_deadlines:
                self._add_deadline_card(deadline, show_course=True)
            self._update_summary(selected_course, submitted_count)
            return
        group_names = {"unsubmitted": "Not submitted", "unknown": "Submission status unavailable", "submitted": "Submitted"}
        ordered_courses = sorted(groups, key=lambda key: (
            next(iter(groups[key].values()))[0].assignment.course_code.casefold(),
            next(iter(groups[key].values()))[0].assignment.course_name.casefold(), key,
        ))
        for key in ordered_courses:
            course_groups = groups[key]
            sample = next(iter(course_groups.values()))[0].assignment
            count = sum(len(items) for items in course_groups.values())
            self.assignment_list.add_section(sample.course_name, f"{sample.course_code + ' · ' if sample.course_code else ''}{count} assignment(s)")
            for group in ("unsubmitted", "unknown", "submitted"):
                deadlines = course_groups.get(group, [])
                if not deadlines:
                    continue
                self.assignment_list.add_section(f"{group_names[group]} · {len(deadlines)}", level=1)
                for deadline in deadlines:
                    self._add_deadline_card(deadline)
        self._update_summary(selected_course, submitted_count)

    def _add_deadline_card(self, deadline, *, show_course=False):
        assignment = deadline.assignment
        group = self._submission_group(assignment)
        colors = {"Overdue": P.danger, "Today": P.warning, "Tomorrow": P.accent, "No Deadline": P.neutral, "Past": P.neutral}
        accent = P.success if group == "submitted" else colors.get(deadline.category, P.accent)
        meta = deadline.countdown
        if group == "submitted" and assignment.submission_submitted_at is not None:
            meta = f"Submitted: {format_local_time(assignment.submission_submitted_at)}"
        if assignment.is_quiz:
            meta = f"Quiz · {meta}"
        subtitle = f"Due: {format_due(deadline)}"
        if show_course:
            subtitle = f"{assignment.course_code or assignment.course_name} · {subtitle}"
        # An undated quiz already says 'No deadline' on the course/date line.
        # Keep the full metadata in its tooltip without spending a third row.
        visible_meta = "" if self.compact_layout and deadline.due_local is None and group != "submitted" else meta
        item = self.assignment_list.add_card(
            assignment.title, subtitle, visible_meta,
            "" if group == "submitted" else deadline.category.upper(), accent,
            status=assignment.submission_label,
            status_color=P.success if group == "submitted" else P.neutral if group == "unknown" else P.warning,
        )
        if visible_meta != meta:
            item.setToolTip(item.toolTip() + "\n" + meta)
        item.setData(Qt.ItemDataRole.UserRole, deadline)
        self._visible_deadlines.append(deadline)

    def _update_summary(self, selected_course, submitted_count):
        total = len(selected_course)
        self.summary_label.setText(f"Showing {len(self._visible_deadlines)} of {total} {self.item_label} · {submitted_count} submitted · {total - submitted_count} to do / unknown")
        if not self._visible_deadlines:
            self.assignment_list.add_section(self.empty_title, "Choose another course or submission status, or refresh Canvas.")

    def _open_item_detail(self, item):
        deadline = item.data(Qt.ItemDataRole.UserRole)
        if deadline is not None:
            AssignmentDetailDialog(deadline, self, credential_store=self._store).exec()

    def _open_detail(self, row):
        item = self.assignment_list.item(row)
        if item is not None:
            self._open_item_detail(item)
