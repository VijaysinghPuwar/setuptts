# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for SetupTTS.

Usage:
    pyinstaller setuptts.spec

Outputs (platform-dependent):
    macOS   → dist/SetupTTS.app         (DMG + zip made by CI / build_macos.sh)
    Windows → dist/SetupTTS/ (onedir)   (packaged by the Inno Setup installer;
              the portable single EXE comes from setuptts_portable.spec)

The version comes from app/__init__.py — nothing here needs editing on a bump.
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

# ── Version: single source of truth is app/__init__.py ──────────────── #
import re as _re
APP_VERSION = _re.search(
    r'^APP_VERSION\s*=\s*"([^"]+)"',
    (ROOT / "app" / "__init__.py").read_text(encoding="utf-8"),
    _re.M,
).group(1)


def _windows_version_file() -> str:
    """
    Write a VERSIONINFO resource so Explorer's Properties ▸ Details shows the
    version — otherwise every SetupTTS.exe looks identical, and an old copy
    launched from a stale shortcut can't be told apart.
    """
    parts = [int(p) for p in _re.findall(r"\d+", APP_VERSION)[:3]] + [0]
    while len(parts) < 4:
        parts.append(0)
    t = tuple(parts[:4])
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'SetupTTS'),
      StringStruct('FileDescription', 'SetupTTS'),
      StringStruct('FileVersion', '{APP_VERSION}'),
      StringStruct('InternalName', 'SetupTTS'),
      StringStruct('OriginalFilename', 'SetupTTS.exe'),
      StringStruct('ProductName', 'SetupTTS'),
      StringStruct('ProductVersion', '{APP_VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    path = ROOT / "build" / "file_version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


# ── Collect data files ─────────────────────────────────────────────── #
edge_tts_datas = collect_data_files("edge_tts")
certifi_datas  = collect_data_files("certifi")

# aiohttp hard dependencies — collect_all gets data, binaries (C extensions),
# AND hidden imports for each package.  This prevents "ModuleNotFoundError:
# No module named 'aiosignal'" crashes in packaged builds, where PyInstaller's
# static analysis sometimes misses packages that have C extension variants
# (multidict, frozenlist, yarl all have optional .pyd/.so accelerators).
_aio_d, _aio_b, _aio_h = collect_all("aiohttp")
_sig_d, _sig_b, _sig_h = collect_all("aiosignal")
_fzl_d, _fzl_b, _fzl_h = collect_all("frozenlist")
_mdi_d, _mdi_b, _mdi_h = collect_all("multidict")
_yrl_d, _yrl_b, _yrl_h = collect_all("yarl")

# ── Hidden imports ────────────────────────────────────────────────── #
# PyInstaller's static analysis misses dynamically-imported submodules.
# edge_tts uses aiohttp for all HTTP/WebSocket connections.
_hidden = [
    # edge_tts submodules
    *collect_submodules("edge_tts"),
    # aiohttp + all its dependencies (collected above via collect_all)
    *_aio_h, *_sig_h, *_fzl_h, *_mdi_h, *_yrl_h,
    "aiohttp", "aiosignal", "frozenlist", "multidict", "yarl",
    "attrs", "attr",
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
    "re",
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
    binaries=[
        # C extension binaries for aiohttp dependencies
        *_aio_b, *_sig_b, *_fzl_b, *_mdi_b, *_yrl_b,
    ],
    datas=[
        (str(ROOT / "app" / "assets"), "app/assets"),
        *edge_tts_datas,
        *certifi_datas,
        *_aio_d, *_sig_d, *_fzl_d, *_mdi_d, *_yrl_d,
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
        version=APP_VERSION,
        info_plist={
            "CFBundleName":              "SetupTTS",
            "CFBundleDisplayName":       "SetupTTS",
            "CFBundleVersion":           APP_VERSION,
            "CFBundleShortVersionString":APP_VERSION,
            "NSHighResolutionCapable":   True,
            "NSRequiresAquaSystemAppearance": False,
            "LSMinimumSystemVersion":    "15.0",   # PySide6 6.10+ (shiboken) needs macOS 15 — CI verifies against the bundled binaries
            "NSHumanReadableCopyright":  "© 2025–2026 SetupTTS",
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
        version=_windows_version_file(),
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
