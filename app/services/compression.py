"""
Audiobook compression: smaller MP3 files for spoken word.

Narration does not need music bitrates.  Three levels trade a little
clarity for a lot of disk space; "off" keeps the voice's native quality.

* Local voices (Piper, Kokoro, built-in) are encoded straight at the chosen
  bitrate — no second pass.
* Online voices arrive from the service as fixed 48 kbit/s MP3, so the
  finished file is decoded and re-encoded once it has been verified.

The re-encode never puts the user's audio at risk: it writes to a ``.part``
file next to the original, checks that the new file decodes to the same
length and really is smaller, and only then swaps it in.  Any failure,
cancel, or locked file leaves the original exactly as it was.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.utils.file_ops import replace_with_retry
from app.utils.mp3_duration import mp3_duration_seconds

logger = logging.getLogger(__name__)

OFF = "off"
LIGHT = "light"
BALANCED = "balanced"
SMALLEST = "smallest"

#: Every accepted setting value, in the order the Settings dialog lists them.
LEVELS = (OFF, LIGHT, BALANCED, SMALLEST)
DEFAULT_LEVEL = BALANCED


@dataclass(frozen=True)
class CompressionProfile:
    level: str
    label: str
    bitrate_kbps: int
    description: str

    @property
    def mb_per_hour(self) -> float:
        return self.bitrate_kbps * 1000 / 8 * 3600 / 1e6


#: The encoder picks the output sample rate for each bitrate itself (LAME's
#: own table: 24 kbit/s → 16 kHz, 32 → 22.05 kHz, 40 → 24 kHz), which keeps
#: speech clearer than forcing the voice's native rate into fewer bits.
PROFILES: dict[str, CompressionProfile] = {
    LIGHT: CompressionProfile(LIGHT, "Light", 40,
                              "Near-original clarity, a little smaller"),
    BALANCED: CompressionProfile(BALANCED, "Balanced", 32,
                                 "Clear narration; the best balance of size and quality for audiobooks"),
    SMALLEST: CompressionProfile(SMALLEST, "Smallest", 24,
                                 "Smallest files; speech stays clear but a little less crisp"),
}

#: LAME quality 5 is its default ("good"): within earshot of 2 for speech at
#: these bitrates and noticeably faster on a book-length file.
ENCODER_QUALITY = 5

#: Decoded per step: four seconds of 24 kHz audio, so progress and cancel
#: stay responsive and memory stays flat however long the book is.
_FRAMES_PER_BLOCK = 96_000

#: Allowed length difference after re-encoding.  Encoder padding adds tens
#: of milliseconds; anything beyond this means the decode stopped early.
_DURATION_TOLERANCE_S = 1.0
_DURATION_TOLERANCE_FRACTION = 0.002


def normalize_level(value: object) -> str:
    return value if isinstance(value, str) and value in LEVELS else DEFAULT_LEVEL


def profile_for(level: str | None) -> CompressionProfile | None:
    """The profile for *level*, or None when compression is off / unknown."""
    return PROFILES.get(level or OFF)


def level_label(level: str) -> str:
    profile = profile_for(level)
    return profile.label if profile else "Off"


def local_bitrate_kbps(level: str | None, native_kbps: int) -> int:
    """Bitrate for a local voice: the profile's, but never above the native one."""
    profile = profile_for(level)
    if profile is None:
        return native_kbps
    return min(native_kbps, profile.bitrate_kbps)


class CompressionCancelled(Exception):
    """The job was cancelled while its file was being compressed."""


@dataclass(frozen=True)
class CompressionResult:
    path: Path
    original_bytes: int
    compressed_bytes: int
    bitrate_kbps: int
    seconds: float

    @property
    def saved_fraction(self) -> float:
        if self.original_bytes <= 0:
            return 0.0
        return 1.0 - self.compressed_bytes / self.original_bytes


