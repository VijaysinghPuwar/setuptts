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
import math
import re
import sys
import time
import uuid
from pathlib import Path

import aiohttp
from edge_tts import exceptions as edge_exceptions
from edge_tts.communicate import escape, remove_incompatible_characters
from PySide6.QtCore import QThread, Signal

from app.services.tts_quality import (
    VoiceCompatibilityAssessment,
    assess_voice_compatibility,
    build_text_profile,
)
from app.services.tts_service import (
    DEFAULT_CONNECT_TIMEOUT_S,
    build_communicate,
    list_voices,
)
from app.utils.paths import AppPaths
from app.workers.chunk_store import ChunkStore, cleanup_stale_staging

logger = logging.getLogger(__name__)

# ── Chunk sizing ──────────────────────────────────────────────────────────── #
_MEDIUM_JOB_THRESHOLD = 12_000
_LONG_JOB_THRESHOLD = 45_000
_XL_JOB_THRESHOLD = 90_000

# Keep well under edge_tts's internal 4096-byte boundary so each SetupTTS
# chunk maps to one actual provider request even for multi-byte languages or
# XML-escaped content.
_CHUNK_CHARS_DEFAULT = 10_500
_CHUNK_CHARS_MEDIUM = 10_000
_CHUNK_CHARS_LONG = 9_600
_CHUNK_CHARS_XL = 9_200

_CHUNK_PAYLOAD_BYTES_DEFAULT = 3_750
_CHUNK_PAYLOAD_BYTES_MEDIUM = 3_700
_CHUNK_PAYLOAD_BYTES_LONG = 3_650
_CHUNK_PAYLOAD_BYTES_XL = 3_600
_FIRST_CHUNK_PROBE_CHARS = 1_200
_FIRST_CHUNK_PROBE_PAYLOAD_BYTES = 1_350
_RAMP_CHUNK_CHARS_DEFAULT = 3_200
_RAMP_CHUNK_PAYLOAD_BYTES_DEFAULT = 2_600

_COMPLEX_SCRIPT_LIMITS = {
    "devanagari": (4_600, 2_550, 2_200, 1_700, 800, 950, 2_500, 24),
    "arabic": (4_800, 2_650, 2_300, 1_750, 800, 950, 2_500, 24),
    "bengali": (4_300, 2_400, 2_000, 1_650, 750, 900, 2_500, 24),
    "gurmukhi": (4_300, 2_400, 2_000, 1_650, 750, 900, 2_500, 24),
    "gujarati": (4_300, 2_400, 2_000, 1_650, 750, 900, 2_500, 24),
    "tamil": (3_900, 2_250, 1_900, 1_550, 700, 850, 2_200, 22),
    "telugu": (3_900, 2_250, 1_900, 1_550, 700, 850, 2_200, 22),
    "kannada": (3_900, 2_250, 1_900, 1_550, 700, 850, 2_200, 22),
    "malayalam": (3_900, 2_250, 1_900, 1_550, 700, 850, 2_200, 22),
    "odia": (4_000, 2_250, 1_900, 1_550, 700, 850, 2_200, 22),
    "sinhala": (4_000, 2_250, 1_900, 1_550, 700, 850, 2_200, 22),
    "han": (3_100, 2_150, 1_450, 1_250, 550, 700, 1_800, 20),
    "japanese": (3_200, 2_200, 1_500, 1_300, 600, 750, 1_800, 20),
    "hangul": (3_400, 2_250, 1_600, 1_350, 650, 800, 1_900, 20),
    "thai": (3_500, 2_300, 1_700, 1_400, 650, 800, 2_000, 22),
    "mixed": (4_800, 2_600, 2_400, 1_750, 800, 950, 2_800, 24),
}

_PREFLIGHT_SAMPLE_CHARS = 220
_PREFLIGHT_SAMPLE_PAYLOAD_BYTES = 360
_PREFLIGHT_TIMEOUT_S = 45

_CHUNK_TIMEOUT_MIN_S = 65
_CHUNK_TIMEOUT_MAX_S = 180
_EDGE_RECEIVE_TIMEOUT_MIN_S = 60
_EDGE_RECEIVE_TIMEOUT_MAX_S = 180
_FIRST_AUDIO_TIMEOUT_MIN_S = 18
_FIRST_AUDIO_TIMEOUT_MAX_S = 40
_STREAM_IDLE_TIMEOUT_MIN_S = 12
_STREAM_IDLE_TIMEOUT_MAX_S = 40

# ── Retry strategy ───────────────────────────────────────────────────────── #
_MAX_ATTEMPTS = 5
_NO_AUDIO_MAX_ATTEMPTS = 2
_BACKOFF_BASE = 2.0

# ── Adaptive recovery ────────────────────────────────────────────────────── #
_MAX_RECOVERY_DEPTH = 3
_MIN_RECOVERY_CHARS = 180
_MIN_RECOVERY_PAYLOAD_BYTES = 320
_ADAPTIVE_SHRINK_FACTOR = 0.82
_SLOW_CHUNK_MULTIPLIER = 1.5
_HEALTHY_GROWTH_FACTOR = 1.18
_EARLY_TIMEOUT_RECOVERY_ATTEMPTS = 2

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")


@dataclass(frozen=True)
class _ChunkPlan:
    max_chars: int
    max_payload_bytes: int
    ramp_chars: int
    ramp_payload_bytes: int
    warmup_chars: int
    warmup_payload_bytes: int
    preflight_threshold: int
    first_audio_timeout_s: int


