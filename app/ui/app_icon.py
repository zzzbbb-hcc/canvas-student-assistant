"""The same versioned icon is used by source and frozen application windows."""

from pathlib import Path
import sys

from PySide6.QtGui import QIcon


ICON_FILENAME = "student-assistant-butter-v1.ico"


def application_icon_path() -> Path:
    """Resolve only our bundled asset, never a working-directory file."""

    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle_root:
        return Path(bundle_root) / "app" / "assets" / ICON_FILENAME
    return Path(__file__).resolve().parents[1] / "assets" / ICON_FILENAME


def application_icon() -> QIcon:
    return QIcon(str(application_icon_path()))
