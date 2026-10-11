"""
Long-session stability: network outages, sleep/wake, and chaos at book scale.

A multi-hour job sees things a short one never does — Wi-Fi dropping for
minutes, the computer waking from sleep with the connection gone, the service
returning 503s or going quiet mid-stream.  These tests drive the production
chunk plan through the echo stub from ``test_long_form_scale`` (each chunk's
"audio" is its own text, so the output is a transcript) and inject those
failures.  The invariant is always the same: the export reproduces the source
exactly once, in order, or the job fails *resumably* — never silently short.
"""

from __future__ import annotations

import asyncio
import random
import subprocess

import aiohttp
import pytest
from edge_tts import exceptions as edge_exceptions

from app.utils import power
from app.workers import tts_worker
from tests.test_long_form_scale import _build_book


def _cleaned(text: str) -> str:
    return "".join(tts_worker.build_text_profile(text).cleaned_text.split())


class _ChaosCommunicate:
    def __init__(self, text: str, network) -> None:
        self._text = text
        self._network = network

    async def stream(self):
        failure = self._network.on_request(self._text)
        if failure == "network":
            raise aiohttp.ClientConnectionError("Connection reset by peer")
        if failure == "dns":
            raise aiohttp.ClientConnectorDNSError(
                aiohttp.client_reqrep.ConnectionKey(
                    "speech.platform.bing.com", 443, True, True, None, None, None
                ),
                OSError(8, "nodename nor servname provided, or not known"),
            )
        if failure == "no_audio":
            raise edge_exceptions.NoAudioReceived("No audio was received.")
        if failure == "timeout_before_audio":
            raise asyncio.TimeoutError()
        midpoint = max(1, len(self._text) // 2)
        yield {"type": "WordBoundary", "text": self._text[:midpoint]}
        if failure == "stall_mid_stream":
            yield {"type": "audio", "data": b"PARTIAL"}
            raise asyncio.TimeoutError()
        yield {"type": "WordBoundary", "text": self._text[midpoint:]}
        yield {"type": "audio", "data": self._text.encode("utf-8")}


class _FakeNetwork:
    """
    Simulated connection.  ``down`` makes every request fail with a network
    error and every reachability probe fail; ``outages`` schedules drops at
    given request numbers lasting a given number of probes.
    """

    def __init__(self, *, seed: int = 0, chaos: dict[str, float] | None = None,
                 outages: dict[int, int] | None = None) -> None:
        self.rng = random.Random(seed)
        self.chaos = chaos or {}
        self.outages = dict(outages or {})
        self.requests = 0
        self.probes = 0
        self.down_for_probes = 0
        self.down_kind = "network"

    def on_request(self, _text: str) -> str:
        self.requests += 1
        if self.requests in self.outages:
            self.down_for_probes = self.outages.pop(self.requests)
            self.down_kind = self.rng.choice(["network", "dns", "stall_mid_stream"])
        if self.down_for_probes > 0:
            return self.down_kind
        roll = self.rng.random()
        acc = 0.0
        for kind, p in self.chaos.items():
            acc += p
            if roll < acc:
                return kind
        return "ok"

    async def reachable(self) -> bool:
        self.probes += 1
        if self.down_for_probes > 0:
            self.down_for_probes -= 1
            return False
        return True


@pytest.fixture
def fake_net(monkeypatch, tmp_path):
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))
    # Real-time waits would make an outage test take minutes.
    monkeypatch.setattr(tts_worker, "_BACKOFF_BASE", 0.001)
    monkeypatch.setattr(tts_worker, "_OUTAGE_POLL_S", (0.001,))

    async def fake_list_voices(*, force_refresh=False):
        return [
            {"ShortName": "en-US-AvaNeural", "Locale": "en-US"},
            {"ShortName": "en-US-AvaMultilingualNeural", "Locale": "en-US"},
        ]

    monkeypatch.setattr(tts_worker, "list_voices", fake_list_voices)
    holder: dict[str, _FakeNetwork] = {}

    def install(network: _FakeNetwork) -> _FakeNetwork:
        holder["net"] = network
        monkeypatch.setattr(tts_worker, "_service_reachable", network.reachable)
        monkeypatch.setattr(
            tts_worker, "build_communicate",
            lambda **kw: _ChaosCommunicate(kw["text"], holder["net"]),
        )
        return network

    return install


