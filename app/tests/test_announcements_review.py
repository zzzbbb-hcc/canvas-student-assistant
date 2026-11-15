"""Independent security, identity, rendering and worker-lifecycle checks."""

from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QApplication, QLabel

from app.models.announcement import Announcement, announcement_text, extract_announcement_summary, sort_announcements
from app.models.course import Course
from app.services import announcement_loader
from app.services.canvas_client import CanvasClientError
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui import announcements
from app.ui.announcements import AnnouncementDetailDialog, AnnouncementsPage


NOW = datetime(2026, 10, 16, 3, tzinfo=timezone.utc)


def notice(identity=1, *, course=1, posted=NOW, title=None, message=None):
    return Announcement(
        announcement_id=identity, course_id=course, course_name=f"Course {course}",
        course_code=f"COMP{course}", title=title or f"Notice {identity}", posted_at=posted,
        message=message if message is not None else "<p>Assignment 4 is due Friday 23:59.</p>",
    )


class NoCredentialStore:
    def get_canvas_token(self):
        pytest.fail("These review tests must not access a user credential")


@pytest.fixture(autouse=True)
def session():
    session_cache.clear()
    yield
    session_cache.clear()


@pytest.fixture
def cache():
    database = SQLiteDatabase(":memory:")
    yield Cache(database)
    database.close()


def test_summary_extracts_exact_source_sentences_and_does_not_copy_active_html():
    html = (
        '<p onclick="window.evil=1">Welcome &amp; good morning.</p>'
        '<script>EXECUTE_ME()</script><style>HIDDEN_STYLE</style>'
        '<svg><text>SVG_PAYLOAD</text></svg><iframe src="https://evil.invalid">FRAME_PAYLOAD</iframe>'
        '<p>Assignment 4 is due Friday 23:59.</p>'
        '<p>Please submit only a PDF, not the draft.</p>'
        '<p><a href="javascript:EXECUTE_ME()">Read the rubric.</a></p>'
        '<img src="https://evil.invalid/tracker?session=private" onerror="EXECUTE_ME()">'
    )
    source = announcement_text(html)
    summary = extract_announcement_summary(html)
    assert summary == "Assignment 4 is due Friday 23:59. · Please submit only a PDF, not the draft."
    assert all(sentence in source for sentence in summary.split(" · "))
    for unsafe in ("EXECUTE_ME", "HIDDEN_STYLE", "SVG_PAYLOAD", "FRAME_PAYLOAD", "evil.invalid", "private"):
        assert unsafe not in source
        assert unsafe not in summary


def test_duplicate_announcement_ids_are_scoped_to_course_and_dates_sort_by_instant():
    older = notice(7, course=1)
    same_identity_update = older.model_copy(update={"title": "Latest body for the same announcement"})
    newer_other_course = notice(7, course=2, posted=NOW + timedelta(minutes=1))
    equal_in_other_zone = notice(8, course=3, posted=NOW.astimezone(timezone(timedelta(hours=-7))))
    undated = notice(9, course=4, posted=None)
    result = sort_announcements([older, undated, newer_other_course, equal_in_other_zone, same_identity_update])
    assert [(item.course_id, item.announcement_id) for item in result] == [(2, 7), (3, 8), (1, 7), (4, 9)]
    assert result[2].title == "Latest body for the same announcement"


def test_successful_empty_course_never_resurrects_while_a_different_course_fails(monkeypatch, cache):
    session_cache.set("courses", [Course(id=1, name="A"), Course(id=2, name="B")])
    session_cache.set("announcements:all", [notice(1), notice(2, course=2)])
    session_cache.set("announcements:1", [{"id": 1, "title": "Obsolete", "message": "Old body"}])

    class Store:
        def get_canvas_token(self):
            return "fixture-only-not-a-user-credential"

    class Client:
        def __init__(self, _token):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get_announcements(self, course_id):
            if course_id == 2:
                raise CanvasClientError("Unavailable")
            return []

    monkeypatch.setattr(announcement_loader, "CanvasClient", Client)
    monkeypatch.setattr(cache, "set", lambda *_args, **_kwargs: pytest.fail("Announcement HTML is memory-only"))
    loaded = announcement_loader.load_announcements(Store(), cache=cache)
    assert [(item.course_id, item.announcement_id) for item in loaded] == [(2, 2)]
    assert session_cache.get("announcements:1") == []
    assert [(item.course_id, item.announcement_id)
            for item in announcement_loader.cached_announcements(cache)] == [(2, 2)]


def test_long_titles_and_summary_are_plain_wrapped_text_without_card_clipping(qtbot, cache):
    long_title = '<b>Important</b> ' + "Extended announcement about learning materials and assessment requirements " * 6
    item = notice(title=long_title)
    page = AnnouncementsPage(NoCredentialStore(), cache=cache)
    qtbot.addWidget(page)
    page.resize(760, 700)
    page._show([item])
    page.show()
    qtbot.wait(30)
    page.announcement_list._resize_cards()
    page.announcement_list.doItemsLayout()
    qtbot.wait(20)
    card = page.announcement_list.itemWidget(page.announcement_list.item(0))
    title = card.findChild(QLabel, "cardTitle")
    assert title.text() == long_title
    for name in ("cardTitle", "cardSubtitle", "cardMeta"):
        label = card.findChild(QLabel, name)
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert label.wordWrap()
        assert label.height() >= label.heightForWidth(label.width())
        assert card.rect().contains(label.mapTo(card, label.rect().topLeft()))
        assert card.rect().contains(label.mapTo(card, label.rect().bottomRight()))


