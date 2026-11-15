"""Quiz views keep every available quiz and distinguish due/open/close times."""

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from app.models.assignment import Assignment
from app.services.deadline_service import DeadlineService
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.deadline_center import DeadlineCenterPage
from app.ui.deadline_widgets import AssignmentDetailDialog, format_due, format_local_time
from app.ui.quizzes import QuizzesPage


BASE_TIME = datetime(2026, 11, 1, 2, 37, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_session_cache():
    session_cache.clear()
    yield
    session_cache.clear()


@pytest.fixture
def cache():
    database = SQLiteDatabase(":memory:")
    yield Cache(database)
    database.close()


def task(task_id, *, course_id=1, state="unsubmitted", due=None, **metadata):
    return Assignment(
        assignment_id=task_id, course_id=course_id,
        course_name=f"Course {course_id}", course_code=f"COMP{course_id}",
        title=f"Task {task_id}", submission_status=state, due_at=due,
        **metadata,
    )


def quiz(task_id, **metadata):
    return task(task_id, quiz_id=abs(task_id) + 100, quiz_engine="classic", **metadata)


def visible_ids(page):
    return [deadline.assignment.assignment_id for deadline in page._visible_deadlines]


def cards(page):
    return [page.assignment_list.item(row) for row in range(page.assignment_list.count())
            if page.assignment_list.item(row).data(Qt.ItemDataRole.UserRole) is not None]


def test_quiz_page_includes_classic_new_and_assignment_backed_online_quizzes(qtbot, cache):
    page = QuizzesPage(cache=cache); qtbot.addWidget(page)
    page._show([
        task(9, due=BASE_TIME),
        quiz(1, course_id=2, state="submitted", due=BASE_TIME),
        task(2, due=BASE_TIME + timedelta(days=1), submission_types=["online_quiz"]),
        task(3, state=None, due=BASE_TIME + timedelta(days=2),
             quiz_id=103, quiz_engine="new", submission_types=["external_tool"]),
        quiz(4, course_id=2),
    ])

    assert page.page_name.startswith("Quizzes")
    assert page.submission_filter.currentData() == "all"
    assert visible_ids(page) == [1, 2, 3, 4]
    assert len(cards(page)) == 4
    assert [deadline.assignment.is_submitted for deadline in page._visible_deadlines] == [True, False, False, False]


def test_quiz_cards_stay_globally_chronological_without_course_headers(qtbot, cache):
    page = QuizzesPage(cache=cache); qtbot.addWidget(page)
    page._show([
        quiz(3, course_id=1, due=BASE_TIME + timedelta(days=3)),
        quiz(1, course_id=2, due=BASE_TIME),
        quiz(4, course_id=1),
        quiz(2, course_id=1, due=BASE_TIME + timedelta(hours=1)),
    ])

    assert not page.group_by_course
    assert visible_ids(page) == [1, 2, 3, 4]
    assert page.assignment_list.count() == len(cards(page)) == 4
    for item in cards(page):
        deadline = item.data(Qt.ItemDataRole.UserRole)
        card = page.assignment_list.itemWidget(item)
        subtitle = card.findChild(QLabel, "cardSubtitle").text()
        assert deadline.assignment.course_code in subtitle
        assert format_due(deadline) in subtitle
        assert "quiz" in item.toolTip().casefold()


def test_quiz_without_due_date_does_not_treat_availability_close_as_due(qtbot, cache):
    page = QuizzesPage(cache=cache); qtbot.addWidget(page)
    page._show([
        quiz(1, lock_at=BASE_TIME, unlock_at=BASE_TIME - timedelta(days=1)),
        quiz(2, due=BASE_TIME + timedelta(days=7)),
    ])

    assert visible_ids(page) == [2, 1]
    undated = page._visible_deadlines[-1]
    assert undated.assignment.lock_at == BASE_TIME
    assert undated.due_local is None
    assert format_due(undated) == "No deadline"
    card = page.assignment_list.itemWidget(cards(page)[-1])
    assert "No deadline" in card.findChild(QLabel, "cardSubtitle").text()


def test_quiz_course_and_status_filters_preserve_unknown_and_survive_refresh(qtbot, cache):
    page = QuizzesPage(cache=cache); qtbot.addWidget(page)
    tasks = [quiz(1, due=BASE_TIME), quiz(2, state=None), quiz(3, state="submitted"),
             quiz(4, course_id=2, state="submitted"), task(99, course_id=3)]
    page._show(tasks)
    assert page.course_filter.count() == 3
    page.course_filter.setCurrentIndex(page.course_filter.findData(1))
    page.submission_filter.setCurrentIndex(page.submission_filter.findData("pending"))
    assert visible_ids(page) == [1, 2]
    page.submission_filter.setCurrentIndex(page.submission_filter.findData("unknown"))
    assert visible_ids(page) == [2]
    page._show(tasks)
    assert page.course_filter.currentData() == 1
    assert page.submission_filter.currentData() == "unknown"
    assert visible_ids(page) == [2]
    page.submission_filter.setCurrentIndex(page.submission_filter.findData("submitted"))
    assert visible_ids(page) == [3]
    page.submission_filter.setCurrentIndex(page.submission_filter.findData("all"))
    assert visible_ids(page) == [1, 2, 3]


def test_deadline_center_contains_quizzes_and_regular_tasks_in_one_date_order(qtbot, cache):
    page = DeadlineCenterPage(cache=cache); qtbot.addWidget(page)
    page._show([
        task(1, course_id=1, due=BASE_TIME + timedelta(days=2)),
        quiz(2, course_id=2, due=BASE_TIME),
        quiz(3, state=None, due=BASE_TIME + timedelta(days=1)),
        quiz(4, state="submitted", due=BASE_TIME - timedelta(days=1)),
    ])

    assert visible_ids(page) == [2, 3, 1]
    page.submission_filter.setCurrentIndex(page.submission_filter.findData("all"))
    assert visible_ids(page) == [4, 2, 3, 1]
    page._show([deadline.assignment for deadline in page._deadlines])
    assert visible_ids(page) == [4, 2, 3, 1]


def test_single_quiz_card_click_opens_the_same_read_only_detail(qtbot, cache, monkeypatch):
    opened = []

    class Detail:
        def __init__(self, deadline, parent, *, credential_store):
            opened.append((deadline.assignment.assignment_id, credential_store))

        def exec(self):
            pass

    monkeypatch.setattr("app.ui.assignments.AssignmentDetailDialog", Detail)
    store = object()
    page = QuizzesPage(store, cache=cache); qtbot.addWidget(page)
    page._show([quiz(1, due=BASE_TIME)]); page.resize(1000, 750); page.show()
    qtbot.wait(20)
    item = cards(page)[0]
    qtbot.mouseClick(page.assignment_list.viewport(), Qt.MouseButton.LeftButton,
                     pos=page.assignment_list.visualItemRect(item).center())
    assert opened == [(1, store)]


def test_quiz_detail_renders_bilingual_introduction_and_distinct_availability(qtbot):
    class Translator:
        calls = []

        def translate(self, text):
            self.calls.append(text)
            return "阅读第六周的介绍。"

    translator = Translator()
    item = quiz(-7, description="<p>Read the Week 6 introduction.</p>",
                due=BASE_TIME, unlock_at=BASE_TIME - timedelta(days=7),
                lock_at=BASE_TIME + timedelta(days=1), quiz_time_limit=45.5,
                quiz_locked=True)
    dialog = AssignmentDetailDialog(DeadlineService().build(item), translator=translator)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=3000)

    assert dialog.description.toPlainText() == "Read the Week 6 introduction."
    assert dialog.chinese_description.toPlainText() == "阅读第六周的介绍。"
    assert translator.calls == ["Read the Week 6 introduction."]
    assert "quiz" in dialog.introduction_heading.text().casefold()
    assert "quiz" in dialog.content_notice.text().casefold() or "测验" in dialog.content_notice.text()
    info = dialog.quiz_info_label.text()
    assert "45.5" in info
    assert format_local_time(item.unlock_at) in info
    assert format_local_time(item.lock_at) in info
    assert "locked" in info.casefold()
    assert format_local_time(item.due_at) in dialog.due_label.text()


