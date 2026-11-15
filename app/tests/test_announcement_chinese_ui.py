"""Independent Chinese inbox rendering and bounded worker lifecycle checks."""

from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QSplitter

from app.models.announcement import Announcement
from app.services.announcement_preview import ChineseAnnouncementPreview
from app.services.announcement_state import AnnouncementStateStore
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui import announcements
from app.ui.announcements import AnnouncementDetailDialog, AnnouncementsPage


NOW = datetime(2026, 10, 16, 3, tzinfo=timezone.utc)


class NoCredentials:
    def get_canvas_token(self):
        pytest.fail("Chinese inbox display must not access a credential")


class RecordingTranslator:
    def __init__(self):
        self.sources = []
        self.off_gui = []

    def is_available(self):
        return True

    def translate(self, source):
        self.sources.append(source)
        self.off_gui.append(QThread.currentThread() != QApplication.instance().thread())
        return f"中文译文：{source}"


class BlockingTranslator(RecordingTranslator):
    def __init__(self):
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def translate(self, source):
        self.entered.set()
        assert self.release.wait(5), "The test must release its controlled worker"
        return super().translate(source)


def notice(identity=1, *, course=1, title=None, message=None):
    return Announcement(
        announcement_id=identity, course_id=course,
        course_name=f"Course {course}", course_code=f"COMP{course}",
        title=title or f"Teacher notice {identity}",
        message=message or f"<p>Please submit Assignment {identity} Friday 23:59.</p>",
        posted_at=NOW + timedelta(minutes=identity),
    )


@pytest.fixture(autouse=True)
def clean_session():
    session_cache.clear()
    yield
    session_cache.clear()


@pytest.fixture
def cache():
    database = SQLiteDatabase(":memory:")
    yield Cache(database)
    database.close()


@pytest.fixture
def page_factory(qtbot, cache):
    pages = []

    def create(items, translator, *, show=True):
        page = AnnouncementsPage(NoCredentials(), cache=cache, translator=translator)
        qtbot.addWidget(page)
        pages.append(page)
        page.resize(1000, 720)
        page._show(items)
        if show:
            page.show()
        return page

    yield create
    for page in pages:
        page.hide()
        translator = page._translator
        if isinstance(translator, BlockingTranslator):
            translator.release.set()
        qtbot.waitUntil(lambda p=page: p._preview_thread is None, timeout=6000)


def card_for(page, course=1, identity=1):
    listing = page.announcement_list
    for row in range(listing.count()):
        item = listing.item(row)
        original = item.data(Qt.ItemDataRole.UserRole)
        if original is not None and (original.course_id, original.announcement_id) == (course, identity):
            return item, listing.itemWidget(item)
    raise AssertionError(f"Missing announcement {(course, identity)}")


def translated(page, course=1, identity=1):
    return "中文译文" in card_for(page, course, identity)[1].findChild(QLabel, "cardTitle").text()


def test_page_labels_are_chinese_without_english_action_duplicates(page_factory):
    page = page_factory([notice()], RecordingTranslator(), show=False)
    assert page.findChild(QLabel, "pageTitle").text() == "公告"
    assert page.refresh_button.text() == "刷新公告"
    assert page.course_filter.itemText(0) == "全部课程"
    labels = [label.text() for label in page.findChildren(QLabel)]
    assert "课程" in labels
    assert not any(label in labels for label in ("Course", "Announcements · 公告"))


def test_hidden_page_does_not_start_model_but_show_automatically_translates(qtbot, page_factory):
    translator = RecordingTranslator()
    page = page_factory([notice()], translator, show=False)
    qtbot.wait(120)
    assert translator.sources == []
    assert page._preview_thread is None
    page.show()
    qtbot.waitUntil(lambda: translated(page), timeout=4000)
    qtbot.waitUntil(lambda: page._preview_thread is None)
    assert translator.sources and all(translator.off_gui)


