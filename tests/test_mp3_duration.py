from app.utils.mp3_duration import mp3_duration_from_bytes


def _build_mpeg1_l3_frame() -> bytes:
    """Build a tiny MPEG-1 Layer III frame (32 kbps, 44.1 kHz, mono).

    Real audio data is not required — the duration parser only reads the
    frame header. We pad with the rest of the calculated frame length so
    the parser advances cleanly to the next frame.
    """
    # Header bytes:
    #   0xFF FB ?? ??
    # Where the third byte's high nibble = bitrate index 1 (32 kbps),
    # bits[3:2] = 0 (44.1 kHz), padding=0.
    # bitrate index 0001 = 32 kbps, sample rate index 00 = 44100Hz.
    # Channel mode bits in byte 4: 11000000 = mono (3<<6).
    header = bytes([0xFF, 0xFB, 0x10, 0xC0])
    # Frame size for MPEG-1 Layer III 32 kbps 44.1 kHz =
    #   (1152 * 32_000 / (8 * 44_100)) = 104.49 -> 104 bytes
    frame_len = (1152 * 32_000) // (8 * 44_100)
    payload = b"\x00" * (frame_len - len(header))
    return header + payload


def test_mp3_duration_from_bytes_reads_synthetic_frames():
    one_frame = _build_mpeg1_l3_frame()
    # 10 frames at 1152 samples / 44100 Hz ≈ 0.261 s.
    data = one_frame * 10
    duration = mp3_duration_from_bytes(data)
    assert duration is not None
    assert 0.2 < duration < 0.35


def test_mp3_duration_from_bytes_returns_none_for_junk():
    assert mp3_duration_from_bytes(b"") is None
    assert mp3_duration_from_bytes(b"not an mp3 file") is None
