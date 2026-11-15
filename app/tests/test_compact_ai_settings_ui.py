"""Compact utility pages keep readable content and every control reachable."""

import pytest
from PySide6.QtWidgets import QGridLayout, QLabel, QScrollArea

from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.ai_chat import AIChatPage
from app.ui.main_window import MainWindow
from app.ui.page_base import PlaceholderPage
from app.ui.settings_page import SettingsPage


@pytest.fixture
def compact_window(qtbot, monkeypatch):
    class NoCredentials:
        def has_canvas_token(self):
            return False

        def has_ai_api_key(self):
            return False

    session_cache.clear()
    monkeypatch.setattr("app.ui.main_window.CredentialStore", NoCredentials)
    monkeypatch.setattr("app.ui.main_window.SQLiteDatabase", lambda: SQLiteDatabase(":memory:"))
    monkeypatch.setattr(MainWindow, "_refresh_current_page", lambda *_args: None)
    window = MainWindow()
    window._reminder_timer.stop()
    qtbot.addWidget(window)
    window.resize(1100, 720)
    window.show()
    yield window
    window._database.close()
    session_cache.clear()


def show_page(qtbot, window, page_type):
    page = next(window.pages.widget(index) for index in range(window.pages.count())
                if isinstance(window.pages.widget(index), page_type))
    window.pages.setCurrentWidget(page)
    qtbot.wait(30)
    return page


def test_ai_compact_header_preserves_chat_reading_space(qtbot, compact_window):
    page = show_page(qtbot, compact_window, AIChatPage)
    margins = page.layout().contentsMargins()
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (20, 16, 20, 14)
    assert page.layout().spacing() == 8
    heading = page.findChild(QLabel, "pageTitle")
    subtitle = page.findChild(QLabel, "pageSubtitle")
    assert heading.geometry().intersects(subtitle.geometry()) is False
    assert abs(heading.geometry().center().y() - subtitle.geometry().center().y()) <= 1
    assert subtitle.wordWrap()
    assert page.chat_view.height() >= 450
    assert "请不要在问题中粘贴密码、Cookie 或令牌" in page.chat_view.toPlainText()
    assert page.status_label.wordWrap()
    assert page.question_input.isVisible() and page.ask_button.isVisible()


def test_settings_compact_rules_and_actions_remain_reachable(qtbot, compact_window):
    page = show_page(qtbot, compact_window, SettingsPage)
    scroll = page.findChild(QScrollArea, "settingsScrollArea")
    margins = scroll.widget().layout().contentsMargins()
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (20, 16, 20, 14)
    assert scroll.widget().layout().spacing() == 8
    rule_grid = next(layout for layout in page.findChildren(QGridLayout)
                     if layout.indexOf(page.reminder_7_days) >= 0)
    assert rule_grid.rowCount() == 3
    assert rule_grid.columnCount() == 2
    for control in (page.token_input, page.save_button, page.delete_button, page.test_button,
                    page.test_notification_button, page.clear_cache_button,
                    page.ai_key_input, page.ai_save_button, page.ai_delete_button):
        scroll.ensureWidgetVisible(control, 0, 0)
        qtbot.wait(1)
        rect = control.rect()
        rect.moveTopLeft(control.mapTo(scroll.viewport(), rect.topLeft()))
        assert scroll.viewport().rect().contains(rect), control.text() if hasattr(control, "text") else ""
    assert page.status_label.wordWrap()
    assert page.reminder_status_label.wordWrap()
    assert page.ai_status_label.wordWrap()
    assert page.token_input.echoMode().name == "Password"
    assert page.ai_key_input.echoMode().name == "Password"


def test_settings_long_status_wraps_inside_scrollable_content(qtbot, compact_window):
    page = show_page(qtbot, compact_window, SettingsPage)
    scroll = page.findChild(QScrollArea, "settingsScrollArea")
    for label in (page.status_label, page.reminder_status_label, page.ai_status_label):
        label.setText("A lengthy connection or reminder explanation must remain readable. " * 8)
    qtbot.wait(30)
    for label in (page.status_label, page.reminder_status_label, page.ai_status_label):
        scroll.ensureWidgetVisible(label, 0, 0)
        qtbot.wait(1)
        assert label.height() >= label.heightForWidth(label.width())
        assert label.width() <= scroll.viewport().width()
    assert scroll.verticalScrollBar().maximum() > 0


def test_placeholder_page_uses_compact_spacing(qtbot):
    page = PlaceholderPage("An upcoming feature")
    qtbot.addWidget(page)
    margins = page.layout().contentsMargins()
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (20, 16, 20, 14)
    assert page.layout().spacing() == 8
    assert page.findChild(QLabel, "placeholderText").wordWrap()
