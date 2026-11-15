"""Mail-like local announcement state, real clicks and compact-card regressions."""

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from app.models.announcement import Announcement
from app.services.announcement_state import AnnouncementStateStore
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui import announcements
from app.ui.announcements import AnnouncementsPage
from app.ui.main_window import MainWindow


NOW = datetime(2026, 10, 16, 3, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_session():
    session_cache.clear()
    yield
    session_cache.clear()


@pytest.fixture
def cache():
    database = SQLiteDatabase(":memory:")
    yield Cache(database)
    database.close()


class NoCredentials:
    def get_canvas_token(self):
        pytest.fail("Announcement state tests must not access credentials")


def notice(identity=1, *, course=1, title=None):
    return Announcement(
        announcement_id=identity, course_id=course,
        course_name=f"Course {course}", course_code=f"COMP{course}",
        title=title or f"Teacher notice {identity}",
        posted_at=NOW + timedelta(minutes=identity),
        message=f"<p>Assignment {identity} must be submitted Friday 23:59.</p>"
                "<p>Additional original instructions, not only a download.</p>",
    )


def make_page(qtbot, cache, items):
    page = AnnouncementsPage(NoCredentials(), cache=cache)
    qtbot.addWidget(page)
    page.resize(1000, 720)
    page._show(items)
    page.show()
    settle(qtbot, page)
    return page


def settle(qtbot, page):
    qtbot.wait(20)
    page.announcement_list._resize_cards()
    page.announcement_list.doItemsLayout()
    qtbot.wait(20)


def visible_keys(page):
    return [(item.course_id, item.announcement_id) for item in page._visible_announcements]


def find_card(page, course, identity):
    listing = page.announcement_list
    for row in range(listing.count()):
        item = listing.item(row)
        announcement = item.data(Qt.ItemDataRole.UserRole)
        if announcement is not None and (announcement.course_id, announcement.announcement_id) == (course, identity):
            return item, listing.itemWidget(item)
    raise AssertionError(f"Missing announcement card {(course, identity)}")


def action(page, name, *, course=1, identity=1):
    _item, card = find_card(page, course, identity)
    button = card.findChild(QPushButton, name)
    assert button is not None and not button.isHidden()
    assert button.accessibleName() or button.toolTip() or button.text().strip()
    return button


def select(combo, value):
    index = combo.findData(value)
    assert index >= 0, f"Missing filter option {value!r}"
    combo.setCurrentIndex(index)
    # Newly created item widgets become visible on the next Qt event turn.
    QApplication.processEvents()


@pytest.fixture
def opened_details(monkeypatch):
    opened = []

    class Detail:
        def __init__(self, announcement, parent=None, **_kwargs):
            opened.append(announcement)

        def exec(self):
            return 0

    monkeypatch.setattr(announcements, "AnnouncementDetailDialog", Detail)
    return opened


def test_new_announcements_default_to_unread_with_separate_clickable_star(qtbot, cache, opened_details):
    page = make_page(qtbot, cache, [notice(1), notice(2)])
    assert page.read_filter.currentData() == "all"
    assert page.star_filter.currentData() == "all"
    read = action(page, "announcementReadButton")
    star = action(page, "announcementStarButton")
    assert "未读" in read.text()
    assert "☆" in star.text()
    qtbot.mouseClick(star, Qt.MouseButton.LeftButton)
    assert opened_details == []
    assert "★" in action(page, "announcementStarButton").text()
    assert "未读" in action(page, "announcementReadButton").text()
    state = AnnouncementStateStore(cache.database).get_states()[(1, 1)]
    assert state.is_starred and not state.is_read


def test_real_read_button_toggles_both_directions_without_opening_detail(qtbot, cache, opened_details):
    page = make_page(qtbot, cache, [notice()])
    qtbot.mouseClick(action(page, "announcementReadButton"), Qt.MouseButton.LeftButton)
    assert opened_details == []
    assert "未读" not in action(page, "announcementReadButton").text()
    assert AnnouncementStateStore(cache.database).get_states()[(1, 1)].is_read
    qtbot.mouseClick(action(page, "announcementReadButton"), Qt.MouseButton.LeftButton)
    assert "未读" in action(page, "announcementReadButton").text()
    assert not AnnouncementStateStore(cache.database).get_states()[(1, 1)].is_read
    assert opened_details == []


def test_star_can_be_removed_without_changing_read_status(qtbot, cache, opened_details):
    store = AnnouncementStateStore(cache.database)
    store.set_read(1, 1, True)
    store.set_starred(1, 1, True)
    page = make_page(qtbot, cache, [notice()])
    qtbot.mouseClick(action(page, "announcementStarButton"), Qt.MouseButton.LeftButton)
    state = store.get_states()[(1, 1)]
    assert state.is_read and not state.is_starred
    assert "☆" in action(page, "announcementStarButton").text()
    assert opened_details == []


def test_course_read_and_star_filters_combine_and_empty_view_is_friendly(qtbot, cache):
    store = AnnouncementStateStore(cache.database)
    store.set_read(1, 2, True)
    store.set_starred(1, 2, True)
    store.set_starred(2, 3, True)
    page = make_page(qtbot, cache, [notice(1), notice(2), notice(3, course=2)])
    select(page.star_filter, "starred")
    assert set(visible_keys(page)) == {(1, 2), (2, 3)}
    select(page.read_filter, "unread")
    assert visible_keys(page) == [(2, 3)]
    select(page.course_filter, 1)
    assert visible_keys(page) == []
    listing = page.announcement_list
    assert listing.count() >= 1
    empty_item = listing.item(0)
    assert empty_item.data(Qt.ItemDataRole.UserRole) is None
    empty_widget = listing.itemWidget(empty_item)
    assert any("没有" in label.text() or "暂无" in label.text()
               for label in empty_widget.findChildren(QLabel))
    select(page.read_filter, "read")
    assert visible_keys(page) == [(1, 2)]
    select(page.star_filter, "all")
    assert visible_keys(page) == [(1, 2)]


def test_click_in_unread_view_marks_read_and_opens_correct_original_after_removal(qtbot, cache, opened_details):
    selected = notice(2, title="Original title and complete instructions")
    page = make_page(qtbot, cache, [notice(1), selected])
    select(page.read_filter, "unread")
    item, _card = find_card(page, 1, 2)
    listing = page.announcement_list
    qtbot.mouseClick(listing.viewport(), Qt.MouseButton.LeftButton,
                     pos=listing.visualItemRect(item).center())
    assert opened_details == [selected]
    assert "Additional original instructions" in opened_details[0].message
    assert visible_keys(page) == [(1, 1)]
    assert AnnouncementStateStore(cache.database).get_states()[(1, 2)].is_read


@pytest.mark.parametrize("button_name,filter_name,filter_value,existing_state", [
    ("announcementReadButton", "read_filter", "unread", False),
    ("announcementReadButton", "read_filter", "read", True),
    ("announcementStarButton", "star_filter", "starred", True),
])
def test_toggle_removes_only_nonmatching_card_without_rebuilding_others(
        qtbot, cache, monkeypatch, opened_details, button_name, filter_name, filter_value, existing_state):
    store = AnnouncementStateStore(cache.database)
    if filter_name == "star_filter":
        store.set_starred(1, 1, existing_state)
        store.set_starred(1, 2, existing_state)
    else:
        store.set_read(1, 1, existing_state)
        store.set_read(1, 2, existing_state)
    page = make_page(qtbot, cache, [notice(1), notice(2)])
    select(getattr(page, filter_name), filter_value)
    other_item, other_widget = find_card(page, 1, 2)
    monkeypatch.setattr(page.announcement_list, "clear", lambda: pytest.fail("A local flag must not rebuild the list"))
    monkeypatch.setattr(page.announcement_list, "add_card", lambda *_args, **_kwargs: pytest.fail("Unchanged cards must remain intact"))
    qtbot.mouseClick(action(page, button_name), Qt.MouseButton.LeftButton)
    assert visible_keys(page) == [(1, 2)]
    item, widget = find_card(page, 1, 2)
    assert item is other_item and widget is other_widget
    assert opened_details == []


def test_local_toggle_preserves_scroll_current_selection_and_all_card_objects(qtbot, cache, monkeypatch):
    page = make_page(qtbot, cache, [notice(number) for number in range(1, 46)])
    listing = page.announcement_list
    item, widget = find_card(page, 1, 24)
    listing.setCurrentItem(item)
    listing.scrollToItem(item)
    settle(qtbot, page)
    before_scroll = listing.verticalScrollBar().value()
    before_items = [listing.item(row) for row in range(listing.count())]
    before_widgets = [listing.itemWidget(card) for card in before_items]
    monkeypatch.setattr(listing, "clear", lambda: pytest.fail("State toggles must be a local update"))
    qtbot.mouseClick(widget.findChild(QPushButton, "announcementStarButton"), Qt.MouseButton.LeftButton)
    settle(qtbot, page)
    assert listing.currentItem() is item
    assert listing.verticalScrollBar().value() == before_scroll
    assert [listing.item(row) for row in range(listing.count())] == before_items
    assert [listing.itemWidget(card) for card in before_items] == before_widgets


def test_status_survives_refresh_and_same_database_page_recreation(qtbot, cache):
    original = notice()
    page = make_page(qtbot, cache, [original])
    qtbot.mouseClick(action(page, "announcementReadButton"), Qt.MouseButton.LeftButton)
    qtbot.mouseClick(action(page, "announcementStarButton"), Qt.MouseButton.LeftButton)
    select(page.read_filter, "read")
    select(page.star_filter, "starred")
    updated = original.model_copy(update={"title": "Updated announcement title"})
    page._show([updated, notice(2)])
    assert page.read_filter.currentData() == "read"
    assert page.star_filter.currentData() == "starred"
    assert visible_keys(page) == [(1, 1)]
    assert find_card(page, 1, 1)[1].findChild(QLabel, "cardTitle").text() == updated.title
    second = make_page(qtbot, cache, [updated, notice(2)])
    assert "未读" not in action(second, "announcementReadButton").text()
    assert "★" in action(second, "announcementStarButton").text()
    assert "未读" in action(second, "announcementReadButton", identity=2).text()
    assert "☆" in action(second, "announcementStarButton", identity=2).text()


def test_identical_notice_id_in_different_courses_has_independent_state(qtbot, cache):
    page = make_page(qtbot, cache, [notice(1), notice(1, course=2)])
    qtbot.mouseClick(action(page, "announcementReadButton", course=1), Qt.MouseButton.LeftButton)
    qtbot.mouseClick(action(page, "announcementStarButton", course=2), Qt.MouseButton.LeftButton)
    select(page.read_filter, "unread")
    assert visible_keys(page) == [(2, 1)]
    select(page.star_filter, "starred")
    assert visible_keys(page) == [(2, 1)]
    select(page.read_filter, "all")
    assert visible_keys(page) == [(2, 1)]
    states = AnnouncementStateStore(cache.database).get_states()
    assert states[(1, 1)].is_read and not states[(1, 1)].is_starred
    assert not states[(2, 1)].is_read and states[(2, 1)].is_starred


def test_bulk_state_loading_does_not_query_database_per_card_or_filter_change(qtbot, cache, monkeypatch):
    calls = []
    original = AnnouncementStateStore.get_states

    def observed_get_states(store):
        calls.append(True)
        return original(store)

    monkeypatch.setattr(AnnouncementStateStore, "get_states", observed_get_states)
    page = make_page(qtbot, cache, [notice(number) for number in range(1, 81)])
    assert 1 <= len(calls) <= 2
    loaded = len(calls)
    select(page.read_filter, "read")
    select(page.read_filter, "unread")
    select(page.star_filter, "starred")
    select(page.star_filter, "all")
    assert len(calls) == loaded


@pytest.mark.parametrize("method,button_name", [
    ("set_read", "announcementReadButton"),
    ("set_starred", "announcementStarButton"),
])
def test_failed_local_save_does_not_change_display_or_open_detail(qtbot, cache, monkeypatch, opened_details, method, button_name):
    page = make_page(qtbot, cache, [notice()])
    before = action(page, button_name).text()

    def failed(*_args, **_kwargs):
        raise OSError("fixture-only internal failure details")

    monkeypatch.setattr(AnnouncementStateStore, method, failed)
    qtbot.mouseClick(action(page, button_name), Qt.MouseButton.LeftButton)
    assert action(page, button_name).text() == before
    assert opened_details == []
    assert visible_keys(page) == [(1, 1)]
    assert any(word in page.status_label.text() for word in ("失败", "无法", "未能", "未保存", "不能"))
    assert "fixture-only" not in page.status_label.text()


def test_auto_read_save_failure_still_allows_reading_original(qtbot, cache, monkeypatch, opened_details):
    page = make_page(qtbot, cache, [notice()])
    select(page.read_filter, "unread")

    def failed(*_args, **_kwargs):
        raise OSError("controlled local database problem")

    monkeypatch.setattr(AnnouncementStateStore, "set_read", failed)
    page._open_item(page.announcement_list.item(0))
    assert len(opened_details) == 1
    assert visible_keys(page) == [(1, 1)]
    assert "未读" in action(page, "announcementReadButton").text()
    assert any(word in page.status_label.text() for word in ("失败", "无法", "未能", "未保存", "不能"))


def test_last_unread_card_becomes_noninteractive_empty_state_and_can_be_found_as_read(qtbot, cache, opened_details):
    page = make_page(qtbot, cache, [notice()])
    select(page.read_filter, "unread")
    qtbot.mouseClick(action(page, "announcementReadButton"), Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert visible_keys(page) == []
    assert page.announcement_list.count() == 1
    empty_item = page.announcement_list.item(0)
    assert empty_item.data(Qt.ItemDataRole.UserRole) is None
    page._open_item(empty_item)
    assert opened_details == []
    select(page.read_filter, "read")
    assert visible_keys(page) == [(1, 1)]
    assert "未读" not in action(page, "announcementReadButton").text()


def test_initial_state_load_failure_is_visible_and_refresh_can_recover(qtbot, cache, monkeypatch):
    store = AnnouncementStateStore(cache.database)
    store.set_read(1, 1, True)
    store.set_starred(1, 1, True)
    original_get_states = AnnouncementStateStore.get_states

    def failed(*_args, **_kwargs):
        raise OSError("fixture-only inaccessible local state")

    monkeypatch.setattr(AnnouncementStateStore, "get_states", failed)
    page = make_page(qtbot, cache, [notice()])
    assert "暂不可读取" in page.status_label.text()
    assert "fixture-only" not in page.status_label.text()
    assert "未读" in action(page, "announcementReadButton").text()
    monkeypatch.setattr(AnnouncementStateStore, "get_states", original_get_states)
    page._show([notice()])
    QApplication.processEvents()
    assert "暂不可读取" not in page.status_label.text()
    assert "未读" not in action(page, "announcementReadButton").text()
    assert "★" in action(page, "announcementStarButton").text()


def test_failed_state_reload_preserves_known_flags_without_guessing_new_notices_read(qtbot, cache, monkeypatch):
    store = AnnouncementStateStore(cache.database)
    store.set_read(1, 1, True)
    store.set_starred(1, 1, True)
    page = make_page(qtbot, cache, [notice()])

    def failed(*_args, **_kwargs):
        raise OSError("fixture-only transient database lock")

    monkeypatch.setattr(AnnouncementStateStore, "get_states", failed)
    page._show([notice(), notice(2)])
    QApplication.processEvents()
    assert "暂不可读取" in page.status_label.text()
    assert "未读" not in action(page, "announcementReadButton").text()
    assert "★" in action(page, "announcementStarButton").text()
    assert "未读" in action(page, "announcementReadButton", identity=2).text()
    assert "☆" in action(page, "announcementStarButton", identity=2).text()
    select(page.read_filter, "read")
    assert visible_keys(page) == [(1, 1)]


def test_reopening_already_read_notice_does_not_rewrite_local_state(qtbot, cache, monkeypatch, opened_details):
    AnnouncementStateStore(cache.database).set_read(1, 1, True)
    page = make_page(qtbot, cache, [notice()])
    monkeypatch.setattr(AnnouncementStateStore, "set_read", lambda *_args, **_kwargs: pytest.fail("Already read needs no write"))
    page._open_item(page.announcement_list.item(0))
    assert len(opened_details) == 1
    assert visible_keys(page) == [(1, 1)]


def test_long_wrapped_titles_leave_read_and_star_buttons_unclipped_with_real_theme(qtbot, monkeypatch):
    class Store(NoCredentials):
        def has_canvas_token(self):
            return False

        def has_ai_api_key(self):
            return False

    monkeypatch.setattr("app.ui.main_window.CredentialStore", Store)
    monkeypatch.setattr("app.ui.main_window.SQLiteDatabase", lambda: SQLiteDatabase(":memory:"))
    monkeypatch.setattr(MainWindow, "_refresh_current_page", lambda *_args: None)
    window = MainWindow()
    window._reminder_timer.stop()
    qtbot.addWidget(window)
    window.resize(1100, 720)
    page = next(window.pages.widget(index) for index in range(window.pages.count())
                if isinstance(window.pages.widget(index), AnnouncementsPage))
    page._show([notice(title="Long announcement about assignment instructions and learning materials " * 8), notice(2)])
    window.pages.setCurrentWidget(page)
    window.show()
    settle(qtbot, page)
    try:
        for identity in (1, 2):
            item, card = find_card(page, 1, identity)
            for name in ("cardTitle", "cardSubtitle", "cardMeta"):
                label = card.findChild(QLabel, name)
                assert label.textFormat() == Qt.TextFormat.PlainText
                assert label.wordWrap()
                assert label.height() >= label.heightForWidth(label.width())
                assert card.rect().contains(label.mapTo(card, label.rect().topLeft()))
                assert card.rect().contains(label.mapTo(card, label.rect().bottomRight()))
            for name in ("announcementReadButton", "announcementStarButton"):
                button = action(page, name, identity=identity)
                assert button.isVisible()
                assert button.width() >= button.minimumSizeHint().width()
                assert button.height() >= button.minimumSizeHint().height()
                assert card.rect().contains(button.mapTo(card, button.rect().topLeft()))
                assert card.rect().contains(button.mapTo(card, button.rect().bottomRight()))
            assert item.sizeHint().height() >= card.layout().totalHeightForWidth(card.width())
    finally:
        window._database.close()
