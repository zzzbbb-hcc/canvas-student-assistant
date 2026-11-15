"""Local summaries, read-only aggregation and safe bilingual dialog lifecycle."""

from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QApplication, QLabel

from app.models.announcement import Announcement, announcement_text, extract_announcement_summary
from app.models.course import Course
from app.services import announcement_loader
from app.services.announcement_loader import cached_announcements, load_announcements
from app.services.canvas_client import CanvasClientError
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui import announcements
from app.ui.announcements import AnnouncementDetailDialog, AnnouncementsPage


NOW = datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc)


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


def notice(number=1, *, course_id=1, posted=NOW, message="<p>Please submit Assignment 2 by Friday 23:59.</p><p>Keep the rubric.</p>"):
    return Announcement(announcement_id=number, course_id=course_id,
                        course_name=f"Course {course_id}", course_code=f"COMP{course_id}",
                        title=f"Announcement {number}", message=message, posted_at=posted)


def courses(*identities):
    return [Course(id=number, name=f"Course {number}", course_code=f"COMP{number}") for number in identities]


class Store:
    def get_canvas_token(self):
        return "offline-stub-credential"


class Translator:
    def __init__(self):
        self.sources = []
        self.threads = []

    def translate(self, source):
        self.sources.append(source); self.threads.append(QThread.currentThread())
        return "请在周五 23:59 前提交作业 2。\n保留评分标准。"


def test_summary_prefers_actions_without_rewriting_dates_or_numbers():
    html = "<p>Hello everyone.</p><p>I hope you enjoyed the break.</p><p>Assignment 2 is due Friday 23:59.</p><p>You must submit the report in PDF.</p>"
    summary = extract_announcement_summary(html)
    assert "Assignment 2 is due Friday 23:59." in summary
    assert "You must submit the report in PDF." in summary
    assert "enjoyed" not in summary
    assert len(summary) <= 280


def test_summary_plain_text_keeps_paragraphs_and_drops_scripts_and_links():
    source = '<p>First &amp; second.</p><script>never translate this</script><p><a href="https://example.invalid/?secret=private">Read guide</a>.</p>'
    assert announcement_text(source) == "First & second.\nRead guide."
    assert "secret" not in extract_announcement_summary(source)
    assert "never translate" not in extract_announcement_summary(source)
    assert len(extract_announcement_summary("<p>" + "A" * 1000 + "</p>")) == 280


def test_canvas_model_normalizes_missing_dates_and_ignores_unneeded_urls():
    item = Announcement.from_canvas({"id": 7, "posted_at": "invalid", "title": "Notice", "message": "<p>Body</p>",
                                    "html_url": "https://example.invalid/private", "author": {"display_name": "Teacher"}}, courses(1)[0])
    assert item.posted_at is None
    assert item.author_name == "Teacher"
    assert "html_url" not in item.model_dump()
    assert notice(posted=datetime(2026, 1, 1)).posted_at.tzinfo is not None


def test_cached_announcements_reads_course_detail_copy_without_credentials(cache):
    cache.set("courses", [course.model_dump() for course in courses(1, 2)])
    cache.set("announcements", [{"id": 4, "title": "Cached", "message": "<p>Body</p>", "posted_at": NOW.isoformat()}], key="course:1")
    session_cache.set("announcements:2", [{"id": 5, "message": "Live memory", "posted_at": (NOW + timedelta(days=1)).isoformat()}])
    assert [item.announcement_id for item in cached_announcements(cache)] == [5, 4]


def test_known_empty_course_cache_removes_stale_aggregate(cache):
    session_cache.set("courses", courses(1))
    session_cache.set("announcements:all", [notice()])
    session_cache.set("announcements:1", [])
    assert cached_announcements(cache) == []


def test_loader_combines_courses_preserves_failed_source_without_writes(monkeypatch, cache):
    session_cache.set("courses", courses(1, 2, 3))
    session_cache.set("announcements:all", [notice(9, course_id=2), notice(10, course_id=3)])
    seen = []

    class Client:
        def __init__(self, token): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_announcements(self, course_id):
            seen.append(course_id)
            if course_id == 2:
                raise CanvasClientError("Unavailable")
            return [{"id": 1, "title": "Newest", "posted_at": (NOW + timedelta(days=1)).isoformat(), "message": "<p>Instructions</p>"}] if course_id == 1 else []

    monkeypatch.setattr(announcement_loader, "CanvasClient", Client)
    monkeypatch.setattr(cache, "set", lambda *args, **kwargs: pytest.fail("Announcement bodies must not be persisted"))
    loaded = load_announcements(Store(), cache=cache)
    assert sorted(seen) == [1, 2, 3]
    assert [(item.course_id, item.announcement_id) for item in loaded] == [(1, 1), (2, 9)]
    assert session_cache.get("announcements:3") == []
    assert "COMP2" in session_cache.get("announcement_coverage_warnings")[0]


