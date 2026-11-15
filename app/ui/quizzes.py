"""Read-only quiz deadlines over the same deduplicated Canvas study data."""

from app.ui.assignments import AssignmentsPage


class QuizzesPage(AssignmentsPage):
    page_name = "Quizzes · 测验"
    refresh_text = "Refresh Quizzes"
    default_submission_filter = "all"
    group_by_course = False
    compact_layout = True
    item_label = "quizzes"
    empty_title = "No quizzes in this view"
    page_subtitle = "Classic & New Quizzes · Earliest deadline first · Overdue last · Also included in Deadlines · Read-only"
    instruction_text = "已逾期置底 · 单击查看测验详情 · 只读，不会开始测验"

    def _show(self, assignments):
        super()._show([item for item in assignments if item.is_quiz])
