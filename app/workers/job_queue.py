"""
Job queue for TTS generation.

Allows the user to submit multiple export jobs without waiting for
each one to finish.  Up to MAX_CONCURRENT jobs run simultaneously;
additional jobs wait in the pending list and are started automatically
as slots free up.

Usage
-----
    queue = JobQueue(parent=some_qobject)
    queue.job_completed.connect(my_slot)
    job_id = queue.submit(text=..., voice=..., ...)
    queue.cancel(job_id)   # cancel any time
    queue.cancel_all()     # shutdown — call before app exit
"""

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from app.workers.tts_worker import TTSWorker

logger = logging.getLogger(__name__)

MAX_CONCURRENT = 2   # safe limit for simultaneous edge_tts connections


# ------------------------------------------------------------------ #
# Job data                                                             #
# ------------------------------------------------------------------ #

@dataclass
class JobItem:
    """All state for one TTS export job."""

    # Supplied at creation
    text:          str
    voice:         str         # full short_name, e.g. "en-US-AvaNeural"
    voice_display: str         # compact display, e.g. "Ava · English US"
    rate:          str
    volume:        str
    output_path:   str

    # Derived / mutable
    id:          str   = field(default_factory=lambda: uuid.uuid4().hex[:8])
    filename:    str   = ""
    status:      str   = "queued"   # queued | running | completed | failed | cancelled
    progress:    int   = 0
    status_text: str   = "Queued"
    error:       str   = ""
    duration:    float = 0.0
    worker:      object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.filename:
            self.filename = Path(self.output_path).name

    # Make hashable by id so it can be stored in sets/dicts
    def __eq__(self, other: object) -> bool:
        return isinstance(other, JobItem) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


# ------------------------------------------------------------------ #
# Queue                                                                #
# ------------------------------------------------------------------ #

