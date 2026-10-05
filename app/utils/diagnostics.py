"""
Environment details for troubleshooting.

Logged once at startup and offered as "Copy Diagnostic Info" in Settings, so a
bug report always says exactly which build, OS and library versions were in
use — the first question when reading someone else's log.
"""

from __future__ import annotations

import locale
import platform
import sys
from importlib import metadata


def _pkg_version(name: str) -> str:
    try:
        return metadata.version(name)
    except Exception:  # noqa: BLE001 — frozen builds may lack dist-info
        module = sys.modules.get(name.replace("-", "_"))
        return str(getattr(module, "__version__", "unknown"))


def build_kind() -> str:
    if not getattr(sys, "frozen", False):
        return "source"
    exe = sys.executable.lower()
    if sys.platform == "darwin":
        return "macOS app"
    if "\\programs\\" in exe or "program files" in exe:
        return "Windows installed"
    return "Windows portable"


def environment_info() -> dict[str, str]:
    """Return ordered key/value pairs describing this process's environment."""
    from app import APP_NAME, APP_VERSION

    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion
        qt = f"{qVersion()} (PySide6 {pyside_version})"
    except Exception:  # noqa: BLE001
        qt = "unavailable"

    try:
        enc = locale.getpreferredencoding(False)
    except Exception:  # noqa: BLE001
        enc = "unknown"

    return {
        "App": f"{APP_NAME} {APP_VERSION}",
        "Build": build_kind(),
        "Executable": sys.executable,
        "OS": f"{platform.system()} {platform.release()} ({platform.version()})",
        "Machine": platform.machine(),
        "Python": sys.version.split()[0],
        "Qt": qt,
        "edge-tts": _pkg_version("edge-tts"),
        "aiohttp": _pkg_version("aiohttp"),
        "certifi": _pkg_version("certifi"),
        "Locale encoding": enc,
    }


def environment_text(extra: dict[str, str] | None = None) -> str:
    info = environment_info()
    if extra:
        info.update(extra)
    width = max(len(k) for k in info)
    return "\n".join(f"{k.ljust(width)} : {v}" for k, v in info.items())
