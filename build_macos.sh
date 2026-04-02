#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# build_macos.sh — build the macOS release zip for SetupTTS
#
# Usage:
#   ./build_macos.sh
#
# Outputs:
#   releases/SetupTTS-macOS-1.4.1.zip   — user-ready .app bundle in a zip
#
# Requirements:
#   - macOS with Python 3.11+ and a virtualenv / pip-installed environment
#   - pip install -r requirements.txt pyinstaller
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_NAME="SetupTTS"
VERSION="1.4.1"
SPEC="setuptts.spec"
DIST_DIR="dist"
APP_BUNDLE="${DIST_DIR}/${APP_NAME}.app"
RELEASES_DIR="releases"
ZIP_NAME="${APP_NAME}-macOS-${VERSION}.zip"
ZIP_PATH="${RELEASES_DIR}/${ZIP_NAME}"

echo "=== SetupTTS macOS Release Build ==="
echo "Version  : ${VERSION}"
echo "Output   : ${ZIP_PATH}"
echo

# ── 1. Clean previous build ─────────────────────────────────────────────────
echo "[1/5] Cleaning previous build..."
rm -rf build
rm -rf "${DIST_DIR}"
rm -f "${ZIP_PATH}"

# ── 2. Build with PyInstaller ────────────────────────────────────────────────
echo "[2/5] Running PyInstaller..."
python3 -m PyInstaller "${SPEC}" --noconfirm

# ── 3. Verify bundle ─────────────────────────────────────────────────────────
echo "[3/5] Verifying bundle..."
if [ ! -d "${APP_BUNDLE}" ]; then
    echo "ERROR: ${APP_BUNDLE} not found after build" >&2
    exit 1
fi

# Check required assets
RESOURCES="${APP_BUNDLE}/Contents/Resources"
if [ ! -f "${RESOURCES}/app/assets/styles/app.qss" ]; then
    echo "ERROR: QSS stylesheet missing from bundle" >&2; exit 1
fi
if [ ! -f "${RESOURCES}/app/assets/icons/app.png" ]; then
    echo "ERROR: App icon missing from bundle" >&2; exit 1
fi
if [ ! -d "${RESOURCES}/edge_tts" ]; then
    echo "ERROR: edge_tts not bundled" >&2; exit 1
fi

echo "       Bundle verified OK"
echo "       Size: $(du -sh "${APP_BUNDLE}" | cut -f1) (uncompressed)"

# ── 4. Create release zip ────────────────────────────────────────────────────
echo "[4/5] Creating release zip (preserving symlinks and xattrs)..."
mkdir -p "${RELEASES_DIR}"
# ditto preserves symlinks, resource forks, and extended attributes — required
# for a valid macOS .app bundle. Plain `zip` can corrupt the bundle.
ditto -c -k --keepParent "${APP_BUNDLE}" "${ZIP_PATH}"

ZIP_SIZE=$(du -sh "${ZIP_PATH}" | cut -f1)
echo "       ${ZIP_PATH}  (${ZIP_SIZE})"

# ── 5. Verify the zip ────────────────────────────────────────────────────────
echo "[5/5] Verifying zip integrity..."
VERIFY_DIR=$(mktemp -d)
ditto -x -k "${ZIP_PATH}" "${VERIFY_DIR}"
if [ ! -d "${VERIFY_DIR}/${APP_NAME}.app" ]; then
    echo "ERROR: .app not found after zip extraction" >&2
    rm -rf "${VERIFY_DIR}"; exit 1
fi
# Confirm the binary is executable
BINARY="${VERIFY_DIR}/${APP_NAME}.app/Contents/MacOS/${APP_NAME}"
if [ ! -x "${BINARY}" ]; then
    echo "ERROR: binary not executable after extraction" >&2
    rm -rf "${VERIFY_DIR}"; exit 1
fi
rm -rf "${VERIFY_DIR}"
echo "       Zip verified OK — binary is executable after extraction"

echo
echo "=== Build complete ==="
echo "Release: ${ZIP_PATH}"
echo
echo "Distribution notes:"
echo "  • Users extract the zip and double-click SetupTTS.app"
echo "  • On first launch on a fresh Mac, right-click → Open to bypass Gatekeeper"
echo "    (expected for unsigned apps; user only needs to do this once)"