def test_pending_keeps_original_then_updates_one_card_without_losing_identity(qtbot, page_factory, monkeypatch):
    translator = BlockingTranslator()
    original = notice(title="Complete original title")
    page = page_factory([original], translator)
    qtbot.waitUntil(translator.entered.is_set)
    item, card = card_for(page)
    try:
        assert card.findChild(QLabel, "cardTitle").text() == original.title
        assert "翻译" in card.findChild(QLabel, "cardMeta").text()
        monkeypatch.setattr(page.announcement_list, "clear", lambda: pytest.fail("A translated card must not rebuild the inbox"))
        monkeypatch.setattr(page.announcement_list, "add_card", lambda *_a, **_k: pytest.fail("A translated card must update in place"))
    finally:
        translator.release.set()
    qtbot.waitUntil(lambda: translated(page))
    updated_item, updated_card = card_for(page)
    assert updated_item is item and updated_card is card
    assert updated_item.data(Qt.ItemDataRole.UserRole) is original
    assert "非完整原文" in updated_card.findChild(QLabel, "cardMeta").text()
    assert "23:59" in updated_card.findChild(QLabel, "cardMeta").text()
    assert all(translator.off_gui)


def test_chinese_preview_updates_on_gui_thread_and_stays_plain_text(qtbot, page_factory, monkeypatch):
    class LiteralTranslator(RecordingTranslator):
        def translate(self, source):
            super().translate(source)
            return "中文译文：<b>不能作为 HTML 执行</b><script>NOT_EXECUTED</script>"

    observed_gui_updates = []
    original_size_item = announcements.CardList._size_item

    def observe_card_resize(listing, item, card):
        heading = card.findChild(QLabel, "cardTitle")
        if heading is not None and "中文译文" in heading.text():
            observed_gui_updates.append(QThread.currentThread() == QApplication.instance().thread())
        return original_size_item(listing, item, card)

    # Instrument a Python helper, never replace a native Qt method binding.
    monkeypatch.setattr(announcements.CardList, "_size_item", observe_card_resize)
    page = page_factory([notice()], LiteralTranslator())
    qtbot.waitUntil(lambda: translated(page))
    _, card = card_for(page)
    for name in ("cardTitle", "cardMeta"):
        label = card.findChild(QLabel, name)
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert "<b>" in label.text()
    assert observed_gui_updates and all(observed_gui_updates)


def test_async_translation_preserves_manual_flags_and_combined_filters(qtbot, cache, page_factory):
    translator = BlockingTranslator()
    page = page_factory([notice(), notice(2, course=2)], translator)
    qtbot.waitUntil(translator.entered.is_set)
    try:
        _, card = card_for(page, course=2, identity=2)
        qtbot.mouseClick(card.findChild(QPushButton, "announcementStarButton"), Qt.MouseButton.LeftButton)
        qtbot.mouseClick(card.findChild(QPushButton, "announcementReadButton"), Qt.MouseButton.LeftButton)
        page.course_filter.setCurrentIndex(page.course_filter.findData(2))
        page.read_filter.setCurrentIndex(page.read_filter.findData("read"))
        page.star_filter.setCurrentIndex(page.star_filter.findData("starred"))
    finally:
        translator.release.set()
    qtbot.waitUntil(lambda: translated(page, course=2, identity=2))
    assert [(item.course_id, item.announcement_id) for item in page._visible_announcements] == [(2, 2)]
    state = AnnouncementStateStore(cache.database).get_states()[(2, 2)]
    assert state.is_read and state.is_starred
    assert page.course_filter.currentData() == 2
    assert page.read_filter.currentData() == "read"
    assert page.star_filter.currentData() == "starred"
    _, card = card_for(page, course=2, identity=2)
    assert "★" in card.findChild(QPushButton, "announcementStarButton").text()
    assert "未读" not in card.findChild(QPushButton, "announcementReadButton").text()


def test_edited_announcement_does_not_receive_obsolete_translation(qtbot, page_factory):
    translator = BlockingTranslator()
    original = notice(title="Old title before teacher revision")
    updated = original.model_copy(update={"title": "Revised important title", "message": "<p>Please submit the revised report Monday 21:30.</p>"})
    page = page_factory([original], translator)
    qtbot.waitUntil(translator.entered.is_set)
    try:
        page._show([updated])
    finally:
        translator.release.set()
    qtbot.waitUntil(lambda: translated(page) and "Revised important title" in card_for(page)[1].findChild(QLabel, "cardTitle").text())
    _, card = card_for(page)
    assert "Old title" not in card.findChild(QLabel, "cardTitle").text()
    assert "Monday 21:30" in card.findChild(QLabel, "cardMeta").text()
    assert card_for(page)[0].data(Qt.ItemDataRole.UserRole) is updated


