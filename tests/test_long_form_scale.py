"""
Full-scale long-form audiobook tests (6-12 hours of narration).

The existing long-form tests use a deliberately tiny chunk plan, which keeps
them fast but never exercises the *production* chunk sizes. These tests run
book-length fixtures through the real chunker and the real assembly path with
the shipping chunk plan, because that is the workload SetupTTS is actually
used for.

Sizing: edge-tts narration runs roughly 950-1000 characters per minute of
audio at a normal rate, so
    6 hours  ~= 350,000 characters  (~35 production chunks)
    12 hours ~= 700,000 characters  (~70 production chunks)

The network is stubbed — a fake Communicate echoes each chunk's text back as
its audio payload — so the assembled "MP3" is a byte-exact transcript of what
was actually sent for synthesis. That makes truncation, duplication, gaps and
out-of-order assembly directly assertable, which is the whole point.
"""

from __future__ import annotations

import asyncio
import json
import random
import tracemalloc
from pathlib import Path

import pytest

from app.workers import tts_worker
from app.workers.chunk_store import ChunkStore


# Characters of narration per hour of audio (see module docstring).
_CHARS_PER_HOUR = 58_000


# ------------------------------------------------------------------ #
# Fixtures                                                            #
# ------------------------------------------------------------------ #

def _build_book(hours: float, seed: int = 20260827) -> str:
    """
    Build a book-shaped fixture of roughly ``hours`` of narration.

    Deliberately messy: chapter headings, dialogue, em dashes, ellipses,
    numbers, quotes and the occasional very long and very short sentence —
    the things that make real chunk boundaries interesting.
    """
    rng = random.Random(seed)
    target = int(hours * _CHARS_PER_HOUR)

    lexicon = (
        "harbour lantern quarry meridian tallow cistern bramble vellum "
        "cinder marrow thistle furrow beacon gantry sable plinth reckon "
        "hollow tide granite ember willow shutter compass anvil"
    ).split()

    parts: list[str] = []
    size = 0
    chapter = 0

    while size < target:
        chapter += 1
        heading = f"Chapter {chapter}"
        parts.append(heading)
        size += len(heading) + 2

        for _ in range(rng.randint(6, 14)):
            sentences = []
            for _ in range(rng.randint(3, 9)):
                words = rng.choices(lexicon, k=rng.randint(4, 22))
                sentence = " ".join(words).capitalize()
                style = rng.random()
                if style < 0.10:
                    sentence = f'"{sentence}?" she asked.'
                elif style < 0.18:
                    sentence = f"{sentence} — and then, {rng.randint(2, 99)} more."
                elif style < 0.24:
                    sentence = f"{sentence}..."
                else:
                    sentence += "."
                sentences.append(sentence)
            paragraph = " ".join(sentences)
            parts.append(paragraph)
            size += len(paragraph) + 2

    return "\n\n".join(parts)


