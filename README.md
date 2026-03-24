# SetupTTS

Convert any text to natural-sounding audio using Microsoft Neural voices.

No Python. No terminal. No setup. Just download and run.

---

## Download

**→ [Go to Releases to download SetupTTS](../../releases/latest)**

| Platform | File | Notes |
|----------|------|-------|
| macOS | `SetupTTS-macOS.dmg` | Drag-to-Applications |
| macOS | `SetupTTS-macOS.zip` | Fallback zip |
| Windows 10/11 | `SetupTTS-Windows-Installer.exe` | Guided installer |
| Windows 10/11 | `SetupTTS-Windows-Portable.zip` | No install needed |

---

## Installing on macOS

**Option 1 — DMG (recommended)**

1. Download `SetupTTS-macOS.dmg`
2. Open it — drag **SetupTTS** into the **Applications** folder
3. Eject the DMG
4. Open SetupTTS from your Applications folder

**Option 2 — Zip**

1. Download `SetupTTS-macOS.zip` and unzip it
2. Double-click **SetupTTS.app**

> **First launch note:** macOS may show a security prompt because the app is not signed with an Apple certificate.
> If it says "cannot be opened because the developer cannot be verified" — right-click the app → **Open** → **Open**. You only need to do this once.

---

## Installing on Windows

**Option 1 — Installer (recommended)**

1. Download `SetupTTS-Windows-Installer.exe`
2. Double-click it and follow the wizard (takes about 10 seconds)
3. Launch SetupTTS from the **Start Menu** or your **desktop shortcut**

**Option 2 — Portable (no install)**

1. Download `SetupTTS-Windows-Portable.zip` and unzip it anywhere
2. Double-click **SetupTTS.exe**

---

## Using SetupTTS

1. Type or paste your text into the editor
2. Choose a voice and speed
3. Click **Generate & Export MP3**
4. Pick where to save the file

An internet connection is required — voices are streamed in real time from Microsoft's Neural TTS service (the same voices used in Microsoft Edge's "Read Aloud" feature).

---

## Features

- 300+ voices across 70+ languages and regions
- Word-by-word progress as audio generates
- Voice search and language filter
- Adjustable speed (0.5× to 2×)
- Preview audio before saving
- Export as MP3
- Job history (recent generations saved locally)
- Dark theme

---

## Requirements

- macOS 12 Monterey or later
- Windows 10 or 11 (64-bit)
- Internet connection

---

---

## For developers

<details>
<summary>Click to expand developer setup instructions</summary>

### Prerequisites

- Python 3.11 or 3.12
- pip

### Local setup

```bash
git clone https://github.com/VijaysinghPuwar/setuptts.git
cd setuptts
python3.12 -m venv .venv
source .venv/bin/activate   # macOS/Linux
# .venv\Scripts\activate    # Windows
pip install -r requirements.txt
```

### Run from source

```bash
python main.py
```

### Build release packages

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

### Trigger automated release (both platforms via GitHub Actions)

```bash
git tag v1.0.0 && git push origin v1.0.0
```

GitHub Actions builds both macOS and Windows packages and publishes them as a GitHub Release with four artifacts attached:
- `SetupTTS-macOS.dmg`
- `SetupTTS-macOS.zip`
- `SetupTTS-Windows-Installer.exe`
- `SetupTTS-Windows-Portable.zip`

### Stack

| Layer | Technology |
|-------|-----------|
| UI | PySide6 (Qt 6) |
| TTS | edge-tts (Microsoft Neural TTS) |
| Networking | aiohttp |
| Storage | SQLite WAL |
| Packaging | PyInstaller |

### Project structure

```
setuptts/
├── main.py                    ← PyInstaller entry point
├── setuptts.spec              ← PyInstaller spec
├── requirements.txt
├── build_macos.sh             ← macOS release build script
├── build_windows.ps1          ← Windows release build (PowerShell)
├── build_windows.bat          ← Windows release build (cmd.exe)
├── installers/
│   └── windows.iss            ← Inno Setup installer script
├── app/
│   ├── __init__.py            ← APP_NAME, APP_VERSION
│   ├── main.py                ← QApplication setup
│   ├── config/settings.py
│   ├── models/
│   ├── services/
│   ├── workers/
│   ├── ui/
│   └── assets/
│       ├── styles/app.qss
│       └── icons/
└── .github/workflows/build.yml
```

</details>
