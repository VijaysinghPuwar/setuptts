"""
Live long-form stress test: synthesise a multi-hour audiobook through the
real speech service with exactly the worker the app uses.

    python scripts/stress_longform.py --hours 10 --voice en-US-AndrewNeural

Text is a public-domain novel (Project Gutenberg), trimmed to roughly the
character count that the voice needs for the requested length.  All app data
(logs, staging) is isolated under --workdir via SETUPTTS_DATA_DIR, so the
user's real history and settings are never touched.

Exit code 0 only if the MP3 was finalised and passed the app's own
coverage + duration checks.  A JSON summary is written next to the MP3.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Measured on real jobs: Andrew at -5 % speaks ~15.8 source chars per second.
_CHARS_PER_AUDIO_SECOND = 15.8
_GUTENBERG = "https://www.gutenberg.org/cache/epub/2701/pg2701.txt"   # Moby-Dick


def _load_text(chars: int, cache: Path) -> str:
    if not cache.exists():
        with urllib.request.urlopen(_GUTENBERG, timeout=60) as resp:
            cache.write_bytes(resp.read())
    raw = cache.read_text(encoding="utf-8-sig")
    start = raw.find("*** START OF")
    end = raw.find("*** END OF")
    body = raw[raw.find("\n", start) + 1 if start >= 0 else 0: end if end > 0 else None]
    body = body.replace("\r\n", "\n").strip()
    while len(body) < chars:            # repeat the book if more text is needed
        body = body + "\n\n" + body
    cut = body.rfind("\n\n", 0, chars)
    return body[: cut if cut > chars * 0.9 else chars]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=10.0)
    ap.add_argument("--voice", default="en-US-AndrewNeural")
    ap.add_argument("--rate", default="-5%")
    ap.add_argument("--workdir", default=str(ROOT / "stress_out"))
    args = ap.parse_args()

    work = Path(args.workdir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["SETUPTTS_DATA_DIR"] = str(work / "appdata")

    from PySide6.QtCore import QCoreApplication, QTimer

    from app import APP_VERSION
    from app.utils.app_logging import setup_logging
    from app.utils.paths import AppPaths
    from app.workers.tts_worker import TTSWorker

    paths = AppPaths()
    setup_logging(paths.log_dir)
    import logging
    log = logging.getLogger("stress")

    target_chars = int(args.hours * 3600 * _CHARS_PER_AUDIO_SECOND)
    text = _load_text(target_chars, work / "source_book.txt")
    out_mp3 = work / f"stress_{args.voice}_{args.hours:g}h.mp3"
    summary_path = out_mp3.with_suffix(".json")
    log.info("Stress start: version=%s voice=%s rate=%s chars=%d target_hours=%.1f out=%s",
             APP_VERSION, args.voice, args.rate, len(text), args.hours, out_mp3)
    print(f"[stress] {len(text):,} chars -> {out_mp3}", flush=True)

    app = QCoreApplication(sys.argv)
    worker = TTSWorker(text, args.voice, args.rate, "+0%", str(out_mp3))
    started = time.monotonic()
    result: dict = {"version": APP_VERSION, "voice": args.voice, "rate": args.rate,
                    "chars": len(text), "target_hours": args.hours}
    last_pct = [-1]

    def on_progress(pct: int) -> None:
        if pct != last_pct[0]:
            last_pct[0] = pct
            mins = (time.monotonic() - started) / 60
            print(f"[stress] progress {pct}% after {mins:.1f} min", flush=True)

    def finish(ok: bool, **extra) -> None:
        result.update(ok=ok, wall_seconds=round(time.monotonic() - started, 1), **extra)
        summary_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"[stress] DONE ok={ok} {json.dumps(extra)[:400]}", flush=True)
        QTimer.singleShot(0, app.quit)

    worker.progress.connect(on_progress)
    worker.completed.connect(lambda path, secs: finish(
        True, output=path, elapsed=secs,
        audio_seconds=worker.audio_duration_seconds,
        audio_hours=round((worker.audio_duration_seconds or 0) / 3600, 2)))
    worker.job_resumable.connect(lambda d, done, failed, total: result.update(
        resumable={"staging": d, "done": done, "failed_at": failed, "total": total}))
    worker.failed.connect(lambda msg: finish(False, error=msg))
    worker.start()
    app.exec()
    worker.wait()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
