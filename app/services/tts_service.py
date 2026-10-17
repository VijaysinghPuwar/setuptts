"""
Core TTS generation logic.

This module is the only place that imports edge_tts directly.
All callers use this service rather than touching edge_tts.
"""

import logging
import os
import ssl
import threading
import time
import urllib.request
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import certifi
import edge_tts
import edge_tts.communicate
import edge_tts.drm
import edge_tts.voices
from edge_tts.constants import TRUSTED_CLIENT_TOKEN

logger = logging.getLogger(__name__)

#: Where edge_tts sends voice-list requests.
SPEECH_HOST = "speech.platform.bing.com"

#: Where speech is synthesised, preferred first.  On 12 October 2026 the
#: legacy host began answering every synthesis request with HTTP 403 (it had
#: already in some countries); Microsoft Edge itself synthesises through
#: api.msedgeservices.com, which accepts the same token and returns the same
#: 24 kHz 48 kbit/s MP3.  If the active host starts refusing, the other is
#: tried, so the next change on Microsoft's side need not break every job.
SYNTHESIS_ENDPOINTS: tuple[str, ...] = (
    "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
    f"?Ocp-Apim-Subscription-Key={TRUSTED_CLIENT_TOKEN}",
    edge_tts.communicate.WSS_URL,
)
_endpoint_lock = threading.Lock()
_endpoint_index = 0


def synthesis_host() -> str:
    """Host name of the synthesis endpoint in use."""
    return urlsplit(SYNTHESIS_ENDPOINTS[_endpoint_index]).hostname or SPEECH_HOST


def _use_endpoint(index: int) -> None:
    global _endpoint_index
    _endpoint_index = index
    # edge_tts reads this module global each time it connects.
    edge_tts.communicate.WSS_URL = SYNTHESIS_ENDPOINTS[index]


def synthesis_refused(host: str | None) -> bool:
    """
    The service refused a synthesis connection on *host* (HTTP 401/403).

    Switches to the next endpoint when *host* is the one in use; a refusal
    reported late for an endpoint already left behind changes nothing, so
    two jobs failing at once cannot flip it back.  Returns True if switched.
    """
    with _endpoint_lock:
        if host and host != synthesis_host():
            return False
        new = (_endpoint_index + 1) % len(SYNTHESIS_ENDPOINTS)
        logger.warning("Speech host %s refused the connection; switching to %s",
                       synthesis_host(), urlsplit(SYNTHESIS_ENDPOINTS[new]).hostname)
        _use_endpoint(new)
        return True


def handshake_refusal(exc: BaseException) -> "aiohttp.WSServerHandshakeError | None":
    """
    The HTTP 401/403 handshake error behind *exc*, or None.

    edge_tts answers a 403 by correcting the clock from the reply's Date
    header; when that fails it raises SkewAdjustmentError chained from the
    handshake error, so the cause chain is followed.
    """
    current: BaseException | None = exc
    for _ in range(5):
        if current is None:
            break
        if isinstance(current, aiohttp.WSServerHandshakeError) and current.status in (401, 403):
            return current
        current = current.__cause__ or current.__context__
    return None


def refused_host(exc: BaseException) -> str | None:
    """Host named by a refusal's request URL, if it has one."""
    url = getattr(getattr(exc, "request_info", None), "url", None)
    return getattr(url, "host", None)


def _parse_http_date(date: str) -> float | None:
    """
    RFC 2616 date → Unix time, in any locale.

    edge_tts parses the server's Date header (used to correct a skewed clock
    after a 403) with strptime, whose day and month names follow the process
    locale: under a German or Portuguese locale, which Qt applies on Linux,
    "Mon, 12 Oct 2026 …" fails and the correction never happens.
    """
    try:
        parsed = parsedate_to_datetime(date)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


_use_endpoint(0)
edge_tts.drm.DRM.parse_rfc2616_date = staticmethod(_parse_http_date)


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


_BYPASS_TTL_S = 300.0
_bypass_cache: tuple[tuple, float, bool] | None = None


def _bypasses_proxy(proxies: dict, host: str) -> bool:
    """
    urllib.request.proxy_bypass(host), remembered for a few minutes.

    On Windows, with a proxy bypass list set, the check does a DNS and a
    reverse-DNS lookup — and it runs once per chunk inside the event loop.
    """
    global _bypass_cache
    check = urllib.request.proxy_bypass
    key = (tuple(sorted(proxies.items())), host, check)
    now = time.monotonic()
    if _bypass_cache is not None and _bypass_cache[0] == key and now - _bypass_cache[1] < _BYPASS_TTL_S:
        return _bypass_cache[2]
    result = bool(check(host))
    _bypass_cache = (key, now, result)
    return result


def system_proxy(host: str | None = None) -> str | None:
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
        if proxies and _bypasses_proxy(proxies, host or synthesis_host()):
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

    for attempt in range(2):
        communicate = build_communicate(
            text=text,
            voice=voice,
            rate=rate,
            volume=volume,
            connect_timeout=connect_timeout,
            receive_timeout=receive_timeout,
        )
        try:
            await communicate.save(str(output_path))
            break
        except Exception as exc:
            # A refused host: switch endpoints and try once more, as long
            # jobs do — a preview would otherwise keep failing on it.
            refusal = handshake_refusal(exc)
            if attempt or refusal is None:
                raise
            synthesis_refused(refused_host(refusal))
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

    voices = await edge_tts.list_voices(proxy=system_proxy(SPEECH_HOST))
    _VOICE_CACHE = list(voices)
    _VOICE_CACHE_AT = now
    return list(voices)
