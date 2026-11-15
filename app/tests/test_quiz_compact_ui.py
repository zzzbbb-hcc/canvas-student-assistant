"""The denser quiz view fits more deadlines without hiding their metadata."""

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from app.models.assignment import Assignment
from app.services.deadline_service import DeadlineService
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.assignments import AssignmentsPage
from app.ui.cards import CardList
from app.ui.deadline_center import DeadlineCenterPage
from app.ui.deadline_widgets import format_due
from app.ui.main_window import MainWindow
from app.ui.quizzes import QuizzesPage


BASE_TIME = datetime(2026, 11, 1, 2, 37, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_session_cache(monkeypatch):
    # Keep the dated fixture tasks in the future regardless of today's date.
    monkeypatch.setattr("app.ui.assignments.DeadlineService",
                        lambda: DeadlineService(now=BASE_TIME - timedelta(days=2)))
    session_cache.clear()
    yield
    session_cache.clear()


@pytest.fixture
def cache():
    database = SQLiteDatabase(":memory:")
    yield Cache(database)
    database.close()


def quiz(task_id, *, course_id=1, state="unsubmitted", due=None, title=None):
    return Assignment(
        assignment_id=task_id, course_id=course_id,
        course_name=f"Course {course_id}", course_code=f"COMP{course_id}",
        title=title or f"Week {task_id} practice quiz", due_at=due,
        submission_status=state, quiz_id=task_id + 100, quiz_engine="classic",
    )


def card_items(page):
    return [page.assignment_list.item(row) for row in range(page.assignment_list.count())
            if page.assignment_list.item(row).data(Qt.ItemDataRole.UserRole) is not None]


def visible_ids(page):
    return [deadline.assignment.assignment_id for deadline in page._visible_deadlines]


def settle_cards(qtbot, card_list):
    # The production card list schedules size-to-width updates after resize.
    qtbot.wait(30)
    card_list._resize_cards()
    card_list.doItemsLayout()
    qtbot.wait(30)


@pytest.fixture
def styled_window(qtbot, monkeypatch):
    class Store:
        def has_canvas_token(self):
            return False

        def has_ai_api_key(self):
            return False

    # Exercise the actual application stylesheet, but never read stored
    # credentials, mutate the user's database, or start a Canvas request.
    monkeypatch.setattr("app.ui.main_window.CredentialStore", Store)
    monkeypatch.setattr("app.ui.main_window.SQLiteDatabase", lambda: SQLiteDatabase(":memory:"))
    monkeypatch.setattr(MainWindow, "_refresh_current_page", lambda *_args: None)
    window = MainWindow()
    window._reminder_timer.stop()
    for index in range(window.pages.count()):
        page = window.pages.widget(index)
        if hasattr(page, "_notification_service"):
            page._notification_service = None
    qtbot.addWidget(window)
    window.resize(1100, 720)
    yield window
    window._database.close()


def test_quizzes_show_at_least_four_full_cards_at_default_window_size(qtbot, styled_window):
    session_cache.set("quiz_coverage_warnings", [f"Course {index}: quiz metadata unavailable."
                                                for index in range(20)])
    page = next(styled_window.pages.widget(index) for index in range(styled_window.pages.count())
                if isinstance(styled_window.pages.widget(index), QuizzesPage))
    page._show([quiz(index, due=BASE_TIME + timedelta(days=index)) for index in range(1, 13)])
    styled_window.pages.setCurrentWidget(page)
    styled_window.show()
    settle_cards(qtbot, page.assignment_list)

    viewport = page.assignment_list.viewport()
    full_cards = [item for item in card_items(page)
                  if viewport.rect().contains(page.assignment_list.visualItemRect(item))]
    assert len(full_cards) >= 4
    assert len(card_items(page)) == 12
    assert visible_ids(page) == list(range(1, 13))
    assert page.quiz_coverage_label.isVisible()
    assert page.quiz_coverage_button.isVisible()
    assert "20" in page.quiz_coverage_label.text()
    assert page.quiz_coverage_label.height() <= 45


def test_compact_cards_reduce_height_and_spacing_without_hiding_information(qtbot):
    ordinary = CardList()
    compact = CardList(compact=True)
    for listing in (ordinary, compact):
        qtbot.addWidget(listing)
        listing.resize(800, 500)
        listing.add_card("Week 1 practice quiz", "COMP5310 · Due: 01 Nov 2026 · 13:37",
                         "Quiz · Due in 7 days", "UPCOMING", status="Not submitted")
        listing.show()
        settle_cards(qtbot, listing)
    ordinary_item, compact_item = ordinary.item(0), compact.item(0)
    assert ordinary_item.sizeHint().height() >= 116
    assert compact_item.sizeHint().height() <= ordinary_item.sizeHint().height() - 20
    assert compact.spacing() < ordinary.spacing()
    card = compact.itemWidget(compact_item)
    for name in ("cardTitle", "cardSubtitle", "cardMeta", "submissionBadge"):
        label = card.findChild(QLabel, name)
        assert label is not None and label.isVisible()
        assert card.rect().contains(label.geometry())
    assert "Not submitted" in compact_item.toolTip()
    assert "Due in 7 days" in compact_item.toolTip()


@pytest.mark.parametrize("state", ["submitted", "graded", None])
def test_long_quiz_titles_and_submission_labels_grow_instead_of_clipping(qtbot, cache, state):
    item = quiz(1, state=state, due=BASE_TIME,
                title="Long quiz title with detailed instructions and multiple sections " * 5)
    page = QuizzesPage(cache=cache)
    qtbot.addWidget(page)
    page.resize(760, 720)
    page._show([item])
    page.show()
    settle_cards(qtbot, page.assignment_list)
    card_item = card_items(page)[0]
    card = page.assignment_list.itemWidget(card_item)
    for name in ("cardTitle", "cardSubtitle", "cardMeta", "submissionBadge"):
        label = card.findChild(QLabel, name)
        assert label is not None and label.isVisible()
        assert card.rect().contains(label.geometry())
        if label.hasHeightForWidth():
            assert label.height() >= label.heightForWidth(label.width())
    assert card.height() >= card.layout().totalHeightForWidth(card.width())
    assert format_due(page._visible_deadlines[0]) in card.findChild(QLabel, "cardSubtitle").text()
    assert card.findChild(QLabel, "submissionBadge").text() == item.submission_label
    assert item.title in card_item.toolTip()


def test_all_assignment_list_pages_use_the_same_compact_density(qtbot, cache):
    for page_type in (AssignmentsPage, DeadlineCenterPage, QuizzesPage):
        page = page_type(cache=cache)
        qtbot.addWidget(page)
        page._show([quiz(1, due=BASE_TIME)])
        page.resize(1000, 720)
        page.show()
        settle_cards(qtbot, page.assignment_list)
        assert page.compact_layout
        assert page.assignment_list.compact
        assert page.assignment_list.spacing() == 4
        item = card_items(page)[0]
        assert 64 <= item.sizeHint().height() < 116
        assert page.assignment_list.itemWidget(item).findChild(QLabel, "submissionBadge") is not None
        assert page.assignment_list.itemWidget(item).findChild(QLabel, "cardBadge") is None


def test_dense_view_preserves_deadline_order_filters_and_click_identity(qtbot, cache, monkeypatch):
    opened = []

    class Detail:
        def __init__(self, deadline, _parent, *, credential_store):
            opened.append((deadline.assignment.assignment_id, credential_store))

        def exec(self):
            pass

    monkeypatch.setattr("app.ui.assignments.AssignmentDetailDialog", Detail)
    store = object()
    page = QuizzesPage(store, cache=cache)
    qtbot.addWidget(page)
    tasks = [quiz(4, course_id=2), quiz(3, state="submitted", due=BASE_TIME + timedelta(days=1)),
             quiz(2, course_id=2, state=None, due=BASE_TIME), quiz(1, due=BASE_TIME - timedelta(days=1))]
    page._show(tasks)
    assert visible_ids(page) == [1, 2, 3, 4]
    page.course_filter.setCurrentIndex(page.course_filter.findData(2))
    page.submission_filter.setCurrentIndex(page.submission_filter.findData("pending"))
    assert visible_ids(page) == [2, 4]
    page.submission_filter.setCurrentIndex(page.submission_filter.findData("unknown"))
    assert visible_ids(page) == [2]
    page._show(tasks)
    assert visible_ids(page) == [2]
    page.resize(1000, 720)
    page.show()
    settle_cards(qtbot, page.assignment_list)
    item = card_items(page)[0]
    qtbot.mouseClick(page.assignment_list.viewport(), Qt.MouseButton.LeftButton,
                     pos=page.assignment_list.visualItemRect(item).center())
    assert opened == [(2, store)]
