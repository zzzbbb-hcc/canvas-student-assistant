"""Render documentation screenshots from fictional data in an isolated process.

Run with the project's virtual environment:
    python scripts/generate_demo_screenshots.py

Uses the real MainWindow, pages and stylesheet, with an offscreen Qt platform.
No desktop capture or existing screenshot is read. The demo clock is fixed at
16 November 2026, 10:00 UTC+11. Layout/fonts can vary with installed Qt/fonts.
Only the three named docs/images outputs are written. Safety guards are
installed before importing application modules and remain active until exit.
"""

from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import socket
import sqlite3
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "images"
FILENAMES = ("demo-dashboard.png", "demo-courses.png", "demo-deadlines.png")
DEMO_ZONE = timezone(timedelta(hours=11), "UTC+11")
DEMO_NOW = datetime(2026, 11, 16, 10, 0, tzinfo=DEMO_ZONE)
SIZE = (1280, 800)
COUNTS: Counter[str] = Counter()
VIOLATIONS: list[str] = []


def blocked(operation):
    """Never call the replaced API, and remember even swallowed failures."""
    def reject(*_args, **_kwargs):
        VIOLATIONS.append(operation)
        raise RuntimeError(f"Demo isolation blocked: {operation}")
    return reject


def probe(call):
    before = len(VIOLATIONS)
    try:
        call()
    except RuntimeError:
        if len(VIOLATIONS) == before + 1:
            return
    raise RuntimeError("A demo safety guard did not fail closed.")