@pytest.mark.parametrize("engine", ["classic", "new"])
def test_standalone_quiz_refresh_uses_quiz_api_not_negative_assignment_id(qtbot, monkeypatch, engine):
    calls = []
    item = task(-701, quiz_id=7, quiz_engine=engine, description=None)

    class Store:
        def get_canvas_token(self):
            return "test-token"

    class Client:
        BASE_URL = "https://canvas.sydney.edu.au"

        def __init__(self, token):
            assert token == "test-token"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get_quiz_assignment(self, course_id, quiz_id, *, engine):
            calls.append((course_id, quiz_id, engine))
            return item

        def get_assignment(self, *_args, **_kwargs):
            pytest.fail("A standalone quiz must not be requested as a negative assignment ID")

    monkeypatch.setattr("app.ui.deadline_widgets.CanvasClient", Client)
    dialog = AssignmentDetailDialog(DeadlineService().build(item)); qtbot.addWidget(dialog)
    dialog._store = Store()
    assert dialog._fetch_assignment() == item
    assert calls == [(1, 7, engine)]


def test_opening_quiz_detail_only_reads_metadata_and_never_starts_an_attempt(qtbot, monkeypatch):
    calls = []
    item = quiz(-1, description="<p>Read this quiz introduction.</p>", due=BASE_TIME)

    class Store:
        def get_canvas_token(self):
            return "test-token"

    class Client:
        BASE_URL = "https://canvas.sydney.edu.au"

        def __init__(self, token):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get_quiz_assignment(self, course_id, quiz_id, *, engine):
            calls.append((course_id, quiz_id, engine))
            return item

        def __getattr__(self, name):
            pytest.fail(f"Unexpected quiz operation: {name}; only quiz metadata is read")

    class Translator:
        def translate(self, text):
            return "阅读测验介绍。"

    monkeypatch.setattr("app.ui.deadline_widgets.CanvasClient", Client)
    dialog = AssignmentDetailDialog(DeadlineService().build(item), credential_store=Store(), translator=Translator())
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=3000)
    assert calls == [(1, 101, "classic")]
    assert dialog.description.toPlainText() == "Read this quiz introduction."
    assert dialog.chinese_description.toPlainText() == "阅读测验介绍。"


