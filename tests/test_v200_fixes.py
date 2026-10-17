"""Regression tests for the v2.0.0 fixes outside the parallel scheduler."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiohttp
import pytest
from edge_tts import exceptions as edge_exceptions

from app.services import tts_service
from app.workers import chunk_store, tts_worker

VOICE = "en-US-AvaNeural"


@pytest.fixture(autouse=True)
def _restore_endpoint():
    yield
    tts_service._use_endpoint(0)


def _handshake_error(status: int, host: str) -> aiohttp.WSServerHandshakeError:
    info = SimpleNamespace(url=SimpleNamespace(host=host), real_url=None, method="GET", headers={})
    return aiohttp.WSServerHandshakeError(info, (), status=status, message="Invalid response status")


# ── Endpoint refusals ───────────────────────────────────────────────────── #

def test_a_403_wrapped_in_a_clock_skew_error_still_switches_endpoint():
    """edge-tts raises SkewAdjustmentError (from the 403) when the reply has
    no usable Date header; that refusal used to be filed as "unexpected"."""
    refusal = _handshake_error(403, "api.msedgeservices.com")
    try:
        try:
            raise refusal
        except aiohttp.WSServerHandshakeError as inner:
            raise edge_exceptions.SkewAdjustmentError("Failed to get server date.") from inner
    except edge_exceptions.SkewAdjustmentError as wrapped:
        failure = tts_worker.TTSWorker._classify_attempt_failure(
            wrapped, tts_worker._AttemptStats(started_at=0.0), "chunk 1")
    assert tts_service.synthesis_host() == "speech.platform.bing.com"
    assert failure.kind == "network"


def test_other_errors_are_not_refusals():
    assert tts_service.handshake_refusal(_handshake_error(500, "x")) is None
    assert tts_service.handshake_refusal(ValueError("403")) is None


def test_dns_failure_names_the_host_in_use():
    tts_service._use_endpoint(1)
    exc = OSError("getaddrinfo failed")
    failure = tts_worker.TTSWorker._classify_attempt_failure(
        exc, tts_worker._AttemptStats(started_at=0.0), "chunk 1")
    assert failure.kind == "dns"
    assert "speech.platform.bing.com" in str(failure)


def test_a_preview_switches_endpoint_and_retries_once(tmp_path, monkeypatch):
    hosts = []

    class _Communicate:
        async def save(self, path):
            host = tts_service.synthesis_host()
            hosts.append(host)
            if host == "api.msedgeservices.com":
                raise _handshake_error(403, host)
            with open(path, "wb") as fh:
                fh.write(b"audio")

    monkeypatch.setattr(tts_service, "build_communicate", lambda **kw: _Communicate())
    out = tmp_path / "p.mp3"
    asyncio.run(tts_service.generate_audio("hi", VOICE, "+0%", "+0%", out))

    assert hosts == ["api.msedgeservices.com", "speech.platform.bing.com"]
    assert out.read_bytes() == b"audio"


def test_a_preview_gives_up_after_one_switch(tmp_path, monkeypatch):
    class _Communicate:
        async def save(self, path):
            raise _handshake_error(403, tts_service.synthesis_host())

    monkeypatch.setattr(tts_service, "build_communicate", lambda **kw: _Communicate())
    with pytest.raises(aiohttp.WSServerHandshakeError):
        asyncio.run(tts_service.generate_audio("hi", VOICE, "+0%", "+0%", tmp_path / "p.mp3"))


# ── Sentence boundaries ─────────────────────────────────────────────────── #

@pytest.mark.parametrize("text, expected", [
    ("Dr. Smith met Mr. Jones. They talked.", ["Dr. Smith met Mr. Jones.", "They talked."]),
    ("See fig. 3 for details. Next.", ["See fig. 3 for details.", "Next."]),
    ("J. R. R. Tolkien wrote it. Yes.", ["J. R. R. Tolkien wrote it.", "Yes."]),
    ("It ended at 5 p.m. and we left.", ["It ended at 5 p.m. and we left."]),
    ("Use e.g. this. Or that.", ["Use e.g. this.", "Or that."]),
    # Ambiguous abbreviations still end a sentence before a capital.
    ("Bring pears, etc. Then go.", ["Bring pears, etc.", "Then go."]),
    ("I said no. Then left.", ["I said no.", "Then left."]),
    ("वह घर गया। फिर सो गया।", ["वह घर गया।", "फिर सो गया।"]),
    ("هل أنت هنا؟ نعم.", ["هل أنت هنا؟", "نعم."]),
])
def test_sentence_splitting(text, expected):
    assert tts_worker._split_sentences(text) == expected


def test_a_section_is_not_cut_between_a_title_and_a_name():
    # The only sentence-like boundary that fits is right after "Dr.".
    text = "Then came Dr. Watson and the rest of them, walking slowly. After."
    limit = text.index("Watson") + 3
    starts = [s for s, _ in tts_worker._boundary_candidates(
        text, tts_worker._SENTENCE_BOUNDARY, limit=limit)]
    assert text.index("Dr.") + 3 not in starts


# ── Final assembly ──────────────────────────────────────────────────────── #

def _small_plan(*_args, **_kwargs):
    return tts_worker._ChunkPlan(
        max_chars=160, max_payload_bytes=540, ramp_chars=160, ramp_payload_bytes=540,
        warmup_chars=160, warmup_payload_bytes=540, preflight_threshold=1_000_000,
        first_audio_timeout_s=5,
    )


class _Communicate:
    def __init__(self, text):
        self._text = text

    async def stream(self):
        yield {"type": "SentenceBoundary", "text": self._text, "offset": 0, "duration": 0}
        yield {"type": "audio", "data": self._text.encode("utf-8")}


@pytest.fixture
def fake_service(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr(tts_worker, "_chunk_plan_for", _small_plan)
    monkeypatch.setattr(tts_worker, "_stream_incomplete_reason", lambda *a: None)

    async def fake_list_voices(*, force_refresh=False):
        return [{"ShortName": VOICE, "Locale": "en-US"}]

    monkeypatch.setattr(tts_worker, "list_voices", fake_list_voices)
    monkeypatch.setattr(tts_worker, "build_communicate", lambda **kw: _Communicate(kw["text"]))


def _worker(output):
    text = ("alpha beta gamma delta epsilon zeta eta theta iota kappa. " * 80).strip()
    return tts_worker.TTSWorker(text=text, voice=VOICE, rate="+0%", volume="+0%",
                                output_path=str(output))


def test_an_old_incomplete_copy_does_not_make_the_job_delete_its_audio(
        tmp_path, monkeypatch, fake_service):
    """If this run's suspect audio could not be kept, the staged chunks are
    the only copy — an older "(incomplete)" file must not count as kept."""
    monkeypatch.setattr(tts_worker, "mp3_duration_seconds", lambda _p: 1.0)
    output = tmp_path / "book.mp3"
    stale = tmp_path / "book (incomplete).mp3"
    stale.write_bytes(b"from last week")

    real_replace = chunk_store._replace_with_retry

    def replace(src, dst):
        if str(dst).endswith("(incomplete).mp3"):
            raise PermissionError("open in a player")
        return real_replace(src, dst)

    monkeypatch.setattr(chunk_store, "_replace_with_retry", replace)
    worker = _worker(output)
    resumable = []
    worker.job_resumable.connect(lambda *a: resumable.append(a))

    with pytest.raises(tts_worker._ChunkError) as excinfo:
        asyncio.run(worker._stream_generate())

    assert excinfo.value.staging_dir is not None
    assert excinfo.value.staging_dir.exists()
    assert resumable
    assert stale.read_bytes() == b"from last week"
    # The message must not point the user at last week's file.
    assert excinfo.value.cause.suggestion is None


def test_a_finished_book_is_not_failed_by_a_late_manifest_write(
        tmp_path, monkeypatch, fake_service):
    monkeypatch.setattr(tts_worker, "mp3_duration_seconds", lambda _p: None)

    def full_disk(self):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(chunk_store.ChunkStore, "mark_completed", full_disk)
    output = tmp_path / "book.mp3"
    worker = _worker(output)

    asyncio.run(worker._stream_generate())

    assert output.exists()
    assert worker._output_written


def test_resume_needs_no_room_for_audio_already_staged(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_worker, "_BYTES_PER_CHAR_ESTIMATE", 1_000)
    worker = tts_worker.TTSWorker(text="x", voice=VOICE, rate="+0%", volume="+0%",
                                  output_path=str(tmp_path / "o.mp3"))
    # 1 000 chars → 1 MB of audio; 0.9 MB of it is already staged.
    free = SimpleNamespace(free=int(1.3e6))
    monkeypatch.setattr(tts_worker.shutil, "disk_usage", lambda _p: free)

    with pytest.raises(tts_worker._PreflightError):
        worker._check_free_space(tmp_path, 1_000, tmp_path / "o.mp3")
    worker._check_free_space(tmp_path, 1_000, tmp_path / "o.mp3", already_staged=900_000)


def test_staged_bytes_counts_chunk_files(tmp_path):
    (tmp_path / "chunk_000000.mp3").write_bytes(b"x" * 10)
    (tmp_path / "chunk_000001.mp3").write_bytes(b"x" * 5)
    (tmp_path / "manifest.json").write_text("{}")
    assert tts_worker._staged_bytes(tmp_path) == 15
    assert tts_worker._staged_bytes(None) == 0
    assert tts_worker._staged_bytes(tmp_path / "missing") == 0


def test_stale_sweep_skips_a_folder_in_use(tmp_path):
    import os
    import time

    busy = tmp_path / "busy"
    busy.mkdir()
    old = time.time() - 400 * 86_400
    os.utime(busy, (old, old))
    chunk_store._set_live(busy, True)
    try:
        chunk_store.cleanup_stale_staging(tmp_path, max_age_days=1)
        assert busy.exists()
    finally:
        chunk_store._set_live(busy, False)
    chunk_store.cleanup_stale_staging(tmp_path, max_age_days=1)
    assert not busy.exists()


# ── Second review round ─────────────────────────────────────────────────── #

@pytest.mark.parametrize("text, expected", [
    ("She studied art. Then she left.", ["She studied art.", "Then she left."]),
    ("Wait a sec. He went.", ["Wait a sec.", "He went."]),
    ("World War I. The end.", ["World War I.", "The end."]),
    ("Take vitamin C. Then rest.", ["Take vitamin C.", "Then rest."]),
    ("John F. Kennedy spoke. Ok.", ["John F. Kennedy spoke.", "Ok."]),
    ("Dec. 5 came. Then.", ["Dec. 5 came.", "Then."]),
    # Closing quotes stay with their danda; a double danda is not split.
    ("वह बोला।” फिर गया।", ["वह बोला।”", "फिर गया।"]),
    ("ab।। cd", ["ab।।", "cd"]),
])
def test_sentence_splitting_second_round(text, expected):
    assert tts_worker._split_sentences(text) == expected


def test_bomless_utf16_thai_still_opens():
    from app.ui.panels.input_panel import decode_text_file

    thai = "สวัสดีครับ วันนี้อากาศดีมาก เราไปเที่ยวทะเลกันเถอะ\n" * 40
    assert decode_text_file(thai.encode("utf-16-le")) == thai
    assert decode_text_file(thai.encode("utf-16-be")) == thai


def test_stray_nuls_in_utf8_are_not_read_as_utf16():
    from app.ui.panels.input_panel import decode_text_file

    english = ("Hello world, this is a normal English book. " * 200).encode()
    damaged = english[:101] + b"\x00" + english[101:301] + b"\x00" + english[301:]
    assert decode_text_file(damaged).startswith("Hello world")


def test_every_thread_gets_its_minimum_wait():
    from app.workers import thread_shutdown

    class _Thread:
        def __init__(self):
            self.waits = []

        def isRunning(self):
            return True

        def wait(self, ms):
            self.waits.append(ms)
            return True

        def requestInterruption(self):
            pass

        def quit(self):
            pass

    threads = [("a", _Thread()), ("b", _Thread()), ("c", _Thread())]
    thread_shutdown.stop_threads(threads, budget_s=0.0, min_wait_s=1.5)
    assert [t.waits for _, t in threads] == [[1500], [1500], [1500]]


def test_resuming_a_folder_already_in_use_fails_instead_of_starting_over(
        tmp_path, monkeypatch, fake_service):
    worker = _worker(tmp_path / "book.mp3")
    folder = tmp_path / "staging"
    folder.mkdir()
    worker._resume_staging_dir = folder

    def in_use(*_a, **_kw):
        raise chunk_store.StagingInUseError("busy")

    monkeypatch.setattr(chunk_store.ChunkStore, "try_resume", in_use)
    with pytest.raises(tts_worker._PreflightError) as excinfo:
        asyncio.run(worker._stream_generate())
    assert excinfo.value.cause.kind == "staging_in_use"
    assert "already being generated" in tts_worker.TTSWorker._user_message(excinfo.value)
