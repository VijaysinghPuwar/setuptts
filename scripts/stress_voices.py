"""
Voice-matrix stress test: every voice source, many languages, hostile text.

    python scripts/stress_voices.py                      # all sources
    python scripts/stress_voices.py --sources online --long-chars 60000

Jobs go through the app's real JobQueue (worker threads, signals, staging,
assembly, keep-awake), two at a time like the app.  Every finished MP3 is
re-parsed and its duration checked against the text length.  All app data is
isolated under --workdir via SETUPTTS_DATA_DIR.

Writes results.jsonl and prints a summary table.  Exit code 0 only if every
case produced a valid MP3 of plausible length.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── Text corpus ────────────────────────────────────────────────────────── #

_EN = (
    "The harbour lay quiet under a low grey sky. Mara counted the boats twice, "
    "then a third time, because the number never came out the same. \"Seven,\" "
    "she said at last — though she knew it was eight. Dr. Okafor arrived at "
    "4:45 p.m. on 12/03/2024 with $1,250.75 and a 3.5% chance of rain. "
)
_SAMPLES = {
    "en": _EN * 6,
    "de": "Der Hafen lag still unter einem grauen Himmel. Mara zählte die Boote "
          "zweimal, dann ein drittes Mal, weil die Zahl nie gleich herauskam. " * 8,
    "fr": "Le port était calme sous un ciel gris. Mara compta les bateaux deux "
          "fois, puis une troisième, car le nombre n'était jamais le même. " * 8,
    "es": "El puerto estaba tranquilo bajo un cielo gris. Mara contó los barcos "
          "dos veces, y luego una tercera, porque el número nunca coincidía. " * 8,
    "ja": "港は灰色の空の下で静かだった。マラは船を二度数え、それから三度目も数えた。"
          "数はいつも同じにならなかったからだ。" * 12,
    "zh": "港口在灰色的天空下很安静。玛拉数了两遍船，然后又数了第三遍，因为数字总是不一样。" * 12,
    "hi": "बंदरगाह धूसर आसमान के नीचे शांत था। मारा ने नावों को दो बार गिना, फिर तीसरी "
          "बार, क्योंकि संख्या कभी एक जैसी नहीं आती थी। " * 8,
    "ar": "كان الميناء هادئًا تحت سماء رمادية. عدّت مارا القوارب مرتين، ثم مرة "
          "ثالثة، لأن العدد لم يكن متطابقًا أبدًا. " * 8,
    "it": "Il porto era tranquillo sotto un cielo grigio. Mara contò le barche "
          "due volte, poi una terza, perché il numero non tornava mai uguale. " * 8,
    "pt": "O porto estava tranquilo sob um céu cinzento. Mara contou os barcos "
          "duas vezes, e depois uma terceira, porque o número nunca batia. " * 8,
    "ru": "Гавань лежала тихо под серым небом. Мара пересчитала лодки дважды, "
          "потом в третий раз, потому что число никогда не совпадало. " * 8,
}
# Text that has broken TTS pipelines before.
_HOSTILE = {
    "markup": "Use <b>bold</b> & 'quotes' \"double\" <speak> </voice> &amp; &lt;tag&gt; "
              "5 < 6 > 4 && x || y. " * 15,
    "symbols": "Emoji 😀🎉🔥 and ♥ ☃ ✓ → ∞ ≠ — … « » ‘ ’ “ ” • ¶ § © ® ™ ° ± × ÷ "
               "urls https://example.com/a?b=c&d=e#f and emails a.b@c.io. " * 10,
    "run_on": ("and then the tide came in " * 400).strip(),
    "no_spaces": "x" * 3000,
    "numbers": " ".join(str(n * 7919 % 100003) for n in range(1, 600)) + ".",
    "blank_lines": ("\n\n\n   \t\n" + "Short line.\n") * 120,
    "mixed_script": "English, then 日本語の文, then русский текст, then العربية, then हिन्दी. " * 15,
}


def _long_english(chars: int) -> str:
    body = (_EN + "\n\n") * (chars // len(_EN) + 1)
    return body[:chars]


# ── Case selection ─────────────────────────────────────────────────────── #

_ONLINE = [
    ("en-US-AndrewNeural", "en"), ("en-US-AvaNeural", "en"),
    ("en-US-ChristopherNeural", "en"), ("en-GB-SoniaNeural", "en"),
    ("en-US-AvaMultilingualNeural", "en"), ("en-US-AndrewMultilingualNeural", "en"),
    ("en-US-EmmaMultilingualNeural", "de"),
    ("de-DE-KatjaNeural", "de"), ("fr-FR-DeniseNeural", "fr"),
    ("es-ES-AlvaroNeural", "es"), ("ja-JP-NanamiNeural", "ja"),
    ("zh-CN-XiaoxiaoNeural", "zh"), ("hi-IN-SwaraNeural", "hi"),
    ("ar-SA-HamedNeural", "ar"), ("ru-RU-SvetlanaNeural", "ru"),
]
_SYSTEM = [
    ("Samantha", "en"), ("Daniel", "en"), ("Fred", "en"), ("Anna", "de"),
    ("Thomas", "fr"), ("Mónica", "es"), ("Kyoko", "ja"), ("Tingting", "zh"),
    ("Lekha", "hi"), ("Majed", "ar"), ("Milena", "ru"),
]
#: Every Kokoro voice, with a sample in its language.
_KOKORO_LANG = {"a": "en", "b": "en", "e": "es", "f": "fr", "h": "hi", "i": "it", "p": "pt"}
_PIPER = [("en_US-lessac-medium", "en"), ("de_DE-thorsten-medium", "de"),
          ("es_ES-davefx-medium", "es"), ("fr_FR-siwis-medium", "fr")]


def _book_segments(path: str, size: int) -> dict[str, str]:
    """Start, end, and the passage with the most unusual characters."""
    book = Path(path).read_text(encoding="utf-8-sig")
    if len(book) <= size * 3:
        return {"whole": book}

    def at_paragraph(pos: int) -> int:
        cut = book.rfind("\n", 0, pos)
        return cut + 1 if cut > 0 else pos

    step = max(size // 4, 1)
    odd = max(range(0, len(book) - size, step),
              key=lambda i: sum(not ch.isascii() for ch in book[i:i + size]))
    starts = {"start": 0, "unusual": at_paragraph(odd), "end": at_paragraph(len(book) - size)}
    return {name: book[i:i + size] for name, i in starts.items()}


_BOOK_ONLINE = [
    "en-US-AndrewNeural", "en-US-AvaNeural", "en-US-BrianNeural", "en-US-EmmaNeural",
    "en-US-ChristopherNeural", "en-GB-SoniaNeural", "en-US-AvaMultilingualNeural",
    "en-US-AndrewMultilingualNeural",
]
_BOOK_SYSTEM = ["Samantha", "Daniel", "Karen", "Moira", "Fred"]


def _ensure_kokoro(keys) -> set[str]:
    from app.services import kokoro_tts

    for key in keys:
        if key not in kokoro_tts.installed_keys():
            print(f"downloading Kokoro voice {key} …", flush=True)
            kokoro_tts.download_voice(kokoro_tts.KokoroCatalogVoice(key))
    return kokoro_tts.installed_keys()


def _book_cases(sources: set[str], path: str, size: int, rate: str) -> list[dict]:
    from app.services import kokoro_tts, piper_tts
    from app.services.system_tts import list_system_voices

    segments = _book_segments(path, size)
    voices: list[tuple[str, str]] = []
    if "online" in sources:
        voices += [("online", v) for v in _BOOK_ONLINE]
    if "piper" in sources and piper_tts.piper_available():
        voices += [("piper", v["ShortName"]) for v in piper_tts.list_piper_voices()
                   if v["Locale"].startswith("en")]
    if "kokoro" in sources and kokoro_tts.kokoro_available():
        english = [k for k in kokoro_tts.VOICES if k[0] in "ab"]
        voices += [("kokoro", kokoro_tts.KOKORO_PREFIX + k)
                   for k in sorted(_ensure_kokoro(english)) if k in english]
    if "system" in sources:
        listed = list_system_voices()
        installed = {v["ShortName"] for v in listed}
        for name in _BOOK_SYSTEM:
            match = next((s for s in installed
                          if s == f"system:{name}" or s.startswith(f"system:{name} (")), None)
            if match:
                voices.append(("system", match))
        if sys.platform == "win32":
            voices += [("system", v["ShortName"]) for v in listed
                       if (v.get("Locale") or "").startswith("en")]
    return [{"source": src, "voice": v, "kind": f"book:{seg}", "text": text, "rate": rate}
            for src, v in voices for seg, text in segments.items()]


def _cases(sources: set[str], long_chars: int) -> list[dict]:
    from app.services import kokoro_tts, piper_tts
    from app.services.system_tts import list_system_voices

    cases: list[dict] = []

    def add(source, voice, kind, text, rate="+0%"):
        cases.append({"source": source, "voice": voice, "kind": kind,
                      "text": text, "rate": rate})

    if "online" in sources:
        for voice, lang in _ONLINE:
            add("online", voice, f"lang:{lang}", _SAMPLES[lang])
        for kind, text in _HOSTILE.items():
            add("online", "en-US-AndrewNeural", f"hostile:{kind}", text)
        add("online", "en-US-AvaNeural", "rate:+100%", _SAMPLES["en"], "+100%")
        add("online", "en-US-AvaNeural", "rate:-50%", _SAMPLES["en"], "-50%")
        if long_chars:
            add("online", "en-US-AndrewNeural", f"long:{long_chars}", _long_english(long_chars))
            add("online", "en-US-AvaMultilingualNeural", f"long:{long_chars}",
                _long_english(long_chars))

    if "system" in sources:
        listed = list_system_voices()
        installed = {v["ShortName"] for v in listed}
        for name, lang in _SYSTEM:
            short = f"system:{name}"
            if short in installed:
                add("system", short, f"lang:{lang}", _SAMPLES[lang])
        if sys.platform == "win32":
            # Windows voices are named by registry token: try every one.
            for v in listed:
                lang = (v.get("Locale") or "")[:2]
                if lang in _SAMPLES:
                    add("system", v["ShortName"], f"lang:{lang}", _SAMPLES[lang])
        main = "system:Samantha" if "system:Samantha" in installed else next(
            (v["ShortName"] for v in listed if (v.get("Locale") or "").startswith("en")), None)
        if main:
            for kind, text in _HOSTILE.items():
                add("system", main, f"hostile:{kind}", text)
            add("system", main, "rate:+100%", _SAMPLES["en"], "+100%")
            if long_chars:
                add("system", main, f"long:{long_chars}", _long_english(long_chars))

    if "piper" in sources and piper_tts.piper_available():
        catalog = {v.key: v for v in piper_tts.fetch_catalog()}
        for key, lang in _PIPER:
            if key not in piper_tts.installed_keys() and key in catalog:
                print(f"downloading Piper voice {key} …", flush=True)
                piper_tts.download_voice(catalog[key])
        installed = piper_tts.installed_keys()
        piper_names = {v["ShortName"]: v for v in piper_tts.list_piper_voices()}
        for key, lang in _PIPER:
            short = next((s for s in piper_names if s.endswith(key)), None)
            if key in installed and short:
                add("piper", short, f"lang:{lang}", _SAMPLES[lang])
        en = next((s for s in piper_names if s.endswith("en_US-lessac-medium")), None)
        if en:
            for kind, text in _HOSTILE.items():
                add("piper", en, f"hostile:{kind}", text)
            add("piper", en, "rate:+100%", _SAMPLES["en"], "+100%")
            if long_chars:
                add("piper", en, f"long:{long_chars}", _long_english(long_chars))

    if "kokoro" in sources and kokoro_tts.kokoro_available():
        installed = _ensure_kokoro(kokoro_tts.VOICES)
        for key in sorted(installed, key=lambda k: (kokoro_tts.grade_rank(k), k)):
            add("kokoro", kokoro_tts.KOKORO_PREFIX + key, f"lang:{_KOKORO_LANG[key[0]]}",
                _SAMPLES[_KOKORO_LANG[key[0]]])
        main = kokoro_tts.KOKORO_PREFIX + kokoro_tts.DEFAULT_VOICE
        for kind, text in _HOSTILE.items():
            add("kokoro", main, f"hostile:{kind}", text)
        add("kokoro", main, "rate:+100%", _SAMPLES["en"], "+100%")
        add("kokoro", main, "rate:-50%", _SAMPLES["en"], "-50%")
        add("kokoro", "kokoro:bf_emma", "hostile:mixed_script", _HOSTILE["mixed_script"])
        if long_chars:
            add("kokoro", main, f"long:{long_chars}", _long_english(long_chars))
            add("kokoro", "kokoro:bm_george", f"long:{long_chars}", _long_english(long_chars))
    return cases


# ── Running ────────────────────────────────────────────────────────────── #

def _rss_mb() -> float:
    """Peak memory of this process, in MB."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        *((n, ctypes.c_size_t) for n in (
                            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                            "PagefileUsage", "PeakPagefileUsage"))]

        c = _Counters()
        c.cb = ctypes.sizeof(c)
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.PeakWorkingSetSize / (1024 * 1024)
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _validate(case: dict, path: Path) -> tuple[bool, str, float | None]:
    from app.services.tts_quality import build_text_profile
    from app.utils.mp3_duration import mp3_duration_seconds

    if not path.exists() or path.stat().st_size == 0:
        return False, "no output file", None
    duration = mp3_duration_seconds(path)
    if not duration:
        return False, "output is not a readable MP3", None
    cleaned = "".join(build_text_profile(case["text"]).cleaned_text.split())
    speakable = len(cleaned)
    digits = sum(ch.isdigit() for ch in cleaned)
    # Very generous bounds: catches empty/truncated audio and runaway output,
    # not voice-to-voice speed differences.  Rate scales the expectation.
    pct = int(case["rate"].rstrip("%"))
    speed = 1 + pct / 100
    lo = speakable / 60 / speed      # 60 non-space chars/s is absurdly fast
    # 2 chars/s is absurdly slow — except for digits: "79190" is spoken as
    # "seventy-nine thousand one hundred ninety" (SAPI David: 2.0 chars/s).
    hi = ((speakable - digits) / 2 + digits) / speed + 5
    if case["kind"] == "hostile:no_spaces":
        lo = 0.5                     # one "word": voices may spell or skip it
    if not lo <= duration <= hi:
        return False, f"duration {duration:.1f}s outside [{lo:.1f}, {hi:.1f}]", duration
    return True, "", duration


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="online,system,piper,kokoro")
    ap.add_argument("--long-chars", type=int, default=30_000)
    ap.add_argument("--workdir", default=str(ROOT / "stress_out" / "voices"))
    ap.add_argument("--only", default="", help="substring filter on voice/kind")
    ap.add_argument("--text-file", default="",
                    help="run passages of this book through every voice instead")
    ap.add_argument("--segment-chars", type=int, default=15_000)
    ap.add_argument("--rate", default="+0%", help="speed for --text-file runs")
    ap.add_argument("--parallel", type=int, default=2, help="jobs at once (the app runs 2)")
    args = ap.parse_args()

    work = Path(args.workdir).resolve()
    (work / "mp3").mkdir(parents=True, exist_ok=True)
    os.environ["SETUPTTS_DATA_DIR"] = str(work / "appdata")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QCoreApplication, QTimer

    from app.utils.app_logging import setup_logging
    from app.workers.job_queue import JobQueue

    try:
        setup_logging(work / "logs")
    except TypeError:
        setup_logging()

    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    sources = set(args.sources.split(","))
    cases = (_book_cases(sources, args.text_file, args.segment_chars, args.rate)
             if args.text_file else _cases(sources, args.long_chars))
    if args.only:
        cases = [c for c in cases if args.only in c["voice"] or args.only in c["kind"]]
    print(f"{len(cases)} cases", flush=True)

    queue = JobQueue()
    by_id: dict[str, dict] = {}
    pending = list(cases)

    def submit_next() -> None:
        while pending and queue.running_count + queue.pending_count < args.parallel:
            case = pending.pop(0)
            idx = cases.index(case)
            # Windows voice ids are registry paths: keep the last part only.
            safe = re.sub(r"[^\w.-]+", "_", case["voice"].replace("\\", "/").rsplit("/", 1)[-1])
            out = work / "mp3" / f"{idx:03d}_{safe}_{case['kind'].replace(':', '_')}.mp3"
            out.unlink(missing_ok=True)
            case["output"] = out
            case["started"] = time.monotonic()
            jid = queue.submit(case["text"], case["voice"], case["voice"],
                               case["rate"], "+0%", str(out),
                               allow_voice_mismatch=True)
            by_id[jid] = case

    results: list[dict] = []
    log = (work / "results.jsonl").open("w", encoding="utf-8")

    def finish(item, error: str = "") -> None:
        case = by_id.get(item.id)
        if case is None or "done" in case:
            return
        case["done"] = True
        elapsed = time.monotonic() - case["started"]
        ok, why, duration = (False, error or "failed", None)
        if not error:
            ok, why, duration = _validate(case, case["output"])
        row = {
            "source": case["source"], "voice": case["voice"], "kind": case["kind"],
            "rate": case["rate"], "chars": len(case["text"]), "ok": ok,
            "problem": why, "elapsed_s": round(elapsed, 1),
            "audio_s": round(duration, 1) if duration else None,
            "realtime_x": round(duration / elapsed, 1) if duration and elapsed else None,
            "peak_rss_mb": round(_rss_mb()),
        }
        results.append(row)
        log.write(json.dumps(row, ensure_ascii=False) + "\n")
        log.flush()
        mark = "PASS" if ok else "FAIL"
        print(f"[{len(results)}/{len(cases)}] {mark} {row['source']:6} {row['voice']:<38} "
              f"{row['kind']:<22} {row['elapsed_s']:>7}s audio={row['audio_s']} {why}",
              flush=True)
        submit_next()
        if len(results) == len(cases):
            QTimer.singleShot(500, app.quit)

    queue.job_completed.connect(lambda item: finish(item))
    queue.job_failed.connect(lambda item: finish(item, f"job failed: {item.error}"
                                                 if hasattr(item, "error") else "job failed"))
    queue.job_cancelled.connect(lambda item: finish(item, "cancelled"))

    QTimer.singleShot(0, submit_next)
    if cases:
        app.exec()
    log.close()

    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed; "
          f"peak RSS {max((r['peak_rss_mb'] for r in results), default=0)} MB")
    for r in failed:
        print(f"  FAIL {r['source']} {r['voice']} {r['kind']}: {r['problem']}")
    return 1 if failed or len(results) != len(cases) else 0


if __name__ == "__main__":
    raise SystemExit(main())
