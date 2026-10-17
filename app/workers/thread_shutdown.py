"""
Stopping worker QThreads at app exit without terminate().

QThread.terminate() kills a thread wherever it is — possibly holding the GIL
or a lock inside Python — which can deadlock or crash the exit.  Destroying a
QThread object that is still running aborts the process ("QThread: Destroyed
while thread is still running").  So a thread that will not stop in time is
left running and kept referenced until the process ends; the OS reclaims it.
Chunk manifests and caches are written atomically, so nothing is half-saved.
"""

import ctypes
import logging
import time

from PySide6.QtCore import QThread

logger = logging.getLogger(__name__)

# Threads that outlived their shutdown wait.  Never cleared while running.
_LINGERING: list[QThread] = []


def keep_alive(thread: QThread) -> None:
    """Hold *thread* until the process exits, even through interpreter teardown."""
    if thread in _LINGERING:
        return
    _LINGERING.append(thread)
    # Module globals are cleared during interpreter shutdown, which would free
    # the wrapper and destroy the running QThread after all.  One extra,
    # never-released reference keeps it alive until the process is gone.
    try:
        ctypes.pythonapi.Py_IncRef(ctypes.py_object(thread))
    except Exception:  # noqa: BLE001
        pass


def lingering() -> list[QThread]:
    return [t for t in _LINGERING if t.isRunning()]


def stop_threads(
    threads: list[tuple[str, QThread]],
    *,
    budget_s: float = 8.0,
    min_wait_s: float = 2.0,
) -> list[QThread]:
    """
    Ask every thread to stop, then wait for each — at least *min_wait_s*
    apiece, *budget_s* in all.  Threads still running are kept alive (never
    terminated) and returned.

    Everything is asked first so the threads wind down in parallel; the
    per-thread minimum keeps a late thread from getting a zero wait just
    because an earlier one used up a shared deadline.
    """
    for _name, thread in threads:
        _request_stop(thread)

    start = time.monotonic()
    still_running: list[QThread] = []
    for i, (name, thread) in enumerate(threads):
        if not thread.isRunning():
            continue
        left = budget_s - (time.monotonic() - start)
        fair_share = left / (len(threads) - i)
        wait_s = max(0.0, min(max(min_wait_s, fair_share), left))
        if thread.wait(int(wait_s * 1000)):
            continue
        logger.warning("Worker %s did not stop in time; leaving it to exit with the app",
                       name or "(unnamed)")
        keep_alive(thread)
        still_running.append(thread)
    return still_running


def _request_stop(thread: QThread) -> None:
    for method in ("cancel", "stop_playback"):
        fn = getattr(thread, method, None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001
                logger.debug("%s() failed during shutdown", method, exc_info=True)
    thread.requestInterruption()
    thread.quit()