def test_assignment_backed_quiz_refresh_keeps_discovered_quiz_metadata(qtbot, monkeypatch):
    original = task(701, due=BASE_TIME, quiz_id=7, quiz_engine="new", quiz_type="graded_quiz",
                    quiz_time_limit=30, quiz_locked=True, quiz_assignment_id=701)
    refreshed = task(701, due=BASE_TIME + timedelta(days=1), state="submitted")

    class Store:
        def get_canvas_token(self):
            return "test-token"

    class Client:
        BASE_URL = "https://canvas.sydney.edu.au"

        def __init__(self, token):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get_assignment(self, course_id, assignment_id):
            assert (course_id, assignment_id) == (1, 701)
            return refreshed

        def get_quiz_assignment(self, course_id, quiz_id, *, engine):
            assert (course_id, quiz_id, engine) == (1, 7, "new")
            return original

    monkeypatch.setattr("app.ui.deadline_widgets.CanvasClient", Client)
    dialog = AssignmentDetailDialog(DeadlineService().build(original)); qtbot.addWidget(dialog)
    dialog._store = Store()
    result = dialog._fetch_assignment()
    assert result.due_at == refreshed.due_at
    assert result.is_submitted
    for field in ("quiz_id", "quiz_engine", "quiz_type", "quiz_time_limit", "quiz_locked", "quiz_assignment_id"):
        assert getattr(result, field) == getattr(original, field)


@pytest.mark.parametrize("unavailable", [False, True])
def test_assignment_quiz_keeps_quiz_instructions_when_assignment_has_no_description(qtbot, monkeypatch, unavailable):
    from app.services.canvas_client import CanvasClientError

    original = quiz(701, description="<p>Previously read quiz instructions.</p>", due=BASE_TIME)
    fresh = quiz(701, description=None, due=BASE_TIME + timedelta(days=1), state="submitted")
    latest = quiz(701, description="<p>Latest quiz instructions.</p>", due=None)

    class Store:
        def get_canvas_token(self):
            return "dummy"

    class Client:
        BASE_URL = "https://canvas.sydney.edu.au"

        def __init__(self, _token):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get_assignment(self, *_args):
            return fresh

        def get_quiz_assignment(self, *_args, **_kwargs):
            if unavailable:
                raise CanvasClientError("Canvas access denied.")
            return latest

    monkeypatch.setattr("app.ui.deadline_widgets.CanvasClient", Client)
    dialog = AssignmentDetailDialog(DeadlineService().build(original)); qtbot.addWidget(dialog)
    dialog._store = Store()
    result = dialog._fetch_assignment()
    assert result.description == (original.description if unavailable else latest.description)
    assert result.due_at == fresh.due_at
    assert result.is_submitted


def test_partial_quiz_coverage_warning_is_visible_plain_text_and_can_clear(qtbot, cache):
    warning = 'Course 2: <b>New Quizzes unavailable</b>; showing the accessible assignments.'
    session_cache.set("quiz_coverage_warnings", [warning])
    page = QuizzesPage(cache=cache); qtbot.addWidget(page)
    page._show([quiz(1)])
    label = page.quiz_coverage_label
    assert label.text()
    assert "1" in label.text()
    assert label.textFormat() == Qt.TextFormat.PlainText
    assert label.wordWrap()
    assert not label.isHidden()
    session_cache.set("quiz_coverage_warnings", [])
    page._show([quiz(1)])
    assert not label.text() or label.isHidden()


def test_coverage_details_keep_all_course_warnings_in_scrollable_details(qtbot, cache, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    warnings = [f"Course {index}: Quiz metadata unavailable." for index in range(25)]
    session_cache.set("quiz_coverage_warnings", warnings)
    page = QuizzesPage(cache=cache); qtbot.addWidget(page); page._show([])
    seen = []

    def inspect(message):
        seen.append((message.text(), message.detailedText(), message.textFormat()))
        return QMessageBox.StandardButton.Close

    monkeypatch.setattr(QMessageBox, "exec", inspect)
    page._show_quiz_coverage()
    assert len(seen) == 1
    assert "25" in seen[0][0]
    assert seen[0][1] == "\n\n".join(warnings)
    assert seen[0][2] == Qt.TextFormat.PlainText