def test_translation_is_viewport_priority_and_does_not_translate_all_history(qtbot, page_factory):
    translator = RecordingTranslator()
    page = page_factory([notice(identity) for identity in range(1, 101)], translator)
    qtbot.waitUntil(lambda: translated(page, identity=100))
    qtbot.waitUntil(lambda: page._preview_thread is None)
    qtbot.wait(150)
    assert "Teacher notice 100" in translator.sources[0]
    assert len(translator.sources) < 40, "A visible page must not queue all 100 historic notices"
    assert not translated(page, identity=1)
    first_item, _card = card_for(page, identity=1)
    page.announcement_list.scrollToItem(first_item)
    qtbot.waitUntil(lambda: translated(page, identity=1))
    assert all(translator.off_gui)


def test_hiding_page_stops_queued_history_work_after_current_job(qtbot, page_factory):
    translator = BlockingTranslator()
    page = page_factory([notice(identity) for identity in range(1, 101)], translator)
    qtbot.waitUntil(translator.entered.is_set)
    page.hide()
    translator.release.set()
    qtbot.waitUntil(lambda: page._preview_thread is None)
    after_finish = len(translator.sources)
    qtbot.wait(150)
    assert len(translator.sources) == after_finish
    assert after_finish < 10


def test_closing_page_defers_until_current_native_worker_finishes(qtbot, page_factory):
    translator = BlockingTranslator()
    page = page_factory([notice(identity) for identity in range(1, 21)], translator)
    qtbot.waitUntil(translator.entered.is_set)
    try:
        page.close()
        assert page._preview_thread is not None
        assert page.isVisible(), "The page must retain its worker owner until it is safe to close"
    finally:
        translator.release.set()
    qtbot.waitUntil(lambda: page._preview_thread is None)
    qtbot.waitUntil(lambda: not page.isVisible())
    after_finish = len(translator.sources)
    qtbot.wait(150)
    assert len(translator.sources) == after_finish


def test_course_filter_recreation_reuses_memory_translation_without_reinference(qtbot, page_factory):
    translator = RecordingTranslator()
    page = page_factory([notice()], translator)
    qtbot.waitUntil(lambda: translated(page))
    qtbot.waitUntil(lambda: page._preview_thread is None)
    calls = len(translator.sources)
    page.course_filter.setCurrentIndex(page.course_filter.findData(1))
    qtbot.wait(180)
    assert translated(page)
    assert len(translator.sources) == calls
    page.course_filter.setCurrentIndex(0)
    qtbot.wait(180)
    assert translated(page)
    assert len(translator.sources) == calls


def test_repeated_show_hide_cancels_pending_work_then_resumes_once_visible(qtbot, page_factory):
    translator = RecordingTranslator()
    page = page_factory([notice()], translator, show=False)
    for _number in range(6):
        page.show()
        page.hide()
        qtbot.wait(20)
    assert translator.sources == []
    assert page._preview_thread is None
    page.show()
    qtbot.waitUntil(lambda: translated(page))
    qtbot.waitUntil(lambda: page._preview_thread is None)
    initial_calls = len(translator.sources)
    for _number in range(3):
        page.hide()
        page.show()
        qtbot.wait(130)
    assert translated(page)
    assert len(translator.sources) == initial_calls


def test_preview_memory_cache_is_bounded_and_does_not_persist_content(page_factory, cache, monkeypatch):
    page = page_factory([], RecordingTranslator(), show=False)
    monkeypatch.setattr(cache, "set", lambda *_a, **_k: pytest.fail("Chinese preview must not be persisted"))
    for number in range(513):
        page._preview_translated((f"fixture-content-{number}", ChineseAnnouncementPreview(f"中文标题{number}", f"中文重点{number}")))
    assert len(page._preview_translations) == 512
    assert "fixture-content-0" not in page._preview_translations
    assert page._preview_translations["fixture-content-512"].title == "中文标题512"


