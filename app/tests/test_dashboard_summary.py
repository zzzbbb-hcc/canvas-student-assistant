from datetime import datetime, timedelta

from app.models.assignment import Assignment
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.ui.dashboard import DashboardPage


def test_dashboard_shows_due_tomorrow_and_next_deadline(qtbot, tmp_path):
    page = DashboardPage(cache=Cache(SQLiteDatabase(tmp_path / "dashboard.sqlite3")))
    qtbot.addWidget(page)
    now = datetime.now().astimezone()
    due_tomorrow = (now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    page._show(
        [
            Assignment(
                assignment_id=7,
                course_id=1,
                course_name="Example Course",
                course_code="EXAM1000",
                title="Tomorrow task",
                due_at=due_tomorrow,
                submission_status="unsubmitted",
            )
        ]
    )

    assert page.stats["Due Tomorrow"].text() == "1"
    assert page.next_deadline_title.text() == "Tomorrow task"
    assert "EXAM1000" in page.next_deadline_detail.text()


def test_dashboard_counts_assignments_without_due_date_without_crashing(qtbot, tmp_path):
    database = SQLiteDatabase(tmp_path / "no-deadline.sqlite3")
    page = DashboardPage(cache=Cache(database)); qtbot.addWidget(page)
    try:
        page._show([
            Assignment(
                assignment_id=9, course_id=1, course_name="Course",
                title="No due date", due_at=None, submission_status="unsubmitted",
            )
        ])
        assert page.stats["Due This Week"].text() == "0"
        assert page.stats["Due Today"].text() == "0"
        assert page.upcoming_list.count() == 0
    finally:
        database.close()
