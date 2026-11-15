"""Branding works in source/frozen launches without relaxing data isolation."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QIcon, QPixmap

from app import main
from app.ui import app_icon
from app.ui.main_window import MainWindow
from scripts.security_audit import scan_packaging_manifest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_source_icon_path_is_independent_of_current_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app_icon.sys, "frozen", False, raising=False)
    monkeypatch.setattr(app_icon.sys, "_MEIPASS", str(tmp_path / "unrelated"), raising=False)
    assert app_icon.application_icon_path() == PROJECT_ROOT / "app" / "assets" / app_icon.ICON_FILENAME


def test_frozen_icon_path_uses_bundle_resource_root(monkeypatch, tmp_path):
    monkeypatch.setattr(app_icon.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_icon.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert app_icon.application_icon_path() == tmp_path / "app" / "assets" / app_icon.ICON_FILENAME


def test_windows_app_identity_is_set_for_taskbar(monkeypatch):
    import ctypes

    calls = []
    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(
        shell32=SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=calls.append)
    ), raising=False)
    main.configure_windows_app_id()
    assert calls == ["StudentAssistant.Desktop"]


def test_windows_identity_failure_does_not_prevent_startup(monkeypatch):
    import ctypes

    def unavailable(_identity):
        raise OSError("Desktop integration unavailable")

    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(
        shell32=SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=unavailable)
    ), raising=False)
    main.configure_windows_app_id()


def test_non_windows_does_not_access_windows_shell(monkeypatch):
    import ctypes

    class UnavailableShell:
        @property
        def shell32(self):
            raise AssertionError("Windows shell must not be accessed")

    monkeypatch.setattr(main.sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "windll", UnavailableShell(), raising=False)
    main.configure_windows_app_id()


def test_main_sets_application_icon_before_showing_window(monkeypatch):
    calls = []
    expected_icon = object()

    class Application:
        def __init__(self, argv):
            calls.append("application")

        def setApplicationName(self, name):
            calls.append(("name", name))

        def setWindowIcon(self, icon):
            assert icon is expected_icon
            calls.append("icon")

        def exec(self):
            calls.append("exec")
            return 23

    class Window:
        def __init__(self):
            calls.append("window")

        def show(self):
            calls.append("show")

    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(main, "configure_windows_app_id", lambda: calls.append("identity"))
    monkeypatch.setattr(main, "application_icon", lambda: expected_icon)
    monkeypatch.setattr(main, "QApplication", Application)
    monkeypatch.setattr(main, "MainWindow", Window)
    assert main.main() == 23
    assert calls.index("identity") < calls.index("application")
    assert calls.index("icon") < calls.index("window") < calls.index("show")


def test_main_window_explicitly_uses_app_icon(qtbot, monkeypatch):
    pixmap = QPixmap(32, 32)
    pixmap.fill()
    expected_icon = QIcon(pixmap)
    monkeypatch.setattr("app.ui.main_window.application_icon", lambda: expected_icon)
    window = MainWindow()
    qtbot.addWidget(window)
    assert window.windowIcon().cacheKey() == expected_icon.cacheKey()


def test_versioned_icon_is_valid_in_all_windows_sizes(qapp):
    icon = app_icon.application_icon()
    assert not icon.isNull()
    assert {(size.width(), size.height()) for size in icon.availableSizes()} >= {
        (16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256),
    }
    assert not icon.pixmap(16, 16).isNull()
    assert not icon.pixmap(256, 256).isNull()


def test_manifest_embeds_same_versioned_icon_in_executable():
    tree = ast.parse((PROJECT_ROOT / "StudentAssistant.spec").read_text(encoding="utf-8"))
    exe = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Name) and node.func.id == "EXE")
    icon = next(keyword.value for keyword in exe.keywords if keyword.arg == "icon")
    assert app_icon.ICON_FILENAME in ast.unparse(icon)
    assert scan_packaging_manifest(PROJECT_ROOT) == []


@pytest.mark.parametrize("datas", [
    "[]",
    '[(str(PROJECT_ROOT / "app" / "assets" / "student-assistant-butter-v1.ico"), "app/assets")]',
])
def test_packaging_audit_allows_only_explicit_public_icon_or_no_data(tmp_path, datas):
    spec = tmp_path / "StudentAssistant.spec"
    spec.write_text(f"a = Analysis([], datas={datas})", encoding="utf-8")
    assert scan_packaging_manifest(tmp_path) == []


@pytest.mark.parametrize("datas, later", [
    ('[(str(PROJECT_ROOT / "app" / "assets"), "app/assets")]', ""),
    ('[(str(PROJECT_ROOT / "app" / "assets" / "*.ico"), "app/assets")]', ""),
    ('[(str(PROJECT_ROOT / "private.json"), ".")]', ""),
    ('[(str(PROJECT_ROOT / "app" / "assets" / "student-assistant-butter-v1.ico"), ".")]', ""),
    ("collect_data_files('app')", ""),
    ("package_assets", ""),
    ("[]", "\na.datas += extra_files"),
    ("[]", "\na.datas.extend(extra_files)"),
    ("[]", "\na.datas = extra_files"),
])
def test_packaging_audit_rejects_directory_wildcard_unknown_or_late_data(tmp_path, datas, later):
    spec = tmp_path / "StudentAssistant.spec"
    spec.write_text(f"a = Analysis([], datas={datas}){later}", encoding="utf-8")
    assert "package-data-must-be-explicit-public-assets" in {
        finding.rule for finding in scan_packaging_manifest(tmp_path)
    }
