from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QComboBox

from app.ui.assignments import AssignmentsPage

class DeadlineCenterPage(AssignmentsPage):
    page_name = "Deadline Center"
    refresh_text = "Refresh Deadlines"
    default_submission_filter = "pending"
    group_by_course = False
    page_subtitle = "All courses · Earliest deadline first · Undated next · Overdue last · Exact local due times"
    instruction_text = "按截止时间从近到远 · 无截止时间随后 · 已逾期置底 · 单击查看详情和资料"

    def __init__(self, credential_store=None, *, cache=None, notification_service=None):
        super().__init__(credential_store, cache=cache, notification_service=notification_service)
        self.period_filter = QComboBox()
        self.period_filter.setObjectName("deadlinePeriodFilter")
        self.period_filter.setAccessibleName("Deadline date range")
        self.period_filter.setToolTip("日期范围 · 首页统计同时包含作业和测验")
        for label, value in [
            ("All deadlines · 全部", "all"),
            ("Due today · 今天", "today"),
            ("Due tomorrow · 明天", "tomorrow"),
            ("Due this week · 本周", "week"),
            ("Overdue · 已逾期", "overdue"),
        ]:
            self.period_filter.addItem(label, value)
        self.header_actions.insertWidget(self.header_actions.count() - 1, self.period_filter)
        self.period_filter.currentIndexChanged.connect(self._render_groups)

    def _deadlines_in_view(self):
        period = self.period_filter.currentData()
        return [deadline for deadline in self._deadlines
                if self._deadline_service.matches_period(deadline, period, include_submitted=True)]

    def set_period_filter(self, period):
        """Open a dashboard count without inheriting stale course/status filters."""
        index = self.period_filter.findData(period)
        if index < 0:
            raise ValueError("Unknown deadline period")
        with QSignalBlocker(self.period_filter), QSignalBlocker(self.course_filter), QSignalBlocker(self.submission_filter):
            self.period_filter.setCurrentIndex(index)
            self.course_filter.setCurrentIndex(self.course_filter.findData(None))
            self.submission_filter.setCurrentIndex(self.submission_filter.findData("pending"))
        self._render_groups()
