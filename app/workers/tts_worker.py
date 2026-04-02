"""
Background QThread worker for TTS generation.

Generation stays compatible with Microsoft's Edge voices, but the runtime
path is now more defensive:

- chunks are kept below a safe payload size so edge_tts does not silently
  re-split them internally;
- long jobs run a tiny preflight synthesis before the full export starts;
- every retry uses a fresh edge_tts session;
- audio is buffered per attempt and only written after the whole attempt
  succeeds, so failed retries never poison the final MP3;
- repeated "NoAudioReceived" failures switch to smaller recovery chunks
  instead of hammering the same doomed request five times.
"""

import asyncio
from dataclasses import dataclass
import io
import logging
import os
import re
import sys
import time
from pathlib import Path

import aiohttp
from edge_tts import exceptions as edge_exceptions
from edge_tts.communicate import escape, remove_incompatible_characters
from PySide6.QtCore import QThread, Signal

from app.services.tts_service import (
    DEFAULT_CONNECT_TIMEOUT_S,
    build_communicate,
    list_voices,
)

logger = logging.getLogger(__name__)

# ── Chunk sizing ──────────────────────────────────────────────────────────── #
_CHUNK_CHARS_DEFAULT = 8_000
_CHUNK_CHARS_LONG = 4_000
_LONG_JOB_THRESHOLD = 50_000

# Keep well under edge_tts's internal 4096-byte boundary so each SetupTTS
# chunk maps to one actual provider request even for multi-byte languages or
# XML-escaped content.
_CHUNK_PAYLOAD_BYTES_DEFAULT = 3_600
_CHUNK_PAYLOAD_BYTES_LONG = 3_000

# Long jobs start with a smaller probe chunk so the first real connection is
# conservative and any provider issue shows up early.
_FIRST_CHUNK_PROBE_CHARS = 1_200
_FIRST_CHUNK_PROBE_PAYLOAD_BYTES = 1_200

# ── Validation and timeout strategy ──────────────────────────────────────── #
_PREFLIGHT_THRESHOLD = 12_000
_PREFLIGHT_SAMPLE_CHARS = 220
_PREFLIGHT_SAMPLE_PAYLOAD_BYTES = 360
_PREFLIGHT_TIMEOUT_S = 45

_CHUNK_TIMEOUT_MIN_S = 90
_CHUNK_TIMEOUT_MAX_S = 240
_EDGE_RECEIVE_TIMEOUT_MIN_S = 90
_EDGE_RECEIVE_TIMEOUT_MAX_S = 180

# ── Retry strategy ───────────────────────────────────────────────────────── #
_MAX_ATTEMPTS = 5
_NO_AUDIO_MAX_ATTEMPTS = 2
_BACKOFF_BASE = 2.0

# ── Adaptive recovery ────────────────────────────────────────────────────── #
_MAX_RECOVERY_DEPTH = 3
_MIN_RECOVERY_CHARS = 180
_MIN_RECOVERY_PAYLOAD_BYTES = 320

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")


def _chunk_size_for(total_chars: int) -> int:
    return _CHUNK_CHARS_LONG if total_chars >= _LONG_JOB_THRESHOLD else _CHUNK_CHARS_DEFAULT


def _payload_limit_for(total_chars: int) -> int:
    return (
        _CHUNK_PAYLOAD_BYTES_LONG
        if total_chars >= _LONG_JOB_THRESHOLD
        else _CHUNK_PAYLOAD_BYTES_DEFAULT
    )


def _edge_payload_size(text: str) -> int:
    """Approximate the payload size edge_tts will actually send."""
    cleaned = remove_incompatible_characters(text)
    return len(escape(cleaned).encode("utf-8"))


def _fits_chunk(text: str, max_chars: int, max_payload_bytes: int) -> bool:
    return len(text) <= max_chars and _edge_payload_size(text) <= max_payload_bytes


def _split_text(text: str, max_chars: int, max_payload_bytes: int) -> list[str]:
    """
    Split text into byte-safe chunks while preferring natural boundaries.

    Strategy:
      1. paragraph boundaries
      2. sentence boundaries
      3. word boundaries
      4. hard split as a last resort
    """
    text = text.strip()
    if not text:
        return []
    if _fits_chunk(text, max_chars, max_payload_bytes):
        return [text]

    chunks: list[str] = []
    _accumulate_para_chunks(
        re.split(r"\n{2,}", text),
        chunks,
        max_chars,
        max_payload_bytes,
    )
    return [chunk for chunk in chunks if chunk.strip()]


