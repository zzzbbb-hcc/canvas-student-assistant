"""The butter-yellow refresh stays readable, distinct, and compact."""

from hashlib import sha256

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFontInfo

from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.main_window import MainWindow


NAVIGATION_KEYS = (
    "dashboard", "courses", "assignments", "quizzes", "deadlines",
    "announcements", "assistant", "settings",
)
NAVIGATION_LABELS = (
    "Dashboard", "Courses", "Assignments", "Quizzes", "Deadlines", "公告",
    "AI Assistant", "Settings",
)


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG relative luminance, including the sRGB linearization step."""
    def luminance(value: str) -> float:
        color = QColor(value)
        assert color.isValid(), value
        channels = (color.redF(), color.greenF(), color.blueF())
        linear = [component / 12.92 if component <= 0.04045
                  else ((component + 0.055) / 1.055) ** 2.4
                  for component in channels]
        return sum(component * weight for component, weight in zip(
            linear, (0.2126, 0.7152, 0.0722), strict=True,
        ))

    first, second = sorted((luminance(foreground), luminance(background)))
    return (second + 0.05) / (first + 0.05)


@pytest.mark.parametrize("foreground,background", [
    ("text", "background"),
    ("text", "surface"),
    ("text", "surface_alt"),
    ("text", "sidebar"),
    ("text_secondary", "background"),
    ("text_secondary", "surface"),
    ("text", "primary"),
    ("text", "primary_hover"),
    ("text", "primary_pressed"),
    ("selection_text", "selection"),
])
def test_warm_palette_keeps_normal_text_readable(foreground, background):
    from app.ui.theme import PALETTE

    assert contrast_ratio(getattr(PALETTE, foreground), getattr(PALETTE, background)) >= 4.5


def test_stylesheet_uses_the_warm_palette_instead_of_the_old_navy_blue():
    from app.ui.theme import PALETTE, application_stylesheet

    stylesheet = application_stylesheet().lower()
    for old_color in ("#111a2e", "#4f6df5", "#f4f6fb", "#3d59df"):
        assert old_color not in stylesheet
    for color in (PALETTE.background, PALETTE.sidebar, PALETTE.primary, PALETTE.text):
        assert color.lower() in stylesheet
    assert "qpushbutton:focus" in stylesheet or "qpushbutton:focus-visible" in stylesheet
    assert "qpushbutton:disabled" in stylesheet


@pytest.mark.parametrize("size", [16, 24])
def test_hand_drawn_navigation_icons_render_at_small_sizes_and_are_distinct(qapp, size):
    from app.ui.doodle_icons import navigation_icon

    images = []
    for key in NAVIGATION_KEYS:
        icon = navigation_icon(key)
        assert not icon.isNull(), key
        pixmap = icon.pixmap(size, size)
        assert not pixmap.isNull(), key
        assert pixmap.width() == size and pixmap.height() == size
        image = pixmap.toImage()
        pixels = bytes(image.constBits())
        assert any(image.pixelColor(x, y).alpha() for y in range(size) for x in range(size)), key
        images.append(sha256(pixels).digest())
    assert len(set(images)) == len(NAVIGATION_KEYS)


def test_coordinated_font_declares_chinese_and_windows_fallbacks(qapp):
    from app.ui.theme import FONT_FAMILIES, theme_font

    font = theme_font()
    assert tuple(font.families()) == FONT_FAMILIES
    # The offscreen plugin has no system font database. Native Windows runs
    # additionally resolve a real family; never substitute a decorative font.
    actual_family = QFontInfo(font).family()
    if actual_family:
        assert actual_family in FONT_FAMILIES
    assert 12 <= font.pixelSize() <= 16


@pytest.fixture
def warm_window(qtbot, monkeypatch):
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
    qtbot.wait(30)
    yield window
    window.close()
    window._database.close()
    session_cache.clear()


def test_warm_navigation_keeps_all_eight_destinations_compact(warm_window):
    navigation = warm_window.navigation
    assert tuple(navigation.item(row).text() for row in range(navigation.count())) == NAVIGATION_LABELS
    assert warm_window.pages.count() == len(NAVIGATION_KEYS)
    assert navigation.width() <= 220
    assert navigation.iconSize().width() == navigation.iconSize().height() == 20
    assert navigation.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    for row in range(navigation.count()):
        item = navigation.item(row)
        assert not item.icon().isNull(), item.text()
        rect = navigation.visualItemRect(item)
        assert rect.top() >= 0 and rect.bottom() < navigation.viewport().height(), item.text()


def test_main_window_applies_shared_warm_theme(warm_window):
    from app.ui.theme import FONT_FAMILIES, PALETTE, application_stylesheet

    assert warm_window.styleSheet() == application_stylesheet()
    assert PALETTE.sidebar.lower() in warm_window.styleSheet().lower()
    assert tuple(warm_window.font().families()) == FONT_FAMILIES