def test_worker_updates_cards_on_gui_thread_and_page_can_close_then_retry(qtbot, cache, monkeypatch):
    entered, release = Event(), Event()
    worker_thread_checks = []
    calls = []

    def load(*_args, **_kwargs):
        worker_thread_checks.append(QThread.currentThread() != QApplication.instance().thread())
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            raise CanvasClientError("Controlled read failure")
        return [notice(2)]

    render_thread_checks = []
    add_card = announcements.CardList.add_card

    def observed_add_card(listing, *args, **kwargs):
        render_thread_checks.append(QThread.currentThread() == QApplication.instance().thread())
        return add_card(listing, *args, **kwargs)

    monkeypatch.setattr(announcements.CardList, "add_card", observed_add_card)
    monkeypatch.setattr(announcements, "load_announcements", load)
    page = AnnouncementsPage(NoCredentialStore(), cache=cache)
    qtbot.addWidget(page)
    page._show([notice(1)])
    page.show()
    page.refresh()
    qtbot.waitUntil(entered.is_set)
    try:
        page.refresh()  # A rapid second click must not start another job.
        page.close()
        assert page._thread is not None
    finally:
        release.set()
    qtbot.waitUntil(lambda: page._thread is None)
    assert len(calls) == 1
    assert page.refresh_button.isEnabled()
    assert page._visible_announcements[0].announcement_id == 1
    page.show()
    qtbot.mouseClick(page.refresh_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: page._thread is None)
    assert len(calls) == 2
    assert page._visible_announcements[0].announcement_id == 2
    assert all(worker_thread_checks)
    assert all(render_thread_checks)


def test_summary_card_real_click_opens_full_original_and_translation_is_literal_text(qtbot, cache, monkeypatch):
    translated = '<b>译文保留为纯文本</b>\n<script>NOT_EXECUTED</script>'
    item = notice(message=(
        '<p>Assignment 4 is due Friday 23:59.</p><p>Extra detail not shown in the short preview.</p>'
        '<p>Third paragraph remains visible in the original.</p>'
        '<a href="https://example.invalid/private">Reference wording</a>'
        '<script>ORIGINAL_PAYLOAD</script>'
    ))
    opened = []

    class Translator:
        def translate(self, source):
            assert "Third paragraph remains visible" in source
            assert "ORIGINAL_PAYLOAD" not in source
            return translated

    class ObservedDialog(AnnouncementDetailDialog):
        def __init__(self, selected, parent):
            super().__init__(selected, parent, translator=Translator())
            opened.append(self)

        def exec(self):
            # Do not enter a nested modal loop in this event-dispatch check.
            return 0

    monkeypatch.setattr(announcements, "AnnouncementDetailDialog", ObservedDialog)
    page = AnnouncementsPage(NoCredentialStore(), cache=cache)
    qtbot.addWidget(page)
    page._show([item])
    page.resize(950, 700)
    page.show()
    qtbot.wait(30)
    card = page.announcement_list.item(0)
    qtbot.mouseClick(page.announcement_list.viewport(), Qt.MouseButton.LeftButton,
                     pos=page.announcement_list.visualItemRect(card).center())
    assert len(opened) == 1
    dialog = opened[0]
    # The page owns this dialog; registering both with qtbot would attempt
    # to close the child's already-deleted C++ object after page teardown.
    qtbot.waitUntil(lambda: dialog._thread is None)
    assert dialog.announcement is item
    assert "Third paragraph remains visible in the original." in dialog.original.toPlainText()
    assert "ORIGINAL_PAYLOAD" not in dialog.original.toPlainText()
    assert "https://example.invalid/private" not in dialog.original.toHtml()
    assert dialog.translation.toPlainText() == translated
    assert not dialog.original.openLinks() and not dialog.original.openExternalLinks()
    assert not dialog.translation.openLinks() and not dialog.translation.openExternalLinks()


def test_retry_during_translation_does_not_queue_duplicate_or_render_before_completion(qtbot):
    entered, release = Event(), Event()
    calls = []

    class Translator:
        def translate(self, source):
            calls.append(source)
            entered.set()
            assert release.wait(5)
            return "离线完成"

    dialog = AnnouncementDetailDialog(notice(), translator=Translator())
    qtbot.addWidget(dialog)
    qtbot.waitUntil(entered.is_set)
    try:
        dialog.translate()
        dialog.translate()
        assert len(calls) == 1
        assert dialog.translation.toPlainText() == ""
        assert not dialog.translate_button.isEnabled()
    finally:
        release.set()
    qtbot.waitUntil(lambda: dialog._thread is None)
    assert len(calls) == 1
    assert dialog.translation.toPlainText() == "离线完成"
    assert dialog.translate_button.isEnabled()

