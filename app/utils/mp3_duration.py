"""
Lightweight MP3 frame parser used to verify long-form audio output.

The runtime concatenates raw MP3 chunks returned by the speech service, so the
final file is a sequence of MPEG audio frames without an ID3v2 prefix (in
practice). For long-form jobs we want to know the *actual* total duration in
seconds so we can compare it against a reasonable estimate derived from chunk
counts. Bringing in ffmpeg or mutagen for a desktop app is overkill, so we
parse frame headers directly.

The parser is intentionally tolerant — unknown bytes between frames (junk,
short ID3 tags, padding) are skipped rather than raising. The return value is
``None`` when no valid frames could be located.
"""

from __future__ import annotations

from pathlib import Path

# Bitrate table (kbps) for MPEG-1 Layer III.
_BITRATES_V1_L3 = (
    0, 32, 40, 48, 56, 64, 80, 96,
    112, 128, 160, 192, 224, 256, 320, -1,
)

# Bitrate table (kbps) for MPEG-2 / MPEG-2.5 Layer III.
_BITRATES_V2_L3 = (
    0, 8, 16, 24, 32, 40, 48, 56,
    64, 80, 96, 112, 128, 144, 160, -1,
)

_SAMPLE_RATES_V1 = (44100, 48000, 32000, 0)
_SAMPLE_RATES_V2 = (22050, 24000, 16000, 0)
_SAMPLE_RATES_V25 = (11025, 12000, 8000, 0)


_READ_BLOCK = 1 << 20   # 1 MiB


def mp3_duration_seconds(path: Path) -> float | None:
    """
    Return the duration of an MP3 file in seconds, or None on failure.

    Streams the file in blocks: a 12-hour audiobook is ~260 MB, and reading it
    whole doubled the app's memory footprint at the very end of a long job.
    """
    try:
        with open(path, "rb") as fh:
            state = _ScanState()
            buffer = fh.read(_READ_BLOCK)
            pos = _skip_id3v2(buffer, 0)
            while True:
                more = fh.read(_READ_BLOCK)
                pos = _scan_frames(buffer, pos, state, final=not more)
                if not more:
                    break
                # Keep the unscanned tail (a partial frame) and continue.
                buffer = buffer[pos:] + more
                pos = 0
    except OSError:
        return None
    return state.duration()


def mp3_duration_from_bytes(data: bytes) -> float | None:
    """Estimate MP3 duration from a raw byte buffer by walking frame headers."""
    if not data:
        return None
    state = _ScanState()
    _scan_frames(data, _skip_id3v2(data, 0), state, final=True)
    return state.duration()


class _ScanState:
    __slots__ = ("total_samples", "sample_rate_hint", "frames_seen")

    def __init__(self) -> None:
        self.total_samples = 0
        self.sample_rate_hint: int | None = None
        self.frames_seen = 0

    def duration(self) -> float | None:
        if self.sample_rate_hint is None or self.frames_seen == 0:
            return None
        return self.total_samples / float(self.sample_rate_hint)


def _scan_frames(data: bytes, pos: int, state: _ScanState, *, final: bool) -> int:
    """
    Walk frame headers from *pos*, accumulating into *state*.

    Returns the position scanning stopped at.  When not *final*, stops before
    a header or frame that runs past the end of *data* so the caller can
    append the next block and resume there.
    """
    length = len(data)

    while pos + 4 <= length:
        if data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
            pos += 1
            continue

        h1 = data[pos + 1]
        h2 = data[pos + 2]

        version_bits = (h1 >> 3) & 0x03
        layer_bits = (h1 >> 1) & 0x03
        bitrate_idx = (h2 >> 4) & 0x0F
        sr_idx = (h2 >> 2) & 0x03
        padding = (h2 >> 1) & 0x01

        if layer_bits != 0b01:
            pos += 1
            continue

        if version_bits == 0b11:
            samples_per_frame = 1152
            bitrate_kbps = _BITRATES_V1_L3[bitrate_idx]
            sample_rate = _SAMPLE_RATES_V1[sr_idx]
        elif version_bits == 0b10:
            samples_per_frame = 576
            bitrate_kbps = _BITRATES_V2_L3[bitrate_idx]
            sample_rate = _SAMPLE_RATES_V2[sr_idx]
        elif version_bits == 0b00:
            samples_per_frame = 576
            bitrate_kbps = _BITRATES_V2_L3[bitrate_idx]
            sample_rate = _SAMPLE_RATES_V25[sr_idx]
        else:
            pos += 1
            continue

        if bitrate_kbps <= 0 or sample_rate <= 0:
            pos += 1
            continue

        frame_size = int((samples_per_frame * bitrate_kbps * 1000) // (8 * sample_rate)) + padding
        if frame_size < 4:
            pos += 1
            continue

        if not final and pos + frame_size > length:
            break   # frame continues in the next block

        state.total_samples += samples_per_frame
        state.sample_rate_hint = sample_rate
        state.frames_seen += 1
        pos += frame_size

    return pos


def _skip_id3v2(data: bytes, pos: int) -> int:
    """Skip past an ID3v2 tag if present at ``pos``. Tolerant of short input."""
    if len(data) - pos < 10 or data[pos:pos + 3] != b"ID3":
        return pos
    size_bytes = data[pos + 6:pos + 10]
    if len(size_bytes) < 4:
        return pos
    size = (
        ((size_bytes[0] & 0x7F) << 21)
        | ((size_bytes[1] & 0x7F) << 14)
        | ((size_bytes[2] & 0x7F) << 7)
        | (size_bytes[3] & 0x7F)
    )
    return pos + 10 + size
