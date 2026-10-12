"""
GPU speed check:  SetupTTS --gpu-check <result.json> [--voice KEY] [--force]

Runs in its own process (started by the GPU support dialog, or by hand when
testing), so a graphics-driver crash can't take the app down.  Loads the
downloaded GPU build of onnxruntime, then speaks the same text with one
offline voice on the processor and on the NVIDIA card and writes:

    {"ok": true, "gpu_name": "...", "driver": "...", "voice": "...",
     "cpu_chars_per_s": 140.2, "gpu_chars_per_s": 610.5, "speedup": 4.35,
     "recommended": true, "error": ""}

``ok`` means the card ran the voice and produced sound; ``recommended``
that it was clearly faster than the processor.  ``--force`` skips the
card/driver requirement check (for testing the download on a PC without a
suitable card: the check then fails cleanly when CUDA can't start).
Exit status is 1 only when the check itself crashed; read ``ok`` and
``error`` for the verdict.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path


def _arg(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            return argv[i + 1]
    return None


def _pick_voice(requested: str | None) -> str | None:
    from app.services import piper_tts

    keys = piper_tts.installed_keys()
    if requested:
        key = requested[len(piper_tts.PIPER_PREFIX):] if requested.startswith(piper_tts.PIPER_PREFIX) else requested
        return key if key in keys else None
    if piper_tts.BUNDLED_VOICE in keys:
        return piper_tts.BUNDLED_VOICE
    return sorted(keys)[0] if keys else None


def check(argv: list[str]) -> dict:
    from app.services import gpu_accel

    result: dict = {"ok": False, "recommended": False, "error": "", "gpu_name": "",
                    "driver": "", "voice": "", "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not gpu_accel.SUPPORTED:
        result["error"] = "GPU support is only available on Windows."
        return result

    gpus = gpu_accel.detect_gpus()
    gpu = gpus[0] if gpus else None
    if gpu is not None:
        result.update(gpu_name=gpu.name, driver=gpu.driver, memory_mb=gpu.memory_mb,
                      compute=".".join(map(str, gpu.compute)) if gpu.compute else "")
    problem = gpu_accel.requirement_problem(gpu)
    if problem and "--force" not in argv:
        result["error"] = problem
        return result
    if not gpu_accel.pack_installed():
        result["error"] = "GPU support is not downloaded yet."
        return result
    if not gpu_accel.activate():
        result["error"] = "GPU support could not be loaded."
        return result

    import onnxruntime

    result["onnxruntime"] = getattr(onnxruntime, "__version__", "")
    result["onnxruntime_file"] = getattr(onnxruntime, "__file__", "")
    result["providers_available"] = list(onnxruntime.get_available_providers())
    if "CUDAExecutionProvider" not in result["providers_available"]:
        result["error"] = "The downloaded GPU runtime has no CUDA provider."
        return result

    from app.services import piper_tts

    key = _pick_voice(_arg(argv, "--voice"))
    if key is None:
        result["error"] = "No offline voice is installed. Download one from Get Voices first."
        return result
    short = piper_tts.PIPER_PREFIX + key
    result["voice"] = key

    cpu = piper_tts.benchmark(short, gpu=False)
    result["cpu_chars_per_s"] = cpu["chars_per_s"]
    try:
        card = piper_tts.benchmark(short, gpu=True)
    except Exception as exc:  # noqa: BLE001 - CUDA failed to start or run
        result["error"] = f"The graphics card could not run the voice: {exc}"
        result["detail"] = traceback.format_exc(limit=6)
        return result
    result["gpu_chars_per_s"] = card["chars_per_s"]
    if not card["on_gpu"]:
        result["error"] = "The voice did not run on the graphics card."
        return result
    # Same text, same voice: the card's audio must be about as long as the
    # processor's (sampling noise varies it slightly), and not silent.
    if card["audio_s"] <= 0 or abs(card["audio_s"] - cpu["audio_s"]) > 0.15 * cpu["audio_s"]:
        result["error"] = (f"The graphics card produced unexpected audio "
                           f"({card['audio_s']:.1f} s against {cpu['audio_s']:.1f} s).")
        return result
    result["speedup"] = round(card["chars_per_s"] / max(cpu["chars_per_s"], 1e-6), 2)
    result["ok"] = True
    result["recommended"] = gpu_accel.check_recommends(result)
    return result


def run(argv: list[str]) -> int:
    out = _arg(argv, "--gpu-check")
    out_path = Path(out) if out else Path("gpu-check.json")
    # Its own log file: the app may be running and rotating setuptts.log.
    try:
        import logging

        from app.utils.paths import AppPaths

        logging.basicConfig(filename=str(AppPaths().log_dir / "gpu-check.log"), filemode="w",
                            level=logging.INFO, encoding="utf-8",
                            format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
    except Exception:  # noqa: BLE001
        pass
    crashed = False
    try:
        result = check(argv)
    except Exception as exc:  # noqa: BLE001
        crashed = True
        result = {"ok": False, "recommended": False, "error": f"The speed check failed: {exc}",
                  "detail": traceback.format_exc(limit=8)}
    try:
        import logging

        logging.getLogger(__name__).info("GPU speed check: %s", json.dumps(result))
    except Exception:  # noqa: BLE001
        pass
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 1 if crashed else 0
