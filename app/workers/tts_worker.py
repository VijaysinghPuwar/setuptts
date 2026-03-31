"""
Background QThread worker for TTS generation.

Uses edge_tts streaming API for real word-boundary progress (3 %→95 %).
Long text is split into chunks that are sent individually to Microsoft's
Neural TTS service (speech.platform.bing.com).  This makes 9–12 hour
audiobook jobs robust: a brief disconnect at minute 25 only retries the
current chunk, not the whole job.

Adaptive chunk sizing
---------------------
The service synthesises at roughly 60-100 chars/s.  Large chunks (10 k chars)
regularly exceed 180 s at that rate, causing every chunk to time out and
retry.  We therefore scale chunk size down for long texts:

  total chars   max chunk size   ~seconds / chunk
  ──────────────────────────────────────────────
  < 50 000      8 000            50–130 s  (comfortable)
  ≥ 50 000      4 000            25– 65 s  (well inside timeout)

This makes long audiobook jobs far more reliable without changing the
number of retries or the per-chunk timeout.

Progress: 3 % → 95 % driven by WordBoundary events across all chunks,
then 100 % once the output file is fully written and synced.
"""

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path

import edge_tts
from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

# ── Adaptive chunk sizing ──────────────────────────────────────────────── #
# Short/medium jobs use larger chunks (fewer round-trips).
# Long jobs use smaller chunks so each request reliably completes within
# the timeout window even on the slowest observed service throughput.
_CHUNK_CHARS_DEFAULT = 8_000    # total text < _LONG_JOB_THRESHOLD
_CHUNK_CHARS_LONG    = 4_000    # total text ≥ _LONG_JOB_THRESHOLD
_LONG_JOB_THRESHOLD  = 50_000   # chars

# Timeout (seconds) for a single chunk attempt.
# With adaptive sizing a long-job chunk is ≤ 4 000 chars → ~25–65 s at
# observed service speed.  180 s gives a 2.5–7× safety margin.
_CHUNK_TIMEOUT_S = 180

# Retry strategy: attempt counts and backoff delays.
_MAX_ATTEMPTS = 5                  # 1 initial + 4 retries
_BACKOFF_BASE = 2.0                # seconds; doubles each retry: 2, 4, 8, 16


# ──────────────────────────────────────────────────────────────────── #
#  Helpers                                                              #
# ──────────────────────────────────────────────────────────────────── #

def _chunk_size_for(total_chars: int) -> int:
    """Return the appropriate max chunk size for a job of *total_chars*."""
    return _CHUNK_CHARS_LONG if total_chars >= _LONG_JOB_THRESHOLD else _CHUNK_CHARS_DEFAULT


# ──────────────────────────────────────────────────────────────────── #
#  Text-splitting helpers                                               #
# ──────────────────────────────────────────────────────────────────── #

def _split_text(text: str, max_chars: int) -> list[str]:
    """
    Split *text* into chunks of at most *max_chars* characters.

    Strategy (in order):
      1. Split at paragraph boundaries (two or more newlines).
      2. Merge short paragraphs up to the limit.
      3. Split long paragraphs at sentence boundaries (. ! ?).
      4. Hard-split at word boundaries as a last resort.

    Returns the original text as a single-element list when no split
    is needed.  Empty chunks are discarded.
    """
    if len(text) <= max_chars:
        return [text] if text.strip() else []
    chunks: list[str] = []
    _accumulate_para_chunks(re.split(r"\n{2,}", text), chunks, max_chars)
    return [c for c in chunks if c.strip()]


def _accumulate_para_chunks(
    paras: list[str], out: list[str], max_chars: int
) -> None:
    current: list[str] = []
    current_len = 0
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            if current:
                out.append("\n\n".join(current))
                current, current_len = [], 0
            _split_at_sentences(para, out, max_chars)
        elif current_len + len(para) + 2 > max_chars and current:
            out.append("\n\n".join(current))
            current, current_len = [para], len(para)
        else:
            current.append(para)
            current_len += len(para) + 2
    if current:
        out.append("\n\n".join(current))