def install_file_guard():
    """Allow code/runtime reads, not user data; allow only named PNG writes."""
    def normalized(path):
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    read_roots = tuple(normalized(path) for path in (
        ROOT / "app", ROOT / "scripts", Path(sys.prefix), Path(sys.base_prefix),
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
    ))
    output_paths = {normalized(OUTPUT / filename) for filename in FILENAMES}

    def audit(event, args):
        if event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        path = normalized(args[0])
        mode, flags = args[1], args[2]
        writing = (isinstance(mode, str) and any(c in mode for c in "wa+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        )
        if writing and path in output_paths:
            return
        if not writing and any(path == root or path.startswith(root + os.sep) for root in read_roots):
            return
        # Avoid logging a path which could itself contain private information.
        blocked("unapproved file access")()

    sys.addaudithook(audit)


class EmptyCredentials:
    """No backend, credentials, persistence or fallback exists in this stub."""
    def __init__(self, *_args, **_kwargs):
        COUNTS["credential_stubs"] += 1

    def has_canvas_token(self):
        COUNTS["empty_credential_checks"] += 1
        return False

    has_ai_api_key = has_canvas_token

    def get_canvas_token(self):
        COUNTS["empty_credential_reads"] += 1
        return None

    get_ai_api_key = get_canvas_token

    def __getattr__(self, _name):
        return blocked("credential mutation")


class EmptyVault:
    def __init__(self, *_args, **_kwargs):
        COUNTS["vault_stubs"] += 1

    def load(self):
        COUNTS["empty_vault_reads"] += 1
        return deepcopy({"profiles": {}})

    save = blocked("vault mutation")


class NoHttpClient:
    """Constructing the ChatGPT page must not read HTTP environment/config."""
    def __init__(self, *_args, **_kwargs):
        pass

    request = get = post = send = stream = blocked("HTTP request")

    def close(self):
        pass


class NoTranslation:
    def is_available(self):
        return False

    translate = blocked("offline model inference")


def format_demo_time(value):
    local = value.astimezone(DEMO_ZONE)
    return f"{local:%d %b %Y} · {local:%H:%M} · UTC+11 (UTC+11:00)"


def generate():
    # Always use a dedicated offscreen process, regardless of caller settings.
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = "1"
    os.environ["QT_FONT_DPI"] = "96"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))

    import httpx
    import keyring
    from PySide6.QtCore import QThread, QTimer, Qt
    from PySide6.QtGui import QFont, QFontDatabase, QRawFont
    from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

    if QApplication.instance() is not None:
        raise RuntimeError("Run this renderer in its own Python process.")

    connect = sqlite3.connect

    def memory_connection(database, *args, **kwargs):
        if database != ":memory:":
            return blocked("non-memory database")()
        COUNTS["memory_databases"] += 1
        return connect(database, *args, **kwargs)

    def no_timer_start(*_args, **_kwargs):
        COUNTS["disabled_timer_starts"] += 1

    with ExitStack() as isolation:
        # These low-level guards precede every app import. Missed injection
        # cannot fall back to the real keyring, database, network or worker.
        for name in ("get_keyring", "get_password", "set_password", "delete_password"):
            isolation.enter_context(patch.object(keyring, name, blocked("OS keyring")))
        isolation.enter_context(patch.object(sqlite3, "connect", memory_connection))
        isolation.enter_context(patch.object(socket.socket, "connect", blocked("socket connection")))
        isolation.enter_context(patch.object(socket.socket, "connect_ex", blocked("socket connection")))
        isolation.enter_context(patch.object(socket, "create_connection", blocked("socket connection")))
        isolation.enter_context(patch.object(socket, "getaddrinfo", blocked("DNS lookup")))
        for cls in (httpx.Client, httpx.AsyncClient):
            for name in ("request", "send"):
                isolation.enter_context(patch.object(cls, name, blocked("HTTP request")))
        isolation.enter_context(patch.object(httpx, "Client", NoHttpClient))
        isolation.enter_context(patch.object(QThread, "start", blocked("background thread")))
        isolation.enter_context(patch.object(QTimer, "start", no_timer_start))
        install_file_guard()

        # Confirm representative guards raise before any app widget exists.
        probe(keyring.get_keyring)
        probe(lambda: sqlite3.connect("not-opened.sqlite3"))
        probe(lambda: socket.create_connection(("example.invalid", 443)))
        probe(lambda: httpx.Client().get("https://example.invalid"))
        VIOLATIONS.clear()

        from app.services import credential_store, chatgpt_vault, offline_translation
        from app.services.deadline_service import DeadlineService
        from app.services.notification_service import ReminderPreferences
        from app.storage import database
        from app.storage.session_cache import session_cache
        from app.ui import main_window
        from app.ui.courses import CoursesPage
        from app.ui.dashboard import DashboardPage
        from app.ui.deadline_center import DeadlineCenterPage
        from app.ui.cards import CardList
        from app.models.assignment import Assignment
        from app.models.course import Course

        class FixedDeadlines(DeadlineService):
            def __init__(self, *_args, **_kwargs):
                self.now = DEMO_NOW

            @staticmethod
            def to_local(value):
                return None if value is None else value.astimezone(DEMO_ZONE)

        class NoNotifications:
            preferences = ReminderPreferences(enabled=False)

            def __init__(self, *_args, **_kwargs):
                pass

            def check_assignments(self, *_args, **_kwargs):
                COUNTS["suppressed_reminder_checks"] += 1

            test_notification = blocked("notification")

        databases = []
        database_init = database.SQLiteDatabase.__init__

        def memory_database_init(self, path=None):
            if path is not None and str(path) != ":memory:":
                return blocked("non-memory database")()
            database_init(self, ":memory:")
            databases.append(self)

        def memory_open(path):
            if str(path) != ":memory:":
                return blocked("non-memory database")()
            return path, sqlite3.connect(":memory:", timeout=5.0, check_same_thread=False)

        isolation.enter_context(patch.object(database.SQLiteDatabase, "__init__", memory_database_init))
        isolation.enter_context(patch.object(database.SQLiteDatabase, "_open", staticmethod(memory_open)))
        isolation.enter_context(patch.object(database, "default_database_path", blocked("user database path")))
        original_credentials = credential_store.CredentialStore
        original_vault = chatgpt_vault.ChatGPTVault
        isolation.enter_context(patch.object(original_credentials, "__init__", blocked("real credential store")))
        isolation.enter_context(patch.object(original_vault, "__init__", blocked("real credential vault")))
        isolation.enter_context(patch.object(offline_translation, "_runtime", blocked("offline model load")))
        translator = NoTranslation()

        # Replace every already-imported alias and the defining module too,
        # so a later import sees the same safe stub rather than a real store.
        for module in tuple(sys.modules.values()):
            if not module or not getattr(module, "__name__", "").startswith("app."):
                continue
            for name, value in tuple(vars(module).items()):
                replacement = None
                if value is original_credentials:
                    replacement = EmptyCredentials
                elif value is original_vault:
                    replacement = EmptyVault
                elif value is DeadlineService:
                    replacement = FixedDeadlines
                elif name == "offline_translation" and isinstance(value, offline_translation.OfflineTranslationService):
                    replacement = translator
                elif name == "format_local_time":
                    replacement = format_demo_time
                if replacement is not None:
                    isolation.enter_context(patch.object(module, name, replacement))
        isolation.enter_context(patch.object(main_window, "NotificationService", NoNotifications))
        isolation.enter_context(patch.object(main_window.MainWindow, "_refresh_current_page", lambda *_: None))

        app = QApplication([])
        app.setStyle("Fusion")
        app.setApplicationName("Student Assistant — fictional documentation demo")
        # Windows' generic offscreen backend has no automatic font discovery.
        # Load only public, installed system fonts; never user font directories.
        if sys.platform == "win32":
            fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
            for name in ("NotoSansSC-VF.ttf", "msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
                QFontDatabase.addApplicationFont(str(fonts / name))
        raw_font = QRawFont.fromFont(QFont("Microsoft YaHei UI", 14))
        if not raw_font.isValid() or not all(raw_font.supportsCharacter(c) for c in "A中"):
            raise RuntimeError("Readable English/Chinese system fonts are required.")
        session_cache.clear()
        window = None
        try:
            window = main_window.MainWindow()
            window._reminder_timer.stop()
            for timer in window.findChildren(QTimer):
                timer.stop()
            courses = [
                Course(id=1001, course_code="DEMO1001", name="Everyday Data Stories"),
                Course(id=1002, course_code="DEMO1002", name="Designing Helpful Interfaces"),
                Course(id=1003, course_code="DEMO1003", name="Creative Problem Solving"),
            ]

            def task(identity, course, title, due, state="unsubmitted", **extra):
                return Assignment(
                    assignment_id=identity, course_id=course.id,
                    course_name=course.name, course_code=course.course_code,
                    title=title, due_at=due, submission_status=state, **extra,
                )

            tasks = [
                task(1, courses[0], "Mini project: a pocket weather chart", DEMO_NOW + timedelta(hours=3)),
                task(2, courses[1], "Practice quiz: friendly navigation", DEMO_NOW + timedelta(days=1, hours=2),
                     quiz_id=102, quiz_engine="classic"),
                task(3, courses[2], "Workshop: turn a sketch into a plan", DEMO_NOW + timedelta(days=3)),
                task(4, courses[0], "Reflection: lessons from a tiny dataset", DEMO_NOW + timedelta(days=6)),
                task(5, courses[2], "Optional reading: a curiosity notebook", None, state=None),
                task(6, courses[1], "Warm-up: three playful color palettes", DEMO_NOW - timedelta(days=1)),
                task(7, courses[0], "Practice task: name five fictional objects", DEMO_NOW - timedelta(days=2), state="submitted"),
            ]
            session_cache.set("courses", courses)
            session_cache.set("assignments", tasks)

            # This marker is a Qt widget inside the rendered app, not a pixel
            # edit or a crop of the user's desktop or existing app session.
            content = window.takeCentralWidget()
            wrapper = QWidget()
            layout = QVBoxLayout(wrapper)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            marker = QLabel("DEMO · Fictional data  |  16 Nov 2026, 10:00 (UTC+11)")
            marker.setTextFormat(Qt.TextFormat.PlainText)
            marker.setStyleSheet("background:#FFF0CF;color:#5B421D;font-size:14px;font-weight:600;padding:10px 18px;")
            layout.addWidget(marker)
            layout.addWidget(content, 1)
            window.setCentralWidget(wrapper)
            window.setFixedSize(*SIZE)
            window.show()

            pages = {type(window.pages.widget(i)): (i, window.pages.widget(i))
                     for i in range(window.pages.count())}
            pages[DashboardPage][1]._show(tasks)
            pages[CoursesPage][1]._show_courses(courses)
            pages[DeadlineCenterPage][1]._show(tasks)
            rendered = []
            for filename, page_type in zip(FILENAMES, (DashboardPage, CoursesPage, DeadlineCenterPage)):
                index, page = pages[page_type]
                window.navigation.setCurrentRow(index)
                app.processEvents()
                for listing in page.findChildren(CardList):
                    listing._resize_cards()
                    listing.doItemsLayout()
                app.processEvents()
                if not marker.isVisible():
                    raise RuntimeError("The fictional-data marker is not visible.")
                image = window.grab()
                if image.isNull() or image.width() != SIZE[0] or image.height() != SIZE[1]:
                    raise RuntimeError("Unexpected offscreen image geometry.")
                rendered.append((filename, image))

            if VIOLATIONS:
                raise RuntimeError("Demo isolation observed a forbidden operation.")
            if not all(str(item.path) == ":memory:" for item in databases):
                raise RuntimeError("Demo database isolation was lost.")
            if not (COUNTS["memory_databases"] and COUNTS["credential_stubs"]
                    and COUNTS["empty_credential_checks"] and COUNTS["empty_vault_reads"]):
                raise RuntimeError("Required empty stores were not exercised.")
            if any(window.findChildren(QThread)) or any(timer.isActive() for timer in window.findChildren(QTimer)):
                raise RuntimeError("A background worker or timer remains active.")

            OUTPUT.mkdir(parents=True, exist_ok=True)
            for filename, image in rendered:
                if not image.save(str(OUTPUT / filename), "PNG"):
                    raise RuntimeError("Could not save a demo screenshot.")
                print(f"Rendered docs/images/{filename}: {SIZE[0]} x {SIZE[1]}")
            print("Isolation verified: fictional fixtures, empty credentials/vault, memory-only SQLite; no network, worker, notification or model access.")
        finally:
            if window is not None:
                window.close()
            for item in databases:
                item.close()
            session_cache.clear()
            app.quit()


if __name__ == "__main__":
    generate()