def _run(text: str, output, voice: str = "en-US-AvaNeural") -> tts_worker.TTSWorker:
    worker = tts_worker.TTSWorker(
        text=text, voice=voice, rate="+0%", volume="+0%", output_path=str(output),
    )
    asyncio.run(worker._stream_generate())
    return worker


# ------------------------------------------------------------------ #
# Outages                                                             #
# ------------------------------------------------------------------ #

def test_long_outage_is_waited_out_without_spending_retries(fake_net, tmp_path):
    """Wi-Fi gone for 40 probes (minutes, in real time) mid-book: the job
    waits, then finishes exactly — no chunk splitting, no failure."""
    net = fake_net(_FakeNetwork(outages={5: 40}))
    text = _build_book(1)
    out = tmp_path / "outage.mp3"

    worker = _run(text, out)

    assert "".join(out.read_bytes().decode().split()) == _cleaned(text)
    assert worker._stats.recovery_splits == 0
    assert net.probes > 40


def test_repeated_outages_across_a_long_job(fake_net, tmp_path):
    """Sleep/wake cycles all through a 6-hour book."""
    net = fake_net(_FakeNetwork(seed=3, outages={4: 3, 9: 15, 15: 1, 22: 30, 30: 6}))
    text = _build_book(6)
    out = tmp_path / "flappy.mp3"

    worker = _run(text, out)

    assert "".join(out.read_bytes().decode().split()) == _cleaned(text)
    assert worker._stats.recovery_splits == 0
    assert not net.outages, "not every scheduled outage was reached"


def test_outage_longer_than_the_limit_fails_resumably(fake_net, tmp_path, monkeypatch):
    monkeypatch.setattr(tts_worker, "_OUTAGE_MAX_WAIT_S", 0.05)
    monkeypatch.setattr(tts_worker, "_OUTAGE_POLL_S", (0.01,))
    fake_net(_FakeNetwork(outages={4: 10**9}))
    worker = tts_worker.TTSWorker(
        text=_build_book(1), voice="en-US-AvaNeural", rate="+0%", volume="+0%",
        output_path=str(tmp_path / "dead.mp3"),
    )
    resumable: list[tuple] = []
    worker.job_resumable.connect(lambda *a: resumable.append(a))

    with pytest.raises(tts_worker._ChunkError) as info:
        asyncio.run(worker._stream_generate())

    assert info.value.preserved_chunks > 0
    assert resumable, "a long outage must leave the job resumable"
    assert not (tmp_path / "dead.mp3").exists()


def test_flapping_connection_cannot_retry_forever(fake_net, tmp_path):
    """Each attempt fails and each probe fails once: the per-chunk outage
    budget must run out and the job end, not loop indefinitely."""

    class _Flapping(_FakeNetwork):
        def on_request(self, _text):
            self.requests += 1
            if self.requests > 3:
                self.down_for_probes = 1
                return "network"
            return "ok"

    fake_net(_Flapping())
    with pytest.raises(tts_worker._ChunkError):
        _run(_build_book(1), tmp_path / "flap.mp3")


def test_network_errors_with_service_reachable_use_normal_retries(fake_net, tmp_path):
    """A 503-style failure while the host is reachable is not an outage:
    the probe says so and the normal retry budget applies."""
    net = fake_net(_FakeNetwork(seed=11, chaos={"network": 0.3}))
    text = _build_book(1)
    out = tmp_path / "503.mp3"

    _run(text, out)

    assert "".join(out.read_bytes().decode().split()) == _cleaned(text)
    assert net.probes > 0


def test_cancel_during_outage_wait_stops_promptly(fake_net, tmp_path, monkeypatch):
    monkeypatch.setattr(tts_worker, "_OUTAGE_POLL_S", (0.01,))
    net = fake_net(_FakeNetwork(outages={3: 10**9}))
    worker = tts_worker.TTSWorker(
        text=_build_book(1), voice="en-US-AvaNeural", rate="+0%", volume="+0%",
        output_path=str(tmp_path / "cancel.mp3"),
    )
    original = net.reachable

    async def reachable_then_cancel():
        if net.probes > 5:
            worker._cancelled = True
        return await original()

    monkeypatch.setattr(tts_worker, "_service_reachable", reachable_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._stream_generate())
    assert net.probes < 50