def _split_at_sentences(text: str, out: list[str], max_chars: int) -> None:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    current: list[str] = []
    current_len = 0
    for sent in sentences:
        if current_len + len(sent) + 1 > max_chars and current:
            out.append(" ".join(current))
            current, current_len = [], 0
        if len(sent) > max_chars:
            if current:
                out.append(" ".join(current))
                current, current_len = [], 0
            _split_at_words(sent, out, max_chars)
        else:
            current.append(sent)
            current_len += len(sent) + 1
    if current:
        out.append(" ".join(current))


def _split_at_words(text: str, out: list[str], max_chars: int) -> None:
    words = text.split()
    if not words:
        return
    current: list[str] = []
    current_len = 0
    for word in words:
        # Hard-split pathological single tokens (e.g. very long URLs/hashes)
        while len(word) > max_chars:
            if current:
                out.append(" ".join(current))
                current, current_len = [], 0
            out.append(word[:max_chars])
            word = word[max_chars:]
        if current_len + len(word) + 1 > max_chars and current:
            out.append(" ".join(current))
            current, current_len = [], 0
        current.append(word)
        current_len += len(word) + 1
    if current:
        out.append(" ".join(current))


# ──────────────────────────────────────────────────────────────────── #
#  Worker                                                               #
# ──────────────────────────────────────────────────────────────────── #