def _chunk_plan_for(
    total_chars: int,
    script_code: str | None = None,
    *,
    multilingual_voice: bool = False,
) -> _ChunkPlan:
    if total_chars >= _XL_JOB_THRESHOLD:
        chars = _CHUNK_CHARS_XL
        payload = _CHUNK_PAYLOAD_BYTES_XL
    elif total_chars >= _LONG_JOB_THRESHOLD:
        chars = _CHUNK_CHARS_LONG
        payload = _CHUNK_PAYLOAD_BYTES_LONG
    elif total_chars >= _MEDIUM_JOB_THRESHOLD:
        chars = _CHUNK_CHARS_MEDIUM
        payload = _CHUNK_PAYLOAD_BYTES_MEDIUM
    else:
        chars = _CHUNK_CHARS_DEFAULT
        payload = _CHUNK_PAYLOAD_BYTES_DEFAULT

    ramp_chars = min(chars, _RAMP_CHUNK_CHARS_DEFAULT)
    ramp_payload = min(payload, _RAMP_CHUNK_PAYLOAD_BYTES_DEFAULT)
    warmup_chars = min(_FIRST_CHUNK_PROBE_CHARS, chars)
    warmup_payload = min(_FIRST_CHUNK_PROBE_PAYLOAD_BYTES, payload)
    preflight_threshold = 4_000
    first_audio_timeout_s = 28

    if script_code in _COMPLEX_SCRIPT_LIMITS:
        (
            script_max_chars,
            script_max_payload,
            script_ramp_chars,
            script_ramp_payload,
            script_warmup_chars,
            script_warmup_payload,
            script_preflight_threshold,
            script_first_audio_timeout,
        ) = _COMPLEX_SCRIPT_LIMITS[script_code]
        chars = min(chars, script_max_chars)
        payload = min(payload, script_max_payload)
        ramp_chars = min(ramp_chars, script_ramp_chars)
        ramp_payload = min(ramp_payload, script_ramp_payload)
        warmup_chars = min(warmup_chars, script_warmup_chars)
        warmup_payload = min(warmup_payload, script_warmup_payload)
        preflight_threshold = min(preflight_threshold, script_preflight_threshold)
        first_audio_timeout_s = min(first_audio_timeout_s, script_first_audio_timeout)

    # Multilingual voices (e.g. en-US-AndrewMultilingualNeural) are more
    # resource-intensive on the provider side.  On very long jobs reduce the
    # chunk ceiling by ~15 % to lower the probability of no-audio failures.
    if multilingual_voice and total_chars >= _LONG_JOB_THRESHOLD:
        shrink = 0.78 if total_chars >= _XL_JOB_THRESHOLD else 0.84
        chars = max(7_000 if total_chars >= _XL_JOB_THRESHOLD else 7_600, int(chars * shrink))
        payload = max(2_650, int(payload * shrink))
        ramp_chars = max(2_600, int(ramp_chars * shrink))
        ramp_payload = max(2_050, int(ramp_payload * shrink))

    return _ChunkPlan(
        max_chars=chars,
        max_payload_bytes=payload,
        ramp_chars=max(ramp_chars, warmup_chars),
        ramp_payload_bytes=max(ramp_payload, warmup_payload),
        warmup_chars=warmup_chars,
        warmup_payload_bytes=warmup_payload,
        preflight_threshold=preflight_threshold,
        first_audio_timeout_s=first_audio_timeout_s,
    )


def _chunk_size_for(
    total_chars: int,
    script_code: str | None = None,
    *,
    multilingual_voice: bool = False,
) -> int:
    return _chunk_plan_for(total_chars, script_code, multilingual_voice=multilingual_voice).max_chars


def _payload_limit_for(
    total_chars: int,
    script_code: str | None = None,
    *,
    multilingual_voice: bool = False,
) -> int:
    return _chunk_plan_for(
        total_chars, script_code, multilingual_voice=multilingual_voice
    ).max_payload_bytes


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


def _apply_first_chunk_probe(
    chunks: list[str],
    total_chars: int,
    plan: _ChunkPlan | None = None,
) -> list[str]:
    if len(chunks) < 2:
        return chunks

    chunk_plan = plan or _chunk_plan_for(total_chars)
    first = chunks[0]
    if (
        len(first) <= chunk_plan.warmup_chars
        and _edge_payload_size(first) <= chunk_plan.warmup_payload_bytes
    ):
        return chunks

    probe_chunks = _split_text(
        first,
        chunk_plan.warmup_chars,
        chunk_plan.warmup_payload_bytes,
    )
    if len(probe_chunks) <= 1:
        return chunks

    return [*probe_chunks, *chunks[1:]]


@dataclass
class _ChunkCursor:
    remaining_text: str

    def has_more(self) -> bool:
        return bool(self.remaining_text.strip())

    def remaining_chars(self) -> int:
        return len(self.remaining_text.lstrip())

    def take_next(self, max_chars: int, max_payload_bytes: int) -> tuple[str, int]:
        text = self.remaining_text.lstrip()
        if not text:
            self.remaining_text = ""
            return "", 0

        chunk, remainder, payload_bytes = _take_chunk_prefix(
            text,
            max_chars,
            max_payload_bytes,
        )
        self.remaining_text = remainder
        return chunk, payload_bytes


def _take_chunk_prefix(
    text: str,
    max_chars: int,
    max_payload_bytes: int,
) -> tuple[str, str, int]:
    stripped = text.lstrip()
    if not stripped:
        return "", "", 0

    if _fits_chunk(stripped, max_chars, max_payload_bytes):
        return stripped, "", _edge_payload_size(stripped)

    window = stripped[:max_chars]
    boundary_sets = (
        _boundary_candidates(window, r"\n{2,}"),
        _boundary_candidates(window, r"(?<=[.!?。！？])\s+"),
        _boundary_candidates(window, r"\s+"),
    )

    for boundaries in boundary_sets:
        for split_at, resume_at in boundaries:
            candidate = stripped[:split_at].strip()
            if candidate and _fits_chunk(candidate, max_chars, max_payload_bytes):
                remainder = stripped[resume_at:].lstrip()
                return candidate, remainder, _edge_payload_size(candidate)

    hard_split = _hard_split_text(stripped, max_chars, max_payload_bytes)
    chunk = hard_split[0]
    remainder = stripped[len(chunk):].lstrip()
    return chunk, remainder, _edge_payload_size(chunk)


def _boundary_candidates(text: str, pattern: str) -> list[tuple[int, int]]:
    candidates = [
        (match.start(), match.end())
        for match in re.finditer(pattern, text)
        if match.start() > 0
    ]
    candidates.sort(reverse=True)
    return candidates


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


