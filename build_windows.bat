@echo off
:: ─────────────────────────────────────────────────────────────────────────────
:: build_windows.bat — build the Windows release zip for SetupTTS
::
:: Usage (from the project root in a standard Command Prompt or PowerShell):
::   build_windows.bat
::
:: Requirements on the Windows build machine:
::   - Python 3.11 or 3.12 (64-bit) from python.org
::   - pip install -r requirements.txt pyinstaller
::
:: Output:
::   releases\SetupTTS-Windows-1.0.0.zip  (single self-contained .exe)
:: ─────────────────────────────────────────────────────────────────────────────

setlocal enabledelayedexpansion

set APP_NAME=SetupTTS
set VERSION=1.0.0
set SPEC=setuptts.spec
set EXE=dist\%APP_NAME%.exe
set RELEASES_DIR=releases
set ZIP_NAME=%APP_NAME%-Windows-%VERSION%.zip
set ZIP_PATH=%RELEASES_DIR%\%ZIP_NAME%

echo === SetupTTS Windows Release Build ===
echo Version  : %VERSION%
echo Output   : %ZIP_PATH%
echo.

:: 1. Clean previous build
echo [1/4] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
if exist "%ZIP_PATH%" del /f "%ZIP_PATH%"

:: 2. Build with PyInstaller
echo [2/4] Running PyInstaller...
python -m PyInstaller %SPEC% --noconfirm
if %errorlevel% neq 0 (
    echo ERROR: PyInstaller failed
    exit /b 1
)

:: 3. Verify exe
echo [3/4] Verifying build...
if not exist "%EXE%" (
    echo ERROR: %EXE% not found after build
    exit /b 1
)
echo        %EXE% found OK

:: 4. Create release zip using PowerShell
echo [4/4] Creating release zip...
if not exist "%RELEASES_DIR%" mkdir "%RELEASES_DIR%"
powershell -NoProfile -Command ^
    "Compress-Archive -Force -Path '%EXE%' -DestinationPath '%ZIP_PATH%'"
if %errorlevel% neq 0 (
    echo ERROR: Failed to create zip
    exit /b 1
)

echo.
echo === Build complete ===
echo Release: %ZIP_PATH%
echo.
echo Users extract the zip and double-click SetupTTS.exe
echo No Python, no pip, no installation required.
