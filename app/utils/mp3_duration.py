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


def mp3_duration_seconds(path: Path) -> float | None:
    """Return the duration of an MP3 file in seconds, or None on failure."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return mp3_duration_from_bytes(data)


def mp3_duration_from_bytes(data: bytes) -> float | None:
    """Estimate MP3 duration from a raw byte buffer by walking frame headers."""
    if not data:
        return None

    pos = _skip_id3v2(data, 0)
    total_samples = 0
    sample_rate_hint: int | None = None
    frames_seen = 0
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

        total_samples += samples_per_frame
        sample_rate_hint = sample_rate
        frames_seen += 1
        pos += frame_size

    if sample_rate_hint is None or frames_seen == 0:
        return None
    return total_samples / float(sample_rate_hint)


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
