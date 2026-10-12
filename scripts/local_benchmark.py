"""
Local (offline voice) benchmark: how fast does this machine turn a real
book into speech?

    python scripts/local_benchmark.py                      # quick: first 30 000 chars
    python scripts/local_benchmark.py --full               # the whole book (~1 h on an M5)
    python scripts/local_benchmark.py --device both        # Windows + GPU support: processor vs card
    python scripts/local_benchmark.py --text other.txt --voice piper:en_US-lessac-medium

The reference text is a ~715 000-character web-novel volume (chapters
201-300) that real jobs were run on.  It is not in the repository (it is
not ours to publish): pass --text, set SETUPTTS_BENCH_TEXT, or keep it at
the default path below.  Runs the same path as a real job — the worker's
1 200-character chunks, PiperSynthesizer, MP3 encoding — at the rate real
jobs use (+24 %), so the numbers predict real job times.

Meant for capable machines (Apple M-series Pro/Max, recent Ryzen/Core
i7-i9, NVIDIA RTX cards).  Each run is appended to benchmark_results.jsonl
in the repo root (git-ignored) so machines and versions can be compared.

Reference results (Ryan high, +24 %, full book):
    MacBook Pro M5 (10 cores), CPU      ~140 chars/s
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_TEXT = Path.home() / "Downloads" / "ch201-300" / \
    "Simulating Endless Possibilities_ Invincible Once I Act" / \
    "Simulating Endless Possibilities_ Invincible Once I Act 201-300.txt"
#: First 16 hex digits of the reference file's SHA-256, so results from a
#: different text are flagged rather than compared by mistake.
REFERENCE_SHA = "99dd0c627cbbf0da"
QUICK_CHARS = 30_000
RESULTS = ROOT / "benchmark_results.jsonl"


def _machine() -> dict:
    info = {"os": platform.platform(), "machine": platform.machine(),
            "cpu_count": os.cpu_count(), "python": platform.python_version()}
    if sys.platform == "darwin":
        import subprocess

        try:
            info["cpu"] = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                         capture_output=True, text=True).stdout.strip()
        except OSError:
            pass
    else:
        info["cpu"] = platform.processor()
    if sys.platform == "win32":
        from app.services import gpu_accel

        gpus = gpu_accel.detect_gpus()
        if gpus:
            info["gpu"] = f"{gpus[0].name} (driver {gpus[0].driver})"
    return info


def _run(text: str, voice: str, rate: str, gpu: bool | None) -> dict:
    from app.services import piper_tts
    from app.services.local_tts import Mp3Writer
    from app.workers.local_tts_worker import CHUNK_CHARS, _NO_PAYLOAD_LIMIT
    from app.workers.tts_worker import _split_text

    chunks = _split_text(text, CHUNK_CHARS, _NO_PAYLOAD_LIMIT)
    total = sum(len(c) for c in chunks)
    out = Path(tempfile.mkdtemp(prefix="setuptts-bench-")) / "bench.mp3"
    with piper_tts.PiperSynthesizer(voice, gpu=gpu) as synth:
        synth.synthesize("Warm up.", rate)
        writer = Mp3Writer(out, synth.sample_rate)
        start = time.perf_counter()
        done = 0
        last = start
        for i, chunk in enumerate(chunks, 1):
            writer.write(synth.synthesize(chunk, rate))
            done += len(chunk)
            now = time.perf_counter()
            if now - last >= 15 or i == len(chunks):
                cps = done / (now - start)
                left = (total - done) / cps if cps else 0
                print(f"  {done * 100 // total:3d}%  {cps:6.0f} chars/s  "
                      f"~{left / 60:.0f} min left", flush=True)
                last = now
        elapsed = time.perf_counter() - start
        writer.finish()
        on_gpu = synth.on_gpu
    audio_s = writer.duration_seconds
    out.unlink(missing_ok=True)
    return {"chars": total, "seconds": round(elapsed, 1),
            "chars_per_s": round(total / elapsed, 1),
            "audio_minutes": round(audio_s / 60, 1),
            "realtime_x": round(audio_s / elapsed, 1), "on_gpu": on_gpu}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--text", type=Path,
                    default=Path(os.environ.get("SETUPTTS_BENCH_TEXT", DEFAULT_TEXT)))
    ap.add_argument("--voice", default="piper:en_US-ryan-high")
    ap.add_argument("--rate", default="+24%")
    ap.add_argument("--full", action="store_true", help="the whole text, not the first 30 000 chars")
    ap.add_argument("--chars", type=int, default=QUICK_CHARS)
    ap.add_argument("--device", choices=("cpu", "gpu", "both"), default="cpu",
                    help="gpu/both need Windows with GPU support downloaded")
    args = ap.parse_args()

    if not args.text.is_file():
        print(f"Benchmark text not found: {args.text}\n"
              "Pass --text or set SETUPTTS_BENCH_TEXT.", file=sys.stderr)
        return 2
    raw = args.text.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()[:16]
    text = raw.decode("utf-8-sig")
    if not args.full:
        text = text[:args.chars]

    devices = ["cpu", "gpu"] if args.device == "both" else [args.device]
    if "gpu" in devices:
        from app.services import gpu_accel

        if not gpu_accel.SUPPORTED or not gpu_accel.pack_installed():
            print("GPU runs need Windows with GPU support downloaded "
                  "(Settings → Graphics card).", file=sys.stderr)
            return 2
        # Before anything imports onnxruntime.
        if not gpu_accel.activate():
            print("GPU support could not be loaded.", file=sys.stderr)
            return 2

    machine = _machine()
    print(f"Text: {args.text.name}  ({len(text):,} of {len(raw):,} bytes, "
          f"{'reference' if sha == REFERENCE_SHA else 'NOT the reference text'})")
    print(f"Voice: {args.voice}  rate {args.rate}  ·  {machine.get('cpu') or machine['machine']}"
          + (f"  ·  {machine['gpu']}" if machine.get("gpu") else ""))

    from app import APP_VERSION

    results = {}
    for dev in devices:
        print(f"\n[{dev.upper()}]")
        r = _run(text, args.voice, args.rate, gpu=(dev == "gpu"))
        if dev == "gpu" and not r["on_gpu"]:
            print("  warning: the voice fell back to the processor during the run")
        results[dev] = r
        full_chars = len(raw.decode("utf-8-sig"))
        print(f"  {r['chars_per_s']:.0f} chars/s · {r['realtime_x']}x real time · "
              f"full book ≈ {full_chars / r['chars_per_s'] / 60:.0f} min")
        record = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "app": APP_VERSION,
                  "device": dev, "voice": args.voice, "rate": args.rate,
                  "text_sha": sha, "reference_text": sha == REFERENCE_SHA,
                  "full": args.full, **machine, **r}
        with open(RESULTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    if len(results) == 2:
        print(f"\nGPU is {results['gpu']['chars_per_s'] / results['cpu']['chars_per_s']:.2f}x "
              "the processor on this machine.")
    print(f"\nSaved to {RESULTS.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