class _EchoCommunicate:
    """Fake edge_tts Communicate that returns the chunk text as its audio."""

    def __init__(self, text: str, controller=None) -> None:
        self._text = text
        self._controller = controller

    async def stream(self):
        if self._controller is not None:
            outcome = self._controller(self._text)
            if outcome == "no_audio":
                from edge_tts import exceptions as edge_exceptions

                raise edge_exceptions.NoAudioReceived("No audio was received.")
        # A couple of WordBoundary events so progress reporting has input.
        midpoint = max(1, len(self._text) // 2)
        yield {"type": "WordBoundary", "text": self._text[:midpoint]}
        yield {"type": "WordBoundary", "text": self._text[midpoint:]}
        yield {"type": "audio", "data": self._text.encode("utf-8")}


@pytest.fixture
def stub_network(monkeypatch):
    """Stub voice listing + synthesis, keeping the production chunk plan."""

    async def fake_list_voices(*, force_refresh=False):
        return [
            {"ShortName": "en-US-AvaNeural", "Locale": "en-US"},
            {"ShortName": "en-US-AndrewMultilingualNeural", "Locale": "en-US"},
        ]

    monkeypatch.setattr(tts_worker, "list_voices", fake_list_voices)

    state = {"controller": None, "chunks": []}

    def build(**kwargs):
        state["chunks"].append(kwargs["text"])
        return _EchoCommunicate(kwargs["text"], state["controller"])

    monkeypatch.setattr(tts_worker, "build_communicate", build)
    return state


def _run(text, output, **kwargs):
    worker = tts_worker.TTSWorker(
        text=text,
        voice=kwargs.pop("voice", "en-US-AvaNeural"),
        rate="+0%",
        volume="+0%",
        output_path=str(output),
        **kwargs,
    )
    asyncio.run(worker._stream_generate())
    return worker


def _fail_everything_after(n_successes: int):
    """
    Controller that succeeds ``n_successes`` times then fails permanently.

    Failing a single chunk is not enough to stop a long job: the worker
    retries, and then splits the chunk into smaller sections that succeed.
    That recovery is exactly what we want in production, so to test the
    resume path we simulate the real stopping condition — connectivity that
    drops and stays down.
    """
    state = {"n": 0, "down": False}

    def controller(_text: str) -> str:
        if not state["down"]:
            state["n"] += 1
            if state["n"] > n_successes:
                state["down"] = True
        return "no_audio" if state["down"] else "success"

    return controller


# ------------------------------------------------------------------ #
# Coverage at real audiobook scale                                    #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("hours", [6, 12])
def test_book_length_job_reproduces_the_source_text_exactly(
    tmp_path, monkeypatch, stub_network, hours
):
    """
    The single most important invariant for a 6-12 hour export: every
    character of the source is synthesised exactly once, in order.

    Because the stub echoes chunk text as audio, the assembled output is a
    transcript — so a silent truncation (the class of bug that loses the last
    two hours of an audiobook) fails this assertion loudly.
    """
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))

    text = _build_book(hours)
    output = tmp_path / f"book_{hours}h.mp3"

    _run(text, output)

    assert output.exists(), "no output produced"

    cleaned = tts_worker.build_text_profile(text).cleaned_text.strip()
    assembled = output.read_bytes().decode("utf-8")

    # Whitespace at chunk seams is the only permitted difference.
    assert "".join(assembled.split()) == "".join(cleaned.split()), (
        f"{hours}h export does not match the source text "
        f"(assembled {len(assembled):,} chars vs cleaned {len(cleaned):,})"
    )


@pytest.mark.parametrize("hours", [6, 12])
def test_book_length_chunk_ranges_tile_the_source_without_gaps(
    tmp_path, monkeypatch, stub_network, hours
):
    """Recorded chunk ranges must tile [0, len(cleaned)) exactly once."""
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))

    text = _build_book(hours)
    output = tmp_path / f"ranges_{hours}h.mp3"

    # Keep the staging dir around so the manifest can be inspected.
    monkeypatch.setattr(ChunkStore, "cleanup", lambda self: None)
    worker = _run(text, output)

    staging_root = Path(tmp_path / "appdata") / "staging"
    manifests = list(staging_root.glob("*/manifest.json"))
    assert manifests, "no manifest was written"
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))

    chunks = sorted(manifest["chunks"], key=lambda c: c["start_char"])
    assert chunks, "manifest recorded no chunks"

    cleaned_len = len(tts_worker.build_text_profile(text).cleaned_text.strip())

    cursor = 0
    for entry in chunks:
        assert entry["start_char"] == cursor, (
            f"gap or overlap at chunk {entry.get('index')}: "
            f"expected start {cursor}, got {entry['start_char']}"
        )
        assert entry["end_char"] > entry["start_char"], "empty chunk recorded"
        assert entry["audio_bytes"] > 0, "chunk recorded with no audio"
        cursor = entry["end_char"]

    assert cursor == cleaned_len, (
        f"chunks stop at {cursor:,} but the cleaned text is {cleaned_len:,} chars — "
        f"{cleaned_len - cursor:,} characters would be missing from the export"
    )


