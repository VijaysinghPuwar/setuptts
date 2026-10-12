"""
Synthesis endpoint selection.

On 12 October 2026 speech.platform.bing.com began refusing every synthesis
request with HTTP 403; api.msedgeservices.com (what Edge itself uses) accepts
the same token.  These tests pin the switch-over logic.
"""

from __future__ import annotations

import locale
from types import SimpleNamespace

import aiohttp
import edge_tts.communicate
import edge_tts.drm
import pytest

from app.services import tts_service
from app.workers import tts_worker


@pytest.fixture(autouse=True)
def _restore_endpoint():
    yield
    tts_service._use_endpoint(0)


def test_synthesis_uses_the_edge_endpoint_by_default():
    assert tts_service.synthesis_host() == "api.msedgeservices.com"
    assert edge_tts.communicate.WSS_URL.startswith("wss://api.msedgeservices.com/")
    assert "Ocp-Apim-Subscription-Key=" in edge_tts.communicate.WSS_URL


def test_a_refusal_switches_to_the_other_endpoint_and_back():
    assert tts_service.synthesis_refused("api.msedgeservices.com")
    assert tts_service.synthesis_host() == "speech.platform.bing.com"
    assert edge_tts.communicate.WSS_URL.startswith("wss://speech.platform.bing.com/")
    assert tts_service.synthesis_refused("speech.platform.bing.com")
    assert tts_service.synthesis_host() == "api.msedgeservices.com"


def test_a_late_refusal_for_the_old_endpoint_does_not_flip_back():
    """Two jobs fail on the same host at once: only the first switches."""
    assert tts_service.synthesis_refused("api.msedgeservices.com")
    assert not tts_service.synthesis_refused("api.msedgeservices.com")
    assert tts_service.synthesis_host() == "speech.platform.bing.com"


def _handshake_error(status: int, host: str) -> aiohttp.WSServerHandshakeError:
    info = SimpleNamespace(url=SimpleNamespace(host=host), real_url=None, method="GET", headers={})
    return aiohttp.WSServerHandshakeError(info, (), status=status, message="Invalid response status")


def test_worker_switches_endpoint_on_a_403():
    exc = _handshake_error(403, "api.msedgeservices.com")
    failure = tts_worker.TTSWorker._classify_attempt_failure(
        exc, tts_worker._AttemptStats(started_at=0.0), "chunk 1")
    assert failure.kind == "network"
    assert tts_service.synthesis_host() == "speech.platform.bing.com"


def test_other_handshake_errors_keep_the_endpoint():
    exc = _handshake_error(503, "api.msedgeservices.com")
    tts_worker.TTSWorker._classify_attempt_failure(
        exc, tts_worker._AttemptStats(started_at=0.0), "chunk 1")
    assert tts_service.synthesis_host() == "api.msedgeservices.com"


@pytest.mark.parametrize("loc", ["de_DE.UTF-8", "pt_BR.UTF-8", "fr_FR.UTF-8"])
def test_server_date_parses_in_any_locale(loc):
    old = locale.setlocale(locale.LC_TIME)
    try:
        locale.setlocale(locale.LC_TIME, loc)
    except locale.Error:
        pytest.skip(f"{loc} not installed")
    try:
        assert edge_tts.drm.DRM.parse_rfc2616_date("Mon, 12 Oct 2026 03:16:09 GMT") == 1791774969.0
        assert edge_tts.drm.DRM.parse_rfc2616_date("Mon, 12 Oct 2026 03:16:09 +0000") == 1791774969.0
        assert edge_tts.drm.DRM.parse_rfc2616_date("not a date") is None
    finally:
        locale.setlocale(locale.LC_TIME, old)