def test_unavailable_model_retains_original_and_can_recover_after_refresh(qtbot, page_factory, monkeypatch):
    class InitiallyUnavailable(RecordingTranslator):
        def __init__(self):
            super().__init__()
            self.available = False

        def is_available(self):
            return self.available

    translator = InitiallyUnavailable()
    original = notice()
    page = page_factory([original], translator)
    qtbot.waitUntil(lambda: "模型暂不可用" in page.preview_status.text())
    assert translator.sources == []
    assert page._preview_thread is None
    assert card_for(page)[1].findChild(QLabel, "cardTitle").text() == original.title
    translator.available = True
    monkeypatch.setattr(announcements, "cached_announcements", lambda _cache: [])
    monkeypatch.setattr(announcements, "load_announcements", lambda *_a, **_k: [original])
    page.refresh()
    qtbot.waitUntil(lambda: page._thread is None)
    qtbot.waitUntil(lambda: translated(page))


def test_failed_preview_never_claims_chinese_or_persists_content_and_refresh_retries(qtbot, cache, page_factory, monkeypatch):
    class RecoverableTranslator(RecordingTranslator):
        def __init__(self):
            super().__init__()
            self.fail = True

        def translate(self, source):
            if self.fail:
                self.sources.append(source)
                raise RuntimeError("fixture-only source and internal backend detail")
            return super().translate(source)

    monkeypatch.setattr(cache, "set", lambda *_a, **_k: pytest.fail("Chinese preview and announcement content are memory only"))
    translator = RecoverableTranslator()
    original = notice()
    page = page_factory([original], translator)
    qtbot.waitUntil(lambda: bool(translator.sources))
    qtbot.waitUntil(lambda: page._preview_thread is None)
    _, card = card_for(page)
    assert card.findChild(QLabel, "cardTitle").text() == original.title
    assert "fixture-only" not in " ".join(label.text() for label in page.findChildren(QLabel))
    assert any(word in page.preview_status.text() for word in ("暂不可", "失败", "未能", "重试"))
    assert AnnouncementStateStore(cache.database).get_states() == {}
    translator.fail = False
    monkeypatch.setattr(announcements, "cached_announcements", lambda _cache: [])
    monkeypatch.setattr(announcements, "load_announcements", lambda *_a, **_k: [original])
    page.refresh()
    qtbot.waitUntil(lambda: page._thread is None)
    qtbot.waitUntil(lambda: translated(page))
    assert AnnouncementStateStore(cache.database).get_states() == {}


def test_detail_shows_chinese_first_with_original_still_available(qtbot):
    dialog = AnnouncementDetailDialog(notice(), translator=RecordingTranslator())
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._thread is None)
    splitter = dialog.findChild(QSplitter)
    assert splitter is not None
    assert splitter.widget(0).isAncestorOf(dialog.translation)
    assert splitter.widget(1).isAncestorOf(dialog.original)
    assert "中文译文" in dialog.translation.toPlainText()
    assert "Please submit Assignment 1 Friday 23:59." in dialog.original.toPlainText()
    assert dialog.translate_button.text() == "重新翻译"


def test_detail_uses_cached_chinese_heading_but_keeps_exact_original_title(qtbot, page_factory):
    original = notice(title="Exact original teacher title")
    page = page_factory([original], RecordingTranslator())
    qtbot.waitUntil(lambda: translated(page))
    page.hide()
    qtbot.waitUntil(lambda: page._preview_thread is None)
    dialog = AnnouncementDetailDialog(original, page, translator=RecordingTranslator())
    # The page owns this dialog; qtbot must not separately close its child.
    qtbot.waitUntil(lambda: dialog._thread is None)
    heading = dialog.findChild(QLabel, "announcementTitle")
    assert "中文译文" in heading.text()
    assert dialog.windowTitle() == heading.text()
    original_heading = dialog.findChild(QLabel, "announcementOriginalTitle")
    assert original_heading is not None
    assert original_heading.text() == f"原文标题：{original.title}"
    assert original_heading.textFormat() == Qt.TextFormat.PlainText
