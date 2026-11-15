import logging
import sys

from PySide6.QtWidgets import QApplication

from app.security.log_filter import CredentialLogFilter
from app.ui.app_icon import application_icon
from app.ui.main_window import MainWindow


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(CredentialLogFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def configure_windows_app_id() -> None:
    """Give source and packaged launches the same Windows taskbar identity."""

    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "StudentAssistant.Desktop"
        )
    except (AttributeError, OSError):
        # Branding is cosmetic and must never prevent the app from opening.
        return


def main() -> int:
    configure_logging()
    logging.getLogger(__name__).info("Starting Student Assistant")
    configure_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("Student Assistant")
    app.setWindowIcon(application_icon())
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
