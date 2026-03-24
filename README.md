# SetupTTS

Convert any text to natural-sounding audio using Microsoft Neural voices — packaged as a native desktop app for macOS and Windows.

No Python. No terminal. No setup required for end users.

---

## Features

- **100+ voices** across 40+ languages — the same voices used in Microsoft Edge's "Read Aloud"
- **Real-time progress** — word-by-word progress bar as audio generates
- **Voice search & filter** — search by name or filter by language
- **Adjustable rate** — slow down or speed up playback
- **Preview** — listen before saving
- **Export** — save as MP3 to any folder
- **Job history** — recent generations saved locally (SQLite)
- **Dark theme** — clean, modern UI built with Qt

---

## Download

Grab the latest release from the [Releases page](../../releases).

| Platform | File |
|----------|------|
| macOS 12+ | `SetupTTS-macOS-1.0.0.zip` |
| Windows 10/11 (64-bit) | `SetupTTS-Windows-1.0.0.zip` |

### macOS

1. Download and extract `SetupTTS-macOS-1.0.0.zip`
2. Drag **SetupTTS.app** to your Applications folder (optional)
3. Double-click to open
4. **First launch only:** right-click → Open to bypass Gatekeeper (unsigned app — one-time step)

### Windows

1. Download and extract `SetupTTS-Windows-1.0.0.zip`
2. Double-click **SetupTTS.exe**
3. Done — no Python, no installer, no extra steps

---

## Requirements

- **Internet connection** — voices are streamed from Microsoft's Neural TTS service in real time
- macOS 12 Monterey or later
- Windows 10 or 11 (64-bit)

---

## Development

### Prerequisites

- Python 3.11 or 3.12
- pip

### Setup

```bash
git clone https://github.com/your-username/setuptts.git
cd setuptts
python3.12 -m venv .venv
source .venv/bin/activate   # macOS/Linux
# .venv\Scripts\activate    # Windows
pip install -r requirements.txt
```

### Run

```bash
python main.py
```

### Build release zips

**macOS** (run on a Mac):
```bash
pip install pyinstaller
./build_macos.sh
# → releases/SetupTTS-macOS-1.0.0.zip
```

**Windows** (run on Windows):
```powershell
pip install pyinstaller
.\build_windows.ps1
# → releases\SetupTTS-Windows-1.0.0.zip
```

**Via GitHub Actions** (both platforms, automated):
```bash
git tag v1.0.0 && git push origin v1.0.0
# Creates a GitHub Release with both zips attached
```

---

## Stack

| Layer | Technology |
|-------|-----------|
| UI | PySide6 (Qt 6) |
| TTS | edge-tts (Microsoft Neural TTS via WebSocket) |
| Networking | aiohttp |
| Storage | SQLite (WAL mode) |
| Packaging | PyInstaller |
| App data paths | platformdirs |

---

## Project structure

```
setuptts/
├── main.py                       ← PyInstaller entry point
├── setuptts.spec                 ← PyInstaller spec
├── requirements.txt
├── pyproject.toml
├── build_macos.sh                ← macOS release build
├── build_windows.ps1             ← Windows release build (PowerShell)
├── build_windows.bat             ← Windows release build (cmd.exe)
├── app/
│   ├── __init__.py               ← APP_NAME, APP_VERSION constants
│   ├── main.py                   ← QApplication setup
│   ├── config/settings.py        ← JSON settings persistence
│   ├── models/                   ← Voice, Job dataclasses
│   ├── services/
│   │   ├── tts_service.py        ← edge_tts wrapper
│   │   └── history_service.py    ← SQLite job history
│   ├── workers/
│   │   ├── tts_worker.py         ← QThread TTS generation
│   │   ├── preview_worker.py     ← QThread audio preview
│   │   ├── job_queue.py          ← Job queue (MAX_CONCURRENT=2)
│   │   └── voice_loader.py       ← QThread voice list loader
│   ├── ui/
│   │   ├── main_window.py        ← QMainWindow
│   │   ├── panels/
│   │   │   ├── input_panel.py    ← text editor, drag & drop
│   │   │   ├── output_panel.py   ← voice/rate/generate/export
│   │   │   └── history_panel.py  ← recent jobs table
│   │   └── dialogs/
│   │       ├── settings_dialog.py
│   │       └── about_dialog.py
│   ├── utils/
│   │   ├── paths.py              ← AppPaths, resource_path()
│   │   └── app_logging.py        ← rotating file logger
│   └── assets/
│       ├── styles/app.qss        ← Qt stylesheet (dark theme)
│       └── icons/                ← app.icns, app.ico, app.png
└── .github/workflows/build.yml   ← CI: macOS + Windows builds
```

---

## License

MIT
