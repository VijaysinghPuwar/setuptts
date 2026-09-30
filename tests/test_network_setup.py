"""
How SetupTTS reaches the speech service: which certificates it trusts and
which proxy it uses.  Both are Windows problems first — antivirus HTTPS
scanning and proxies set in Windows Settings — where edge_tts on its own
fails to load a single voice.
"""

import asyncio
import ssl
import urllib.request

import certifi
import edge_tts.communicate
import edge_tts.voices
import pytest

from app.services import tts_service
from app.utils.errors import friendly_error_text


def test_edge_tts_uses_the_app_tls_context_for_voices_and_synthesis():
    # These private names are what the fix replaces; an edge-tts bump that
    # renames them would silently bring back the certifi-only behaviour.
    assert isinstance(edge_tts.voices._SSL_CTX, ssl.SSLContext)
    assert edge_tts.voices._SSL_CTX is edge_tts.communicate._SSL_CTX


def test_tls_context_keeps_certifi_and_verifies():
    ctx = tts_service.build_ssl_context()
    bundled = ssl.create_default_context(cafile=certifi.where())
    assert len(ctx.get_ca_certs()) >= len(bundled.get_ca_certs())
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname


def test_tls_context_adds_the_system_store(monkeypatch):
    called = []
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs",
                        lambda self, *a, **k: called.append(True))
    tts_service.build_ssl_context()
    assert called


def test_unreadable_system_store_still_leaves_certifi(monkeypatch):
    def broken(self, *a, **k):
        raise ssl.SSLError("store unavailable")
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", broken)
    ctx = tts_service.build_ssl_context()
    assert ctx.get_ca_certs()


@pytest.fixture
def no_env_proxy(monkeypatch):
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
                "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)


def test_system_proxy_is_used(monkeypatch, no_env_proxy):
    monkeypatch.setattr(urllib.request, "getproxies",
                        lambda: {"https": "http://10.0.0.1:8080", "http": "http://10.0.0.1:8080"})
    assert tts_service.system_proxy() == "http://10.0.0.1:8080"


def test_no_proxy_configured(monkeypatch, no_env_proxy):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    assert tts_service.system_proxy() is None


def test_socks_proxy_is_not_passed_to_aiohttp(monkeypatch, no_env_proxy):
    monkeypatch.setattr(urllib.request, "getproxies",
                        lambda: {"https": "socks5://10.0.0.1:1080"})
    assert tts_service.system_proxy() is None


def test_bypassed_host_goes_direct(monkeypatch, no_env_proxy):
    monkeypatch.setattr(urllib.request, "getproxies",
                        lambda: {"https": "http://10.0.0.1:8080"})
    monkeypatch.setattr(urllib.request, "proxy_bypass",
                        lambda host: host == tts_service.SPEECH_HOST)
    assert tts_service.system_proxy() is None


def test_environment_proxy_is_left_to_aiohttp(monkeypatch, no_env_proxy):
    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy:3128")
    monkeypatch.setattr(urllib.request, "getproxies",
                        lambda: {"https": "http://10.0.0.1:8080"})
    assert tts_service.system_proxy() is None


def test_broken_proxy_setting_does_not_raise(monkeypatch, no_env_proxy):
    def broken():
        raise OSError("registry unreadable")
    monkeypatch.setattr(urllib.request, "getproxies", broken)
    assert tts_service.system_proxy() is None


def test_proxy_reaches_edge_tts(monkeypatch):
    monkeypatch.setattr(tts_service, "system_proxy", lambda: "http://10.0.0.1:8080")
    comm = tts_service.build_communicate("Hi.", "en-US-AvaNeural", "+0%", "+0%")
    assert comm.proxy == "http://10.0.0.1:8080"


def test_voice_list_request_uses_the_proxy(monkeypatch):
    seen = {}

    async def fake_list_voices(*, proxy=None):
        seen["proxy"] = proxy
        return [{"ShortName": "en-US-AvaNeural"}]

    monkeypatch.setattr(tts_service.edge_tts, "list_voices", fake_list_voices)
    monkeypatch.setattr(tts_service, "system_proxy", lambda: "http://10.0.0.1:8080")
    asyncio.run(tts_service.list_voices(force_refresh=True))
    assert seen["proxy"] == "http://10.0.0.1:8080"


def test_certificate_error_mentions_https_scanning():
    text = friendly_error_text("SSLCertVerificationError CERTIFICATE_VERIFY_FAILED")
    assert "date and time" in text
    assert "HTTPS" in text
