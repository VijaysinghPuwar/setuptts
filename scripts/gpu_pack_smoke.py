"""
Download SetupTTS's optional NVIDIA GPU support pack (Windows only).

    python scripts/gpu_pack_smoke.py [--data-dir DIR] [--retries N]

Used by CI (.github/workflows/build.yml) before running the frozen app with
``--gpu-check``, and by hand when testing GPU support from a dev checkout
(see docs/GPU_TESTING_WINDOWS.md).

The pack goes where the app looks for it: ``AppPaths().data_dir /
"gpu_runtime" / PACK_ID``.  AppPaths resolves the same folder for this
interpreter and for the frozen SetupTTS.exe (platformdirs, per user:
%LOCALAPPDATA%\\SetupTTSApp\\SetupTTS), unless SETUPTTS_DATA_DIR is set — then
both use that folder.  ``--data-dir`` sets SETUPTTS_DATA_DIR for this run;
the app must then be started with the same variable.

Exits 0 when the pack is installed (already present counts), 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", help="use this folder as the app-data folder "
                        "(sets SETUPTTS_DATA_DIR; start the app with the same value)")
    parser.add_argument("--retries", type=int, default=3,
                        help="attempts; a failed download resumes where it stopped")
    args = parser.parse_args()

    if args.data_dir:
        os.environ["SETUPTTS_DATA_DIR"] = str(Path(args.data_dir).resolve())
    sys.path.insert(0, str(ROOT))

    from app.services import gpu_accel

    if not gpu_accel.SUPPORTED:
        print("GPU support is Windows-only; nothing to do on", sys.platform)
        return 1

    target = gpu_accel.pack_dir()
    print(f"Pack {gpu_accel.PACK_ID} -> {target}")
    if gpu_accel.pack_installed():
        print("Already installed.")
        return 0

    last = [-1]
    started = time.monotonic()

    def progress(done: int, total: int) -> None:
        step = int(done * 10 / max(total, 1))
        if step != last[0]:
            last[0] = step
            mb = done / 1024 ** 2
            print(f"  {step * 10:3d}%  {mb:7.0f} / {total / 1024 ** 2:.0f} MB  "
                  f"({time.monotonic() - started:.0f} s)", flush=True)

    for attempt in range(1, max(1, args.retries) + 1):
        try:
            gpu_accel.download_pack(progress)
            break
        except Exception as exc:  # noqa: BLE001 - report and resume
            print(f"Attempt {attempt} failed: {exc}", flush=True)
            if attempt >= args.retries:
                return 1
            time.sleep(5 * attempt)

    if not gpu_accel.pack_installed():
        print("Download finished but the pack is not complete.")
        return 1
    size = gpu_accel.pack_size_on_disk() / 1024 ** 3
    print(f"Installed in {time.monotonic() - started:.0f} s, {size:.2f} GB on disk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
