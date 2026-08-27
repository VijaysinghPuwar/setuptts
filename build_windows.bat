@echo off
:: ─────────────────────────────────────────────────────────────────────────────
:: build_windows.bat — build the Windows portable release zip for SetupTTS
::
:: Usage (from the project root in a standard Command Prompt or PowerShell):
::   build_windows.bat
::
:: Requirements on the Windows build machine:
::   - Python 3.11 or 3.12 (64-bit) from python.org
::   - pip install -r requirements.txt pyinstaller
::
:: Output:
::   releases\SetupTTS-Windows-<version>.zip  (single self-contained EXE)
::
:: Uses setuptts_portable.spec (onefile). Users extract the zip and
:: double-click SetupTTS.exe — no folder structure required.
:: ─────────────────────────────────────────────────────────────────────────────

setlocal enabledelayedexpansion

set APP_NAME=SetupTTS
:: Single source of truth: app/__init__.py (keeps the zip name, the
:: EXE version and the About dialog from drifting apart).
set VERSION=
for /f "tokens=2 delims==" %%V in ('findstr /b "APP_VERSION" app\__init__.py') do (
    set VERSION=%%V
)
set VERSION=%VERSION: =%
set VERSION=%VERSION:"=%
if "%VERSION%"=="" (
    echo ERROR: could not read APP_VERSION from app/__init__.py
    exit /b 1
)
set PORT_SPEC=setuptts_portable.spec
set DIST_DIR=dist_portable
set EXE=%DIST_DIR%\%APP_NAME%.exe
set RELEASES_DIR=releases
set ZIP_NAME=%APP_NAME%-Windows-%VERSION%.zip
set ZIP_PATH=%RELEASES_DIR%\%ZIP_NAME%

echo === SetupTTS Windows Portable Build ===
echo Version  : %VERSION%
echo Output   : %ZIP_PATH%
echo.

:: 1. Clean previous build
echo [1/4] Cleaning previous build...
if exist build    rmdir /s /q build
if exist dist     rmdir /s /q dist
if exist %DIST_DIR% rmdir /s /q %DIST_DIR%
if exist "%ZIP_PATH%" del /f "%ZIP_PATH%"

:: 2. Build portable (onefile) with PyInstaller
echo [2/4] Running PyInstaller (onefile portable)...
python -m PyInstaller %PORT_SPEC% --noconfirm --distpath %DIST_DIR%
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
echo Users extract the zip and double-click SetupTTS.exe.
echo No Python, no pip, no installation required.
