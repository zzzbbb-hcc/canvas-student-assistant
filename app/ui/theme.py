"""A shared, readable butter-cream theme for widgets and local readers."""

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette


@dataclass(frozen=True)
class SoftPalette:
    background: str = "#FFF9ED"
    surface: str = "#FFFEF8"
    surface_alt: str = "#FFF3D6"
    sidebar: str = "#F7EAC5"
    text: str = "#453C2D"
    text_secondary: str = "#74644C"
    text_muted: str = "#7D6D55"
    border: str = "#E8DAB8"
    primary: str = "#EFD48A"
    primary_hover: str = "#E7C775"
    primary_pressed: str = "#DDB85F"
    accent: str = "#88611E"
    selection: str = "#F3DFA7"
    selection_text: str = "#5B421D"
    focus: str = "#A47729"
    success: str = "#3D7052"
    success_bg: str = "#F0F6EC"
    warning: str = "#876126"
    warning_bg: str = "#FFF0CF"
    danger: str = "#A44E47"
    danger_bg: str = "#FFF0EB"
    neutral: str = "#756B5C"
    neutral_bg: str = "#F5F0E4"
    peach: str = "#FFF2E9"
    sage: str = "#EDF3E6"


PALETTE = SoftPalette()
FONT_FAMILIES = ("Noto Sans SC", "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")
FONT_FAMILY_CSS = ", ".join(f'"{family}"' for family in FONT_FAMILIES)


def theme_font(pixel_size: int = 14) -> QFont:
    available = set(QFontDatabase.families())
    family = next((name for name in FONT_FAMILIES if name in available), "Segoe UI")
    font = QFont(family)
    font.setFamilies(list(FONT_FAMILIES))
    font.setPixelSize(pixel_size)
    return font


def theme_palette(base: QPalette | None = None) -> QPalette:
    palette = QPalette(base) if base is not None else QPalette()
    for role, value in (
        (QPalette.ColorRole.Window, PALETTE.background),
        (QPalette.ColorRole.Base, PALETTE.surface),
        (QPalette.ColorRole.AlternateBase, PALETTE.surface_alt),
        (QPalette.ColorRole.WindowText, PALETTE.text),
        (QPalette.ColorRole.Text, PALETTE.text),
        (QPalette.ColorRole.Button, PALETTE.primary),
        (QPalette.ColorRole.ButtonText, PALETTE.text),
        (QPalette.ColorRole.Highlight, PALETTE.selection),
        (QPalette.ColorRole.HighlightedText, PALETTE.selection_text),
        (QPalette.ColorRole.Link, PALETTE.accent),
        (QPalette.ColorRole.LinkVisited, PALETTE.text_secondary),
    ):
        palette.setColor(role, QColor(value))
    return palette


def warning_stylesheet(compact: bool = True) -> str:
    padding = "5px 8px" if compact else "8px"
    return (f"color: {PALETTE.warning}; background: {PALETTE.warning_bg}; "
            f"padding: {padding}; border-radius: 9px; font-size: 12px;")


def reader_stylesheet(background: str | None = None, *, font_size: int = 16) -> str:
    p = PALETTE
    return (f"QTextBrowser {{ background: {background or p.surface}; color: {p.text}; "
            f"border: 1px solid {p.border}; border-radius: 12px; padding: 12px; "
            f"selection-background-color: {p.selection}; selection-color: {p.selection_text}; "
            f"font-family: {FONT_FAMILY_CSS}; font-size: {font_size}px; }}")