# ------------------------------------------------------------------ #
# Chaos at book scale                                                 #
# ------------------------------------------------------------------ #

_CHAOS = {
    "network": 0.04,
    "dns": 0.01,
    "no_audio": 0.04,
    "timeout_before_audio": 0.03,
    "stall_mid_stream": 0.04,
}


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("voice", ["en-US-AvaNeural", "en-US-AvaMultilingualNeural"])
def test_chaos_book_is_reproduced_exactly(fake_net, tmp_path, seed, voice):
    """~16% of requests fail in every way the service has been seen to fail,
    plus outages: the 6-hour export must still be an exact transcript."""
    rng = random.Random(seed)
    outages = {rng.randint(3, 60): rng.randint(1, 25) for _ in range(4)}
    fake_net(_FakeNetwork(seed=seed, chaos=_CHAOS, outages=outages))
    text = _build_book(6, seed=seed)
    out = tmp_path / f"chaos_{seed}.mp3"

    _run(text, out, voice=voice)

    assembled = out.read_bytes().decode("utf-8")
    assert "PARTIAL" not in assembled, "audio from a stalled attempt leaked into the export"
    assert "".join(assembled.split()) == _cleaned(text)


# ------------------------------------------------------------------ #
# Chunk-count estimate                                                #
# ------------------------------------------------------------------ #

def test_warmup_chunk_does_not_inflate_the_chunk_estimate():
    # A 74-char warm-up probe on a 376k-char job used to read "1/4955".
    raw = tts_worker._estimate_total_chunks(0, 0, 76, 376_566)
    fixed = tts_worker._estimate_total_chunks(0, 0, 76, 376_566, typical_len=2_600)
    assert raw > 4_000
    assert 100 < fixed < 200


# ------------------------------------------------------------------ #
# Keeping the computer awake                                          #
# ------------------------------------------------------------------ #

class _FakeProc:
    instances: list["_FakeProc"] = []

    def __init__(self, cmd, **_kw):
        self.cmd = cmd
        self.terminated = False
        _FakeProc.instances.append(self)

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def fake_popen(monkeypatch):
    _FakeProc.instances = []
    monkeypatch.setattr(power.sys, "platform", "darwin")
    monkeypatch.setattr(power.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(power.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(power, "_process", None)
    monkeypatch.setattr(power, "_holders", 0)
    return _FakeProc


def test_keep_awake_shares_one_inhibitor_across_concurrent_jobs(fake_popen):
    with power.keep_awake():
        with power.keep_awake():
            assert len(fake_popen.instances) == 1
            assert "-i" in fake_popen.instances[0].cmd
            assert "-w" in fake_popen.instances[0].cmd
        assert not fake_popen.instances[0].terminated, "released while a job still runs"
    assert fake_popen.instances[0].terminated


def test_keep_awake_releases_on_error(fake_popen):
    with pytest.raises(RuntimeError):
        with power.keep_awake():
            raise RuntimeError("job failed")
    assert fake_popen.instances[0].terminated
    assert power._holders == 0


def test_keep_awake_never_breaks_a_job_when_inhibitor_is_missing(monkeypatch):
    monkeypatch.setattr(power.sys, "platform", "darwin")
    monkeypatch.setattr(power, "_process", None)
    monkeypatch.setattr(power, "_holders", 0)

    def boom(*_a, **_k):
        raise OSError("no caffeinate")

    monkeypatch.setattr(power.subprocess, "Popen", boom)
    with power.keep_awake():
        pass
    assert power._holders == 0


def test_keep_awake_restarts_inhibitor_that_died(fake_popen):
    with power.keep_awake():
        fake_popen.instances[0].terminated = True   # killed externally
        with power.keep_awake():
            assert len(fake_popen.instances) == 2


@pytest.mark.skipif(power.sys.platform != "darwin", reason="macOS power assertion")
def test_keep_awake_really_holds_a_macos_assertion(monkeypatch):
    monkeypatch.setattr(power, "_process", None)
    monkeypatch.setattr(power, "_holders", 0)
    with power.keep_awake():
        proc = power._process
        assert proc is not None and proc.poll() is None
        out = subprocess.run(
            ["pmset", "-g", "assertions"], capture_output=True, text=True,
        ).stdout
        assert f"pid {proc.pid}(caffeinate)" in out
    assert proc.poll() is not None
