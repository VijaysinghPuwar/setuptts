# ─────────────────────────────────────────────────────────────────────────────
# build_windows.ps1 — build the Windows release zip for SetupTTS
#
# Run this from the project root in PowerShell:
#   .\build_windows.ps1
#
# Requirements:
#   - Python 3.11 or 3.12 64-bit  (python.org — add to PATH during install)
#   - pip install -r requirements.txt pyinstaller
#
# Output:
#   releases\SetupTTS-Windows-1.0.0.zip   folder-based portable build
#                                          (extract → open SetupTTS folder →
#                                           double-click SetupTTS.exe)
#
# Note: The build uses PyInstaller onedir mode (a folder of files, not a single
# EXE). This eliminates the per-launch self-extraction overhead that onefile
# builds impose on Windows — no more 5-30 s cold-start delay.
# ─────────────────────────────────────────────────────────────────────────────
param(
    [string]$Version = "1.0.0"
)

$ErrorActionPreference = "Stop"
$AppName  = "SetupTTS"
$Spec     = "setuptts.spec"
$AppDir   = "dist\$AppName"
$Exe      = "$AppDir\$AppName.exe"
$ZipDir   = "releases"
$ZipName  = "$AppName-Windows-$Version.zip"
$ZipPath  = "$ZipDir\$ZipName"

Write-Host "=== SetupTTS Windows Release Build ===" -ForegroundColor Cyan
Write-Host "Version  : $Version"
Write-Host "Output   : $ZipPath"
Write-Host ""

# 1. Clean
Write-Host "[1/4] Cleaning previous build..." -ForegroundColor Yellow
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist
Remove-Item -Force -ErrorAction SilentlyContinue $ZipPath

# 2. Build
Write-Host "[2/4] Running PyInstaller..." -ForegroundColor Yellow
python -m PyInstaller $Spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# 3. Verify
Write-Host "[3/4] Verifying build..." -ForegroundColor Yellow
if (-not (Test-Path $Exe)) { throw "$Exe not found" }
$exeMB = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
$dirMB = [math]::Round((Get-ChildItem -Recurse $AppDir | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "       $Exe  ($exeMB MB)"
Write-Host "       $AppDir\  (total $dirMB MB)"

# 4. Zip the whole folder (not just the exe)
Write-Host "[4/4] Creating release zip..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $ZipDir | Out-Null
Compress-Archive -Force -Path $AppDir -DestinationPath $ZipPath
$zipMB = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
Write-Host "       $ZipPath  ($zipMB MB)"

# Verify zip
$tmpDir = New-Item -ItemType Directory -Path "$env:TEMP\st_verify_$((Get-Random))"
Expand-Archive -Path $ZipPath -DestinationPath $tmpDir.FullName
if (-not (Test-Path "$($tmpDir.FullName)\$AppName\$AppName.exe")) {
    Remove-Item -Recurse -Force $tmpDir.FullName
    throw "$AppName\$AppName.exe missing after extraction"
}
Remove-Item -Recurse -Force $tmpDir.FullName
Write-Host "       Zip verified — $AppName\$AppName.exe present after extraction"

Write-Host ""
Write-Host "=== Build complete ===" -ForegroundColor Green
Write-Host "Release: $ZipPath"
Write-Host ""
Write-Host "Distribution:"
Write-Host "  Users extract the zip — a '$AppName' folder appears."
Write-Host "  Open that folder and double-click $AppName.exe."
Write-Host "  No Python, pip, or installation required."
