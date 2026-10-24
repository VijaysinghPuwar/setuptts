"""
Voices that run on this computer: OS built-in voices, offline Piper voices and
offline HD Kokoro voices.

Every engine exposes the same small synthesizer interface —
``synthesize(text, rate) -> bytes`` (16-bit mono PCM at ``sample_rate``),
``cancel()`` and ``close()`` — so one worker, one preview path and one MP3
writer serve them all.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Protocol

from app.services import kokoro_tts, piper_tts, system_tts
from app.utils.file_ops import replace_with_retry
from app.utils.output_paths import next_free_path

logger = logging.getLogger(__name__)

MP3_BITRATE_KBPS = 64

#: Values for Voice.source.
SOURCE_ONLINE = "online"
SOURCE_SYSTEM = "system"
SOURCE_PIPER = "piper"
SOURCE_KOKORO = "kokoro"


class LocalSynthesizer(Protocol):
    sample_rate: int

    def synthesize(self, text: str, rate: str) -> bytes: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...


CANCELLED_ERRORS = (system_tts.SystemSpeechCancelled, piper_tts.PiperCancelled,
                    kokoro_tts.KokoroCancelled)


def is_local_voice(short_name: str | None) -> bool:
    return (system_tts.is_system_voice(short_name) or piper_tts.is_piper_voice(short_name)
            or kokoro_tts.is_kokoro_voice(short_name))


def source_of(short_name: str | None) -> str:
    if system_tts.is_system_voice(short_name):
        return SOURCE_SYSTEM
    if piper_tts.is_piper_voice(short_name):
        return SOURCE_PIPER
    if kokoro_tts.is_kokoro_voice(short_name):
        return SOURCE_KOKORO
    return SOURCE_ONLINE


def local_display_name(short_name: str) -> str:
    if system_tts.is_system_voice(short_name):
        return system_tts.system_display_name(short_name)
    if piper_tts.is_piper_voice(short_name):
        return piper_tts.display_name(short_name[len(piper_tts.PIPER_PREFIX):])
    if kokoro_tts.is_kokoro_voice(short_name):
        return kokoro_tts.display_name(short_name[len(kokoro_tts.KOKORO_PREFIX):])
    return short_name


def local_locale(short_name: str) -> str:
    """Best-effort locale for a local ShortName when only the name is known."""
    if piper_tts.is_piper_voice(short_name):
        return short_name[len(piper_tts.PIPER_PREFIX):].split("-")[0].replace("_", "-")
    if kokoro_tts.is_kokoro_voice(short_name):
        return kokoro_tts.locale_for(short_name[len(kokoro_tts.KOKORO_PREFIX):])
    return ""


def make_synthesizer(short_name: str) -> LocalSynthesizer:
    if system_tts.is_system_voice(short_name):
        return system_tts.SystemSynthesizer(short_name)
    if piper_tts.is_piper_voice(short_name):
        return piper_tts.PiperSynthesizer(short_name)
    if kokoro_tts.is_kokoro_voice(short_name):
        return kokoro_tts.KokoroSynthesizer(short_name)
    raise ValueError(f"Not a local voice: {short_name}")


def list_local_voices() -> list[dict]:
    """Every installed local voice (never raises)."""
    voices = []
    try:
        voices.extend(kokoro_tts.list_kokoro_voices())
    except Exception:  # noqa: BLE001
        logger.warning("Listing Kokoro voices failed", exc_info=True)
    try:
        voices.extend(piper_tts.list_piper_voices())
    except Exception:  # noqa: BLE001
        logger.warning("Listing Piper voices failed", exc_info=True)
    voices.extend(system_tts.list_system_voices())
    return voices


_VOLUME_RE = re.compile(r"\s*([+-]?\d+)\s*%\s*")


def volume_gain(volume: str | None) -> float:
    """
    Linear gain for an edge-style volume string ("+0%", "-20%", "+10%").

    Online voices get the volume from the service; local voices render at a
    fixed level, so the same setting is applied to their samples instead.
    Anything unreadable means "unchanged".
    """
    match = _VOLUME_RE.fullmatch(volume or "")
    if not match:
        return 1.0
    return max(0.0, 1.0 + int(match.group(1)) / 100.0)


def apply_gain(pcm: bytes, gain: float) -> bytes:
    """16-bit PCM scaled by *gain*, clipped so loud peaks don't wrap around."""
    if gain == 1.0 or not pcm:
        return pcm
    import numpy as np

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * gain
    return np.clip(np.rint(samples), -32768, 32767).astype("<i2").tobytes()


def sweep_stale_files(max_age_s: float = 24 * 3600) -> None:
    """
    Remove what hard-killed runs leave behind: system-voice temp folders
    (which hold the book's text) and half-downloaded Piper voices.  Anything
    still in use is touched far more often than once a day, so only files
    older than *max_age_s* go.  Best effort; never raises.
    """
    for sweep in (system_tts.sweep_stale_temp_dirs, piper_tts.sweep_stale_parts):
        try:
            sweep(max_age_s)
        except Exception:  # noqa: BLE001 - housekeeping must not break startup
            logger.debug("Stale file sweep failed", exc_info=True)


class Mp3Writer:
    """
    Streams PCM into one MP3 file.  Written to ``<path>.part`` and moved into
    place by :meth:`finish`, so a failed or cancelled job never leaves a
    truncated MP3 behind (or replaces a good one).
    """

    def __init__(self, path: str | Path, sample_rate: int,
                 bitrate_kbps: int = MP3_BITRATE_KBPS, *, gain: float = 1.0) -> None:
        import lameenc

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._part = self.path.with_name(self.path.name + ".part")
        # Encoder first: if it can't be set up (a bad sample rate), nothing
        # has been opened yet, so no handle or empty .part is left behind.
        self._enc = lameenc.Encoder()
        self._enc.set_bit_rate(bitrate_kbps)
        self._enc.set_in_sample_rate(sample_rate)
        self._enc.set_channels(1)
        self._enc.set_quality(2)
        self._rate = sample_rate
        self._gain = gain
        self.samples = 0
        self._done = False
        self._fh = open(self._part, "wb")

    @property
    def duration_seconds(self) -> float:
        return self.samples / self._rate

    def write(self, pcm: bytes) -> None:
        if pcm:
            if len(pcm) % 2:
                pcm = pcm[:-1]
            self.samples += len(pcm) // 2
            if self._gain != 1.0:
                pcm = apply_gain(pcm, self._gain)
            self._fh.write(self._enc.encode(pcm))

    def write_silence(self, seconds: float) -> None:
        self.write(bytes(2 * int(self._rate * seconds)))

    def finish(self) -> Path:
        self._fh.write(self._enc.flush())
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        try:
            replace_with_retry(self._part, self.path)
        except PermissionError:
            # The file being replaced is open elsewhere (a media player, or
            # Explorer's preview pane on Windows).  The .part was written in
            # the same folder, so the folder is writable: keep the finished
            # audio under the next free name rather than throwing it away.
            if not self.path.exists():
                raise
            alt = next_free_path(self.path)
            replace_with_retry(self._part, alt)
            logger.warning("%s is in use; saved as %s instead", self.path, alt.name)
            self.path = alt
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