@pytest.mark.parametrize("hours", [6, 12])
def test_book_length_chunk_count_and_sizes_are_sane(
    tmp_path, monkeypatch, stub_network, hours
):
    """Guard against a pathological chunk plan (thousands of tiny requests,
    or a handful of oversized ones the service will reject)."""
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))

    text = _build_book(hours)
    _run(text, tmp_path / f"sizes_{hours}h.mp3")

    sent = stub_network["chunks"]
    assert sent, "nothing was sent for synthesis"

    # Production plan tops out around 10.5k chars per chunk.
    oversized = [c for c in sent if len(c) > 11_000]
    assert not oversized, f"{len(oversized)} chunk(s) exceed the service limit"

    # A warm-up probe and a ramp chunk are expected to be small; the body
    # should not be. Allow a generous allowance for those plus seams.
    small = [c for c in sent if len(c) < 1_000]
    assert len(small) <= 5, (
        f"{len(small)} undersized chunks for a {hours}h job — "
        "the chunker is fragmenting the text"
    )

    # Sanity: roughly len(text)/10k chunks, within a wide band.
    expected = len(text) / 10_000
    assert 0.4 * expected <= len(sent) <= 4 * expected, (
        f"{len(sent)} chunks for {len(text):,} chars looks wrong "
        f"(expected roughly {expected:.0f})"
    )


def test_twelve_hour_job_does_not_accumulate_audio_in_memory(
    tmp_path, monkeypatch, stub_network
):
    """
    Chunks are staged on disk and concatenated at the end. Peak Python heap
    must stay well under the size of the finished audiobook, otherwise a
    12-hour export would balloon memory on long runs.
    """
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))

    text = _build_book(12)
    output = tmp_path / "memory.mp3"

    tracemalloc.start()
    try:
        _run(text, output)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    produced = output.stat().st_size
    assert produced > 0

    # The source text alone is ~700 KB and is legitimately held in memory.
    # Peak should stay within a small multiple of it, not scale with the
    # number of chunks already rendered.
    budget = max(24 * 1024 * 1024, len(text.encode("utf-8")) * 8)
    assert peak < budget, (
        f"peak heap {peak / 1e6:.1f} MB exceeds the {budget / 1e6:.1f} MB budget "
        f"for a 12-hour job"
    )


# ------------------------------------------------------------------ #
# Failure and resume, deep into a long job                            #
# ------------------------------------------------------------------ #

def test_failure_deep_into_a_long_job_preserves_prior_chunks(
    tmp_path, monkeypatch, stub_network
):
    """
    Losing connectivity 5 hours into a 6-hour render must not throw away the
    finished work — that is the difference between a 2-minute resume and a
    full re-render.
    """
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))

    text = _build_book(6)
    output = tmp_path / "deep_failure.mp3"

    fail_after = 25
    stub_network["controller"] = _fail_everything_after(fail_after)

    with pytest.raises(tts_worker._ChunkError) as excinfo:
        _run(text, output)

    error = excinfo.value
    assert not output.exists(), "a partial file must never be presented as complete"
    assert error.staging_dir is not None
    assert error.preserved_chunks >= 20, (
        f"only {error.preserved_chunks} chunks preserved after failing at "
        f"chunk ~{fail_after} — prior work was discarded"
    )

    manifest = json.loads(
        (error.staging_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "failed"
    assert len(manifest["chunks"]) == error.preserved_chunks


def test_long_job_resumes_and_still_reproduces_the_full_text(
    tmp_path, monkeypatch, stub_network
):
    """A resumed 6-hour job must produce the same bytes as an uninterrupted one."""
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))

    text = _build_book(6)
    output = tmp_path / "resumed.mp3"

    stub_network["controller"] = _fail_everything_after(18)

    with pytest.raises(tts_worker._ChunkError) as excinfo:
        _run(text, output)
    staging_dir = excinfo.value.staging_dir

    # Connectivity restored — resume.
    stub_network["controller"] = None
    _run(
        text,
        output,
        job_id=staging_dir.name,
        resume_staging_dir=staging_dir,
    )

    assert output.exists()
    cleaned = tts_worker.build_text_profile(text).cleaned_text.strip()
    assembled = output.read_bytes().decode("utf-8")
    assert "".join(assembled.split()) == "".join(cleaned.split()), (
        "resumed export does not match the source text"
    )
    assert not staging_dir.exists(), "staging should be cleaned up on success"