def _accumulate_para_chunks(
    paras: list[str],
    out: list[str],
    max_chars: int,
    max_payload_bytes: int,
) -> None:
    current: list[str] = []

    for para in paras:
        para = para.strip()
        if not para:
            continue

        if not _fits_chunk(para, max_chars, max_payload_bytes):
            if current:
                out.append("\n\n".join(current))
                current = []
            _split_at_sentences(para, out, max_chars, max_payload_bytes)
            continue

        candidate = "\n\n".join([*current, para]) if current else para
        if current and not _fits_chunk(candidate, max_chars, max_payload_bytes):
            out.append("\n\n".join(current))
            current = [para]
        else:
            current.append(para)

    if current:
        out.append("\n\n".join(current))


def _split_at_sentences(
    text: str,
    out: list[str],
    max_chars: int,
    max_payload_bytes: int,
) -> None:
    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    if len(sentences) <= 1:
        _split_at_words(text, out, max_chars, max_payload_bytes)
        return

    current: list[str] = []
    for sentence in sentences:
        if not _fits_chunk(sentence, max_chars, max_payload_bytes):
            if current:
                out.append(" ".join(current))
                current = []
            _split_at_words(sentence, out, max_chars, max_payload_bytes)
            continue

        candidate = " ".join([*current, sentence]) if current else sentence
        if current and not _fits_chunk(candidate, max_chars, max_payload_bytes):
            out.append(" ".join(current))
            current = [sentence]
        else:
            current.append(sentence)

    if current:
        out.append(" ".join(current))


def _split_at_words(
    text: str,
    out: list[str],
    max_chars: int,
    max_payload_bytes: int,
) -> None:
    words = text.split()
    if not words:
        return

    current: list[str] = []
    for word in words:
        if not _fits_chunk(word, max_chars, max_payload_bytes):
            if current:
                out.append(" ".join(current))
                current = []
            out.extend(_hard_split_text(word, max_chars, max_payload_bytes))
            continue

        candidate = " ".join([*current, word]) if current else word
        if current and not _fits_chunk(candidate, max_chars, max_payload_bytes):
            out.append(" ".join(current))
            current = [word]
        else:
            current.append(word)

    if current:
        out.append(" ".join(current))


def _hard_split_text(text: str, max_chars: int, max_payload_bytes: int) -> list[str]:
    """Split a pathological fragment (long token / no spaces) into safe pieces."""
    remaining = text.strip()
    pieces: list[str] = []

    while remaining:
        hi = min(len(remaining), max_chars)
        lo = 1
        best = 1

        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = remaining[:mid].strip()
            if candidate and _fits_chunk(candidate, max_chars, max_payload_bytes):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        piece = remaining[:best].strip()
        if not piece:
            piece = remaining[0]
            best = 1

        pieces.append(piece)
        remaining = remaining[best:].lstrip()

    return pieces


def _apply_first_chunk_probe(chunks: list[str], total_chars: int) -> list[str]:
    if total_chars < _PREFLIGHT_THRESHOLD or len(chunks) < 2:
        return chunks

    first = chunks[0]
    if (
        len(first) <= _FIRST_CHUNK_PROBE_CHARS
        and _edge_payload_size(first) <= _FIRST_CHUNK_PROBE_PAYLOAD_BYTES
    ):
        return chunks

    probe_chunks = _split_text(
        first,
        _FIRST_CHUNK_PROBE_CHARS,
        _FIRST_CHUNK_PROBE_PAYLOAD_BYTES,
    )
    if len(probe_chunks) <= 1:
        return chunks

    return [*probe_chunks, *chunks[1:]]


def _voice_locale(short_name: str) -> str:
    parts = short_name.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else short_name


def _find_voice(voices: list[dict], short_name: str) -> dict | None:
    return next((voice for voice in voices if voice.get("ShortName") == short_name), None)


def _suggest_alternative_voice(selected_voice: str, voices: list[dict]) -> str | None:
    locale = _voice_locale(selected_voice)
    same_locale = [
        voice.get("ShortName", "")
        for voice in voices
        if voice.get("ShortName") != selected_voice
        and voice.get("Locale") == locale
    ]
    if same_locale:
        return same_locale[0]

    language = locale.split("-")[0]
    same_language = [
        voice.get("ShortName", "")
        for voice in voices
        if voice.get("ShortName") != selected_voice
        and voice.get("Locale", "").split("-")[0] == language
    ]
    return same_language[0] if same_language else None


