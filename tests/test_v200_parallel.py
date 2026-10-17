"""Sections stream in parallel (v2.0) but are saved strictly in order.

The fake service below answers later sections *faster* than earlier ones,
so any ordering mistake shows up as scrambled output.  The "audio" it
returns is the section's own text, which lets the assembled file be
compared with the source text character for character.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from edge_tts import exceptions as edge_exceptions

from app.workers import tts_worker

VOICE = "en-US-AvaNeural"


def _small_plan(*_args, **_kwargs):
    return tts_worker._ChunkPlan(
        max_chars=160,
        max_payload_bytes=540,
        ramp_chars=160,
        ramp_payload_bytes=540,
        warmup_chars=160,
        warmup_payload_bytes=540,
        preflight_threshold=1_000_000,
        first_audio_timeout_s=5,
    )


def _text() -> str:
    return "\n\n".join(
        " ".join(
            f"Paragraph {p:02d} sentence {s} alpha beta gamma delta epsilon."
            for s in range(6)
        )
        for p in range(40)
    )


class _Service:
    """Counts concurrent streams; slower for earlier sections."""

    def __init__(self, fail_when=None) -> None:
        self.active = 0
        self.peak = 0
        self.calls = 0
        self.fail_when = fail_when or (lambda text, call: False)

    def communicate(self, **kwargs):
        service = self
        text = kwargs["text"]

        class _Communicate:
            async def stream(self):
                service.calls += 1
                call = service.calls
                service.active += 1
                service.peak = max(service.peak, service.active)
                try:
                    # Earlier calls sleep longer, so they finish last.
                    await asyncio.sleep(0.03 * (1 + (7 - call % 7)) / 7)
                    if service.fail_when(text, call):
                        raise edge_exceptions.NoAudioReceived("No audio was received.")
                    yield {"type": "SentenceBoundary", "text": text,
                           "offset": 0, "duration": 0}
                    yield {"type": "audio", "data": text.encode("utf-8")}
                finally:
                    service.active -= 1

        return _Communicate()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr(tts_worker, "_chunk_plan_for", _small_plan)
    # The fake audio is text, not MP3: completeness is checked by content.
    monkeypatch.setattr(tts_worker, "_stream_incomplete_reason", lambda *a: None)
    monkeypatch.setattr(tts_worker, "_BACKOFF_BASE", 0.0)

    async def fake_list_voices(*, force_refresh=False):
        return [{"ShortName": VOICE, "Locale": "en-US"}]

    monkeypatch.setattr(tts_worker, "list_voices", fake_list_voices)

    async def reachable():
        return True

    monkeypatch.setattr(tts_worker, "_service_reachable", reachable)

    def install(service: _Service):
        monkeypatch.setattr(tts_worker, "build_communicate", service.communicate)

    return install


def _squash(text: str) -> str:
    # Sections are sent trimmed, so the spaces between them are not "spoken".
    return "".join(text.split())


def _worker(tmp_path, name="book.mp3", **kw):
    return tts_worker.TTSWorker(
        text=_text(), voice=VOICE, rate="+0%", volume="+0%",
        output_path=str(tmp_path / name), **kw,
    )


def test_sections_stream_in_parallel_and_are_saved_in_order(tmp_path, setup):
    service = _Service()
    setup(service)
    worker = _worker(tmp_path)

    asyncio.run(worker._stream_generate())

    cleaned = tts_worker.build_text_profile(_text()).cleaned_text.strip()
    assembled = (tmp_path / "book.mp3").read_bytes().decode("utf-8")
    assert _squash(assembled) == _squash(cleaned)
    assert service.peak == tts_worker._MAX_PARALLEL_CHUNKS


def test_progress_never_passes_the_text_and_settles(tmp_path, setup):
    service = _Service()
    setup(service)
    worker = _worker(tmp_path)
    seen: list[int] = []
    worker.progress.connect(seen.append)

    asyncio.run(worker._stream_generate())

    assert seen and max(seen) == 100
    assert all(3 <= pct <= 100 for pct in seen)


def test_first_section_streams_alone(tmp_path, setup, monkeypatch):
    """The warm-up section proves the voice works before more are opened."""
    service = _Service()
    setup(service)
    active_at_open: list[int] = []

    def communicate(**kwargs):
        active_at_open.append(service.active)
        return service.communicate(**kwargs)

    monkeypatch.setattr(tts_worker, "build_communicate", communicate)
    asyncio.run(_worker(tmp_path)._stream_generate())

    # The second request was only opened once the first had finished.
    assert active_at_open[1] == 0
    assert max(active_at_open) > 0


def test_a_failure_drops_back_to_one_section_at_a_time(tmp_path, setup):
    failed_once: set[str] = set()

    def fail_when(text, call):
        # Section 5's first attempt fails; its retry succeeds.
        if call == 5 and text not in failed_once:
            failed_once.add(text)
            return True
        return False

    service = _Service(fail_when)
    setup(service)
    peaks_after_failure: list[int] = []
    worker = _worker(tmp_path)
    original_limit = worker._parallel_limit

    def watch(committed):
        limit = original_limit(committed)
        if worker._last_failure_at is not None:
            peaks_after_failure.append(limit)
        return limit

    worker._parallel_limit = watch
    asyncio.run(worker._stream_generate())

    cleaned = tts_worker.build_text_profile(_text()).cleaned_text.strip()
    assert _squash((tmp_path / "book.mp3").read_bytes().decode("utf-8")) == _squash(cleaned)
    assert peaks_after_failure and set(peaks_after_failure) == {1}


def test_a_later_section_failing_keeps_every_earlier_section(tmp_path, setup):
    """A section that cannot be rendered ends the job resumably, and the
    sections before it are saved even if they finished after it failed."""
    bad = "Paragraph 07"

    service = _Service(lambda text, call: bad in text)
    setup(service)
    worker = _worker(tmp_path)

    with pytest.raises(tts_worker._ChunkError) as excinfo:
        asyncio.run(worker._stream_generate())

    staging = excinfo.value.staging_dir
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    cleaned = tts_worker.build_text_profile(_text()).cleaned_text.strip()
    saved_end = max(c["end_char"] for c in manifest["chunks"])
    # Everything up to the bad paragraph is kept, and nothing after it.
    assert bad not in cleaned[:saved_end]
    assert cleaned.index(bad) - saved_end < 200
    starts = sorted(c["start_char"] for c in manifest["chunks"])
    assert starts[0] == 0


def test_cancel_with_sections_in_flight_leaves_no_stray_tasks(tmp_path, setup, caplog):
    service = _Service()
    setup(service)
    worker = _worker(tmp_path)

    async def run():
        task = asyncio.ensure_future(worker._stream_generate())
        # Cancel once a few sections are saved and others are streaming.
        while worker._chunk_store is None or worker._chunk_store.completed_count < 3:
            await asyncio.sleep(0.005)
        assert service.active > 0
        worker._cancelled = True
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        asyncio.run(run())

    assert service.active == 0
    assert not [r for r in caplog.records if "never retrieved" in r.getMessage()
                or "destroyed but it is pending" in r.getMessage()]
    manifest = json.loads(
        (worker._chunk_store.staging_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"
    assert len(manifest["chunks"]) >= 3
