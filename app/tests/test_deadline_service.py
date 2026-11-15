from datetime import datetime, timedelta, timezone

from app.models.assignment import Assignment
from app.services.deadline_service import DeadlineService

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=timezone(timedelta(hours=10), "AEST"))

def assignment(due=None, submitted=False, assignment_id=1):
    return Assignment(assignment_id=assignment_id, course_id=10, course_name="Course", title=f"Task {assignment_id}", due_at=due, submission_status="submitted" if submitted else "unsubmitted")

def test_due_at_null_is_no_deadline():
    deadline = DeadlineService(NOW).build(assignment())
    assert deadline.category == "No Deadline" and deadline.countdown == "No deadline"

def test_deadline_tomorrow():
    deadline = DeadlineService(NOW).build(assignment(NOW + timedelta(hours=23)))
    assert deadline.category == "Tomorrow" and deadline.countdown == "Due tomorrow"

def test_deadline_today():
    deadline = DeadlineService(NOW).build(assignment(NOW + timedelta(hours=3)))
    assert deadline.category == "Today" and deadline.status == "today"

def test_deadline_overdue():
    deadline = DeadlineService(NOW).build(assignment(NOW - timedelta(hours=2)))
    assert deadline.category == "Overdue" and deadline.countdown == "OVERDUE by 2 hours"

def test_deadline_seven_days_later():
    deadline = DeadlineService(NOW).build(assignment(NOW + timedelta(days=7)))
    assert deadline.category == "Next 7 Days"

def test_timezone_conversion_from_canvas_utc():
    due_utc = datetime(2026, 9, 20, 13, 59, tzinfo=timezone.utc)
    deadline = DeadlineService(NOW).build(assignment(due_utc))
    assert deadline.due_local.tzinfo is not None
    assert deadline.due_local.astimezone(timezone.utc) == due_utc

def test_submitted_and_unsubmitted_status():
    due = NOW + timedelta(days=2)
    assert DeadlineService(NOW).build(assignment(due, True)).status == "completed"
    assert DeadlineService(NOW).build(assignment(due, False)).status == "upcoming"


def test_assignment_level_submission_flag_does_not_mark_current_student_submitted():
    item = assignment(NOW + timedelta(days=2), False)
    item.has_submitted_submissions = True
    assert not item.is_submitted


def test_graded_without_student_attempt_is_not_claimed_as_submitted():
    item = assignment(NOW + timedelta(days=2), False)
    item.submission_status = "graded"
    assert not item.is_submitted
    assert item.submission_label == "No submission recorded (graded)"
    item.submission_attempt = 1
    assert item.is_submitted

def test_sorting_places_no_deadline_last():
    items = [assignment(None, assignment_id=1), assignment(NOW + timedelta(days=2), assignment_id=2), assignment(NOW + timedelta(days=1), assignment_id=3)]
    result = DeadlineService(NOW).deadlines(items)
    assert [item.assignment.assignment_id for item in result] == [3, 2, 1]
