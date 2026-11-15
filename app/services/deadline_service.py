from datetime import datetime, timedelta
from app.models.assignment import Assignment
from app.models.deadline import Deadline

class DeadlineService:
    def __init__(self, now: datetime | None = None):
        self.now = (now or datetime.now().astimezone()).astimezone()

    @staticmethod
    def to_local(value):
        if value is None: return None
        if value.tzinfo is None: raise ValueError("Canvas deadline must include a timezone.")
        return value.astimezone()

    def build(self, assignment):
        due = self.to_local(assignment.due_at)
        if due is None:
            return Deadline(assignment=assignment, due_local=None, status="completed" if assignment.is_submitted else "upcoming", category="No Deadline", countdown="No deadline")
        delta = due - self.now
        status = "completed" if assignment.is_submitted else "overdue" if delta.total_seconds() < 0 else "today" if due.date() == self.now.date() else "due_soon" if delta <= timedelta(days=1) else "upcoming"
        return Deadline(assignment=assignment, due_local=due, status=status, category=self._category(due), countdown=self._countdown(due, assignment.is_submitted))

    def _category(self, due):
        delta = due - self.now; days = (due.date() - self.now.date()).days
        if delta.total_seconds() < 0: return "Overdue" if days >= -7 else "Past"
        if days == 0: return "Today"
        if days == 1: return "Tomorrow"
        if due.isocalendar()[:2] == self.now.isocalendar()[:2]: return "This Week"
        if due <= self.now + timedelta(days=7): return "Next 7 Days"
        return "Later"

    def _countdown(self, due, submitted):
        if submitted: return "Submitted"
        seconds = int((due - self.now).total_seconds()); overdue = seconds < 0; seconds = abs(seconds)
        days, remainder = divmod(seconds, 86400); hours = remainder // 3600
        if not overdue and due.date() == (self.now + timedelta(days=1)).date() and days == 0: return "Due tomorrow"
        if days: amount = f"{days} day{'s' if days != 1 else ''}" + (f" {hours} hour{'s' if hours != 1 else ''}" if hours else "")
        elif hours: amount = f"{hours} hour{'s' if hours != 1 else ''}"
        else:
            minutes = max(1, seconds // 60); amount = f"{minutes} minute{'s' if minutes != 1 else ''}"
        return f"OVERDUE by {amount}" if overdue else f"Due in {amount}"

    def deadlines(self, assignments):
        items = [self.build(a) for a in assignments]
        # Keep actionable upcoming dates first, then undated work, and put
        # overdue work at the very bottom. Use the actual submission-aware
        # status: a submitted assignment with a past due date is not overdue.
        return sorted(items, key=lambda d: (
            2 if d.status == "overdue" else 1 if d.due_local is None else 0,
            d.due_local.timestamp() if d.due_local else float("inf"),
        ))

    def matches_period(self, deadline, period, *, include_submitted=False):
        """Use the same local-time windows for dashboard counts and lists."""
        if period == "all":
            return True
        if not include_submitted and deadline.assignment.is_submitted:
            return False
        if period == "overdue":
            return deadline.status == "overdue"
        due = deadline.due_local
        if due is None or due < self.now:
            return False
        if period == "today":
            return due.date() == self.now.date()
        if period == "tomorrow":
            return due.date() == self.now.date() + timedelta(days=1)
        if period == "week":
            # Calendar dates avoid an off-by-one-hour boundary if the local
            # UTC offset changes before Sunday (for example at DST).
            return due.date() <= self.now.date() + timedelta(days=6 - self.now.weekday())
        raise ValueError("Unknown deadline period")

    def upcoming_unsubmitted(self, assignments, limit=5):
        return [d for d in self.deadlines(assignments) if d.due_local and not d.assignment.is_submitted and d.due_local >= self.now][:limit]