def test_resume_does_not_duplicate_already_rendered_audio(
    tmp_path, monkeypatch, stub_network
):
    """The seam between preserved and newly rendered chunks must not repeat
    or drop text."""
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))

    text = _build_book(6)
    output = tmp_path / "seam.mp3"

    stub_network["controller"] = _fail_everything_after(12)
    with pytest.raises(tts_worker._ChunkError) as excinfo:
        _run(text, output)
    staging_dir = excinfo.value.staging_dir

    stub_network["controller"] = None
    _run(text, output, job_id=staging_dir.name, resume_staging_dir=staging_dir)

    cleaned = "".join(
        tts_worker.build_text_profile(text).cleaned_text.strip().split()
    )
    assembled = "".join(output.read_bytes().decode("utf-8").split())

    assert len(assembled) == len(cleaned), (
        f"resumed output is {len(assembled):,} chars vs {len(cleaned):,} expected "
        f"({len(assembled) - len(cleaned):+,} — duplicated or dropped at the seam)"
    )
    assert assembled == cleaned


# ------------------------------------------------------------------ #
# Multilingual voices on long English jobs                            #
# ------------------------------------------------------------------ #

def test_multilingual_voice_uses_smaller_chunks_for_long_jobs(
    tmp_path, monkeypatch, stub_network
):
    """
    Multilingual models are treated as more failure-prone on very long
    English narration, so they should be driven with smaller chunks.
    """
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))
    text = _build_book(6)

    _run(text, tmp_path / "plain.mp3", voice="en-US-AvaNeural")
    plain = list(stub_network["chunks"])

    stub_network["chunks"] = []
    _run(
        text,
        tmp_path / "multi.mp3",
        voice="en-US-AndrewMultilingualNeural",
    )
    multi = list(stub_network["chunks"])

    assert plain and multi
    biggest_plain = max(len(c) for c in plain)
    biggest_multi = max(len(c) for c in multi)
    assert biggest_multi <= biggest_plain, (
        f"multilingual chunks ({biggest_multi}) are not smaller than "
        f"standard ones ({biggest_plain})"
    )


# ------------------------------------------------------------------ #
# Disk space pre-flight                                               #
# ------------------------------------------------------------------ #

def test_job_is_refused_up_front_when_the_disk_is_full(tmp_path, monkeypatch):
    """
    A 12-hour render must not get ten hours in before hitting ENOSPC.
    The check runs before any synthesis starts.
    """
    import shutil as _shutil
    from collections import namedtuple

    _Usage = namedtuple("_Usage", "total used free")
    monkeypatch.setattr(
        _shutil, "disk_usage", lambda _p: _Usage(1 << 40, 1 << 40, 5 * 1024 * 1024)
    )

    worker = tts_worker.TTSWorker(
        text="x", voice="en-US-AvaNeural", rate="+0%", volume="+0%",
        output_path=str(tmp_path / "book.mp3"),
    )
    with pytest.raises(tts_worker._PreflightError) as excinfo:
        worker._check_free_space(
            tmp_path, total_chars=700_000, output_path=tmp_path / "book.mp3"
        )
    assert excinfo.value.cause.kind == "insufficient_disk"

    # And the user-facing message must explain it, not leak a raw error.
    message = tts_worker.TTSWorker._user_message(excinfo.value)
    assert "disk space" in message.lower()
    assert "free up space" in message.lower()


def test_disk_preflight_allows_a_job_that_fits(tmp_path, monkeypatch):
    import shutil as _shutil
    from collections import namedtuple

    _Usage = namedtuple("_Usage", "total used free")
    monkeypatch.setattr(
        _shutil, "disk_usage", lambda _p: _Usage(1 << 40, 0, 50 * 1024 ** 3)
    )
    worker = tts_worker.TTSWorker(
        text="x", voice="en-US-AvaNeural", rate="+0%", volume="+0%",
        output_path=str(tmp_path / "book.mp3"),
    )
    # Must not raise.
    worker._check_free_space(
        tmp_path, total_chars=700_000, output_path=tmp_path / "book.mp3"
    )


def test_disk_preflight_never_blocks_a_job_on_its_own_errors(tmp_path, monkeypatch):
    """A failure inside the check itself must not stop a legitimate job."""
    import shutil as _shutil

    def boom(_p):
        raise OSError("cannot stat volume")

    monkeypatch.setattr(_shutil, "disk_usage", boom)
    worker = tts_worker.TTSWorker(
        text="x", voice="en-US-AvaNeural", rate="+0%", volume="+0%",
        output_path=str(tmp_path / "book.mp3"),
    )
    worker._check_free_space(
        tmp_path, total_chars=700_000, output_path=tmp_path / "book.mp3"
    )
