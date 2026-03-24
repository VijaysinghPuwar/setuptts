# SetupTTS — Build & Distribution Guide

## What this is

SetupTTS converts text to natural-sounding audio using Microsoft's neural voices.
It is packaged as a native desktop application — no Python, no terminal, no setup required for end users.

---

## For end users

| Platform | What to share                  | What users do                              |
|----------|--------------------------------|--------------------------------------------|
| macOS    | `SetupTTS-macOS-1.0.0.zip`    | Extract zip, double-click SetupTTS.app     |
| Windows  | `SetupTTS-Windows-1.0.0.zip`  | Extract zip, double-click SetupTTS.exe     |

No Python. No pip. No terminal. No extra steps. The app handles everything.

---

## For developers: getting started

### 1. Clone and create a virtual environment

```bash
git clone https://github.com/your-username/setuptts.git
cd setuptts
python3.12 -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
pip install pyinstaller          # only needed for building
```

### 3. Run in development

```bash
python main.py
```

---

## Building distributable packages

### macOS → .app + zip

**Prerequisites (once):**
```bash
pip install pyinstaller
```

**Build:**
```bash
chmod +x build_macos.sh
./build_macos.sh
```

**Output:**
- `dist/SetupTTS.app` — the app bundle
- `releases/SetupTTS-macOS-1.0.0.zip` — distributable zip

**Share:** Send `SetupTTS-macOS-1.0.0.zip` to users. They extract and double-click.

---

### Windows → .exe + zip

**Prerequisites (once):**
```powershell
pip install pyinstaller
```

**Build (PowerShell):**
```powershell
.\build_windows.ps1
```

**Build (cmd.exe):**
```bat
build_windows.bat
```

**Output:**
- `dist/SetupTTS.exe` — standalone executable (no install needed)
- `releases\SetupTTS-Windows-1.0.0.zip` — distributable zip

**Share:** Send `SetupTTS-Windows-1.0.0.zip` to users. They extract and double-click.

---

## CI/CD (GitHub Actions)

Push a version tag to trigger automated builds for both platforms:

```bash
git tag v1.0.0
git push origin v1.0.0
```

The workflow (`.github/workflows/build.yml`) will:
1. Build on a real macOS runner → produces `SetupTTS-macOS-1.0.0.zip`
2. Build on a real Windows runner → produces `SetupTTS-Windows-1.0.0.zip`
3. Attach both to a GitHub Release automatically

You can also trigger a manual build from the GitHub Actions UI using `workflow_dispatch`.

---

## Code signing

### macOS
Unsigned app — users see a Gatekeeper warning on first launch. To bypass:
- **Users:** right-click → Open on first launch only. Subsequent launches work normally.
- **Developers (ad-hoc removal):** `xattr -cr /Applications/SetupTTS.app`
- **Proper signing:** set `CODESIGN_IDENTITY` before running `build_macos.sh`

### Windows
Windows SmartScreen may flag unsigned executables. Options:
- EV code signing certificate (~$300/yr from DigiCert, Sectigo, etc.)
- Azure Trusted Signing (cheaper, Microsoft-backed)

---

## Project structure

```
setuptts/
├── main.py                       ← entry point (PyInstaller target)
├── requirements.txt
├── pyproject.toml
├── setuptts.spec                 ← PyInstaller spec
├── build_macos.sh
├── build_windows.ps1
├── build_windows.bat
├── app/
│   ├── __init__.py               ← APP_NAME, APP_VERSION constants
│   ├── main.py                   ← QApplication setup, main()
│   ├── config/
│   │   └── settings.py           ← JSON settings persistence
│   ├── models/
│   │   ├── voice.py              ← Voice dataclass
│   │   └── job.py                ← Job dataclass + JobStatus enum
│   ├── services/
│   │   ├── tts_service.py        ← edge_tts wrapper
│   │   └── history_service.py    ← SQLite job history
│   ├── workers/
│   │   ├── tts_worker.py         ← QThread TTS generation
│   │   ├── preview_worker.py     ← QThread audio preview
│   │   ├── job_queue.py          ← Job queue (MAX_CONCURRENT=2)
│   │   └── voice_loader.py       ← QThread voice list loader
│   ├── ui/
│   │   ├── main_window.py        ← QMainWindow, layout, menus
│   │   ├── panels/
│   │   │   ├── input_panel.py    ← text editor, drag & drop
│   │   │   ├── output_panel.py   ← voice/rate/path/generate
│   │   │   └── history_panel.py  ← recent jobs table
│   │   └── dialogs/
│   │       ├── settings_dialog.py
│   │       └── about_dialog.py
│   ├── utils/
│   │   ├── paths.py              ← AppPaths, resource_path()
│   │   └── app_logging.py        ← rotating file logger
│   └── assets/
│       ├── styles/app.qss        ← full Qt stylesheet
│       └── icons/                ← app.icns, app.ico, app.png
└── .github/
    └── workflows/build.yml
```

---

## Architecture

### Why PySide6 instead of Tkinter?
| | Tkinter | PySide6 |
|---|---|---|
| Look & feel | Dated | Native on macOS and Windows |
| Drag & drop | Complex | Built-in QMimeData support |
| Background threads | Difficult | QThread + signals |
| Styling | Not possible | Full Qt Style Sheets |
| Packaging | Easy | Easy (PyInstaller) |

### How dependencies are hidden from users
1. `pip install` happens only on the developer's machine
2. PyInstaller copies all packages into `dist/`
3. The packaged app carries its own Python runtime
4. Users never see pip, requirements.txt, or a terminal

### Where user data lives
| Platform | Location |
|---|---|
| macOS | `~/Library/Application Support/SetupTTS/` |
| Windows | `%APPDATA%\SetupTTS\` |

Stored there: `settings.json`, `history.db`, `setuptts.log`
