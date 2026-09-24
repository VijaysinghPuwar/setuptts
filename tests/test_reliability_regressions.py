"""
Regression tests for the long-form reliability defects fixed in 1.6.0.

Each test reproduces one real failure mode end to end through the worker
(with the network stubbed) and pins the fixed behaviour:

* a connection that closes mid-chunk no longer yields silently truncated audio;
* a split inside a whitespace run no longer leaves a coverage gap that made
  the job impossible to finish;
* Resume uses the staged text verbatim, so non-idempotent text cleanup can't
  make it silently restart from chunk 1;
* the stale-staging sweep never deletes the job being resumed, or recent
  resumable progress;
* a disk error while staging a chunk is resumable and explained plainly;
* a DNS blip at job start is retried;
* the resume listing never touches a job that is still running.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from edge_tts import exceptions as edge_exceptions

from app.workers import chunk_store as chunk_store_mod
from app.workers import tts_worker
from app.workers.chunk_store import ChunkStore, cleanup_stale_staging

# Real service numbers: 48 kbit/s CBR MP3, boundary offsets in 100 ns ticks.
_BYTES_PER_S = 6_000
_TICKS = 10_000_000
# Fake speech rate used to derive audio length from text length.
_CHARS_PER_S = 15.0


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


def _sentences(text: str) -> list[str]:
    out, current = [], ""
    for ch in text:
        current += ch
        if ch in ".!?":
            out.append(current)
            current = ""
    if current.strip():
        out.append(current)
    return out


class _RealisticCommunicate:
    """
    Speaks like the real service: one SentenceBoundary per sentence, with
    offset/duration in ticks, announced before that sentence's audio, and
    audio bytes at 48 kbit/s.  The audio payload is the chunk text itself
    (padded to the right byte length) so tests can check what was assembled.

    ``cut_after`` simulates the websocket closing early: the stream just ends
    after that fraction of the audio, exactly as edge_tts does.
    """

    def __init__(self, text: str, cut_after: float | None = None) -> None:
        self._text = text
        self._cut_after = cut_after

    async def stream(self):
        sentences = _sentences(self._text)
        total_s = max(len(self._text) / _CHARS_PER_S, 0.5)
        audio_len = int(total_s * _BYTES_PER_S)
        payload = (self._text.encode("utf-8") * (audio_len // max(len(self._text), 1) + 1))[:audio_len]
        cut_at = None if self._cut_after is None else int(audio_len * self._cut_after)

        sent_bytes = 0
        offset_s = 0.0
        for sentence in sentences:
            dur_s = total_s * len(sentence) / max(len(self._text), 1)
            yield {
                "type": "SentenceBoundary",
                "offset": int(offset_s * _TICKS),
                "duration": int(dur_s * _TICKS),
                "text": sentence.strip(),
            }
            end_byte = min(audio_len, int((offset_s + dur_s) * _BYTES_PER_S))
            while sent_bytes < end_byte:
                step = min(4096, end_byte - sent_bytes)
                if cut_at is not None and sent_bytes + step > cut_at:
                    if cut_at > sent_bytes:
                        yield {"type": "audio", "data": payload[sent_bytes:cut_at]}
                    return   # socket closed: the stream simply ends
                yield {"type": "audio", "data": payload[sent_bytes:sent_bytes + step]}
                sent_bytes += step
            offset_s += dur_s
        if sent_bytes < audio_len:
            yield {"type": "audio", "data": payload[sent_bytes:]}


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr(tts_worker, "_chunk_plan_for", _small_plan)
    monkeypatch.setattr(tts_worker, "_BACKOFF_BASE", 0.01)

    async def fake_list_voices(*, force_refresh=False):
        return [{"ShortName": "en-US-AvaNeural", "Locale": "en-US"}]

    monkeypatch.setattr(tts_worker, "list_voices", fake_list_voices)
    return monkeypatch


def _worker(text, output, **kwargs):
    return tts_worker.TTSWorker(
        text=text, voice="en-US-AvaNeural", rate="+0%", volume="+0%",
        output_path=str(output), **kwargs,
    )


_BOOK = " ".join(
    f"Sentence number {i} tells a small part of the story."
    for i in range(60)
)


# ------------------------------------------------------------------ #
# 1. Mid-chunk disconnect                                             #
# ------------------------------------------------------------------ #

def test_complete_stream_is_accepted():
    text = "One sentence here. Another one there! And a third?"
    assert tts_worker._stream_incomplete_reason(
        text, [(0, int(3.0 * _TICKS), text)], int(3.0 * _BYTES_PER_S)
    ) is None


def test_audio_that_stops_inside_the_last_sentence_is_incomplete():
    text = "One sentence here. Another one there."
    boundaries = [(0, int(2 * _TICKS), "One sentence here."),
                  (int(2 * _TICKS), int(2 * _TICKS), "Another one there.")]
    reason = tts_worker._stream_incomplete_reason(text, boundaries, int(2.5 * _BYTES_PER_S))
    assert reason and "speech runs to" in reason


def test_stream_that_ends_between_sentences_is_incomplete():
    text = "One sentence here. Another one there. And a final sentence."
    boundaries = [(0, int(2 * _TICKS), "One sentence here.")]
    reason = tts_worker._stream_incomplete_reason(text, boundaries, int(2.0 * _BYTES_PER_S))
    assert reason and "unspoken" in reason


def test_audio_without_any_boundary_is_incomplete():
    assert tts_worker._stream_incomplete_reason("Hello there.", [], 3000)


def test_boundary_text_that_cannot_be_matched_falls_back_to_the_audio_check():
    # The service rewrote the sentence completely; the text check can't judge
    # it, so only the (passing) audio-length check decides.
    reason = tts_worker._stream_incomplete_reason(
        "Chapter IV", [(0, int(1 * _TICKS), "Chapter four")], int(1 * _BYTES_PER_S)
    )
    assert reason is None


def test_html_entities_in_boundaries_match_the_source():
    text = 'Smith & Sons said "hi" <loudly>.'
    escaped = "Smith &amp; Sons said &quot;hi&quot; &lt;loudly&gt;."
    assert tts_worker._stream_incomplete_reason(
        text, [(0, int(2 * _TICKS), escaped)], int(2 * _BYTES_PER_S)
    ) is None


def test_mid_chunk_disconnect_is_retried_not_saved(stubbed, tmp_path):
    """The first attempt of every chunk is cut at 60 %; the retry is whole."""
    attempts: dict[str, int] = {}

    def build(**kwargs):
        text = kwargs["text"]
        attempts[text] = attempts.get(text, 0) + 1
        return _RealisticCommunicate(text, cut_after=0.6 if attempts[text] == 1 else None)

    stubbed.setattr(tts_worker, "build_communicate", build)
    output = tmp_path / "book.mp3"
    asyncio.run(_worker(_BOOK, output)._stream_generate())

    assert all(n == 2 for n in attempts.values()), "every cut attempt must be retried"
    # The assembled file is exactly the full audio of every chunk, in order.
    expected = b"".join(
        _full_audio(text) for text in attempts  # dict preserves request order
    )
    assert output.read_bytes() == expected


def test_persistent_disconnects_fail_the_job_instead_of_completing(stubbed, tmp_path):
    stubbed.setattr(
        tts_worker, "build_communicate",
        lambda **kw: _RealisticCommunicate(kw["text"], cut_after=0.5),
    )
    output = tmp_path / "book.mp3"
    with pytest.raises(tts_worker._ChunkError):
        asyncio.run(_worker(_BOOK, output)._stream_generate())
    assert not output.exists()


def _full_audio(text: str) -> bytes:
    total_s = max(len(text) / _CHARS_PER_S, 0.5)
    audio_len = int(total_s * _BYTES_PER_S)
    return (text.encode("utf-8") * (audio_len // max(len(text), 1) + 1))[:audio_len]


# ------------------------------------------------------------------ #
# 2. Whitespace gap between chunk ranges                              #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("seed", range(40))
def test_cursor_ranges_tile_the_text_with_no_gaps(seed):
    import random

    rng = random.Random(seed)
    words = []
    for _ in range(400):
        word = "".join(rng.choice("abcdefghij") for _ in range(rng.randint(1, 9)))
        sep = rng.choice([" ", " ", " ", "\n\n", "\n\n\n", ".  ", "   ", "\t"])
        words.append(word + sep)
    text = "".join(words).strip()

    cursor = tts_worker._ChunkCursor(text)
    ranges = []
    while cursor.has_more():
        _chunk, _payload, start, end = cursor.take_next(rng.randint(20, 90), rng.randint(60, 300))
        ranges.append((start, end))
    assert ranges[0][0] == 0
    assert ranges[-1][1] == len(text)
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:])), ranges


def test_paragraph_break_on_the_warmup_boundary_still_completes(stubbed, tmp_path):
    """The real-world shape that previously failed on every resume."""
    stubbed.setattr(tts_worker, "_chunk_plan_for", _plan_1200)
    stubbed.setattr(tts_worker, "build_communicate",
                    lambda **kw: _RealisticCommunicate(kw["text"]))
    sentence = "The old man sat by the window and watched the rain fall. "
    first = (sentence * 25)[:1197].rstrip()
    first += "x" * (1198 - len(first))
    body = "\n\n".join((sentence * 10).strip() for _ in range(8))
    text = first + "\n\n\nChapter Two\n\n" + body

    output = tmp_path / "gap.mp3"
    asyncio.run(_worker(text, output)._stream_generate())
    assert output.exists()


def _plan_1200(*_args, **_kwargs):
    return tts_worker._ChunkPlan(
        max_chars=9200, max_payload_bytes=3600,
        ramp_chars=3000, ramp_payload_bytes=3000,
        warmup_chars=1200, warmup_payload_bytes=1350,
        preflight_threshold=1_000_000, first_audio_timeout_s=5,
    )


# ------------------------------------------------------------------ #
# 3. Resume with text whose cleanup is not idempotent                 #
# ------------------------------------------------------------------ #

def test_resume_reuses_saved_chunks_even_when_cleanup_is_not_idempotent(stubbed, tmp_path):
    from app.services.tts_quality import normalize_text_for_tts

    raw = " ".join(f"Stop ! ! ! she cried {i}. See [[Main Page {i}]] now." for i in range(40))
    cleaned = normalize_text_for_tts(raw)
    assert normalize_text_for_tts(cleaned) != cleaned, "precondition: cleanup not idempotent"

    state = {"calls": 0, "down_after": 4}

    def build(**kwargs):
        state["calls"] += 1
        if state["down_after"] and state["calls"] > state["down_after"]:
            class _Down:
                async def stream(self_inner):
                    raise edge_exceptions.NoAudioReceived("down")
                    yield  # pragma: no cover
            return _Down()
        return _RealisticCommunicate(kwargs["text"])

    stubbed.setattr(tts_worker, "build_communicate", build)
    output = tmp_path / "resume.mp3"
    with pytest.raises(tts_worker._ChunkError) as excinfo:
        asyncio.run(_worker(raw, output)._stream_generate())
    staging = excinfo.value.staging_dir
    preserved = excinfo.value.preserved_chunks
    assert staging is not None and preserved >= 2

    # Resume exactly as the UI does: with the candidate's (cleaned) text.
    candidate = ChunkStore.list_resume_candidates(staging.parent)[0]
    state.update(calls=0, down_after=0)
    resumed = _worker(candidate.text, output, job_id=candidate.job_id,
                      resume_staging_dir=candidate.staging_dir)
    stages = []
    resumed.stage_changed.connect(lambda kind, text: stages.append(text))
    asyncio.run(resumed._stream_generate())

    assert any(s.startswith("Resuming from chunk") for s in stages), stages
    assert not any("couldn't be reused" in s for s in stages)
    assert output.exists()


def test_unusable_saved_progress_is_left_intact_when_starting_over(stubbed, tmp_path):
    stubbed.setattr(tts_worker, "build_communicate",
                    lambda **kw: _RealisticCommunicate(kw["text"]))
    staging_root = tmp_path / "appdata" / "staging"
    old = ChunkStore.create(staging_root, "oldjob", voice="en-US-AvaNeural", rate="+0%",
                            volume="+0%", output_path=str(tmp_path / "x.mp3"),
                            text="completely different text.")
    old.release()
    before = (old.staging_dir / "manifest.json").read_bytes()

    worker = _worker(_BOOK, tmp_path / "x.mp3", job_id="oldjob",
                     resume_staging_dir=old.staging_dir)
    # The staged source.txt is authoritative on resume, so feed a store whose
    # manifest can't be trusted instead: corrupt its hash.
    manifest = json.loads(before)
    manifest["text_hash"] = "0" * len(manifest["text_hash"])
    (old.staging_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    before = (old.staging_dir / "manifest.json").read_bytes()

    asyncio.run(worker._stream_generate())
    assert (old.staging_dir / "manifest.json").read_bytes() == before
    assert (tmp_path / "x.mp3").exists()


# ------------------------------------------------------------------ #
# 4. Stale-staging sweep                                              #
# ------------------------------------------------------------------ #

def _age(path: Path, days: float) -> None:
    old = time.time() - days * 86_400
    os.utime(path, (old, old))


def _make_store(root: Path, job_id: str, *, chunks: int) -> ChunkStore:
    store = ChunkStore.create(root, job_id, voice="v", rate="+0%", volume="+0%",
                              output_path="o.mp3", text="alpha beta gamma delta " * 10)
    for i in range(chunks):
        store.record_chunk(i, start_char=i * 10, end_char=i * 10 + 10,
                           text_hash="h", audio_bytes=b"x" * 10)
    store.mark_failed(chunks + 1, chunks + 2)
    return store


def test_sweep_keeps_recent_resumable_jobs_and_the_one_being_resumed(tmp_path):
    root = tmp_path / "staging"
    resumable = _make_store(root, "resumable", chunks=2)
    target = _make_store(root, "target", chunks=2)
    empty = _make_store(root, "empty", chunks=0)
    ancient = _make_store(root, "ancient", chunks=2)
    for store, days in ((resumable, 20), (target, 90), (empty, 20), (ancient, 90)):
        _age(store.staging_dir, days)

    cleanup_stale_staging(root, max_age_days=7, keep=(target.staging_dir,))

    assert resumable.staging_dir.exists()   # 20 days: still offered for resume
    assert target.staging_dir.exists()      # being resumed right now
    assert not empty.staging_dir.exists()   # nothing to resume
    assert not ancient.staging_dir.exists() # past the resumable window


# ------------------------------------------------------------------ #
# 5. Disk error while staging a chunk                                 #
# ------------------------------------------------------------------ #

def test_disk_full_while_staging_is_resumable_and_explained(stubbed, tmp_path):
    stubbed.setattr(tts_worker, "build_communicate",
                    lambda **kw: _RealisticCommunicate(kw["text"]))
    real_record = ChunkStore.record_chunk
    calls = {"n": 0}

    def flaky_record(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        return real_record(self, *args, **kwargs)

    stubbed.setattr(ChunkStore, "record_chunk", flaky_record)
    worker = _worker(_BOOK, tmp_path / "full.mp3")
    resumable = []
    worker.job_resumable.connect(lambda *a: resumable.append(a))

    with pytest.raises(tts_worker._ChunkError) as excinfo:
        asyncio.run(worker._stream_generate())

    assert excinfo.value.cause.kind == "staging_io"
    assert resumable and resumable[0][1] == 2   # two chunks preserved
    message = tts_worker.TTSWorker._user_message(excinfo.value)
    assert message.startswith("The disk is full.")
    assert "Resume" in message
    manifest = json.loads((excinfo.value.staging_dir / "manifest.json").read_text())
    assert manifest["status"] == "failed"


# ------------------------------------------------------------------ #
# 6. DNS blip at job start                                            #
# ------------------------------------------------------------------ #

def test_voice_check_rides_out_a_brief_dns_failure(stubbed, tmp_path):
    calls = {"n": 0}

    async def flaky_list_voices(*, force_refresh=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("Cannot connect to host speech.platform.bing.com:443 "
                          "[nodename nor servname provided, or not known]")
        return [{"ShortName": "en-US-AvaNeural", "Locale": "en-US"}]

    stubbed.setattr(tts_worker, "list_voices", flaky_list_voices)
    stubbed.setattr(tts_worker, "build_communicate",
                    lambda **kw: _RealisticCommunicate(kw["text"]))
    output = tmp_path / "dns.mp3"
    asyncio.run(_worker(_BOOK, output)._stream_generate())
    assert output.exists()
    assert calls["n"] == 2


def test_voice_check_outage_gives_a_plain_message(stubbed, tmp_path):
    async def down(*, force_refresh=False):
        raise OSError("Cannot connect to host speech.platform.bing.com:443 "
                      "[nodename nor servname provided, or not known]")

    stubbed.setattr(tts_worker, "list_voices", down)
    with pytest.raises(tts_worker._PreflightError) as excinfo:
        asyncio.run(_worker("Hello there.", tmp_path / "o.mp3")._stream_generate())
    message = tts_worker.TTSWorker._user_message(excinfo.value)
    headline = message.split("Technical details:")[0]
    assert "speech service" in headline
    assert "nodename" not in headline and "aiohttp" not in headline


# ------------------------------------------------------------------ #
# 7. Resume listing vs. a running job                                 #
# ------------------------------------------------------------------ #

def test_resume_listing_ignores_a_job_that_is_still_running(tmp_path):
    root = tmp_path / "staging"
    text = "alpha beta gamma delta " * 10
    store = ChunkStore.create(root, "live", voice="v", rate="+0%", volume="+0%",
                              output_path="o.mp3", text=text)
    store.record_chunk(0, start_char=0, end_char=10,
                       text_hash=tts_worker._short_text_hash(text[0:10]),
                       audio_bytes=b"x" * 10)

    assert ChunkStore.list_resume_candidates(root) == []
    manifest = json.loads((store.staging_dir / "manifest.json").read_text())
    assert manifest["status"] == "running"

    store.mark_cancelled(preserve_progress=True, failed_at_chunk=2, total=5)
    assert [c.job_id for c in ChunkStore.list_resume_candidates(root)] == ["live"]


def test_worker_releases_its_staging_dir_after_an_unexpected_error(stubbed, tmp_path):
    def boom(**_kwargs):
        raise RuntimeError("unexpected")

    stubbed.setattr(tts_worker, "build_communicate", boom)
    worker = _worker(_BOOK, tmp_path / "o.mp3")
    failures = []
    worker.failed.connect(failures.append)
    worker.run()   # synchronous: exercises run()'s finally block
    assert failures
    staging = worker._chunk_store.staging_dir
    assert not chunk_store_mod.is_live(staging)
    manifest = json.loads((staging / "manifest.json").read_text())
    assert manifest["status"] == "running"   # left as-is by the crash…
    # …and therefore offered for resume (the listing marks it interrupted),
    # which it would not be while still registered as live.


def test_atomic_writes_use_unique_temp_names(tmp_path, monkeypatch):
    seen = []
    real_replace = chunk_store_mod._replace_with_retry

    def spy(src, dst, attempts=5):
        seen.append(src.name)
        return real_replace(src, dst, attempts)

    monkeypatch.setattr(chunk_store_mod, "_replace_with_retry", spy)
    target = tmp_path / "manifest.json"
    chunk_store_mod._atomic_write_text(target, "one")
    chunk_store_mod._atomic_write_text(target, "two")
    assert target.read_text() == "two"
    assert len(set(seen)) == 2
    assert not list(tmp_path.glob("*.tmp"))


def test_disk_still_full_in_the_failure_handler_keeps_the_job_resumable(stubbed, tmp_path):
    """The manifest write inside the failure handler hits the same full disk."""
    stubbed.setattr(tts_worker, "build_communicate",
                    lambda **kw: _RealisticCommunicate(kw["text"]))
    real_record = ChunkStore.record_chunk
    real_save = ChunkStore._save_manifest
    state = {"records": 0, "full": False}

    def record(self, *args, **kwargs):
        state["records"] += 1
        if state["records"] == 3:
            state["full"] = True
            raise OSError(28, "No space left on device")
        return real_record(self, *args, **kwargs)

    def save(self):
        if state["full"]:
            raise OSError(28, "No space left on device")
        return real_save(self)

    stubbed.setattr(ChunkStore, "record_chunk", record)
    stubbed.setattr(ChunkStore, "_save_manifest", save)
    worker = _worker(_BOOK, tmp_path / "full.mp3")
    with pytest.raises(tts_worker._ChunkError) as excinfo:
        asyncio.run(worker._stream_generate())
    assert excinfo.value.cause.kind == "staging_io"

    state["full"] = False   # space freed
    candidates = ChunkStore.list_resume_candidates(excinfo.value.staging_dir.parent)
    assert [c.completed_count for c in candidates] == [2]