def _suggest_stable_long_form_voice(selected_voice: str, voices: list[dict]) -> str | None:
    locale = _voice_locale(selected_voice)
    same_locale_non_multilingual = [
        voice.get("ShortName", "")
        for voice in voices
        if voice.get("ShortName") != selected_voice
        and voice.get("Locale") == locale
        and "multilingual" not in voice.get("ShortName", "").lower()
    ]
    if same_locale_non_multilingual:
        return same_locale_non_multilingual[0]
    return _suggest_alternative_voice(selected_voice, voices)


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
    started_at: float = 0.0
    first_audio_at: float | None = None
    last_event_at: float | None = None


@dataclass
class _ChunkOutcome:
    attempts: int
    elapsed: float
    used_recovery: bool = False
    first_audio_delay: float | None = None
    receive_duration: float | None = None
    write_duration: float | None = None
    failure_kinds: tuple[str, ...] = ()


@dataclass
class _RollingHealthState:
    no_audio_events: int = 0
    timeout_events: int = 0
    network_events: int = 0
    conservative_chunks_remaining: int = 0


@dataclass(frozen=True)
class JobTelemetry:
    current_chunk: int
    estimated_total_chunks: int | None
    chunk_chars: int
    char_limit: int
    payload_limit: int
    rolling_chars_per_second: float
    eta_seconds: float | None
    phase: str
    detail: str
    first_audio_delay: float | None = None
    receive_duration: float | None = None
    write_duration: float | None = None


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

    def __init__(self, chunk: int, total: int | None, cause: _AttemptFailure) -> None:
        label = f"Chunk {chunk}/{total}" if total is not None else f"Chunk {chunk}"
        super().__init__(f"{label} failed: {cause}")
        self.chunk = chunk
        self.total = total
        self.cause = cause
        # Set by _stream_generate after catching, before re-raising:
        self.preserved_chunks: int = 0
        self.staging_dir: Path | None = None


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
    stage_changed = Signal(str, str)    # kind="local"|"remote"|"waiting"
    speed_updated = Signal(float)       # chars/s
    telemetry_updated = Signal(object)  # JobTelemetry
    completed = Signal(str, float)
    failed = Signal(str)
    # Emitted before `failed` when the job failed after partial success.
    # Payload: (staging_dir_str, completed_count, failed_chunk, total_chunks)
    job_resumable = Signal(str, int, int, int)

    def __init__(
        self,
        text: str,
        voice: str,
        rate: str,
        volume: str,
        output_path: str,
        *,
        allow_voice_mismatch: bool = False,
        job_id: str | None = None,
        resume_staging_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self._text = text
        self._voice = voice
        self._rate = rate
        self._volume = volume
        self._output_path = output_path
        self._allow_voice_mismatch = allow_voice_mismatch
        self._job_id: str = job_id or uuid.uuid4().hex
        self._resume_staging_dir: Path | None = resume_staging_dir
        self._cancelled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_task: asyncio.Task | None = None
        self._last_pct = 0
        self._text_profile = build_text_profile(text)
        self._compatibility: VoiceCompatibilityAssessment | None = None
        self._health = _RollingHealthState()

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

    async def _stream_generate(self) -> None:  # noqa: C901 – inherently complex
        if self._cancelled:
            raise asyncio.CancelledError()

        self.status_changed.emit("Preparing…")
        self.stage_changed.emit("local", "Preparing text locally")
        self.progress.emit(3)

        stripped_text = self._text_profile.cleaned_text.strip()
        if not stripped_text:
            raise ValueError("No text was available to generate after text cleanup.")

        if stripped_text != self._text.strip():
            self.stage_changed.emit(
                "local",
                "Cleaning punctuation and unicode for more reliable speech output",
            )

        total_chars = max(len(stripped_text), 1)
        is_multilingual = "Multilingual" in self._voice
        chunk_plan = _chunk_plan_for(
            total_chars,
            self._text_profile.script_code,
            multilingual_voice=is_multilingual,
        )
        max_chunk_chars = chunk_plan.max_chars
        max_payload_bytes = chunk_plan.max_payload_bytes

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

        self._compatibility = assess_voice_compatibility(
            self._text_profile,
            self._voice,
            voices,
        )
        if self._compatibility.requires_confirmation and not self._allow_voice_mismatch:
            raise _PreflightError(
                self._voice,
                _AttemptFailure(
                    "incompatible_voice",
                    self._compatibility.message,
                    suggestion=self._compatibility.recommended_voice,
                ),
                suggestion=self._compatibility.recommended_voice,
            )
        if self._compatibility.requires_confirmation:
            logger.warning(
                "Proceeding despite voice/text mismatch: voice=%s message=%s",
                self._voice,
                self._compatibility.short_message,
            )

        # Warn if a multilingual voice is selected for a long English-only job —
        # multilingual models are more resource-intensive and tend to produce
        # intermittent no-audio failures on very long runs.
        if (
            is_multilingual
            and total_chars >= _LONG_JOB_THRESHOLD
            and self._text_profile.script_code in {None, "latin", ""}
        ):
            alt = _suggest_stable_long_form_voice(self._voice, voices)
            alt_hint = f"  Suggested alternative: {alt}." if alt else ""
            self.stage_changed.emit(
                "local",
                f"Note: '{self._voice}' is a multilingual model and may be less stable "
                f"for long English audiobook jobs (chunk ceiling reduced to {max_chunk_chars:,} chars).{alt_hint}",
            )
            logger.info(
                "Multilingual voice warning: voice=%s total_chars=%d alt=%s",
                self._voice,
                total_chars,
                alt,
            )

        if total_chars >= chunk_plan.preflight_threshold:
            await self._run_preflight(voices)

        # ── Set up chunk staging (checkpoint / resume) ─────────────────── #
        staging_root = AppPaths().staging_dir
        # Clean up orphaned staging dirs from previous sessions in the background.
        try:
            cleanup_stale_staging(staging_root, max_age_days=7)
        except Exception:
            pass

        chunk_store: ChunkStore
        if self._resume_staging_dir is not None:
            resumed = ChunkStore.try_resume(self._resume_staging_dir, stripped_text, self._voice)
            if resumed is not None:
                chunk_store = resumed
                logger.info(
                    "Resuming job %s — %d chunks already completed (%d chars consumed)",
                    self._job_id,
                    chunk_store.completed_count,
                    chunk_store.manifest.chars_consumed,
                )
            else:
                logger.warning(
                    "Could not resume from %s (mismatch or no valid data) — starting fresh",
                    self._resume_staging_dir,
                )
                chunk_store = ChunkStore.create(
                    staging_root,
                    self._job_id,
                    voice=self._voice,
                    rate=self._rate,
                    volume=self._volume,
                    output_path=self._output_path,
                    text=stripped_text,
                )
        else:
            chunk_store = ChunkStore.create(
                staging_root,
                self._job_id,
                voice=self._voice,
                rate=self._rate,
                volume=self._volume,
                output_path=self._output_path,
                text=stripped_text,
            )

        # ── Initialise cursor, possibly from a resume point ─────────────── #
        resume_chars = chunk_store.manifest.chars_consumed
        resume_chunk_idx = chunk_store.resume_from_chunk

        job_start = time.monotonic()
        progress_state = _ProgressState(
            processed_chars=resume_chars,
            spd_chars=resume_chars,
            spd_time=job_start,
            spd_ema=0.0,
            chars_at_last_stage_emit=resume_chars,
            time_at_last_stage_emit=job_start,
        )

        # For a fresh run start at warmup size; for a resume start at target
        # size since the voice is already warmed up.
        if resume_chunk_idx > 0:
            adaptive_char_limit = max_chunk_chars
            adaptive_payload_limit = max_payload_bytes
        else:
            adaptive_char_limit = chunk_plan.warmup_chars
            adaptive_payload_limit = chunk_plan.warmup_payload_bytes

        # Start the text cursor at the already-consumed position.
        chunk_cursor = _ChunkCursor(stripped_text[resume_chars:] if resume_chars > 0 else stripped_text)

        if not chunk_cursor.has_more() and resume_chunk_idx == 0:
            raise ValueError("No text was available to generate.")

        logger.info(
            "Starting: voice=%s rate=%s chars=%d chunk_ceiling=%d payload_limit=%d "
            "resume_chunk=%d resume_chars=%d output=%s",
            self._voice,
            self._rate,
            len(stripped_text),
            max_chunk_chars,
            max_payload_bytes,
            resume_chunk_idx,
            resume_chars,
            self._output_path,
        )

        if resume_chunk_idx > 0:
            self.stage_changed.emit(
                "local",
                f"Resuming from chunk {resume_chunk_idx + 1} — "
                f"{chunk_store.completed_count} chunk(s) already completed, "
                f"{resume_chars:,} chars already processed",
            )
            self._emit_progress_from_chars(resume_chars, total_chars)
        else:
            estimated_chunks = self._estimate_remaining_chunks(
                total_chars,
                0,
                chunk_plan.ramp_chars,
            )
            if estimated_chunks > 1:
                self.stage_changed.emit(
                    "local",
                    "Preparing adaptive chunk pipeline "
                    f"(warm-up {chunk_plan.warmup_chars:,} chars, "
                    f"healthy ceiling {max_chunk_chars:,} chars)",
                )

        self.status_changed.emit("Connecting…")
        self.stage_changed.emit(
            "remote",
            "Connecting to Microsoft Neural TTS (speech.platform.bing.com)",
        )

        output_path = Path(self._output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        chunk_idx = resume_chunk_idx

        try:
            while chunk_cursor.has_more():
                if self._cancelled:
                    raise asyncio.CancelledError()

                text_chunk, chunk_payload = chunk_cursor.take_next(
                    adaptive_char_limit,
                    adaptive_payload_limit,
                )
                if not text_chunk:
                    break

                estimated_total = self._estimate_remaining_chunks(
                    total_chars,
                    progress_state.processed_chars,
                    max(adaptive_char_limit, chunk_plan.ramp_chars),
                )
                if estimated_total < chunk_idx + 1:
                    estimated_total = chunk_idx + 1
                # Account for chunks already completed in the estimate
                estimated_total = max(estimated_total, chunk_idx + 1)

                chunk_number = chunk_idx + 1
                chunk_label = (
                    f"chunk {chunk_number}/{estimated_total}"
                    if estimated_total > 1
                    else f"chunk {chunk_number}"
                )

                chunk_bytes, outcome = await self._process_chunk(
                    text_chunk=text_chunk,
                    chunk_payload=chunk_payload,
                    chunk_idx=chunk_idx,
                    estimated_total_chunks=estimated_total,
                    total_chars=total_chars,
                    progress_state=progress_state,
                    char_limit=adaptive_char_limit,
                    payload_limit=adaptive_payload_limit,
                    plan=chunk_plan,
                    display_label=chunk_label,
                )

                # ── Write chunk to staging ──────────────────────────────── #
                write_started_at = time.monotonic()
                self.stage_changed.emit("local", f"Writing {chunk_label} to disk")
                chunk_store.save_chunk(chunk_idx, chunk_bytes)
                chunk_store.update_chars_consumed(progress_state.processed_chars)
                write_duration = time.monotonic() - write_started_at

                self._emit_saved_stage(
                    chunk_label,
                    outcome.elapsed,
                    progress_state,
                    first_audio_delay=outcome.first_audio_delay,
                    receive_duration=outcome.receive_duration,
                    write_duration=write_duration,
                )
                self._emit_telemetry(
                    current_chunk=chunk_number,
                    estimated_total_chunks=estimated_total,
                    chunk_chars=len(text_chunk),
                    char_limit=adaptive_char_limit,
                    payload_limit=adaptive_payload_limit,
                    progress_state=progress_state,
                    total_chars=total_chars,
                    phase="local",
                    detail="Chunk saved locally",
                    first_audio_delay=outcome.first_audio_delay,
                    receive_duration=outcome.receive_duration,
                    write_duration=write_duration,
                )

                adaptive_char_limit, adaptive_payload_limit = self._retune_after_chunk(
                    outcome,
                    adaptive_char_limit,
                    adaptive_payload_limit,
                    chunk_plan,
                    chunk_index=chunk_idx,
                )
                chunk_idx += 1

        except asyncio.CancelledError:
            logger.info(
                "Stream cancelled at chunk %d — preserving staged progress for job %s when available",
                chunk_idx + 1,
                self._job_id,
            )
            if chunk_store.completed_count > 0:
                chunk_store.mark_cancelled(
                    preserve_progress=True,
                    failed_at_chunk=chunk_idx + 1,
                    total=chunk_idx + self._estimate_remaining_chunks(
                        total_chars,
                        progress_state.processed_chars,
                        max(adaptive_char_limit, 1),
                    ),
                )
            else:
                chunk_store.mark_cancelled(preserve_progress=False)
                chunk_store.cleanup()
            raise

        except _ChunkError as exc:
            # Preserve completed chunk files and mark job as resumable.
            preserved = chunk_store.completed_count
            if preserved > 0:
                chunk_store.mark_failed(exc.chunk, exc.total)
                exc.preserved_chunks = preserved
                exc.staging_dir = chunk_store.staging_dir
                self.job_resumable.emit(
                    str(chunk_store.staging_dir),
                    preserved,
                    exc.chunk,
                    exc.total or 0,
                )
            else:
                chunk_store.mark_failed(exc.chunk, exc.total)
                chunk_store.cleanup()
            raise

        if self._cancelled:
            if chunk_store.completed_count > 0:
                chunk_store.mark_cancelled(
                    preserve_progress=True,
                    failed_at_chunk=chunk_idx + 1,
                    total=chunk_idx + self._estimate_remaining_chunks(
                        total_chars,
                        progress_state.processed_chars,
                        max(adaptive_char_limit, 1),
                    ),
                )
            else:
                chunk_store.mark_cancelled(preserve_progress=False)
                chunk_store.cleanup()
            raise asyncio.CancelledError()

        # ── All chunks done: assemble final file ─────────────────────────── #
        self.stage_changed.emit("local", "Assembling final audio file from all chunks…")
        chunk_store.finalize(output_path)

        size = output_path.stat().st_size
        total_elapsed = time.monotonic() - job_start
        if size <= 0:
            raise RuntimeError("The speech service completed without writing any audio.")

        chunk_store.cleanup()

        logger.info(
            "File written: %s size=%d bytes chunks=%d total=%.2fs",
            output_path,
            size,
            chunk_idx,
            total_elapsed,
        )
        self.stage_changed.emit("local", "Finalizing MP3 file")
        self.status_changed.emit("Done")
        self.progress.emit(100)

    async def _run_preflight(self, voices: list[dict]) -> None:
        sample = self._preflight_sample_text()
        if not sample:
            return
        preflight_plan = _chunk_plan_for(
            len(self._text_profile.cleaned_text),
            self._text_profile.script_code,
        )

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
                    first_audio_timeout_s=preflight_plan.first_audio_timeout_s,
                )
                return
            except _AttemptFailure as exc:
                last_failure = exc
                if exc.kind in {"invalid_voice", "no_audio", "metadata_without_audio"}:
                    break

        if last_failure is None:
            return

        suggestion = (
            self._compatibility.recommended_voice
            if self._compatibility and self._compatibility.recommended_voice
            else _suggest_stable_long_form_voice(self._voice, voices)
        )
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
                suggestion = (
                    self._compatibility.recommended_voice
                    if self._compatibility and self._compatibility.recommended_voice
                    else _suggest_stable_long_form_voice(self._voice, refreshed)
                )

        raise _PreflightError(self._voice, last_failure, suggestion=suggestion)

    def _preflight_sample_text(self) -> str:
        sample_chunks = _split_text(
            self._text_profile.cleaned_text.strip(),
            _PREFLIGHT_SAMPLE_CHARS,
            _PREFLIGHT_SAMPLE_PAYLOAD_BYTES,
        )
        return sample_chunks[0] if sample_chunks else ""

    async def _process_chunk(
        self,
        *,
        text_chunk: str,
        chunk_payload: int,
        chunk_idx: int,
        estimated_total_chunks: int | None,
        total_chars: int,
        progress_state: _ProgressState,
        char_limit: int,
        payload_limit: int,
        plan: _ChunkPlan,
        depth: int = 0,
        display_label: str | None = None,
    ) -> tuple[bytes, _ChunkOutcome]:
        chunk_number = chunk_idx + 1
        chunk_label = display_label or (
            f"chunk {chunk_number}/{estimated_total_chunks}"
            if estimated_total_chunks and estimated_total_chunks > 1
            else f"chunk {chunk_number}"
        )

        self._emit_telemetry(
            current_chunk=chunk_number,
            estimated_total_chunks=estimated_total_chunks,
            chunk_chars=len(text_chunk),
            char_limit=char_limit,
            payload_limit=payload_limit,
            progress_state=progress_state,
            total_chars=total_chars,
            phase="remote",
            detail="Waiting for Microsoft to return audio",
        )

        if depth == 0:
            self.status_changed.emit(f"Chunk {chunk_number}…")
            self.stage_changed.emit(
                "remote",
                f"Sending {chunk_label} to Microsoft ({len(text_chunk):,} chars / {chunk_payload:,} bytes)",
            )
        else:
            self.stage_changed.emit(
                "remote",
                f"Retrying {chunk_label} with a smaller recovery section ({len(text_chunk):,} chars / {chunk_payload:,} bytes)",
            )
            self.status_changed.emit(f"Recovering chunk {chunk_number}…")

        last_failure: _AttemptFailure | None = None
        chunk_start = time.monotonic()
        failure_kinds: set[str] = set()

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
                audio_bytes, attempt_chars, stats = await self._synthesise_attempt(
                    text_chunk,
                    chunk_label=chunk_label,
                    total_chars=total_chars,
                    progress_state=progress_state,
                    timeout_s=timeout_s,
                    first_audio_timeout_s=plan.first_audio_timeout_s,
                )

                progress_state.processed_chars = min(
                    progress_state.processed_chars + attempt_chars,
                    total_chars,
                )
                self._emit_progress_from_chars(progress_state.processed_chars, total_chars)
                self._maybe_emit_speed(progress_state.processed_chars, progress_state, force=True)

                chunk_elapsed = time.monotonic() - chunk_start
                first_audio_delay = (
                    stats.first_audio_at - stats.started_at
                    if stats.first_audio_at is not None
                    else None
                )
                receive_duration = (
                    (stats.last_event_at or time.monotonic()) - stats.first_audio_at
                    if stats.first_audio_at is not None and stats.last_event_at is not None
                    else None
                )
                logger.info(
                    "%s succeeded (attempt %d) in %.2fs bytes=%d first_audio=%s receive=%s",
                    chunk_label,
                    attempt + 1,
                    chunk_elapsed,
                    len(audio_bytes),
                    f"{first_audio_delay:.2f}s" if first_audio_delay is not None else "n/a",
                    f"{receive_duration:.2f}s" if receive_duration is not None else "n/a",
                )
                return audio_bytes, _ChunkOutcome(
                    attempts=attempt + 1,
                    elapsed=chunk_elapsed,
                    used_recovery=depth > 0,
                    first_audio_delay=first_audio_delay,
                    receive_duration=receive_duration,
                    write_duration=None,
                    failure_kinds=tuple(sorted(failure_kinds)),
                )

            except asyncio.CancelledError:
                raise

            except _AttemptFailure as exc:
                last_failure = exc
                failure_kinds.add(exc.kind)
                self._record_failure_pattern(exc, chunk_label)
                logger.warning("%s failed on attempt %d: %s", chunk_label, attempt + 1, exc)
                if exc.kind in {"no_audio", "metadata_without_audio"} and attempt + 1 >= _NO_AUDIO_MAX_ATTEMPTS:
                    logger.warning(
                        "%s returned no audio repeatedly; stopping full-size retries early",
                        chunk_label,
                    )
                    break
                if exc.kind == "timeout_waiting_for_audio" and attempt + 1 >= _EARLY_TIMEOUT_RECOVERY_ATTEMPTS:
                    logger.warning(
                        "%s kept timing out before audio arrived; switching to smaller recovery sections",
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
                recovered_audio = bytearray()
                recovered_failure_kinds = set(failure_kinds)
                first_audio_delays: list[float] = []
                receive_durations: list[float] = []
                for sub_idx, subchunk in enumerate(recovery_chunks, start=1):
                    sub_audio, sub_outcome = await self._process_chunk(
                        text_chunk=subchunk,
                        chunk_payload=_edge_payload_size(subchunk),
                        chunk_idx=chunk_idx,
                        estimated_total_chunks=estimated_total_chunks,
                        total_chars=total_chars,
                        progress_state=progress_state,
                        char_limit=next_char_limit,
                        payload_limit=next_payload_limit,
                        plan=plan,
                        depth=depth + 1,
                        display_label=f"{chunk_label} · recovery {sub_idx}/{len(recovery_chunks)}",
                    )
                    recovered_audio.extend(sub_audio)
                    recovered_failure_kinds.update(sub_outcome.failure_kinds)
                    if sub_outcome.first_audio_delay is not None:
                        first_audio_delays.append(sub_outcome.first_audio_delay)
                    if sub_outcome.receive_duration is not None:
                        receive_durations.append(sub_outcome.receive_duration)
                chunk_elapsed = time.monotonic() - chunk_start
                return bytes(recovered_audio), _ChunkOutcome(
                    attempts=_MAX_ATTEMPTS,
                    elapsed=chunk_elapsed,
                    used_recovery=True,
                    first_audio_delay=min(first_audio_delays) if first_audio_delays else None,
                    receive_duration=sum(receive_durations) if receive_durations else None,
                    write_duration=None,
                    failure_kinds=tuple(sorted(recovered_failure_kinds)),
                )

            if (
                last_failure.kind in {"no_audio", "metadata_without_audio", "timeout_waiting_for_audio"}
                and self._compatibility
                and self._compatibility.recommended_voice
                and last_failure.suggestion is None
            ):
                last_failure.suggestion = self._compatibility.recommended_voice
            elif (
                last_failure.kind in {"no_audio", "metadata_without_audio", "timeout_waiting_for_audio"}
                and "multilingual" in self._voice.lower()
                and last_failure.suggestion is None
            ):
                try:
                    voices = await list_voices()
                except Exception:
                    voices = []
                stable_voice = _suggest_stable_long_form_voice(self._voice, voices)
                if stable_voice:
                    last_failure.suggestion = stable_voice

        raise _ChunkError(
            chunk_number,
            estimated_total_chunks,
            last_failure or _AttemptFailure("unexpected", "Unknown chunk failure"),
        )

    def _split_for_recovery(
        self,
        text_chunk: str,
        char_limit: int,
        payload_limit: int,
        depth: int,
    ) -> list[str]:
        chunk_chars = len(text_chunk)
        chunk_payload = _edge_payload_size(text_chunk)
        if depth >= _MAX_RECOVERY_DEPTH:
            return []
        if chunk_chars <= _MIN_RECOVERY_CHARS:
            return []
        if chunk_payload <= _MIN_RECOVERY_PAYLOAD_BYTES:
            return []

        next_char_limit = max(
            _MIN_RECOVERY_CHARS,
            min(char_limit // 2, max(_MIN_RECOVERY_CHARS, chunk_chars // 2)),
        )
        next_payload_limit = max(
            _MIN_RECOVERY_PAYLOAD_BYTES,
            min(payload_limit // 2, max(_MIN_RECOVERY_PAYLOAD_BYTES, chunk_payload // 2)),
        )
        if next_char_limit >= chunk_chars and next_payload_limit >= chunk_payload:
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
        first_audio_timeout_s: int,
    ) -> tuple[bytes, int, _AttemptStats]:
        stats = _AttemptStats(started_at=time.monotonic())
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
            stream = communicate.stream().__aiter__()
            deadline = stats.started_at + timeout_s

            while True:
                if self._cancelled:
                    raise asyncio.CancelledError()

                now = time.monotonic()
                if now >= deadline:
                    raise asyncio.TimeoutError()

                if stats.audio_bytes == 0:
                    wait_timeout = min(deadline - now, self._first_audio_timeout_for(text, first_audio_timeout_s))
                else:
                    wait_timeout = min(deadline - now, self._stream_idle_timeout_for(text))

                try:
                    event = await asyncio.wait_for(stream.__anext__(), timeout=max(wait_timeout, 1.0))
                except StopAsyncIteration:
                    break

                stats.last_event_at = time.monotonic()

                if event["type"] == "audio":
                    if stats.audio_bytes == 0:
                        stats.first_audio_at = stats.last_event_at
                        first_audio_delay = stats.first_audio_at - stats.started_at
                        self.stage_changed.emit(
                            "remote",
                            f"Receiving audio — {chunk_label} (first audio after {first_audio_delay:.1f} s)",
                        )
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

        return audio_buffer.getvalue(), stats.attempt_chars, stats

    def _rollback_failed_attempt(
        self,
        progress_state: _ProgressState | None,
        total_chars: int,
    ) -> None:
        if progress_state is not None:
            self._emit_progress_from_chars(progress_state.processed_chars, total_chars)

    @staticmethod
    def _chunk_timeout_for(text: str) -> int:
        estimated = 28 + (_edge_payload_size(text) / 36.0)
        return int(max(_CHUNK_TIMEOUT_MIN_S, min(_CHUNK_TIMEOUT_MAX_S, estimated)))

    @staticmethod
    def _first_audio_timeout_for(text: str, base_timeout_s: int) -> int:
        estimated = base_timeout_s + int(_edge_payload_size(text) / 220.0)
        return int(max(_FIRST_AUDIO_TIMEOUT_MIN_S, min(_FIRST_AUDIO_TIMEOUT_MAX_S, estimated)))

    @staticmethod
    def _stream_idle_timeout_for(text: str) -> int:
        estimated = 12 + int(_edge_payload_size(text) / 260.0)
        return int(max(_STREAM_IDLE_TIMEOUT_MIN_S, min(_STREAM_IDLE_TIMEOUT_MAX_S, estimated)))

    def _retune_after_chunk(
        self,
        outcome: _ChunkOutcome,
        current_char_limit: int,
        current_payload_limit: int,
        plan: _ChunkPlan,
        *,
        chunk_index: int,
    ) -> tuple[int, int]:
        next_chars = current_char_limit
        next_payload = current_payload_limit
        conservative_mode = self._health.conservative_chunks_remaining > 0
        conservative_chars = max(_MIN_RECOVERY_CHARS, int(plan.max_chars * 0.74))
        conservative_payload = max(
            _MIN_RECOVERY_PAYLOAD_BYTES,
            int(plan.max_payload_bytes * 0.74),
        )

        slow_chunk = (
            outcome.elapsed > 32.0
            or (
                outcome.first_audio_delay is not None
                and outcome.first_audio_delay >= max(12.0, plan.first_audio_timeout_s * 0.9)
            )
        )
        if outcome.used_recovery or outcome.attempts > 1 or slow_chunk:
            next_chars = max(_MIN_RECOVERY_CHARS, int(current_char_limit * _ADAPTIVE_SHRINK_FACTOR))
            next_payload = max(
                _MIN_RECOVERY_PAYLOAD_BYTES,
                int(current_payload_limit * _ADAPTIVE_SHRINK_FACTOR),
            )
            self.stage_changed.emit(
                "local",
                f"Reducing upcoming chunk size to keep throughput stable (≤{next_chars:,} chars)",
            )
        else:
            target_chars = plan.ramp_chars if chunk_index == 0 else plan.max_chars
            target_payload = plan.ramp_payload_bytes if chunk_index == 0 else plan.max_payload_bytes
            if conservative_mode:
                target_chars = min(target_chars, conservative_chars)
                target_payload = min(target_payload, conservative_payload)
            growth_factor = 1.0 if chunk_index == 0 else _HEALTHY_GROWTH_FACTOR
            next_chars = min(
                target_chars,
                max(current_char_limit, int(current_char_limit * growth_factor)),
            )
            next_payload = min(
                target_payload,
                max(current_payload_limit, int(current_payload_limit * growth_factor)),
            )
            if chunk_index == 0 and next_chars < target_chars:
                next_chars = target_chars
                next_payload = target_payload

        if conservative_mode and outcome.attempts == 1 and not outcome.used_recovery:
            self._health.conservative_chunks_remaining = max(
                0,
                self._health.conservative_chunks_remaining - 1,
            )

        return next_chars, next_payload

    def _record_failure_pattern(
        self,
        failure: _AttemptFailure,
        chunk_label: str,
    ) -> None:
        if failure.kind in {"no_audio", "metadata_without_audio"}:
            self._health.no_audio_events += 1
            new_window = max(self._health.conservative_chunks_remaining, 4)
            if new_window > self._health.conservative_chunks_remaining:
                self.stage_changed.emit(
                    "waiting",
                    f"Repeated no-audio responses detected on {chunk_label} — using safer smaller chunks for the rest of the run",
                )
            self._health.conservative_chunks_remaining = new_window
            return

        if failure.kind.startswith("timeout"):
            self._health.timeout_events += 1
            if self._health.timeout_events >= 2:
                new_window = max(self._health.conservative_chunks_remaining, 3)
                if new_window > self._health.conservative_chunks_remaining:
                    self.stage_changed.emit(
                        "waiting",
                        f"Repeated speech timeouts detected on {chunk_label} — reducing chunk sizes earlier",
                    )
                self._health.conservative_chunks_remaining = new_window
            return

        if failure.kind == "network":
            self._health.network_events += 1

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

    def _maybe_emit_speed(
        self,
        total_processed: int,
        state: _ProgressState,
        *,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        dt = now - state.spd_time
        if not force and dt < 1.0:
            return

        delta_chars = total_processed - state.spd_chars
        if delta_chars > 0 and dt > 0:
            raw = delta_chars / dt
            state.spd_ema = 0.65 * state.spd_ema + 0.35 * raw if state.spd_ema > 0 else raw
            self.speed_updated.emit(state.spd_ema)

        state.spd_chars = total_processed
        state.spd_time = now

    def _emit_telemetry(
        self,
        *,
        current_chunk: int,
        estimated_total_chunks: int | None,
        chunk_chars: int,
        char_limit: int,
        payload_limit: int,
        progress_state: _ProgressState,
        total_chars: int,
        phase: str,
        detail: str,
        first_audio_delay: float | None = None,
        receive_duration: float | None = None,
        write_duration: float | None = None,
    ) -> None:
        cps = progress_state.spd_ema
        eta_seconds = None
        if cps > 0:
            remaining_chars = max(total_chars - progress_state.processed_chars, 0)
            eta_seconds = remaining_chars / cps if remaining_chars > 0 else 0.0

        self.telemetry_updated.emit(
            JobTelemetry(
                current_chunk=current_chunk,
                estimated_total_chunks=estimated_total_chunks,
                chunk_chars=chunk_chars,
                char_limit=char_limit,
                payload_limit=payload_limit,
                rolling_chars_per_second=cps,
                eta_seconds=eta_seconds,
                phase=phase,
                detail=detail,
                first_audio_delay=first_audio_delay,
                receive_duration=receive_duration,
                write_duration=write_duration,
            )
        )

    @staticmethod
    def _estimate_remaining_chunks(
        total_chars: int,
        processed_chars: int,
        char_limit: int,
    ) -> int:
        if char_limit <= 0:
            return 1
        remaining_chars = max(total_chars - processed_chars, 0)
        if remaining_chars <= 0:
            return 0
        return max(1, math.ceil(remaining_chars / char_limit))

    def _emit_saved_stage(
        self,
        chunk_label: str,
        chunk_elapsed: float,
        state: _ProgressState,
        *,
        first_audio_delay: float | None,
        receive_duration: float | None,
        write_duration: float | None,
    ) -> None:
        now = time.monotonic()
        dt = now - state.time_at_last_stage_emit
        parts = [f"{chunk_elapsed:.1f} s"]
        if first_audio_delay is not None:
            parts.append(f"wait {first_audio_delay:.1f} s")
        if receive_duration is not None:
            parts.append(f"audio {receive_duration:.1f} s")
        if write_duration is not None:
            parts.append(f"write {write_duration:.1f} s")
        timing_summary = " · ".join(parts)
        if dt >= 0.5:
            delta_chars = state.processed_chars - state.chars_at_last_stage_emit
            if delta_chars > 0 and dt > 0:
                cps = delta_chars / dt
                self.stage_changed.emit(
                    "local",
                    f"Saved {chunk_label} ({timing_summary}) · {cps:.0f} chars/s",
                )
            else:
                self.stage_changed.emit(
                    "local",
                    f"Saved {chunk_label} ({timing_summary})",
                )
            state.chars_at_last_stage_emit = state.processed_chars
            state.time_at_last_stage_emit = now
        else:
            self.stage_changed.emit(
                "local",
                f"Saved {chunk_label} ({timing_summary})",
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
            if stats.metadata_events > 0:
                return _AttemptFailure(
                    "metadata_without_audio",
                    f"The speech service kept returning metadata but no audio for {chunk_label}.",
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
                f"\nRecommended voice: {exc.suggestion}"
                if exc.suggestion else ""
            )
            if exc.cause.kind == "invalid_voice":
                return (
                    "The selected voice is no longer available from the Microsoft speech service.\n\n"
                    f"Voice: {exc.voice}\n"
                    "Reload the voice list and choose another voice before generating."
                    f"{suggestion}"
                )
            if exc.cause.kind == "incompatible_voice":
                return (
                    f"{exc.cause}\n\n"
                    "SetupTTS stopped before the full job started to avoid a bad voice/text pairing."
                    f"{suggestion}"
                )
            if exc.cause.kind in {"no_audio", "metadata_without_audio"}:
                return (
                    "The selected voice may not be compatible with this text.\n\n"
                    f"Voice: {exc.voice}\n"
                    "The speech service returned no audio for the current voice/text combination during the startup check.\n"
                    "SetupTTS stopped before the full job started so it would not waste time on a likely bad run."
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
            chunk_ctx = f"chunk {exc.chunk}/{exc.total}" if exc.total else f"chunk {exc.chunk}"
            suggestion = (
                f"\nRecommended voice: {cause.suggestion}"
                if cause.suggestion else ""
            )
            preserved_note = ""
            if exc.preserved_chunks > 0:
                preserved_note = (
                    f"\n\nCompleted audio up to chunk {exc.preserved_chunks} has been preserved.\n"
                    "You can retry/resume from the failed section."
                )

            if cause.kind == "invalid_voice":
                return (
                    f"The selected voice became unavailable while generating {chunk_ctx}.\n\n"
                    "Reload the voice list and choose another voice before trying again."
                    f"{suggestion}{preserved_note}"
                )
            if cause.kind == "dns":
                return (
                    f"Could not resolve speech.platform.bing.com while generating {chunk_ctx}.\n\n"
                    "Please check your internet connection or DNS settings and try again."
                    f"{preserved_note}"
                )
            if cause.kind.startswith("timeout"):
                return (
                    f"The speech request timed out repeatedly on {chunk_ctx}.\n\n"
                    "The speech service was too slow or stalled before that chunk finished.\n"
                    "Try again — transient slowdowns usually recover."
                    f"{preserved_note}"
                )
            if cause.kind in {"no_audio", "metadata_without_audio"}:
                return (
                    f"{chunk_ctx.capitalize()} failed after multiple attempts; the app could not recover automatically.\n\n"
                    "The speech service returned no audio for that section.\n"
                    "SetupTTS retried with fresh connections and smaller recovery chunks, but the selected voice/provider still returned no audio."
                    f"{suggestion}{preserved_note}"
                )
            if cause.kind == "network":
                return (
                    f"The network/service connection was unstable during {chunk_ctx}.\n\n"
                    "Please try again. If the problem continues, wait a minute and retry."
                    f"{preserved_note}"
                )
            if cause.kind == "service_response":
                return (
                    f"The speech service returned an unexpected response on {chunk_ctx}.\n\n"
                    "Try again. If the problem keeps happening, choose another voice."
                    f"{preserved_note}"
                )
            return (
                f"Generation failed on {chunk_ctx} after recovery attempts.\n\n"
                f"Details: {cause}{preserved_note}"
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