def test_loader_whole_refresh_failure_preserves_existing_view(monkeypatch, cache):
    session_cache.set("courses", courses(1))
    session_cache.set("announcements:all", [])

    class Client:
        def __init__(self, token): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_announcements(self, course_id): raise CanvasClientError("Denied")

    monkeypatch.setattr(announcement_loader, "CanvasClient", Client)
    with pytest.raises(CanvasClientError, match="保留当前列表"):
        load_announcements(Store(), cache=cache)
    assert session_cache.get("announcements:all") == []
    assert len(session_cache.get("announcement_coverage_warnings")) == 1


def test_loader_gets_courses_only_when_no_cached_course_inventory(monkeypatch, cache):
    calls = []

    class Client:
        def __init__(self, token): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_courses(self): calls.append("courses"); return courses(1)
        def get_announcements(self, course_id): calls.append(course_id); return []

    monkeypatch.setattr(announcement_loader, "CanvasClient", Client)
    assert load_announcements(Store(), cache=cache) == []
    assert calls == ["courses", 1]


def test_page_compact_latest_first_course_filter_and_detail_click(qtbot, monkeypatch, cache):
    page = AnnouncementsPage(Store(), cache=cache); qtbot.addWidget(page)
    page._show([notice(1), notice(2, course_id=2, posted=NOW + timedelta(days=1)), notice(3, posted=None)])
    assert page.announcement_list.compact
    assert [item.announcement_id for item in page._visible_announcements] == [2, 1, 3]
    card = page.announcement_list.itemWidget(page.announcement_list.item(0))
    assert "非完整原文" in card.findChild(QLabel, "cardMeta").text()
    assert "23:59" in card.findChild(QLabel, "cardMeta").text()
    page.course_filter.setCurrentIndex(page.course_filter.findData(1))
    assert [item.announcement_id for item in page._visible_announcements] == [1, 3]
    opened = []

    class Detail:
        def __init__(self, item, parent): opened.append(item.announcement_id)
        def exec(self): pass

    monkeypatch.setattr(announcements, "AnnouncementDetailDialog", Detail)
    page._open_item(page.announcement_list.item(0))
    assert opened == [1]
    page._show([notice(4), notice(5, course_id=2)])
    assert page.course_filter.currentData() == 1


def test_page_refresh_failure_keeps_existing_cards_and_warns(qtbot, monkeypatch, cache):
    page = AnnouncementsPage(Store(), cache=cache); qtbot.addWidget(page)
    page._show([notice(1)])

    def failed(*args, **kwargs):
        session_cache.set("announcement_coverage_warnings", ["COMP1 unreadable"])
        raise CanvasClientError("Unavailable; keeping current view")

    monkeypatch.setattr(announcements, "load_announcements", failed)
    page.refresh()
    qtbot.waitUntil(lambda: page._thread is None)
    assert page._visible_announcements[0].announcement_id == 1
    assert page.refresh_button.isEnabled()
    assert "Unavailable" in page.status_label.text()
    assert not page.coverage_label.isHidden()


def test_detail_sanitizes_original_translates_paragraphs_off_gui_thread(qtbot):
    translator = Translator()
    dialog = AnnouncementDetailDialog(notice(), translator=translator); qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._thread is None)
    assert translator.sources == ["Please submit Assignment 2 by Friday 23:59.\nKeep the rubric."]
    assert translator.threads[0] != QApplication.instance().thread()
    assert "请在周五" in dialog.translation.toPlainText()
    assert "Assignment 2" in dialog.original.toPlainText()
    assert not dialog.original.openLinks() and not dialog.original.openExternalLinks()
    assert not dialog.translation.openLinks() and not dialog.translation.openExternalLinks()
    assert "16px" in dialog.original.styleSheet()
    assert dialog.translate_button.isEnabled()


def test_detail_failure_retry_preserves_original(qtbot):
    class FailingTranslator(Translator):
        def translate(self, source):
            if not self.sources:
                self.sources.append(source)
                raise RuntimeError("Safe mock failure")
            return super().translate(source)

    dialog = AnnouncementDetailDialog(notice(), translator=FailingTranslator()); qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._thread is None)
    assert "暂不可用" in dialog.translation_status.text()
    assert "Assignment 2" in dialog.original.toPlainText()
    dialog.translate()
    qtbot.waitUntil(lambda: dialog._thread is None)
    assert "请在周五" in dialog.translation.toPlainText()


@pytest.mark.parametrize("close_method", ["reject", "close", "escape"])
def test_detail_deferred_close_during_translation_is_safe(qtbot, close_method):
    started, release = Event(), Event()

    class BlockingTranslator:
        def translate(self, source):
            started.set()
            assert release.wait(5)
            return "译文"

    dialog = AnnouncementDetailDialog(notice(), translator=BlockingTranslator()); qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(started.is_set)
    try:
        if close_method == "escape":
            qtbot.keyClick(dialog, Qt.Key.Key_Escape)
        else:
            getattr(dialog, close_method)()
        assert dialog._pending_result is not None
        assert dialog._thread is not None
    finally:
        release.set()
    qtbot.waitUntil(lambda: dialog._thread is None)
    qtbot.waitUntil(lambda: not dialog.isVisible())
