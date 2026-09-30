"""
Core TTS generation logic.

This module is the only place that imports edge_tts directly.
All callers use this service rather than touching edge_tts.
"""

import logging
import os
import ssl
import time
import urllib.request
from pathlib import Path

import certifi
import edge_tts
import edge_tts.communicate
import edge_tts.voices

logger = logging.getLogger(__name__)

#: Where edge_tts sends voice-list and synthesis requests.
SPEECH_HOST = "speech.platform.bing.com"


def build_ssl_context() -> ssl.SSLContext:
    """
    Certificates to trust for the speech service: certifi's bundle plus the
    operating system's own store.

    edge_tts trusts certifi alone.  On Windows that fails wherever something
    re-signs HTTPS traffic with a root it installed in the Windows store -
    antivirus web shields (Avast, AVG, Kaspersky, ESET, Bitdefender), school
    and office proxies - so the voice list never loaded and SetupTTS looked
    as if Edge TTS were missing.  Adding the system store fixes that without
    trusting anything the machine does not already trust.
    """
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        # On Windows this reads the system ROOT and CA stores.
        ctx.load_default_certs()
    except (ssl.SSLError, OSError):
        logger.warning("Could not load the system certificate store", exc_info=True)
    return ctx


def system_proxy() -> str | None:
    """
    The HTTP proxy the system is set to use, or None.

    aiohttp only reads proxies from environment variables, but on Windows the
    proxy is normally set in Settings > Network > Proxy (the registry), which
    urllib knows how to read.  Environment variables still win: aiohttp
    applies those itself.
    """
    if any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")):
        return None
    try:
        proxies = urllib.request.getproxies()
        if proxies and urllib.request.proxy_bypass(SPEECH_HOST):
            return None
    except Exception:  # noqa: BLE001 - a broken proxy setting must not stop the app
        logger.warning("Could not read the system proxy setting", exc_info=True)
        return None
    for key in ("https", "http"):
        proxy = proxies.get(key)
        # aiohttp can only tunnel through an HTTP proxy.
        if proxy and proxy.lower().startswith("http://"):
            return proxy
    return None


def _install_ssl_context() -> None:
    # edge_tts builds its context at import and reads the module global on
    # every request, so replacing it covers voices and synthesis alike.
    # tests/test_network_setup.py checks these names against the pinned version.
    ctx = build_ssl_context()
    edge_tts.communicate._SSL_CTX = ctx
    edge_tts.voices._SSL_CTX = ctx


_install_ssl_context()

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
        proxy=system_proxy(),
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

    voices = await edge_tts.list_voices(proxy=system_proxy())
    _VOICE_CACHE = list(voices)
    _VOICE_CACHE_AT = now
    return list(voices)