def compress_mp3_in_place(
    path: str | Path,
    level: str | None,
    *,
    source_bitrate_kbps: int | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> CompressionResult | None:
    """
    Re-encode the MP3 at *path* to the bitrate of *level*.

    Returns the result when the smaller file has replaced the original, or
    None when nothing was changed (compression off, the file is already at
    or below the target bitrate, or any step failed — the original stays).
    Raises :class:`CompressionCancelled` if *should_cancel* turns true; the
    original is left untouched in that case too.
    """
    profile = profile_for(level)
    if profile is None:
        return None
    path = Path(path)
    if source_bitrate_kbps is not None and source_bitrate_kbps <= profile.bitrate_kbps:
        logger.info("Compression skipped for %s: already %d kbit/s (target %d)",
                    path.name, source_bitrate_kbps, profile.bitrate_kbps)
        return None

    part = path.with_name(path.name + ".compress.part")
    started = time.monotonic()
    try:
        original_bytes = path.stat().st_size
        original_duration = mp3_duration_seconds(path)
        _transcode(path, part, profile.bitrate_kbps,
                   original_duration, should_cancel, on_progress)
        compressed_bytes = part.stat().st_size
        new_duration = mp3_duration_seconds(part)
        problem = _verify(original_bytes, compressed_bytes, original_duration, new_duration)
        if problem:
            logger.warning("Compression of %s discarded (%s); keeping the original",
                           path.name, problem)
            _unlink(part)
            return None
        replace_with_retry(part, path)
    except CompressionCancelled:
        _unlink(part)
        raise
    except Exception:  # noqa: BLE001 - the original file is always the fallback
        logger.warning("Compression of %s failed; keeping the original", path,
                       exc_info=True)
        _unlink(part)
        return None

    result = CompressionResult(path, original_bytes, compressed_bytes,
                               profile.bitrate_kbps, time.monotonic() - started)
    logger.info(
        "Compressed %s: level=%s %d kbit/s %d → %d bytes (%.0f%% smaller) "
        "duration=%s in %.1fs",
        path.name, profile.level, profile.bitrate_kbps, original_bytes,
        compressed_bytes, result.saved_fraction * 100,
        f"{new_duration:.1f}s" if new_duration is not None else "n/a", result.seconds,
    )
    return result


def _transcode(src: Path, dst: Path, bitrate_kbps: int, duration: float | None,
               should_cancel: Callable[[], bool] | None,
               on_progress: Callable[[int], None] | None) -> None:
    import lameenc
    import miniaudio

    info = miniaudio.mp3_get_file_info(str(src))
    rate = int(info.sample_rate)
    if rate <= 0:
        raise ValueError("The MP3 has no readable audio")
    total_frames = int(duration * rate) if duration else int(info.num_frames or 0)

    enc = lameenc.Encoder()
    enc.set_bit_rate(bitrate_kbps)
    enc.set_in_sample_rate(rate)
    enc.set_channels(1)
    enc.set_quality(ENCODER_QUALITY)

    done = 0
    last_pct = -1
    with open(dst, "wb") as fh:
        # Mixed down to mono: every voice is mono already, and a stray
        # stereo source would otherwise be read as interleaved samples.
        for block in miniaudio.stream_file(str(src), nchannels=1, sample_rate=rate,
                                           frames_to_read=_FRAMES_PER_BLOCK):
            if should_cancel is not None and should_cancel():
                raise CompressionCancelled()
            fh.write(enc.encode(block.tobytes()))
            done += len(block)
            if on_progress is not None and total_frames > 0:
                pct = min(99, done * 100 // total_frames)
                if pct != last_pct:
                    last_pct = pct
                    on_progress(pct)
        fh.write(enc.flush())
        fh.flush()
        os.fsync(fh.fileno())
    if done == 0:
        raise ValueError("The MP3 decoded to no audio")
    if on_progress is not None:
        on_progress(100)


def _verify(original_bytes: int, compressed_bytes: int,
            original_duration: float | None, new_duration: float | None) -> str:
    """Why the compressed file must not replace the original ('' if it may)."""
    if compressed_bytes <= 0:
        return "empty output"
    if compressed_bytes >= original_bytes:
        return f"not smaller ({compressed_bytes} >= {original_bytes} bytes)"
    if new_duration is None:
        return "compressed file is unreadable"
    if original_duration is not None:
        tolerance = max(_DURATION_TOLERANCE_S, original_duration * _DURATION_TOLERANCE_FRACTION)
        if abs(new_duration - original_duration) > tolerance:
            return (f"length changed {original_duration:.1f}s → {new_duration:.1f}s")
    return ""


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove %s", path, exc_info=True)