def application_stylesheet() -> str:
    p = PALETTE
    return f"""
        QMainWindow, QDialog {{ background: {p.background}; color: {p.text}; }}
        QWidget {{ font-family: {FONT_FAMILY_CSS}; font-size: 14px; color: {p.text}; }}
        QLabel {{ background: transparent; }}
        QFrame#studySidebar {{ background: {p.sidebar}; border: none; }}
        QLabel#brandTitle {{ font-size: 17px; font-weight: 600; color: {p.text}; }}
        QLabel#brandSubtitle {{ font-size: 11px; color: {p.text_secondary}; }}
        QLabel#sidebarNote {{ color: {p.text_secondary}; font-size: 12px; padding: 8px 14px; }}
        QListWidget#navigation {{ background: {p.sidebar}; color: {p.text_secondary}; border: none;
            padding: 6px 10px; font-size: 14px; font-weight: 500; outline: none; }}
        QListWidget#navigation::item {{ height: 40px; padding-left: 10px; padding-right: 4px;
            margin: 2px 0; border-radius: 12px; }}
        QListWidget#navigation::item:hover {{ background: {p.surface_alt}; color: {p.text}; }}
        QListWidget#navigation::item:selected {{ background: {p.selection}; color: {p.selection_text}; }}
        QLabel#pageTitle {{ font-size: 24px; font-weight: 600; color: {p.text}; }}
        QLabel#pageSubtitle {{ font-size: 13px; color: {p.text_secondary}; }}
        QLabel#sectionTitle {{ font-size: 16px; font-weight: 600; color: {p.text}; margin-top: 2px; }}
        QLabel#statusText {{ color: {p.text_muted}; padding: 0; font-size: 12px; }}
        QPushButton {{ background: {p.primary}; color: {p.text}; border: 1px solid transparent;
            border-radius: 10px; padding: 5px 10px; font-weight: 500; }}
        QPushButton:hover {{ background: {p.primary_hover}; }}
        QPushButton:pressed {{ background: {p.primary_pressed}; }}
        QPushButton:focus {{ border: 1px solid {p.focus}; }}
        QPushButton:disabled {{ background: {p.neutral_bg}; color: {p.neutral}; border-color: {p.border}; }}
        QPushButton#secondaryButton {{ background: {p.surface}; color: {p.text_secondary}; border: 1px solid {p.border}; }}
        QPushButton#secondaryButton:hover {{ background: {p.surface_alt}; color: {p.text}; }}
        QPushButton#secondaryButton:focus {{ border-color: {p.focus}; }}
        QListWidget#cardList {{ background: transparent; border: none; outline: none; }}
        QListWidget#cardList::item, QListWidget#cardList::item:selected {{ background: transparent; border: none; }}
        QFrame#contentCard, QFrame#statCard {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 15px; }}
        QFrame#contentCard[selected="true"] {{ background: {p.surface_alt}; border-color: {p.focus}; }}
        QPushButton#statCard {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.border};
            border-radius: 15px; padding: 0; text-align: left; }}
        QPushButton#statCard:hover {{ background: {p.surface_alt}; border-color: {p.primary_pressed}; }}
        QPushButton#statCard:focus {{ background: {p.surface_alt}; border-color: {p.focus}; }}
        QFrame#noticeCard {{ background: {p.warning_bg}; border: 1px solid {p.border}; border-radius: 12px; }}
        QLabel#cardTitle {{ font-size: 16px; font-weight: 600; color: {p.text}; }}
        QLabel#cardSubtitle {{ color: {p.text_secondary}; }}
        QLabel#cardMeta {{ color: {p.text_muted}; font-size: 12px; }}
        QLabel#cardBadge {{ color: {p.accent}; background: {p.surface_alt}; border-radius: 9px;
            padding: 6px 9px; font-size: 11px; font-weight: 600; }}
        QLabel#statNumber {{ font-size: 26px; font-weight: 600; }}
        QLabel#statLabel {{ color: {p.text_secondary}; font-weight: 500; }}
        QTabWidget::pane {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 12px; top: -1px; }}
        QTabBar::tab {{ background: {p.neutral_bg}; color: {p.text_secondary}; padding: 6px 12px;
            margin-right: 3px; border-top-left-radius: 10px; border-top-right-radius: 10px; }}
        QTabBar::tab:selected {{ background: {p.surface}; color: {p.accent}; font-weight: 600; }}
        QTabBar::tab:hover {{ background: {p.surface_alt}; }}
        QTextBrowser, QTreeWidget, QListWidget, QTextEdit, QPlainTextEdit {{ background: {p.surface};
            color: {p.text}; border: 1px solid {p.border}; border-radius: 11px; padding: 4px;
            selection-background-color: {p.selection}; selection-color: {p.selection_text}; }}
        QGroupBox {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 12px;
            margin-top: 10px; padding: 10px; font-weight: 600; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 6px; }}
        QLineEdit, QComboBox, QSpinBox {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.border};
            border-radius: 9px; padding: 6px 8px; selection-background-color: {p.selection};
            selection-color: {p.selection_text}; }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {p.focus}; }}
        QComboBox QAbstractItemView {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.border};
            selection-background-color: {p.selection}; selection-color: {p.selection_text}; outline: none; }}
        QMenu {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.border}; padding: 5px; }}
        QMenu::item {{ padding: 6px 12px; border-radius: 6px; }}
        QMenu::item:selected {{ background: {p.selection}; color: {p.selection_text}; }}
        QMenu::item:disabled {{ color: {p.neutral}; }}
        QToolTip {{ background: {p.surface_alt}; color: {p.text}; border: 1px solid {p.border}; padding: 5px; }}
        QHeaderView::section {{ background: {p.surface_alt}; color: {p.text_secondary}; border: none;
            border-bottom: 1px solid {p.border}; padding: 5px; font-weight: 500; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 3px; min-height: 24px; }}
        QScrollBar::handle:vertical:hover {{ background: {p.primary_pressed}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        QProgressBar {{ background: {p.neutral_bg}; border: 1px solid {p.border}; border-radius: 7px; text-align: center; }}
        QProgressBar::chunk {{ background: {p.primary}; border-radius: 6px; }}
        QScrollArea#settingsScrollArea {{ border: none; background: {p.background}; }}
        QScrollArea#settingsScrollArea > QWidget > QWidget {{ background: {p.background}; }}
        QLabel#placeholderText {{ font-size: 14px; color: {p.text_secondary}; }}
    """
