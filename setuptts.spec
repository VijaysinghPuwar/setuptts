# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for SetupTTS.

Usage:
    pyinstaller setuptts.spec

Outputs (platform-dependent):
    macOS   → dist/SetupTTS.app    (zipped by build_macos.sh)
    Windows → dist/SetupTTS.exe    (zipped by build_windows.bat)
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

# ── Collect data files ─────────────────────────────────────────────── #
edge_tts_datas = collect_data_files("edge_tts")
certifi_datas  = collect_data_files("certifi")

# ── Hidden imports ────────────────────────────────────────────────── #
# PyInstaller's static analysis misses dynamically-imported submodules.
# edge_tts uses aiohttp for all HTTP/WebSocket connections.
_hidden = [
    # edge_tts submodules (collect_submodules catches __main__ etc.)
    *collect_submodules("edge_tts"),
    # aiohttp — edge_tts network backend (HTTP + WebSocket)
    *collect_submodules("aiohttp"),
    "aiohttp",
    # async runtime
    "asyncio",
    # typing helpers used by edge_tts
    "typing_extensions",
    # Qt essentials
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    # certs + paths
    "platformdirs",
    "ssl",
    "certifi",
    # preview playback helpers (safe on all platforms)
    "ctypes",
    "threading",
    "tempfile",
]

# Windows-only additions
if sys.platform == "win32":
    _hidden += [
        "ctypes.windll",
        # ProactorEventLoop for async I/O on Windows
        "asyncio.proactor_events",
        "asyncio.windows_events",
    ]

block_cipher = None

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "app" / "assets"), "app/assets"),
        *edge_tts_datas,
        *certifi_datas,
    ],
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Heavy PySide6 modules we never use
        "PySide6.QtWebEngine",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtMultimedia",
        "PySide6.QtBluetooth",
        "PySide6.QtNfc",
        "PySide6.QtLocation",
        "PySide6.QtPositioning",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "PySide6.QtTest",
        # Large data-science packages
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "PIL",
        "cv2",
        "tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_icns = ROOT / "app" / "assets" / "icons" / "app.icns"
_ico  = ROOT / "app" / "assets" / "icons" / "app.ico"

# ── macOS bundle ──────────────────────────────────────────────────── #
if sys.platform != "win32":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="SetupTTS",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(_icns) if _icns.exists() else None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="SetupTTS",
    )
    app = BUNDLE(
        coll,
        name="SetupTTS.app",
        icon=str(_icns) if _icns.exists() else None,
        bundle_identifier="com.setuptts.setuptts",
        version="1.0.0",
        info_plist={
            "CFBundleName":              "SetupTTS",
            "CFBundleDisplayName":       "SetupTTS",
            "CFBundleVersion":           "1.0.0",
            "CFBundleShortVersionString":"1.0.0",
            "NSHighResolutionCapable":   True,
            "NSRequiresAquaSystemAppearance": False,
            "LSMinimumSystemVersion":    "12.0",
            "NSHumanReadableCopyright":  "© 2025 SetupTTS",
        },
    )

# ── Windows onedir build ─────────────────────────────────────────── #
# onedir (folder) avoids the per-launch self-extraction that onefile
# requires — eliminating the 5-30 s startup penalty and Windows
# Defender scanning delay on every run.
# Produces: dist/SetupTTS/SetupTTS.exe  (plus supporting DLLs/data)
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,   # binaries go in the COLLECT step
        name="SetupTTS",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,           # do not require UPX to be installed on CI
        console=False,
        disable_windowed_traceback=False,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(_ico) if _ico.exists() else None,
        version_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="SetupTTS",
    )
