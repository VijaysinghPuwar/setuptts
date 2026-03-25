# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Windows PORTABLE build of SetupTTS.

Why a separate spec?
--------------------
The main setuptts.spec produces an onedir build (a folder of files) which
is correct for the guided installer — Inno Setup copies the whole folder to
Program Files and users launch via Start Menu / desktop shortcut.

For the PORTABLE release, onedir creates a serious UX problem:
  - The zip contains SetupTTS.exe + _internal/ (hundreds of DLLs/data).
  - Windows Explorer lets users "run" files from inside a zip without fully
    extracting — so the EXE runs but _internal/ stays in the zip, causing:
        Failed to load Python DLL ... python312.dll
        LoadLibrary: The specified module could not be found
  - Users who copy only the EXE (not the whole folder) hit the same error.

This spec builds a ONEFILE EXE — the entire runtime is embedded inside the
single EXE, which self-extracts to %TEMP% at launch.  Users can run it from
anywhere (double-click from the zip, the desktop, a USB drive, …) with zero
folder-management overhead.

Usage (CI / local build):
    python -m PyInstaller setuptts_portable.spec --noconfirm --distpath dist_portable

Output:
    dist_portable/SetupTTS.exe   ← fully self-contained; goes into the portable zip

This spec is Windows-only.  Do not run it on macOS.
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

if sys.platform != "win32":
    raise SystemExit("setuptts_portable.spec is for Windows only. "
                     "Use setuptts.spec on macOS.")

ROOT = Path(SPECPATH)

# ── Collect data files ─────────────────────────────────────────────── #
edge_tts_datas = collect_data_files("edge_tts")
certifi_datas  = collect_data_files("certifi")

# ── Hidden imports (identical to setuptts.spec) ───────────────────── #
_hidden = [
    *collect_submodules("edge_tts"),
    *collect_submodules("aiohttp"),
    "aiohttp",
    "asyncio",
    "typing_extensions",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "platformdirs",
    "ssl",
    "certifi",
    "ctypes",
    "threading",
    "tempfile",
    # Windows async I/O
    "ctypes.windll",
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

_ico = ROOT / "app" / "assets" / "icons" / "app.ico"

# ── Windows onefile EXE ────────────────────────────────────────────── #
# All binaries, data, and the Python runtime are embedded inside this
# single EXE.  On launch, the bootloader extracts them to %TEMP% and
# then starts the app.  No external folder required.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="SetupTTS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_ico) if _ico.exists() else None,
    version_file=None,
)
