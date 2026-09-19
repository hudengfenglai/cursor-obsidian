# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for cursor-sidecar Windows x64 onefile helper."""

from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent / "cursor-sidecar"
MAIN = ROOT / "main.py"

a = Analysis(
    [str(MAIN)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "win32api",
        "win32con",
        "win32gui",
        "win32process",
        "win32event",
        "pywintypes",
        "psutil",
                "context_follow",
                "context_sync",
                "runtime_paths",
                "agents_auto",
                "cursor_windows",
                "native_embed",
                "pane_geometry",
                "embedded_window",
                "editor_bridge",
                "win_events",
                "geometry",
                "window",
            ],
            hookspath=[],
            hooksconfig={},
            runtime_hooks=[],
            excludes=[
                "pytest",
                "_pytest",
                "py",
                "pluggy",
                "iniconfig",
                "acceptance_run",
                "acceptance_live_follow",
                "acceptance_context_bridge",
                "acceptance_packaged",
                "acceptance_embedded_pane",
                "acceptance_native_child",
                "tests",
            ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="cursor-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
