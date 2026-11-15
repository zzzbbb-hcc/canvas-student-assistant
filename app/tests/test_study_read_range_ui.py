"""The local reading-range preference does not disturb saved study chats."""

from copy import deepcopy
from threading import Event

import httpx
import pytest
from PySide6.QtWidgets import QApplication, QComboBox

from app.models.course import Course
from app.services.ai_service import AIAnswer
from app.services.chat_history import ChatHistoryStore
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.ai_chat import AIChatPage
from app.ui.main_window import MainWindow


PREFERENCE = "ai.study_read_profile"


class LocalRecordingService:
    provider_configured = False

    def __init__(self):
        self.calls = []

    def ask(self, question, **kwargs):
        self.calls.append((question, deepcopy(kwargs)))
        return AIAnswer("这是保存在本机的回答。", (), False)


@pytest.fixture
def range_env(qtbot, monkeypatch):
    session_cache.clear()
    database = SQLiteDatabase(":memory:")
    pages = []
    services = []

    def no_http(*_args, **_kwargs):
        raise AssertionError("Reading-range changes must not make HTTP requests")

    monkeypatch.setattr(httpx.Client, "request", no_http)

    def create(service=None):
        service = service or LocalRecordingService()
        page = AIChatPage(service, database=database)
        qtbot.addWidget(page)
        page.resize(1000, 720)
        page.show()
        QApplication.processEvents()
        pages.append(page)
        services.append(service)
        return page, service

    yield database, create
    for service in services:
        release = getattr(service, "release", None)
        if release is not None:
            release.set()
    qtbot.waitUntil(lambda: all(page._thread is None for page in pages), timeout=10_000)
    for page in pages:
        page.close()
    database.close()
    session_cache.clear()


def choose_range(page, value):
    index = page.reading_range_combo.findData(value)
    assert index >= 0
    page.reading_range_combo.setCurrentIndex(index)
    QApplication.processEvents()


def send(qtbot, page, text):
    page.question_input.setText(text)
    page.ask_button.click()
    qtbot.waitUntil(lambda: page._thread is None, timeout=10_000)


def test_expanded_reading_is_default_with_two_explicit_page_ranges(range_env):
    database, create = range_env
    page, service = create()
    assert page.findChild(QComboBox, "studyReadRangeCombo") is page.reading_range_combo
    assert page.reading_range_combo.currentData() == "expanded"
    assert database.get_preference(PREFERENCE, "expanded") == "expanded"
    options = {page.reading_range_combo.itemData(index): page.reading_range_combo.itemText(index)
               for index in range(page.reading_range_combo.count())}
    assert options == {"standard": "标准 · 50 页", "expanded": "扩展 · 200 页"}
    assert service.calls == []


def test_standard_range_is_persisted_and_restored_on_page_recreation(range_env):
    database, create = range_env
    page, service = create()
    choose_range(page, "standard")
    assert database.get_preference(PREFERENCE) == "standard"
    recreated, recreated_service = create()
    assert recreated.reading_range_combo.currentData() == "standard"
    assert service.calls == recreated_service.calls == []
    choose_range(recreated, "expanded")
    assert database.get_preference(PREFERENCE) == "expanded"


def test_unknown_saved_range_falls_back_to_expanded_without_affecting_chat(range_env):
    database, create = range_env
    database.set_preference(PREFERENCE, "no-longer-supported-profile")
    store = ChatHistoryStore(database)
    conversation = store.create_conversation("general", "仍然保留的历史")
    store.append_message(conversation.id, "user", "读取范围异常也不能清除这个问题")
    page, service = create()
    assert page.reading_range_combo.currentData() == "expanded"
    assert "读取范围异常也不能清除这个问题" in page.chat_view.toPlainText()
    assert page._current_conversation_id == conversation.id
    assert service.calls == []


def test_switching_range_preserves_current_course_conversation_and_full_history(range_env, qtbot):
    database, create = range_env
    session_cache.set("courses", [Course(id=10, name="Project management", course_code="PMGT5891")])
    page, service = create()
    page.group_combo.setCurrentIndex(page.group_combo.findData("course:10"))
    send(qtbot, page, "需要保留的报告写作问题")
    identity = page._current_conversation_id
    before_text = page.chat_view.toPlainText()
    before_messages = ChatHistoryStore(database).list_messages(identity)
    before_calls = deepcopy(service.calls)
    for value in ("standard", "expanded", "standard"):
        choose_range(page, value)
        assert page.group_combo.currentData() == "course:10"
        assert page._current_conversation_id == identity
        assert page.chat_view.toPlainText() == before_text
        assert ChatHistoryStore(database).list_messages(identity) == before_messages
    assert service.calls == before_calls
    assert database.get_preference("chatgpt.allow_study_data", False) is False
    recreated, _service = create()
    assert recreated.reading_range_combo.currentData() == "standard"
    assert recreated._current_conversation_id == identity
    assert recreated.chat_view.toPlainText() == before_text


