"""
Core TTS generation logic.

This module is the only place that imports edge_tts directly.
All callers use this service rather than touching edge_tts.
"""

import asyncio
import logging
from pathlib import Path

import edge_tts

logger = logging.getLogger(__name__)


async def generate_audio(
    text: str,
    voice: str,
    rate: str,
    volume: str,
    output_path: str | Path,
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

    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
        volume=volume,
    )
    await communicate.save(str(output_path))
    logger.debug("Audio saved: %s  size=%d bytes", output_path, output_path.stat().st_size)


async def list_voices() -> list[dict]:
    """Return the full list of available voices from the edge_tts service."""
    return await edge_tts.list_voices()