@dataclass
class _ProgressState:
    processed_chars: int
    spd_chars: int
    spd_time: float
    spd_ema: float
    chars_at_last_stage_emit: int
    time_at_last_stage_emit: float


@dataclass
class _AttemptStats:
    audio_bytes: int = 0
    metadata_events: int = 0
    attempt_chars: int = 0


class _AttemptFailure(RuntimeError):
    def __init__(
        self,
        kind: str,
        detail: str,
        *,
        original: Exception | None = None,
        suggestion: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.kind = kind
        self.original = original
        self.suggestion = suggestion


class _PreflightError(RuntimeError):
    def __init__(
        self,
        voice: str,
        cause: _AttemptFailure,
        *,
        suggestion: str | None = None,
    ) -> None:
        super().__init__(f"Preflight failed for {voice}: {cause}")
        self.voice = voice
        self.cause = cause
        self.suggestion = suggestion


class _ChunkError(RuntimeError):
    """Raised when all recovery options for one logical chunk are exhausted."""

    def __init__(self, chunk: int, total: int, cause: _AttemptFailure) -> None:
        super().__init__(f"Chunk {chunk}/{total} failed: {cause}")
        self.chunk = chunk
        self.total = total
        self.cause = cause


class TTSWorker(QThread):
    """
    Signals
    -------
    progress(int)            0-100 based on words processed
    status_changed(str)      Short status string for the UI
    completed(str, float)    output_path, elapsed seconds
    failed(str)              User-friendly error message
    """

    progress = Signal(int)
    status_changed = Signal(str)
    stage_changed = Signal(str, str)   # kind="local"|"remote"|"waiting"
    speed_updated = Signal(float)      # chars/s
    completed = Signal(str, float)
    failed = Signal(str)

    def __init__(
        self,
        text: str,
        voice: str,
        rate: str,
        volume: str,
        output_path: str,
    ) -> None:
        super().__init__()
        self._text = text
        self._voice = voice
        self._rate = rate
        self._volume = volume
        self._output_path = output_path
        self._cancelled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_task: asyncio.Task | None = None
        self._last_pct = 0

    def cancel(self) -> None:
        """Cancel mid-stream. Interrupts the async Task cleanly."""
        self._cancelled = True
        self.requestInterruption()
        loop = self._loop
        task = self._async_task
        if loop and not loop.is_closed() and task:
            loop.call_soon_threadsafe(task.cancel)

    def run(self) -> None:
        start = time.monotonic()

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
                self._voice,
                self._output_path,
                elapsed,
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
            self._loop = None
            self._async_task = None
            asyncio.set_event_loop(None)

    async def _run_with_task(self) -> None:
        self._async_task = asyncio.current_task()
        await self._stream_generate()

    async def _stream_generate(self) -> None:
        if self._cancelled:
            raise asyncio.CancelledError()

        self.status_changed.emit("Preparing…")
        self.stage_changed.emit("local", "Preparing text locally")
        self.progress.emit(3)

        stripped_text = self._text.strip()
        total_chars = max(len(stripped_text), 1)
        max_chunk_chars = _chunk_size_for(total_chars)
        max_payload_bytes = _payload_limit_for(total_chars)

        self.status_changed.emit("Validating voice…")
        self.stage_changed.emit(
            "remote",
            f"Validating selected voice ({self._voice}) with Microsoft",
        )
        voices = await list_voices()
        selected_voice = _find_voice(voices, self._voice)
        if selected_voice is None:
            voices = await list_voices(force_refresh=True)
            selected_voice = _find_voice(voices, self._voice)
            if selected_voice is None:
                raise _PreflightError(
                    self._voice,
                    _AttemptFailure(
                        "invalid_voice",
                        f"Voice {self._voice} is not present in the current voice catalog.",
                        suggestion=_suggest_alternative_voice(self._voice, voices),
                    ),
                    suggestion=_suggest_alternative_voice(self._voice, voices),
                )

        if total_chars >= _PREFLIGHT_THRESHOLD:
            await self._run_preflight(voices)

        self.stage_changed.emit("local", "Splitting text into chunks")
        chunks = _split_text(stripped_text, max_chunk_chars, max_payload_bytes)
        chunks = _apply_first_chunk_probe(chunks, total_chars)
        n_chunks = len(chunks)
        if not chunks:
            raise ValueError("No text was available to generate.")

        logger.info(
            "Starting: voice=%s rate=%s chars=%d chunks=%d chunk_size=%d payload_limit=%d output=%s",
            self._voice,
            self._rate,
            len(self._text),
            n_chunks,
            max_chunk_chars,
            max_payload_bytes,
            self._output_path,
        )

        if n_chunks > 1:
            self.stage_changed.emit(
                "local",
                f"Split into {n_chunks} chunks (≤{max_chunk_chars:,} chars / ≤{max_payload_bytes:,} bytes each)",
            )

        self.status_changed.emit("Connecting…")
        self.stage_changed.emit(
            "remote",
            "Connecting to Microsoft Neural TTS (speech.platform.bing.com)",
        )

        output_path = Path(self._output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        job_start = time.monotonic()
        progress_state = _ProgressState(
            processed_chars=0,
            spd_chars=0,
            spd_time=job_start,
            spd_ema=0.0,
            chars_at_last_stage_emit=0,
            time_at_last_stage_emit=job_start,
        )

        try:
            with open(output_path, "wb") as audio_file:
                for chunk_idx, text_chunk in enumerate(chunks):
                    if self._cancelled:
                        raise asyncio.CancelledError()

                    await self._process_chunk(
                        audio_file=audio_file,
                        text_chunk=text_chunk,
                        chunk_idx=chunk_idx,
                        n_chunks=n_chunks,
                        total_chars=total_chars,
                        progress_state=progress_state,
                        char_limit=max_chunk_chars,
                        payload_limit=max_payload_bytes,
                    )

        except asyncio.CancelledError:
            logger.info("Stream cancelled — removing partial file")
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

        if self._cancelled:
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise asyncio.CancelledError()

        size = output_path.stat().st_size
        total_elapsed = time.monotonic() - job_start
        if size <= 0:
            raise RuntimeError("The speech service completed without writing any audio.")

        logger.info(
            "File written: %s size=%d bytes chunks=%d total=%.2fs",
            output_path,
            size,
            n_chunks,
            total_elapsed,
        )
        self.stage_changed.emit("local", "Finalizing MP3 file")
        self.status_changed.emit("Done")
        self.progress.emit(100)

    async def _run_preflight(self, voices: list[dict]) -> None:
        sample = self._preflight_sample_text()
        if not sample:
            return

        self.status_changed.emit("Validating voice…")
        self.stage_changed.emit(
            "remote",
            "Running a small startup synthesis check before the full job",
        )

        last_failure: _AttemptFailure | None = None
        for attempt in range(2):
            if self._cancelled:
                raise asyncio.CancelledError()

            if attempt > 0:
                await asyncio.sleep(1.5)

            try:
                await self._synthesise_attempt(
                    sample,
                    chunk_label="startup check",
                    total_chars=1,
                    progress_state=None,
                    timeout_s=_PREFLIGHT_TIMEOUT_S,
                )
                return
            except _AttemptFailure as exc:
                last_failure = exc
                if exc.kind in {"invalid_voice", "no_audio", "metadata_without_audio"}:
                    break

        if last_failure is None:
            return

        suggestion = _suggest_alternative_voice(self._voice, voices)
        if last_failure.kind in {"no_audio", "metadata_without_audio"}:
            refreshed = await list_voices(force_refresh=True)
            if _find_voice(refreshed, self._voice) is None:
                suggestion = _suggest_alternative_voice(self._voice, refreshed)
                last_failure = _AttemptFailure(
                    "invalid_voice",
                    f"Voice {self._voice} is no longer available from the speech service.",
                    suggestion=suggestion,
                )
            else:
                suggestion = _suggest_alternative_voice(self._voice, refreshed)

        raise _PreflightError(self._voice, last_failure, suggestion=suggestion)

    def _preflight_sample_text(self) -> str:
        sample_chunks = _split_text(
            self._text.strip(),
            _PREFLIGHT_SAMPLE_CHARS,
            _PREFLIGHT_SAMPLE_PAYLOAD_BYTES,
        )
        return sample_chunks[0] if sample_chunks else ""

    async def _process_chunk(
        self,
        *,
        audio_file,
        text_chunk: str,
        chunk_idx: int,
        n_chunks: int,
        total_chars: int,
        progress_state: _ProgressState,
        char_limit: int,
        payload_limit: int,
        depth: int = 0,
        display_label: str | None = None,
    ) -> None:
        chunk_number = chunk_idx + 1
        chunk_label = display_label or (
            f"chunk {chunk_number}/{n_chunks}" if n_chunks > 1 else "text"
        )

        if n_chunks > 1:
            self.status_changed.emit(f"Part {chunk_number} of {n_chunks}…")
            if depth == 0:
                self.stage_changed.emit(
                    "remote",
                    f"Sending {chunk_label} to Microsoft ({len(text_chunk):,} chars)",
                )
            else:
                self.stage_changed.emit(
                    "remote",
                    f"Retrying {chunk_label} with a smaller recovery section ({len(text_chunk):,} chars)",
                )
        else:
            self.status_changed.emit("Generating audio…")
            self.stage_changed.emit(
                "remote",
                f"Sending text to Microsoft Neural TTS ({len(text_chunk):,} chars)",
            )

        last_failure: _AttemptFailure | None = None
        chunk_start = time.monotonic()

        for attempt in range(_MAX_ATTEMPTS):
            if self._cancelled:
                raise asyncio.CancelledError()

            if attempt > 0:
                wait = _BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "%s attempt %d/%d failed: %s — retrying in %.0f s",
                    chunk_label,
                    attempt,
                    _MAX_ATTEMPTS - 1,
                    last_failure,
                    wait,
                )
                self.status_changed.emit(self._retry_status_text(last_failure, attempt))
                self.stage_changed.emit(
                    "waiting",
                    f"Retry {attempt}/{_MAX_ATTEMPTS - 1} on {chunk_label} — waiting {wait:.0f} s before a fresh connection",
                )
                await asyncio.sleep(wait)

            try:
                timeout_s = self._chunk_timeout_for(text_chunk)
                audio_bytes, attempt_chars = await self._synthesise_attempt(
                    text_chunk,
                    chunk_label=chunk_label,
                    total_chars=total_chars,
                    progress_state=progress_state,
                    timeout_s=timeout_s,
                )

                self.stage_changed.emit("local", f"Writing {chunk_label} to disk")
                audio_file.write(audio_bytes)
                audio_file.flush()
                try:
                    os.fsync(audio_file.fileno())
                except OSError:
                    pass

                progress_state.processed_chars = min(
                    progress_state.processed_chars + attempt_chars,
                    total_chars,
                )
                self._emit_progress_from_chars(progress_state.processed_chars, total_chars)

                chunk_elapsed = time.monotonic() - chunk_start
                self._emit_saved_stage(chunk_label, chunk_elapsed, progress_state)
                logger.info(
                    "%s succeeded (attempt %d) in %.2fs bytes=%d",
                    chunk_label,
                    attempt + 1,
                    chunk_elapsed,
                    len(audio_bytes),
                )
                return

            except asyncio.CancelledError:
                raise

            except _AttemptFailure as exc:
                last_failure = exc
                logger.warning("%s failed on attempt %d: %s", chunk_label, attempt + 1, exc)
                if exc.kind in {"no_audio", "metadata_without_audio"} and attempt + 1 >= _NO_AUDIO_MAX_ATTEMPTS:
                    logger.warning(
                        "%s returned no audio repeatedly; stopping full-size retries early",
                        chunk_label,
                    )
                    break

        if last_failure is not None:
            recovery_chunks = self._split_for_recovery(text_chunk, char_limit, payload_limit, depth)
            if recovery_chunks:
                self.status_changed.emit("Recovering failed chunk…")
                self.stage_changed.emit(
                    "waiting",
                    f"{chunk_label} kept failing — retrying {len(recovery_chunks)} smaller sections",
                )
                logger.warning(
                    "%s failed after retries (%s) — splitting into %d smaller sections",
                    chunk_label,
                    last_failure.kind,
                    len(recovery_chunks),
                )
                next_char_limit = max(_MIN_RECOVERY_CHARS, char_limit // 2)
                next_payload_limit = max(_MIN_RECOVERY_PAYLOAD_BYTES, payload_limit // 2)
                for sub_idx, subchunk in enumerate(recovery_chunks, start=1):
                    await self._process_chunk(
                        audio_file=audio_file,
                        text_chunk=subchunk,
                        chunk_idx=chunk_idx,
                        n_chunks=n_chunks,
                        total_chars=total_chars,
                        progress_state=progress_state,
                        char_limit=next_char_limit,
                        payload_limit=next_payload_limit,
                        depth=depth + 1,
                        display_label=f"{chunk_label} · recovery {sub_idx}/{len(recovery_chunks)}",
                    )
                return

        raise _ChunkError(chunk_number, n_chunks, last_failure or _AttemptFailure("unexpected", "Unknown chunk failure"))

    def _split_for_recovery(
        self,
        text_chunk: str,
        char_limit: int,
        payload_limit: int,
        depth: int,
    ) -> list[str]:
        if depth >= _MAX_RECOVERY_DEPTH:
            return []
        if len(text_chunk) <= _MIN_RECOVERY_CHARS:
            return []
        if _edge_payload_size(text_chunk) <= _MIN_RECOVERY_PAYLOAD_BYTES:
            return []

        next_char_limit = max(_MIN_RECOVERY_CHARS, char_limit // 2)
        next_payload_limit = max(_MIN_RECOVERY_PAYLOAD_BYTES, payload_limit // 2)
        if next_char_limit >= len(text_chunk) and next_payload_limit >= _edge_payload_size(text_chunk):
            return []

        recovery_chunks = _split_text(text_chunk, next_char_limit, next_payload_limit)
        return recovery_chunks if len(recovery_chunks) > 1 else []

    async def _synthesise_attempt(
        self,
        text: str,
        *,
        chunk_label: str,
        total_chars: int,
        progress_state: _ProgressState | None,
        timeout_s: int,
    ) -> tuple[bytes, int]:
        stats = _AttemptStats()
        audio_buffer = io.BytesIO()
        receive_timeout = max(
            _EDGE_RECEIVE_TIMEOUT_MIN_S,
            min(_EDGE_RECEIVE_TIMEOUT_MAX_S, timeout_s - 15),
        )
        communicate = build_communicate(
            text=text,
            voice=self._voice,
            rate=self._rate,
            volume=self._volume,
            connect_timeout=DEFAULT_CONNECT_TIMEOUT_S,
            receive_timeout=receive_timeout,
        )

        async def _consume_stream() -> None:
            async for event in communicate.stream():
                if self._cancelled:
                    raise asyncio.CancelledError()

                if event["type"] == "audio":
                    if stats.audio_bytes == 0:
                        self.stage_changed.emit("remote", f"Receiving audio — {chunk_label}")
                    data = event["data"]
                    stats.audio_bytes += len(data)
                    audio_buffer.write(data)
                    continue

                if event["type"] in ("WordBoundary", "SentenceBoundary"):
                    stats.metadata_events += 1
                    if progress_state is None:
                        continue

                    word = event.get("text", "")
                    stats.attempt_chars = min(
                        stats.attempt_chars + len(word) + 1,
                        max(len(text), 1),
                    )
                    total_processed = min(
                        progress_state.processed_chars + stats.attempt_chars,
                        total_chars,
                    )
                    self._emit_progress_from_chars(total_processed, total_chars)
                    self._maybe_emit_speed(total_processed, progress_state)

        try:
            await asyncio.wait_for(_consume_stream(), timeout=timeout_s)

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            self._rollback_failed_attempt(progress_state, total_chars)
            raise self._classify_attempt_failure(exc, stats, chunk_label) from exc

        if stats.audio_bytes == 0:
            self._rollback_failed_attempt(progress_state, total_chars)
            kind = "metadata_without_audio" if stats.metadata_events else "no_audio"
            detail = (
                f"The speech service returned metadata but no audio for {chunk_label}."
                if stats.metadata_events
                else f"The speech service returned no audio for {chunk_label}."
            )
            raise _AttemptFailure(kind, detail)

        return audio_buffer.getvalue(), stats.attempt_chars

    def _rollback_failed_attempt(
        self,
        progress_state: _ProgressState | None,
        total_chars: int,
    ) -> None:
        if progress_state is not None:
            self._emit_progress_from_chars(progress_state.processed_chars, total_chars)

    @staticmethod
    def _chunk_timeout_for(text: str) -> int:
        estimated = 35 + (_edge_payload_size(text) / 30.0)
        return int(max(_CHUNK_TIMEOUT_MIN_S, min(_CHUNK_TIMEOUT_MAX_S, estimated)))

    @staticmethod
    def _retry_status_text(failure: _AttemptFailure | None, attempt: int) -> str:
        if failure is None:
            return f"Retrying ({attempt}/{_MAX_ATTEMPTS - 1})…"
        if failure.kind in {"dns"}:
            return f"DNS issue — retrying ({attempt}/{_MAX_ATTEMPTS - 1})…"
        if failure.kind.startswith("timeout"):
            return f"Speech request timed out — retrying ({attempt}/{_MAX_ATTEMPTS - 1})…"
        if failure.kind in {"no_audio", "metadata_without_audio"}:
            return f"No audio received — retrying ({attempt}/{_MAX_ATTEMPTS - 1})…"
        return f"Network issue — retrying ({attempt}/{_MAX_ATTEMPTS - 1})…"

    def _emit_progress_from_chars(self, processed_chars: int, total_chars: int) -> None:
        pct = int(3 + (processed_chars / max(total_chars, 1)) * 92)
        pct = max(3, min(95, pct))
        if pct != self._last_pct:
            self._last_pct = pct
            self.progress.emit(pct)

    def _maybe_emit_speed(self, total_processed: int, state: _ProgressState) -> None:
        now = time.monotonic()
        dt = now - state.spd_time
        if dt < 1.0:
            return

        delta_chars = total_processed - state.spd_chars
        if delta_chars > 0 and dt > 0:
            raw = delta_chars / dt
            state.spd_ema = 0.65 * state.spd_ema + 0.35 * raw if state.spd_ema > 0 else raw
            self.speed_updated.emit(state.spd_ema)

        state.spd_chars = total_processed
        state.spd_time = now

    def _emit_saved_stage(
        self,
        chunk_label: str,
        chunk_elapsed: float,
        state: _ProgressState,
    ) -> None:
        now = time.monotonic()
        dt = now - state.time_at_last_stage_emit
        if dt >= 0.5:
            delta_chars = state.processed_chars - state.chars_at_last_stage_emit
            if delta_chars > 0 and dt > 0:
                cps = delta_chars / dt
                self.stage_changed.emit(
                    "local",
                    f"Saved {chunk_label} ({chunk_elapsed:.1f} s) · {cps:.0f} chars/s",
                )
            else:
                self.stage_changed.emit(
                    "local",
                    f"Saved {chunk_label} ({chunk_elapsed:.1f} s)",
                )
            state.chars_at_last_stage_emit = state.processed_chars
            state.time_at_last_stage_emit = now
        else:
            self.stage_changed.emit(
                "local",
                f"Saved {chunk_label} ({chunk_elapsed:.1f} s)",
            )

    @staticmethod
    def _classify_attempt_failure(
        exc: Exception,
        stats: _AttemptStats,
        chunk_label: str,
    ) -> _AttemptFailure:
        msg = str(exc).lower()

        if isinstance(exc, asyncio.TimeoutError) or isinstance(
            exc, (aiohttp.ServerTimeoutError, aiohttp.SocketTimeoutError)
        ):
            if stats.audio_bytes > 0:
                return _AttemptFailure(
                    "timeout_after_audio",
                    f"The speech request stalled after partial audio on {chunk_label}.",
                    original=exc,
                )
            return _AttemptFailure(
                "timeout_waiting_for_audio",
                f"The speech request timed out before audio arrived for {chunk_label}.",
                original=exc,
            )

        if isinstance(exc, edge_exceptions.NoAudioReceived):
            if stats.metadata_events > 0:
                return _AttemptFailure(
                    "metadata_without_audio",
                    f"The speech service returned metadata but no audio for {chunk_label}.",
                    original=exc,
                )
            return _AttemptFailure(
                "no_audio",
                f"The speech service returned no audio for {chunk_label}.",
                original=exc,
            )

        if isinstance(exc, aiohttp.ClientConnectorDNSError) or "getaddrinfo failed" in msg:
            return _AttemptFailure(
                "dns",
                f"Could not resolve speech.platform.bing.com for {chunk_label}.",
                original=exc,
            )

        if isinstance(exc, ValueError) and "voice" in msg:
            return _AttemptFailure(
                "invalid_voice",
                f"The selected voice appears to be invalid: {exc}",
                original=exc,
            )

        if isinstance(
            exc,
            (
                aiohttp.ClientConnectionError,
                aiohttp.ClientError,
                edge_exceptions.WebSocketError,
            ),
        ) or any(
            keyword in msg
            for keyword in (
                "connection",
                "network",
                "resolve",
                "ssl",
                "wss",
                "websocket",
                "connecterror",
                "connectionerror",
                "dns",
                "nodename",
                "servname",
                "gaierror",
                "503",
                "invalid response status",
            )
        ):
            return _AttemptFailure(
                "network",
                f"The network/service connection failed on {chunk_label}: {exc}",
                original=exc,
            )

        if isinstance(exc, (edge_exceptions.UnexpectedResponse, edge_exceptions.UnknownResponse)):
            return _AttemptFailure(
                "service_response",
                f"The speech service returned an unexpected response for {chunk_label}.",
                original=exc,
            )

        return _AttemptFailure(
            "unexpected",
            f"An unexpected error occurred on {chunk_label}: {exc}",
            original=exc,
        )

    @staticmethod
    def _user_message(exc: Exception) -> str:
        if isinstance(exc, _PreflightError):
            suggestion = (
                f"\nSuggested alternative: {exc.suggestion}"
                if exc.suggestion else ""
            )
            if exc.cause.kind == "invalid_voice":
                return (
                    "The selected voice is no longer available from the Microsoft speech service.\n\n"
                    f"Voice: {exc.voice}\n"
                    "Reload the voice list and choose another voice before generating."
                    f"{suggestion}"
                )
            if exc.cause.kind in {"no_audio", "metadata_without_audio"}:
                return (
                    "The selected voice did not return audio during the startup check.\n\n"
                    f"Voice: {exc.voice}\n"
                    "SetupTTS stopped before the full job started because this voice or voice/text combination appears unavailable right now.\n"
                    "Try another voice and run the job again."
                    f"{suggestion}"
                )
            if exc.cause.kind == "dns":
                return (
                    "Could not resolve speech.platform.bing.com during the startup check.\n\n"
                    "Please check your internet connection or DNS settings and try again."
                )
            if exc.cause.kind.startswith("timeout"):
                return (
                    "The startup speech check timed out repeatedly.\n\n"
                    "The Microsoft speech service is responding too slowly right now.\n"
                    "Try again in a minute."
                )
            if exc.cause.kind == "network":
                return (
                    "The network/service connection was unstable during the startup check.\n\n"
                    "Please try again. If the problem continues, wait a minute and retry."
                )
            return (
                "The voice validation check failed before generation started.\n\n"
                f"Details: {exc.cause}"
            )

        if isinstance(exc, _ChunkError):
            cause = exc.cause
            chunk_ctx = f"chunk {exc.chunk}/{exc.total}"
            suggestion = (
                f"\nSuggested alternative: {cause.suggestion}"
                if cause.suggestion else ""
            )

            if cause.kind == "invalid_voice":
                return (
                    f"The selected voice became unavailable while generating {chunk_ctx}.\n\n"
                    "Reload the voice list and choose another voice before trying again."
                    f"{suggestion}"
                )
            if cause.kind == "dns":
                return (
                    f"Could not resolve speech.platform.bing.com while generating {chunk_ctx}.\n\n"
                    "Please check your internet connection or DNS settings and try again."
                )
            if cause.kind.startswith("timeout"):
                return (
                    f"The speech request timed out repeatedly on {chunk_ctx}.\n\n"
                    "The speech service was too slow or stalled before that chunk finished.\n"
                    "Try again — transient slowdowns usually recover."
                )
            if cause.kind in {"no_audio", "metadata_without_audio"}:
                return (
                    f"{chunk_ctx.capitalize()} failed after multiple attempts; the app could not recover automatically.\n\n"
                    "The speech service returned no audio for that section.\n"
                    "SetupTTS retried with fresh connections and smaller recovery chunks, but the selected voice/provider still returned no audio."
                    f"{suggestion}"
                )
            if cause.kind == "network":
                return (
                    f"The network/service connection was unstable during {chunk_ctx}.\n\n"
                    "Please try again. If the problem continues, wait a minute and retry."
                )
            if cause.kind == "service_response":
                return (
                    f"The speech service returned an unexpected response on {chunk_ctx}.\n\n"
                    "Try again. If the problem keeps happening, choose another voice."
                )
            return (
                f"Generation failed on {chunk_ctx} after recovery attempts.\n\n"
                f"Details: {cause}"
            )

        msg = str(exc).lower()
        if "permission" in msg or "access denied" in msg or "read-only" in msg:
            return (
                "Cannot write to the selected output location.\n"
                "Please choose a different folder."
            )
        if "no such file" in msg or "directory" in msg:
            return (
                "The output folder does not exist.\n"
                "Please select a valid save location."
            )
        if "timeout" in msg:
            return (
                "The speech service timed out.\n\n"
                "Please try again. If this keeps happening, the service may be under heavy load."
            )
        return (
            "An unexpected error occurred while generating audio.\n\n"
            f"Details: {exc}"
        )