def test_switching_range_does_not_send_model_requests_create_chats_or_grant_online_consent(range_env):
    database, create = range_env
    page, service = create()
    store = ChatHistoryStore(database)
    for value in ("standard", "expanded", "standard"):
        choose_range(page, value)
    assert service.calls == []
    assert page._thread is None
    assert page._connection is None
    assert store.list_conversations() == []
    assert database.get_preference("chatgpt.allow_study_data", False) is False


@pytest.mark.parametrize("initial,requested", [("expanded", "standard"), ("standard", "expanded")])
def test_failed_range_preference_save_reverts_control_and_keeps_generic_error(range_env, monkeypatch, initial, requested):
    database, create = range_env
    database.set_preference(PREFERENCE, initial)
    page, service = create()
    original = database.set_preference

    def fail_range(key, value, *args, **kwargs):
        if key == PREFERENCE:
            raise RuntimeError("PRIVATE_READING_RANGE_STORAGE_DIAGNOSTIC")
        return original(key, value, *args, **kwargs)

    monkeypatch.setattr(database, "set_preference", fail_range)
    choose_range(page, requested)
    assert page.reading_range_combo.currentData() == initial
    assert database.get_preference(PREFERENCE) == initial
    assert "PRIVATE_READING_RANGE_STORAGE_DIAGNOSTIC" not in page.status_label.text()
    assert page.status_label.text().strip()
    assert service.calls == []


def test_reading_range_is_disabled_during_answer_and_reenabled_after_worker_finishes(range_env, qtbot):
    class WaitingService(LocalRecordingService):
        def __init__(self):
            super().__init__()
            self.started = Event()
            self.release = Event()

        def ask(self, question, **kwargs):
            self.started.set()
            assert self.release.wait(5), "Test did not release the fake answer"
            return super().ask(question, **kwargs)

    database, create = range_env
    service = WaitingService()
    page, _ = create(service)
    page.question_input.setText("后台处理时不要更改读取范围")
    page.ask_button.click()
    try:
        qtbot.waitUntil(service.started.is_set)
        assert not page.reading_range_combo.isEnabled()
        assert page.reading_range_combo.currentData() == "expanded"
        choose_range(page, "standard")
        assert page.reading_range_combo.currentData() == "expanded"
        assert database.get_preference(PREFERENCE, "expanded") == "expanded"
        assert ChatHistoryStore(database).list_messages(page._current_conversation_id)[0].content == "后台处理时不要更改读取范围"
    finally:
        service.release.set()
        qtbot.waitUntil(lambda: page._thread is None, timeout=10_000)
    assert page.reading_range_combo.isEnabled()
    choose_range(page, "standard")
    assert database.get_preference(PREFERENCE) == "standard"


def test_reading_range_keeps_compact_main_window_chat_area_readable(qtbot, monkeypatch):
    class NoCredentials:
        def has_canvas_token(self):
            return False

        def has_ai_api_key(self):
            return False

    session_cache.clear()
    service = LocalRecordingService()
    monkeypatch.setattr("app.ui.main_window.CredentialStore", NoCredentials)
    monkeypatch.setattr("app.ui.main_window.AIChatPage",
                        lambda **kwargs: AIChatPage(service, database=kwargs["database"]))
    monkeypatch.setattr(MainWindow, "_refresh_current_page", lambda *_args: None)
    window = MainWindow()
    window._reminder_timer.stop()
    qtbot.addWidget(window)
    page = next(window.pages.widget(index) for index in range(window.pages.count())
                if isinstance(window.pages.widget(index), AIChatPage))
    # QStackedWidget normally includes every page's minimum width. Isolate
    # this header while retaining the real app stylesheet and 196px sidebar;
    # unrelated assignment/deadline filter rows must not grow this fixture.
    for index in reversed(range(window.pages.count())):
        other = window.pages.widget(index)
        if other is not page:
            window.pages.removeWidget(other)
            other.hide()
    window.resize(1100, 720)
    window.show()
    window.pages.setCurrentWidget(page)
    qtbot.wait(30)
    try:
        page_minimums = [(type(window.pages.widget(index)).__name__, window.pages.widget(index).minimumSizeHint().width())
                         for index in range(window.pages.count())]
        assert window.width() == 1100, page_minimums
        assert page.reading_range_combo.isVisible()
        assert page.connection_button.isVisible()
        assert page.chat_view.height() >= 450
        assert page.reading_range_combo.geometry().intersects(page.connection_button.geometry()) is False
        assert page.rect().contains(page.reading_range_combo.geometry())
        assert page.question_input.isVisible() and page.ask_button.isVisible()
        assert service.calls == []
    finally:
        window.close()
        window._database.close()
        session_cache.clear()