class TTSWorker(QThread):
    """
    Signals
    -------
    progress(int)            0-100 based on words processed
    status_changed(str)      Short status string for the UI
    completed(str, float)    output_path, elapsed seconds
    failed(str)              User-friendly error message
    """

    progress       = Signal(int)
    status_changed = Signal(str)
    stage_changed  = Signal(str, str)   # (kind, text)  kind="local"|"remote"|"waiting"
    speed_updated  = Signal(float)      # chars/s, EMA-smoothed; emitted ~1/s during download
    completed      = Signal(str, float)
    failed         = Signal(str)

    def __init__(
        self,
        text: str,
        voice: str,
        rate: str,
        volume: str,
        output_path: str,
    ) -> None:
        super().__init__()
        self._text        = text
        self._voice       = voice
        self._rate        = rate
        self._volume      = volume
        self._output_path = output_path
        self._cancelled   = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_task: asyncio.Task | None = None
        self._last_pct: int = 0

    # ------------------------------------------------------------------ #
    # Public                                                               #
    # ------------------------------------------------------------------ #

    def cancel(self) -> None:
        """Cancel mid-stream.  Interrupts the async Task cleanly."""
        self._cancelled = True
        self.requestInterruption()
        loop = self._loop
        task = self._async_task
        if loop and not loop.is_closed() and task:
            loop.call_soon_threadsafe(task.cancel)

    # ------------------------------------------------------------------ #
    # QThread.run                                                          #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        start = time.monotonic()

        # On Windows, explicitly use ProactorEventLoop so that aiohttp's
        # WebSocket connections (used by edge_tts) work correctly.
        if sys.platform == "win32":
            self._loop = asyncio.ProactorEventLoop()
        else:
            self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._run_with_task())

            if self._cancelled:
                return

            elapsed = time.monotonic() - start
            logger.info(
                "Generation complete: voice=%s output=%s elapsed=%.2fs",
                self._voice, self._output_path, elapsed,
            )
            self.completed.emit(self._output_path, elapsed)

        except asyncio.CancelledError:
            logger.info("Generation cancelled: %s", self._output_path)

        except Exception as exc:
            logger.exception("TTS generation failed")
            if not self._cancelled:
                self.failed.emit(self._user_message(exc))

        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop       = None
            self._async_task = None
            asyncio.set_event_loop(None)

    # ------------------------------------------------------------------ #
    # Async internals                                                      #
    # ------------------------------------------------------------------ #

    async def _run_with_task(self) -> None:
        """Wrap generation in a named Task so cancel() can reach it."""
        self._async_task = asyncio.current_task()
        await self._stream_generate()

    async def _stream_generate(self) -> None:
        """
        Stream audio from the TTS service, writing to disk in real time.

        Pipeline (what runs where):
          LOCAL  — text splitting (regex, ~1 ms)
          REMOTE — WebSocket to Microsoft Neural TTS (speech.platform.bing.com)
                   All speech synthesis happens here; CPU stays near 0 %.
          LOCAL  — writing received MP3 bytes to disk, fsync after each chunk

        Progress: 3 % → 95 % from WordBoundary events; 100 % on file sync.
        """
        if self._cancelled:
            raise asyncio.CancelledError()

        # ── LOCAL: prepare text ────────────────────────────────────────── #
        self.status_changed.emit("Preparing…")
        self.stage_changed.emit("local", "Preparing text locally")
        self.progress.emit(3)

        total_chars   = max(len(self._text.strip()), 1)
        max_chunk_chars = _chunk_size_for(total_chars)

        self.stage_changed.emit("local", "Splitting text into chunks")
        chunks  = _split_text(self._text, max_chunk_chars)
        n_chunks = len(chunks)
        processed_chars = 0

        logger.info(
            "Starting: voice=%s rate=%s chars=%d chunks=%d "
            "chunk_size=%d output=%s",
            self._voice, self._rate, len(self._text), n_chunks,
            max_chunk_chars, self._output_path,
        )

        if n_chunks > 1:
            self.stage_changed.emit(
                "local",
                f"Split into {n_chunks} chunks "
                f"(≤{max_chunk_chars:,} chars each) — "
                f"sent to Microsoft one by one",
            )

        # ── REMOTE: connect ────────────────────────────────────────────── #
        self.status_changed.emit("Connecting…")
        self.stage_changed.emit(
            "remote",
            "Connecting to Microsoft Neural TTS  "
            "(speech.platform.bing.com) — low CPU is expected",
        )

        output_path = Path(self._output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        _job_start = time.monotonic()

        # ── Real-time speed tracking ───────────────────────────────────── #
        _spd_chars = [0]
        _spd_time  = [_job_start]
        _spd_ema   = [0.0]
        _chars_at_last_speed_emit = 0
        _time_at_last_speed_emit  = _job_start

        try:
            with open(output_path, "wb") as audio_file:
                for chunk_idx, text_chunk in enumerate(chunks):
                    if self._cancelled:
                        raise asyncio.CancelledError()

                    chunk_start = time.monotonic()
                    chunk_label = (
                        f"chunk {chunk_idx + 1}/{n_chunks}"
                        if n_chunks > 1 else "text"
                    )

                    if n_chunks > 1:
                        self.status_changed.emit(
                            f"Part {chunk_idx + 1} of {n_chunks}…"
                        )
                        self.stage_changed.emit(
                            "remote",
                            f"Sending {chunk_label} to Microsoft "
                            f"({len(text_chunk):,} chars)",
                        )
                    else:
                        self.status_changed.emit("Generating audio…")
                        self.stage_changed.emit(
                            "remote",
                            f"Sending text to Microsoft Neural TTS "
                            f"({len(text_chunk):,} chars) — "
                            "waiting for response…",
                        )

                    # Retry each chunk up to _MAX_ATTEMPTS times with
                    # exponential backoff.  Each attempt is wrapped in a
                    # _CHUNK_TIMEOUT_S timeout so a hung connection never
                    # stalls the app indefinitely.
                    last_exc: Exception | None = None
                    for attempt in range(_MAX_ATTEMPTS):
                        if self._cancelled:
                            raise asyncio.CancelledError()

                        if attempt > 0:
                            wait = _BACKOFF_BASE * (2 ** (attempt - 1))
                            logger.warning(
                                "Chunk %d/%d attempt %d/%d failed: %s"
                                " — retrying in %.0f s",
                                chunk_idx + 1, n_chunks,
                                attempt, _MAX_ATTEMPTS - 1,
                                last_exc, wait,
                            )
                            self.status_changed.emit(
                                f"Network issue — retrying"
                                f" ({attempt}/{_MAX_ATTEMPTS - 1})…"
                            )
                            self.stage_changed.emit(
                                "waiting",
                                f"Retry {attempt}/{_MAX_ATTEMPTS - 1} on "
                                f"{chunk_label} — waiting {wait:.0f} s "
                                f"before next attempt",
                            )
                            await asyncio.sleep(wait)

                        if self._cancelled:
                            raise asyncio.CancelledError()

                        try:
                            communicate = edge_tts.Communicate(
                                text=text_chunk,
                                voice=self._voice,
                                rate=self._rate,
                                volume=self._volume,
                            )

                            # _received_audio: mutable flag so the inner
                            # coroutine can emit the "Downloading" stage
                            # once on the first audio packet of this attempt.
                            _received_audio = [False]

                            async def _stream_chunk(
                                comm=communicate,
                                _ra=_received_audio,
                                _sc=_spd_chars,
                                _st=_spd_time,
                                _se=_spd_ema,
                            ):
                                async for ev in comm.stream():
                                    if self._cancelled:
                                        raise asyncio.CancelledError()
                                    if ev["type"] == "audio":
                                        if not _ra[0]:
                                            # First byte received — service
                                            # has started streaming back.
                                            if n_chunks > 1:
                                                self.stage_changed.emit(
                                                    "remote",
                                                    f"Receiving audio — "
                                                    f"{chunk_label}",
                                                )
                                            else:
                                                self.stage_changed.emit(
                                                    "remote",
                                                    "Receiving audio from "
                                                    "Microsoft Neural TTS",
                                                )
                                            _ra[0] = True
                                        audio_file.write(ev["data"])
                                    elif ev["type"] in (
                                        "WordBoundary", "SentenceBoundary"
                                    ):
                                        word = ev.get("text", "")
                                        nonlocal processed_chars
                                        processed_chars = min(
                                            processed_chars + len(word) + 1,
                                            total_chars,
                                        )
                                        # Throttle: emit only on integer %
                                        # change to avoid flooding the Qt
                                        # cross-thread queue on long jobs.
                                        pct = int(
                                            3
                                            + (processed_chars / total_chars)
                                            * 92
                                        )
                                        if pct != self._last_pct:
                                            self._last_pct = pct
                                            self.progress.emit(pct)

                                        # ── Real-time speed (≤1 emit/s) ── #
                                        _now = time.monotonic()
                                        _dt  = _now - _st[0]
                                        if _dt >= 1.0:
                                            _dc = processed_chars - _sc[0]
                                            if _dc > 0:
                                                raw = _dc / _dt
                                                _se[0] = (
                                                    0.65 * _se[0] + 0.35 * raw
                                                    if _se[0] > 0 else raw
                                                )
                                                self.speed_updated.emit(
                                                    _se[0]
                                                )
                                            _sc[0] = processed_chars
                                            _st[0] = _now

                            await asyncio.wait_for(
                                _stream_chunk(),
                                timeout=_CHUNK_TIMEOUT_S,
                            )

                            # ── LOCAL: flush to disk ───────────────────── #
                            self.stage_changed.emit(
                                "local",
                                f"Writing {chunk_label} to disk",
                            )
                            audio_file.flush()
                            try:
                                os.fsync(audio_file.fileno())
                            except OSError:
                                pass  # not all filesystems support fsync

                            chunk_elapsed = time.monotonic() - chunk_start

                            now = time.monotonic()
                            dt  = now - _time_at_last_speed_emit
                            if dt >= 0.5:
                                dc = processed_chars - _chars_at_last_speed_emit
                                if dc > 0 and dt > 0:
                                    cps = dc / dt
                                    self.stage_changed.emit(
                                        "local",
                                        f"Saved {chunk_label} "
                                        f"({chunk_elapsed:.1f} s) · "
                                        f"{cps:.0f} chars/s",
                                    )
                                else:
                                    self.stage_changed.emit(
                                        "local",
                                        f"Saved {chunk_label} "
                                        f"({chunk_elapsed:.1f} s)",
                                    )
                                _chars_at_last_speed_emit = processed_chars
                                _time_at_last_speed_emit  = now
                            else:
                                self.stage_changed.emit(
                                    "local",
                                    f"Saved {chunk_label} "
                                    f"({chunk_elapsed:.1f} s)",
                                )

                            last_exc = None
                            logger.info(
                                "Chunk %d/%d succeeded (attempt %d) in %.2fs",
                                chunk_idx + 1, n_chunks, attempt + 1,
                                chunk_elapsed,
                            )
                            break  # chunk done; move to next

                        except asyncio.CancelledError:
                            raise  # never retry on cancellation

                        except asyncio.TimeoutError as exc:
                            last_exc = exc
                            logger.warning(
                                "Chunk %d/%d timed out after %ds (attempt %d)",
                                chunk_idx + 1, n_chunks,
                                _CHUNK_TIMEOUT_S, attempt + 1,
                            )

                        except Exception as exc:
                            last_exc = exc

                    if last_exc is not None:
                        # All retries exhausted — wrap with chunk context so
                        # _user_message can produce a helpful error string.
                        raise _ChunkError(
                            chunk_idx + 1, n_chunks, last_exc
                        ) from last_exc

        except asyncio.CancelledError:
            logger.info("Stream cancelled — removing partial file")
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

        # Second cancellation guard for the case where _cancelled was set
        # after the last chunk completed but before the file stat below.
        if self._cancelled:
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise asyncio.CancelledError()

        size = output_path.stat().st_size
        total_elapsed = time.monotonic() - _job_start
        logger.info(
            "File written: %s  size=%d bytes  chunks=%d  total=%.2fs",
            output_path, size, n_chunks, total_elapsed,
        )
        # ── LOCAL: finalize ────────────────────────────────────────────── #
        self.stage_changed.emit("local", "Finalizing MP3 file")
        self.status_changed.emit("Done")
        self.progress.emit(100)

    # ------------------------------------------------------------------ #
    # Error messages                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _user_message(exc: Exception) -> str:
        # Chunk-level failure with known position
        if isinstance(exc, _ChunkError):
            cause_msg = str(exc.cause).lower()
            chunk_ctx = f"chunk {exc.chunk}/{exc.total}"

            if "timeout" in cause_msg or isinstance(exc.cause, asyncio.TimeoutError):
                return (
                    f"Generation timed out on {chunk_ctx} after "
                    f"{_MAX_ATTEMPTS} attempts.\n\n"
                    "The Microsoft speech service is responding slowly.\n"
                    "Try again — transient slowdowns usually resolve quickly."
                )
            if any(
                k in cause_msg
                for k in ("connection", "network", "resolve", "ssl", "wss",
                          "websocket", "connecterror", "connectionerror",
                          "dns", "nodename", "servname", "gaierror")
            ):
                return (
                    f"Could not reach the speech service on {chunk_ctx}.\n\n"
                    "Please check your internet connection and try again.\n"
                    "If the problem persists, wait a minute — the service\n"
                    "may be temporarily unavailable."
                )
            return (
                f"Generation failed on {chunk_ctx} after "
                f"{_MAX_ATTEMPTS} attempts.\n\n"
                f"Details: {exc.cause}\n\n"
                "Please check the log file for more information."
            )

        msg = str(exc).lower()
        if any(
            k in msg
            for k in (
                "connection", "network", "resolve", "ssl", "wss",
                "websocket", "connecterror", "connectionerror",
                "dns", "nodename", "servname", "gaierror",
            )
        ):
            return (
                "Could not reach the Microsoft speech service.\n\n"
                "Please check your internet connection and try again.\n"
                "Tip: For very long text, transient network errors are more\n"
                "common. The app already retries each section automatically."
            )
        if "timeout" in msg:
            return (
                "The speech service timed out.\n\n"
                "Please try again. If this keeps happening, check your\n"
                "internet connection — the service may be under heavy load."
            )
        if any(k in msg for k in ("permission", "access denied", "read-only")):
            return (
                "Cannot write to the selected output location.\n"
                "Please choose a different folder."
            )
        if any(k in msg for k in ("no such file", "directory")):
            return (
                "The output folder does not exist.\n"
                "Please select a valid save location."
            )
        return (
            f"An unexpected error occurred while generating audio.\n\n"
            f"Details: {exc}\n\n"
            "Please check the log file for more information."
        )


# ──────────────────────────────────────────────────────────────────── #
#  Internal exception used to carry chunk context through the call     #
#  stack to _user_message.                                             #
# ──────────────────────────────────────────────────────────────────── #

class _ChunkError(RuntimeError):
    """Raised when all retries for one chunk are exhausted."""

    def __init__(self, chunk: int, total: int, cause: Exception) -> None:
        super().__init__(
            f"All {_MAX_ATTEMPTS} attempts failed for chunk {chunk}/{total}"
        )
        self.chunk = chunk
        self.total = total
        self.cause = cause
