"""
Background worker for voices that run on this computer (OS built-in voices
and offline Piper voices).

Same signals as TTSWorker, so the job queue and job cards treat both alike.
Local synthesis has no network to fail, so the pipeline is much simpler: the
text is cut at paragraph/sentence boundaries, each piece is rendered and
streamed straight into one MP3 encoder, and the file is moved into place only
when every piece has been spoken.
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.services.local_tts import (
    CANCELLED_ERRORS,
    Mp3Writer,
    local_display_name,
    make_synthesizer,
)
from app.services.tts_quality import build_text_profile
from app.workers.tts_worker import JobTelemetry, _split_text

logger = logging.getLogger(__name__)

#: About a minute of speech: small enough for responsive progress and
#: cancel, large enough that per-call overhead stays negligible.
CHUNK_CHARS = 1_200
_NO_PAYLOAD_LIMIT = 10**9
_RETRIES = 2
#: 64 kbit/s MP3 ≈ 8 KB/s of audio; narration ≈ 15 chars/s.
_BYTES_PER_CHAR = 8_000 / 15


class _NothingToSay(ValueError):
    """The input has no speakable text — a user error, not a crash."""


class LocalTTSWorker(QThread):
    progress = Signal(int)
    status_changed = Signal(str)
    stage_changed = Signal(str, str)
    speed_updated = Signal(float)
    telemetry_updated = Signal(object)
    completed = Signal(str, float)
    failed = Signal(str)
    job_resumable = Signal(str, int, int, int)   # never emitted; parity with TTSWorker

    def __init__(
        self,
        text: str,
        voice: str,
        rate: str,
        volume: str,
        output_path: str,
        *,
        job_id: str | None = None,
        **_ignored,
    ) -> None:
        super().__init__()
        self._text = text
        self._voice = voice
        self._rate = rate
        self._volume = volume
        self._output_path = output_path
        self._job_id = job_id or uuid.uuid4().hex
        self._cancelled = False
        self._synth = None
        self.audio_duration_seconds: float | None = None

    def cancel(self) -> None:
        self._cancelled = True
        self.requestInterruption()
        synth = self._synth
        if synth is not None:
            synth.cancel()

    # ------------------------------------------------------------------ #

    def run(self) -> None:
        start = time.monotonic()
        writer: Mp3Writer | None = None
        synth = None
        try:
            self.status_changed.emit("Preparing…")
            self.stage_changed.emit("local", "Preparing text")
            text = build_text_profile(self._text).cleaned_text
            chunks = _split_text(text, CHUNK_CHARS, _NO_PAYLOAD_LIMIT)
            if not chunks:
                raise _NothingToSay("There is no readable text to convert.")
            total_chars = sum(len(c) for c in chunks)
            self._check_disk(total_chars)

            self.status_changed.emit("Loading voice…")
            self.stage_changed.emit("local", f"Loading {local_display_name(self._voice)}")
            synth = make_synthesizer(self._voice)
            self._synth = synth
            if self._cancelled:
                synth.cancel()
                return
            writer = Mp3Writer(self._output_path, synth.sample_rate)

            done_chars = 0
            silent = 0
            render_start = time.monotonic()
            for index, chunk in enumerate(chunks, 1):
                if self._cancelled:
                    return
                self.status_changed.emit(f"Speaking {index}/{len(chunks)}…")
                pcm = self._render(synth, chunk, index, len(chunks))
                if not pcm and any(ch.isalnum() for ch in chunk):
                    silent += 1
                    logger.warning("Local voice produced no audio for chunk %d/%d (%d chars)",
                                   index, len(chunks), len(chunk))
                writer.write(pcm)
                done_chars += len(chunk)

                elapsed = max(1e-6, time.monotonic() - render_start)
                cps = done_chars / elapsed
                remaining = total_chars - done_chars
                self.progress.emit(min(99, int(done_chars * 100 / total_chars)))
                self.speed_updated.emit(cps)
                self.telemetry_updated.emit(JobTelemetry(
                    current_chunk=index,
                    estimated_total_chunks=len(chunks),
                    chunk_chars=len(chunk),
                    char_limit=CHUNK_CHARS,
                    payload_limit=0,
                    rolling_chars_per_second=cps,
                    eta_seconds=remaining / cps if cps > 0 else None,
                    phase="local",
                    detail="Speaking on this computer",
                ))
                self.stage_changed.emit(
                    "local", f"Spoke part {index} of {len(chunks)} · {cps:.0f} chars/s")

            if self._cancelled:
                return
            if writer.samples == 0:
                raise RuntimeError(
                    "The voice produced no audio for this text. It may not be able "
                    "to read this language — pick a voice for the text's language."
                )
            if silent:
                logger.warning("%d of %d chunks produced no audio", silent, len(chunks))

            self.status_changed.emit("Saving…")
            self.stage_changed.emit("local", "Writing the MP3 file")
            writer.finish()
            self.audio_duration_seconds = writer.duration_seconds
            elapsed = time.monotonic() - start
            self.progress.emit(100)
            self.status_changed.emit("Done")
            logger.info("Local generation complete: voice=%s chars=%d audio=%.1fs elapsed=%.1fs",
                        self._voice, total_chars, writer.duration_seconds, elapsed)
            self.completed.emit(self._output_path, elapsed)
        except CANCELLED_ERRORS:
            logger.info("Local generation cancelled: %s", self._output_path)
        except _NothingToSay as exc:
            logger.warning("Local generation refused: %s", exc)
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            if self._cancelled:
                logger.info("Local generation cancelled: %s", self._output_path)
            else:
                logger.exception("Local generation failed: voice=%s output=%s",
                                 self._voice, self._output_path)
                self.failed.emit(self._user_message(exc))
        finally:
            if writer is not None:
                writer.abort()      # no-op after finish()
            if synth is not None:
                try:
                    synth.close()
                except Exception:  # noqa: BLE001
                    logger.warning("Closing local synthesizer failed", exc_info=True)
            self._synth = None

    def _render(self, synth, chunk: str, index: int, total: int) -> bytes:
        last: Exception | None = None
        for attempt in range(_RETRIES + 1):
            if self._cancelled:
                raise CANCELLED_ERRORS[0]("cancelled")
            try:
                return synth.synthesize(chunk, self._rate)
            except CANCELLED_ERRORS:
                raise
            except Exception as exc:  # noqa: BLE001
                if self._cancelled:
                    raise CANCELLED_ERRORS[0]("cancelled") from exc
                last = exc
                logger.warning("Local chunk %d/%d failed (attempt %d): %s",
                               index, total, attempt + 1, exc)
                self.stage_changed.emit("waiting", f"Retrying part {index}…")
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"Part {index} of {total} could not be spoken: {last}")

    def _check_disk(self, total_chars: int) -> None:
        target = Path(self._output_path).parent
        try:
            target.mkdir(parents=True, exist_ok=True)
            need = int(total_chars * _BYTES_PER_CHAR * 1.2)
            free = shutil.disk_usage(target).free
        except OSError:
            return
        if free < need:
            raise OSError(
                f"Not enough free disk space: about {need / 1e6:.0f} MB is needed, "
                f"but only {free / 1e6:.0f} MB is free."
            )

    def _user_message(self, exc: Exception) -> str:
        name = local_display_name(self._voice)
        if isinstance(exc, PermissionError):
            return (f"SetupTTS can't write to this location.\n\n{self._output_path}\n\n"
                    "Choose another folder and try again.")
        return f"Could not create audio with {name}.\n\n{exc}"
