"""
Keep the computer awake while a job is generating audio.

A long audiobook job runs for hours.  If the system idle-sleeps part way,
the connection to the speech service is cut and every chunk in flight fails;
on wake the job has usually used up its retries.  ``keep_awake()`` holds an
"I'm busy" claim for as long as the block runs — the display may still turn
off, only system sleep is held off.

  * macOS   — ``caffeinate -i -w <pid>``: one shared process, reference
              counted across jobs.  ``-w`` makes it exit with SetupTTS, so a
              crash never leaves the Mac unable to sleep.
  * Windows — ``SetThreadExecutionState``, which is per thread: each worker
              thread sets and clears its own claim.
  * Linux   — ``systemd-inhibit`` when available, shared like macOS.

Failure to take the claim is logged and otherwise ignored: a job must never
fail because the computer could not be kept awake.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator

logger = logging.getLogger(__name__)

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

_lock = threading.Lock()
_holders = 0
_process: subprocess.Popen | None = None


def _inhibitor_command() -> list[str] | None:
    if sys.platform == "darwin":
        caffeinate = shutil.which("caffeinate") or "/usr/bin/caffeinate"
        if os.path.exists(caffeinate):
            return [caffeinate, "-i", "-w", str(os.getpid())]
        return None
    inhibit = shutil.which("systemd-inhibit")
    sleep = shutil.which("sleep")
    if inhibit and sleep:
        return [
            inhibit, "--what=idle:sleep", "--who=SetupTTS",
            "--why=Generating audio", "--mode=block", sleep, "infinity",
        ]
    return None


def _acquire_shared() -> None:
    global _holders, _process
    with _lock:
        _holders += 1
        if _process is not None and _process.poll() is None:
            return
        command = _inhibitor_command()
        if command is None:
            return
        try:
            _process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("Holding off system sleep while audio is generated")
        except OSError as exc:
            _process = None
            logger.warning("Could not keep the computer awake: %s", exc)


def _release_shared() -> None:
    global _holders, _process
    with _lock:
        _holders = max(0, _holders - 1)
        if _holders or _process is None:
            return
        proc, _process = _process, None
    with contextlib.suppress(Exception):
        proc.terminate()
        proc.wait(timeout=2)
    logger.info("System sleep allowed again")


def _set_windows_state(flags: int) -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception as exc:
        logger.warning("Could not change the Windows sleep setting: %s", exc)
        return False


@contextlib.contextmanager
def keep_awake() -> Iterator[None]:
    """Hold off idle system sleep for the duration of the block."""
    if sys.platform == "win32":
        claimed = _set_windows_state(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        try:
            yield
        finally:
            if claimed:
                _set_windows_state(_ES_CONTINUOUS)
        return

    _acquire_shared()
    try:
        yield
    finally:
        _release_shared()
