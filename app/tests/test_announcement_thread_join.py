"""Do not release announcement thread wrappers before native teardown joins."""

import pytest

from app.models.announcement import Announcement
from app.models.assignment import Assignment
from app.services.deadline_service import DeadlineService
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.ui.announcements import AnnouncementDetailDialog, AnnouncementsPage
from app.ui.deadline_widgets import AssignmentDetailDialog


@pytest.mark.parametrize("kind", ["page", "dialog", "assignment"])
def test_finished_joins_native_thread_before_deleting_or_releasing_wrappers(qtbot, kind):
    database = SQLiteDatabase(":memory:")
    try:
        if kind == "page":
            widget = AnnouncementsPage(object(), cache=Cache(database))
        elif kind == "dialog":
            widget = AnnouncementDetailDialog(Announcement(
                announcement_id=1, course_id=1, course_name="Course", title="Empty notice",
            ))
        else:
            widget = AssignmentDetailDialog(DeadlineService().build(Assignment(
                assignment_id=1, course_id=1, course_name="Course", title="Empty assignment",
            )))
        qtbot.addWidget(widget)
        events = []
        worker = object()

        class NativeThread:
            def wait(self):
                assert widget._thread is self
                assert widget._worker is worker
                events.append("joined")
                return True

            def deleteLater(self):
                assert events == ["joined"]
                events.append("delete scheduled")

        widget._thread = NativeThread()
        widget._worker = worker
        if kind == "assignment":
            widget._job_finished()
        else:
            widget._finished()
        assert events == ["joined", "delete scheduled"]
        assert widget._thread is None
        assert widget._worker is None
    finally:
        database.close()
