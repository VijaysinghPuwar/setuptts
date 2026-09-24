"""
Packaged-build self test:  SetupTTS --selftest <result.json> [--network]

Run by CI against the *frozen* app — the .app bundle, the installed Windows
EXE and the portable EXE — to catch what only breaks after PyInstaller: a
missing data file, an unbundled Qt plugin, a hidden import, or TLS failing
because the CA bundle wasn't found.  A windowed Windows build has no console,
so results are written to a JSON file rather than printed.

With --network it also fetches the voice list and synthesises one short
sentence through the same code the app uses, verifying the audio is complete.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path


def run(argv: list[str]) -> int:
    idx = argv.index("--selftest")
    out = Path(argv[idx + 1]) if len(argv) > idx + 1 and not argv[idx + 1].startswith("--") \
        else Path("setuptts-selftest.json")
    network = "--network" in argv

    results: dict = {"checks": {}, "ok": False}

    def check(name: str, fn) -> None:
        started = time.monotonic()
        try:
            detail = fn()
            results["checks"][name] = {"ok": True, "detail": detail,
                                       "seconds": round(time.monotonic() - started, 2)}
        except Exception as exc:  # noqa: BLE001 — every failure is reported
            results["checks"][name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                                       "trace": traceback.format_exc(limit=6)}

    from app import APP_VERSION
    results["version"] = APP_VERSION
    results["frozen"] = bool(getattr(sys, "frozen", False))
    results["platform"] = sys.platform

    def assets():
        from app.utils.paths import resource_path
        needed = ["app/assets/styles/app.qss", "app/assets/icons/app.png",
                  "app/assets/icons/chevron-down.svg"]
        missing = [p for p in needed if not resource_path(p).exists()]
        if missing:
            raise FileNotFoundError(", ".join(missing))
        return "all bundled"

    def qt_gui():
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import QApplication
        from app.ui.style import stylesheet_text
        from app.utils.paths import resource_path
        app = QApplication.instance() or QApplication([sys.argv[0]])
        app.setStyleSheet(stylesheet_text())
        # SVG needs the qsvg image-format plugin — the dropdown chevrons
        # silently vanish without it.
        chevron = QPixmap(str(resource_path("app/assets/icons/chevron-down.svg")))
        if chevron.isNull():
            raise RuntimeError("SVG image plugin missing (chevron did not load)")
        icon = QPixmap(str(resource_path("app/assets/icons/app.png")))
        if icon.isNull():
            raise RuntimeError("app icon did not load")
        from app.ui.main_window import MainWindow  # imports the whole UI
        return f"Qt {__import__('PySide6').__version__}, platform {app.platformName()}"

    def tls():
        import ssl
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        return f"{len(ctx.get_ca_certs()) or 'n/a'} CA certs via certifi"

    def imports():
        import aiohttp  # noqa: F401
        import edge_tts
        import sqlite3  # noqa: F401
        from PySide6 import QtNetwork  # noqa: F401
        return f"edge_tts {getattr(edge_tts, '__version__', '?')}, aiohttp {aiohttp.__version__}"

    check("assets", assets)
    check("imports", imports)
    check("tls", tls)
    check("qt_gui", qt_gui)

    if network:
        import asyncio

        def voices():
            from app.services.tts_service import list_voices
            found = asyncio.run(list_voices(force_refresh=True))
            names = {v.get("ShortName") for v in found}
            for required in ("en-US-AndrewNeural", "en-US-AvaNeural"):
                if required not in names:
                    raise RuntimeError(f"{required} missing from {len(found)} voices")
            return f"{len(found)} voices"

        def synthesis():
            from app.services.tts_service import build_communicate
            from app.utils.mp3_duration import mp3_duration_from_bytes
            from app.workers.tts_worker import _stream_incomplete_reason

            text = "SetupTTS packaged build check. The quick brown fox jumps over the lazy dog."

            async def go():
                comm = build_communicate(text=text, voice="en-US-AndrewNeural",
                                         rate="+0%", volume="+0%")
                audio, bounds = b"", []
                async for ev in comm.stream():
                    if ev["type"] == "audio":
                        audio += ev["data"]
                    else:
                        bounds.append((ev["offset"], ev["duration"], ev["text"]))
                return audio, bounds

            audio, bounds = asyncio.run(go())
            problem = _stream_incomplete_reason(text, bounds, len(audio))
            if problem:
                raise RuntimeError(f"incomplete audio: {problem}")
            seconds = mp3_duration_from_bytes(audio)
            if not seconds or seconds < 2:
                raise RuntimeError(f"implausible duration {seconds}")
            return f"{len(audio)} bytes, {seconds:.1f} s"

        check("voices", voices)
        check("synthesis", synthesis)

    results["ok"] = all(c["ok"] for c in results["checks"].values())
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if results["ok"] else 1
