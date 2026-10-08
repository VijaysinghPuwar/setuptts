"""
Voices that run on this computer: OS built-in voices and offline Piper voices.

Both engines expose the same small synthesizer interface —
``synthesize(text, rate) -> bytes`` (16-bit mono PCM at ``sample_rate``),
``cancel()`` and ``close()`` — so one worker, one preview path and one MP3
writer serve both.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from app.services import piper_tts, system_tts

MP3_BITRATE_KBPS = 64

#: Values for Voice.source.
SOURCE_ONLINE = "online"
SOURCE_SYSTEM = "system"
SOURCE_PIPER = "piper"


class LocalSynthesizer(Protocol):
    sample_rate: int

    def synthesize(self, text: str, rate: str) -> bytes: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...


CANCELLED_ERRORS = (system_tts.SystemSpeechCancelled, piper_tts.PiperCancelled)


def is_local_voice(short_name: str | None) -> bool:
    return system_tts.is_system_voice(short_name) or piper_tts.is_piper_voice(short_name)


def source_of(short_name: str | None) -> str:
    if system_tts.is_system_voice(short_name):
        return SOURCE_SYSTEM
    if piper_tts.is_piper_voice(short_name):
        return SOURCE_PIPER
    return SOURCE_ONLINE


def local_display_name(short_name: str) -> str:
    if system_tts.is_system_voice(short_name):
        return system_tts.system_display_name(short_name)
    if piper_tts.is_piper_voice(short_name):
        return piper_tts.display_name(short_name[len(piper_tts.PIPER_PREFIX):])
    return short_name


def local_locale(short_name: str) -> str:
    """Best-effort locale for a local ShortName when only the name is known."""
    if piper_tts.is_piper_voice(short_name):
        return short_name[len(piper_tts.PIPER_PREFIX):].split("-")[0].replace("_", "-")
    return ""


def make_synthesizer(short_name: str) -> LocalSynthesizer:
    if system_tts.is_system_voice(short_name):
        return system_tts.SystemSynthesizer(short_name)
    if piper_tts.is_piper_voice(short_name):
        return piper_tts.PiperSynthesizer(short_name)
    raise ValueError(f"Not a local voice: {short_name}")


def list_local_voices() -> list[dict]:
    """Every installed local voice (never raises)."""
    voices = []
    try:
        voices.extend(piper_tts.list_piper_voices())
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("Listing Piper voices failed", exc_info=True)
    voices.extend(system_tts.list_system_voices())
    return voices


class Mp3Writer:
    """
    Streams PCM into one MP3 file.  Written to ``<path>.part`` and moved into
    place by :meth:`finish`, so a failed or cancelled job never leaves a
    truncated MP3 behind (or replaces a good one).
    """

    def __init__(self, path: str | Path, sample_rate: int,
                 bitrate_kbps: int = MP3_BITRATE_KBPS) -> None:
        import lameenc

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._part = self.path.with_name(self.path.name + ".part")
        self._fh = open(self._part, "wb")
        self._enc = lameenc.Encoder()
        self._enc.set_bit_rate(bitrate_kbps)
        self._enc.set_in_sample_rate(sample_rate)
        self._enc.set_channels(1)
        self._enc.set_quality(2)
        self._rate = sample_rate
        self.samples = 0
        self._done = False

    @property
    def duration_seconds(self) -> float:
        return self.samples / self._rate

    def write(self, pcm: bytes) -> None:
        if pcm:
            if len(pcm) % 2:
                pcm = pcm[:-1]
            self.samples += len(pcm) // 2
            self._fh.write(self._enc.encode(pcm))

    def write_silence(self, seconds: float) -> None:
        self.write(bytes(2 * int(self._rate * seconds)))

    def finish(self) -> Path:
        self._fh.write(self._enc.flush())
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        os.replace(self._part, self.path)
        self._done = True
        return self.path

    def abort(self) -> None:
        if self._done:
            return
        try:
            self._fh.close()
        except OSError:
            pass
        try:
            self._part.unlink(missing_ok=True)
        except OSError:
            pass
        self._done = True


def synthesize_to_mp3(short_name: str, text: str, rate: str, output_path: str | Path,
                      synth: LocalSynthesizer | None = None) -> float:
    """Render *text* to an MP3 in one go (previews, short clips).  Returns seconds."""
    own = synth is None
    synth = synth or make_synthesizer(short_name)
    writer = None
    try:
        writer = Mp3Writer(output_path, synth.sample_rate)
        writer.write(synth.synthesize(text, rate))
        if writer.samples == 0:
            raise RuntimeError("The voice produced no audio for this text")
        writer.finish()
        return writer.duration_seconds
    except BaseException:
        if writer is not None:
            writer.abort()
        raise
    finally:
        if own:
            synth.close()
