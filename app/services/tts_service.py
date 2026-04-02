"""
Core TTS generation logic.

This module is the only place that imports edge_tts directly.
All callers use this service rather than touching edge_tts.
"""

import asyncio
import logging
import time
from pathlib import Path

import edge_tts

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_S = 20
DEFAULT_RECEIVE_TIMEOUT_S = 90
_VOICE_CACHE_TTL_S = 15 * 60
_VOICE_CACHE: list[dict] | None = None
_VOICE_CACHE_AT = 0.0


def build_communicate(
    text: str,
    voice: str,
    rate: str,
    volume: str,
    *,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_S,
    receive_timeout: int = DEFAULT_RECEIVE_TIMEOUT_S,
) -> edge_tts.Communicate:
    """
    Build a fresh edge_tts Communicate instance.

    Each retry should use a new object so no half-dead websocket/session
    state is ever reused across attempts.
    """
    return edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
        volume=volume,
        connect_timeout=connect_timeout,
        receive_timeout=receive_timeout,
    )


async def generate_audio(
    text: str,
    voice: str,
    rate: str,
    volume: str,
    output_path: str | Path,
    *,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_S,
    receive_timeout: int = DEFAULT_RECEIVE_TIMEOUT_S,
) -> None:
    """
    Generate an MP3 file from text using Microsoft Edge TTS.

    Parameters
    ----------
    text        : The text to convert.
    voice       : Voice short name, e.g. "en-US-AvaNeural".
    rate        : Rate string, e.g. "+5%" or "-10%".
    volume      : Volume string, e.g. "+0%" or "-5%".
    output_path : Destination file path (will be created/overwritten).

    Raises
    ------
    RuntimeError on network or service errors.
    PermissionError if the output path is not writable.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    logger.debug("Generating audio: voice=%s rate=%s output=%s", voice, rate, output_path)

    communicate = build_communicate(
        text=text,
        voice=voice,
        rate=rate,
        volume=volume,
        connect_timeout=connect_timeout,
        receive_timeout=receive_timeout,
    )
    await communicate.save(str(output_path))
    logger.debug("Audio saved: %s  size=%d bytes", output_path, output_path.stat().st_size)


async def list_voices(*, force_refresh: bool = False) -> list[dict]:
    """Return the full list of available voices from the edge_tts service."""
    global _VOICE_CACHE, _VOICE_CACHE_AT

    now = time.monotonic()
    if (
        not force_refresh
        and _VOICE_CACHE is not None
        and (now - _VOICE_CACHE_AT) < _VOICE_CACHE_TTL_S
    ):
        return list(_VOICE_CACHE)

    voices = await edge_tts.list_voices()
    _VOICE_CACHE = list(voices)
    _VOICE_CACHE_AT = now
    return list(voices)
