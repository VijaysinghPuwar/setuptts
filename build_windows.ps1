# ─────────────────────────────────────────────────────────────────────────────
# build_windows.ps1 — build the Windows portable release zip for SetupTTS
#
# Run this from the project root in PowerShell:
#   .\build_windows.ps1
#
# Requirements:
#   - Python 3.11 or 3.12 64-bit  (python.org — add to PATH during install)
#   - pip install -r requirements.txt pyinstaller
#
# Output:
#   releases\SetupTTS-Windows-1.4.1.zip   single self-contained EXE
#
# This script builds the PORTABLE release using setuptts_portable.spec
# (onefile mode).  The resulting EXE is fully self-contained — users
# extract the zip and double-click SetupTTS.exe.  No folder management needed.
#
# For the guided installer, use GitHub Actions (build.yml) which also runs
# the onedir installer build via Inno Setup.
# ─────────────────────────────────────────────────────────────────────────────
param(
    [string]$Version = "1.4.1"
)

$ErrorActionPreference = "Stop"
$AppName   = "SetupTTS"
$PortSpec  = "setuptts_portable.spec"
$DistDir   = "dist_portable"
$Exe       = "$DistDir\$AppName.exe"
$ZipDir    = "releases"
$ZipName   = "$AppName-Windows-$Version.zip"
$ZipPath   = "$ZipDir\$ZipName"

Write-Host "=== SetupTTS Windows Portable Build ===" -ForegroundColor Cyan
Write-Host "Version  : $Version"
Write-Host "Output   : $ZipPath"
Write-Host ""

# 1. Clean
Write-Host "[1/4] Cleaning previous build..." -ForegroundColor Yellow
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist, $DistDir
Remove-Item -Force -ErrorAction SilentlyContinue $ZipPath

# 2. Build portable (onefile)
Write-Host "[2/4] Running PyInstaller (onefile portable)..." -ForegroundColor Yellow
python -m PyInstaller $PortSpec --noconfirm --distpath $DistDir
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# 3. Verify
Write-Host "[3/4] Verifying build..." -ForegroundColor Yellow
if (-not (Test-Path $Exe)) { throw "$Exe not found" }
$exeMB = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
Write-Host "       $Exe  ($exeMB MB)"

# 4. Zip the single EXE
Write-Host "[4/4] Creating release zip..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $ZipDir | Out-Null
Compress-Archive -Force -Path $Exe -DestinationPath $ZipPath
$zipMB = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
Write-Host "       $ZipPath  ($zipMB MB)"

# Verify zip
$tmpDir = New-Item -ItemType Directory -Path "$env:TEMP\st_verify_$((Get-Random))"
Expand-Archive -Path $ZipPath -DestinationPath $tmpDir.FullName
if (-not (Test-Path "$($tmpDir.FullName)\$AppName.exe")) {
    Remove-Item -Recurse -Force $tmpDir.FullName
    throw "$AppName.exe missing after extraction"
}
Remove-Item -Recurse -Force $tmpDir.FullName
Write-Host "       Zip verified — $AppName.exe present after extraction"

Write-Host ""
Write-Host "=== Build complete ===" -ForegroundColor Green
Write-Host "Release: $ZipPath"
Write-Host ""
Write-Host "Distribution:"
Write-Host "  Users extract the zip and double-click $AppName.exe."
Write-Host "  No Python, pip, folder management, or installation required."
