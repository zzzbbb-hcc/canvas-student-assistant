# -*- mode: python ; coding: utf-8 -*-
"""Reproducible Windows package definition for Student Assistant.

Only Python application code, the explicit public app icon, and imported
third-party packages are collected.
User data, credentials, caches, databases, logs, screenshots, tests and local
configuration files are intentionally not added as data files.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


PROJECT_ROOT = Path(SPECPATH)

# Keyring discovers its backend dynamically. Collect backend modules so the
# Windows Credential Manager backend remains available in the frozen app.
HIDDEN_IMPORTS = collect_submodules("keyring.backends")

a = Analysis(
    [str(PROJECT_ROOT / "app" / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        (str(PROJECT_ROOT / "app" / "assets" / "student-assistant-butter-v1.ico"), "app/assets"),
    ],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "pytestqt", "unittest"],
    noarchive=False,
    optimize=1,
)

# Qt 6 uses the Windows system ICU compatibility library.  The development
# environment can also expose Poppler's versioned ICU DLLs on PATH; PyInstaller
# may mistake those incompatible files for Qt dependencies, causing QtWidgets
# to fail at startup with WinError 127.  Leave them out of the bundle so Qt can
# resolve the correct system library.
_INCOMPATIBLE_ICU_DLLS = {"icuuc.dll", "icudt78.dll"}
a.binaries = [
    entry
    for entry in a.binaries
    if Path(entry[0]).name.casefold() not in _INCOMPATIBLE_ICU_DLLS
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StudentAssistant",
    icon=str(PROJECT_ROOT / "app" / "assets" / "student-assistant-butter-v1.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=False,
    uac_uiaccess=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="StudentAssistant",
)