class JobQueue(QObject):
    """
    Manages concurrent TTS jobs.

    Signals
    -------
    job_submitted(JobItem)          Added to the pending list.
    job_started(JobItem)            Moved from pending → running.
    job_progress(str, int)          (job_id, 0-100) real streaming progress.
    job_status_changed(str, str)    (job_id, short status text).
    job_completed(JobItem)          Finished successfully.
    job_failed(JobItem)             Finished with an error.
    job_cancelled(JobItem)          Cancelled before or during run.
    """

    job_submitted      = Signal(object)       # JobItem
    job_started        = Signal(object)       # JobItem
    job_progress       = Signal(str, int)     # job_id, pct
    job_status_changed = Signal(str, str)     # job_id, text
    job_stage_changed  = Signal(str, str, str)  # job_id, kind, text
    job_speed_updated  = Signal(str, float)   # job_id, chars/s
    job_completed      = Signal(object)       # JobItem
    job_failed         = Signal(object)       # JobItem
    job_cancelled      = Signal(object)       # JobItem

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pending: list[JobItem] = []
        self._running: dict[str, tuple[JobItem, TTSWorker]] = {}
        # Workers that were individually cancelled: still running but popped
        # from _running.  Kept here so Python doesn't GC them mid-thread.
        self._finishing: list[TTSWorker] = []
        # All live workers (running + recently finished).  Holds a Python
        # strong reference so that CPython's refcount GC cannot destroy a
        # QThread while its OS thread is still executing.  Removed only
        # after the QThread.finished signal fires.
        self._active_workers: list[TTSWorker] = []

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def submit(
        self,
        text:          str,
        voice:         str,
        voice_display: str,
        rate:          str,
        volume:        str,
        output_path:   str,
    ) -> str:
        """
        Add a job to the queue.  Returns the new job's ID.
        Starts immediately if a concurrency slot is free.
        """
        if self.has_active_output_path(output_path):
            raise ValueError(
                "A job writing to this output path is already running or queued."
            )

        item = JobItem(
            text=text, voice=voice, voice_display=voice_display,
            rate=rate, volume=volume, output_path=output_path,
        )
        logger.info(
            "Job queued: id=%s voice=%s output=%s",
            item.id, voice, output_path,
        )
        self._pending.append(item)
        self.job_submitted.emit(item)
        self._try_start()
        return item.id

    def cancel(self, job_id: str) -> None:
        """Cancel a running or pending job by id."""
        if job_id in self._running:
            # Pop immediately so the concurrency slot is freed for new jobs.
            # The worker exits silently (no completed/failed signal) so we
            # cannot rely on _on_worker_* to clean up _running.
            item, worker = self._running.pop(job_id)
            logger.info("Cancelling running job: %s", job_id)
            item.status = "cancelled"
            worker.cancel()
            # Keep a reference so the QThread is not GC-collected while
            # still running; prune any already-finished workers first.
            self._finishing = [w for w in self._finishing if w.isRunning()]
            self._finishing.append(worker)
            self.job_cancelled.emit(item)
            self._try_start()   # fill the freed slot if pending jobs exist
            return

        for i, item in enumerate(self._pending):
            if item.id == job_id:
                logger.info("Cancelling pending job: %s", job_id)
                self._pending.pop(i)
                item.status = "cancelled"
                self.job_cancelled.emit(item)
                return

    def cancel_all(self) -> None:
        """
        Stop everything — called on app shutdown.
        Blocks briefly waiting for workers to exit cleanly.
        """
        pending_copy = self._pending[:]
        self._pending.clear()
        for item in pending_copy:
            item.status = "cancelled"
            self.job_cancelled.emit(item)

        for job_id, (item, worker) in list(self._running.items()):
            logger.info("Stopping worker for job: %s", job_id)
            item.status = "cancelled"
            worker.cancel()
            if not worker.wait(4_000):
                logger.warning("Worker %s timed out — terminating", job_id)
                worker.terminate()
                worker.wait(1_000)
        self._running.clear()

        # Also wait for any workers that were individually cancelled
        for worker in self._finishing:
            if worker.isRunning():
                if not worker.wait(4_000):
                    worker.terminate()
                    worker.wait(1_000)
        self._finishing.clear()
        self._active_workers.clear()

    def is_busy(self) -> bool:
        """True if any jobs are running or pending."""
        return bool(self._pending or self._running)

    def has_active_output_path(self, output_path: str) -> bool:
        """
        Return True if any running or pending job already targets *output_path*.

        Comparison is done on the resolved absolute path so that equivalent
        paths with different representations (e.g. trailing slash, symlinks)
        are treated as the same destination.
        """
        try:
            norm = str(Path(output_path).resolve())
        except Exception:
            norm = output_path
        for item, _ in self._running.values():
            try:
                if str(Path(item.output_path).resolve()) == norm:
                    return True
            except Exception:
                if item.output_path == output_path:
                    return True
        for item in self._pending:
            try:
                if str(Path(item.output_path).resolve()) == norm:
                    return True
            except Exception:
                if item.output_path == output_path:
                    return True
        return False

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # ------------------------------------------------------------------ #
    # Internal scheduling                                                  #
    # ------------------------------------------------------------------ #

    def _try_start(self) -> None:
        """Start pending jobs up to the concurrency limit."""
        while self._pending and len(self._running) < MAX_CONCURRENT:
            item = self._pending.pop(0)
            self._start_job(item)

    def _start_job(self, item: JobItem) -> None:
        worker = TTSWorker(
            text=item.text, voice=item.voice,
            rate=item.rate, volume=item.volume,
            output_path=item.output_path,
        )

        jid = item.id
        worker.progress.connect(
            lambda v, j=jid: self._on_progress(j, v)
        )
        worker.status_changed.connect(
            lambda s, j=jid: self._on_status_changed(j, s)
        )
        worker.stage_changed.connect(
            lambda kind, text, j=jid: self._on_stage_changed(j, kind, text)
        )
        worker.speed_updated.connect(
            lambda cps, j=jid: self._on_speed_updated(j, cps)
        )
        worker.completed.connect(
            lambda p, d, j=jid: self._on_worker_completed(j, p, d)
        )
        worker.failed.connect(
            lambda e, j=jid: self._on_worker_failed(j, e)
        )

        item.status      = "running"
        item.status_text = "Connecting…"
        item.worker      = worker
        self._running[jid] = (item, worker)

        # Keep a strong Python reference for the lifetime of the OS thread.
        # Without this, CPython's refcount GC can destroy the QThread object
        # before the thread has fully exited, causing "QThread destroyed
        # while still running" crashes — especially on Windows.
        self._active_workers.append(worker)
        worker.finished.connect(
            lambda w=worker: self._on_worker_thread_finished(w)
        )

        logger.info("Job started: id=%s voice=%s", jid, item.voice)
        self.job_started.emit(item)
        worker.start()

    # ------------------------------------------------------------------ #
    # Worker callbacks                                                     #
    # ------------------------------------------------------------------ #

    def _on_worker_thread_finished(self, worker: TTSWorker) -> None:
        """Remove from active list once the OS thread has fully exited."""
        try:
            self._active_workers.remove(worker)
        except ValueError:
            pass

    def _on_stage_changed(self, job_id: str, kind: str, text: str) -> None:
        if job_id in self._running:
            self.job_stage_changed.emit(job_id, kind, text)

    def _on_speed_updated(self, job_id: str, cps: float) -> None:
        if job_id in self._running:
            self.job_speed_updated.emit(job_id, cps)

    def _on_progress(self, job_id: str, pct: int) -> None:
        if job_id in self._running:
            item, _ = self._running[job_id]
            item.progress = pct
            self.job_progress.emit(job_id, pct)

    def _on_status_changed(self, job_id: str, text: str) -> None:
        if job_id in self._running:
            item, _ = self._running[job_id]
            item.status_text = text
            self.job_status_changed.emit(job_id, text)

    def _on_worker_completed(
        self, job_id: str, output_path: str, duration: float
    ) -> None:
        if job_id not in self._running:
            return
        item, _ = self._running.pop(job_id)
        if item.status == "cancelled":
            return
        item.status      = "completed"
        item.status_text = "Completed"
        item.progress    = 100
        item.duration    = duration
        item.worker      = None
        logger.info(
            "Job completed: id=%s duration=%.2fs output=%s",
            job_id, duration, output_path,
        )
        self.job_completed.emit(item)
        self._try_start()

    def _on_worker_failed(self, job_id: str, error: str) -> None:
        if job_id not in self._running:
            return
        item, _ = self._running.pop(job_id)
        if item.status == "cancelled":
            return
        item.status      = "failed"
        item.status_text = "Failed"
        item.error       = error
        item.worker      = None
        logger.error("Job failed: id=%s error=%s", job_id, error)
        self.job_failed.emit(item)
        self._try_start()
