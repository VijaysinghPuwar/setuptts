import asyncio
import json
import random
import string

import pytest
from edge_tts import exceptions as edge_exceptions

from app.workers import tts_worker


class _FakeCommunicate:
    def __init__(self, text: str, controller) -> None:
        self._text = text
        self._controller = controller

    async def stream(self):
        outcome = self._controller(self._text)
        if outcome == "no_audio":
            raise edge_exceptions.NoAudioReceived(
                "No audio was received. Please verify that your parameters are correct."
            )
        if outcome == "timeout":
            raise asyncio.TimeoutError()

        yield {"type": "WordBoundary", "text": self._text}
        yield {"type": "audio", "data": self._text.encode("utf-8")}


def _small_plan(*_args, **_kwargs):
    return tts_worker._ChunkPlan(
        max_chars=120,
        max_payload_bytes=400,
        ramp_chars=120,
        ramp_payload_bytes=400,
        warmup_chars=120,
        warmup_payload_bytes=400,
        preflight_threshold=1_000_000,
        first_audio_timeout_s=5,
    )


def test_long_jobs_use_a_smaller_probe_first_chunk():
    text = ("Halló heimur. " * 2000).strip()
    chunks = tts_worker._split_text(
        text,
        tts_worker._chunk_size_for(len(text)),
        tts_worker._payload_limit_for(len(text)),
    )
    probed = tts_worker._apply_first_chunk_probe(chunks, len(text))

    assert len(probed) > len(chunks)
    assert len(probed[0]) < len(chunks[0])
    assert (
        tts_worker._edge_payload_size(probed[0])
        <= tts_worker._FIRST_CHUNK_PROBE_PAYLOAD_BYTES
    )


def test_cursor_take_next_emits_absolute_ranges():
    text = "abc def ghi jkl mno pqr stu vwx yz"
    cursor = tts_worker._ChunkCursor(text)
    chunk, payload, start, end = cursor.take_next(10, 40)
    assert start == 0
    assert end > 0
    assert text[start:end].startswith(chunk)
    # Next call continues where the previous one left off.
    next_chunk, _, next_start, _ = cursor.take_next(10, 40)
    assert next_start >= end


def test_preflight_fails_fast_on_no_audio(tmp_path, monkeypatch):
    async def fake_list_voices(*, force_refresh=False):
        return [{"ShortName": "is-IS-GudrunNeural", "Locale": "is-IS"}]

    monkeypatch.setattr(tts_worker, "list_voices", fake_list_voices)
    monkeypatch.setattr(
        tts_worker,
        "build_communicate",
        lambda **kwargs: _FakeCommunicate(kwargs["text"], lambda _text: "no_audio"),
    )

    output = tmp_path / "gudrun.mp3"
    worker = tts_worker.TTSWorker(
        text=("Halló heimur. " * 3000).strip(),
        voice="is-IS-GudrunNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(output),
    )

    with pytest.raises(tts_worker._PreflightError) as excinfo:
        asyncio.run(worker._stream_generate())

    assert not output.exists()
    message = tts_worker.TTSWorker._user_message(excinfo.value)
    assert "may not be compatible with this text" in message
    assert "startup check" in message


def test_voice_language_mismatch_is_blocked_before_generation(tmp_path, monkeypatch):
    async def fake_list_voices(*, force_refresh=False):
        return [
            {"ShortName": "is-IS-GudrunNeural", "Locale": "is-IS", "Gender": "Female"},
            {"ShortName": "en-US-AvaNeural", "Locale": "en-US", "Gender": "Female"},
        ]

    monkeypatch.setattr(tts_worker, "list_voices", fake_list_voices)
    monkeypatch.setattr(
        tts_worker,
        "build_communicate",
        lambda **kwargs: pytest.fail("build_communicate should not run for a blocked mismatch"),
    )

    output = tmp_path / "mismatch.mp3"
    worker = tts_worker.TTSWorker(
        text="This is a short English test that should not use an Icelandic voice.",
        voice="is-IS-GudrunNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(output),
    )

    with pytest.raises(tts_worker._PreflightError) as excinfo:
        asyncio.run(worker._stream_generate())

    message = tts_worker.TTSWorker._user_message(excinfo.value)
    assert "appears to be English" in message
    assert "Recommended voice: en-US-AvaNeural" in message


def test_chunk_recovery_succeeds_with_smaller_sections(tmp_path, monkeypatch):
    async def fake_list_voices(*, force_refresh=False):
        return [{"ShortName": "en-US-AvaNeural", "Locale": "en-US"}]

    calls: dict[str, int] = {}
    successful_texts: list[str] = []

    def controller(text: str) -> str:
        calls[text] = calls.get(text, 0) + 1
        if tts_worker._edge_payload_size(text) > 900 and calls[text] <= 2:
            return "no_audio"
        if len(text) > 300:
            successful_texts.append(text)
        return "success"

    monkeypatch.setattr(tts_worker, "list_voices", fake_list_voices)
    monkeypatch.setattr(
        tts_worker,
        "build_communicate",
        lambda **kwargs: _FakeCommunicate(kwargs["text"], controller),
    )

    output = tmp_path / "recovered.mp3"
    worker = tts_worker.TTSWorker(
        text=("alpha beta gamma delta epsilon zeta eta theta iota kappa " * 90).strip(),
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(output),
    )

    asyncio.run(worker._stream_generate())

    data = output.read_bytes()
    assert data
    assert any(count == 2 for count in calls.values())
    assert any(
        tts_worker._edge_payload_size(text) <= 900 for text in successful_texts
    )


def test_adaptive_chunk_policy_grows_for_healthy_long_jobs(tmp_path):
    text = ("alpha beta gamma delta epsilon zeta eta theta iota kappa " * 2000).strip()
    plan = tts_worker._chunk_plan_for(len(text), "latin")
    cursor = tts_worker._ChunkCursor(text)
    worker = tts_worker.TTSWorker(
        text=text,
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(tmp_path / "adaptive.mp3"),
    )

    char_limit = plan.warmup_chars
    payload_limit = plan.warmup_payload_bytes
    seen_payloads: list[int] = []
    chunk_count = 0

    while cursor.has_more():
        chunk, payload, _start, _end = cursor.take_next(char_limit, payload_limit)
        assert chunk
        seen_payloads.append(payload)
        char_limit, payload_limit = worker._retune_after_chunk(
            tts_worker._ChunkOutcome(
                attempts=1,
                elapsed=8.0,
                used_recovery=False,
                first_audio_delay=2.0,
                receive_duration=4.0,
                write_duration=0.01,
            ),
            char_limit,
            payload_limit,
            plan,
            chunk_index=chunk_count,
        )
        chunk_count += 1

    assert seen_payloads[0] <= plan.warmup_payload_bytes
    assert max(seen_payloads) >= plan.max_payload_bytes * 0.9
    assert chunk_count < 40


def test_multilingual_long_jobs_use_smaller_limits():
    regular = tts_worker._chunk_plan_for(120_000, "latin", multilingual_voice=False)
    multilingual = tts_worker._chunk_plan_for(120_000, "latin", multilingual_voice=True)

    assert multilingual.max_chars < regular.max_chars
    assert multilingual.max_payload_bytes < regular.max_payload_bytes


def test_failed_chunk_preserves_progress_and_resume_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr(tts_worker, "_chunk_plan_for", _small_plan)

    async def fake_list_voices(*, force_refresh=False):
        return [{"ShortName": "en-US-AvaNeural", "Locale": "en-US"}]

    def controller(text: str) -> str:
        if "omega" in text:
            return "no_audio"
        return "success"

    monkeypatch.setattr(tts_worker, "list_voices", fake_list_voices)
    monkeypatch.setattr(
        tts_worker,
        "build_communicate",
        lambda **kwargs: _FakeCommunicate(kwargs["text"], controller),
    )

    text = (
        ("alpha beta gamma delta epsilon " * 12).strip()
        + "\n\n"
        + ("omega psi chi phi upsilon " * 12).strip()
    )
    output = tmp_path / "preserved.mp3"
    worker = tts_worker.TTSWorker(
        text=text,
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(output),
    )

    with pytest.raises(tts_worker._ChunkError) as excinfo:
        asyncio.run(worker._stream_generate())

    err = excinfo.value
    assert err.preserved_chunks >= 1
    assert err.staging_dir is not None
    assert (err.staging_dir / "chunk_000000.mp3").exists()
    assert (err.staging_dir / "source.txt").exists()

    manifest = json.loads((err.staging_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert len(manifest["chunks_completed"]) == err.preserved_chunks
    assert manifest["chunks"][0]["start_char"] == 0

    message = tts_worker.TTSWorker._user_message(err)
    assert "preserved" in message.lower()
    assert "retry/resume" in message.lower()


def test_resume_reuses_preserved_chunks_without_regenerating_them(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr(tts_worker, "_chunk_plan_for", _small_plan)

    async def fake_list_voices(*, force_refresh=False):
        return [{"ShortName": "en-US-AvaNeural", "Locale": "en-US"}]

    fail_second_chunk = {"enabled": True}
    calls: dict[str, int] = {}

    def controller(text: str) -> str:
        calls[text] = calls.get(text, 0) + 1
        if "omega" in text and fail_second_chunk["enabled"]:
            return "no_audio"
        return "success"

    monkeypatch.setattr(tts_worker, "list_voices", fake_list_voices)
    monkeypatch.setattr(
        tts_worker,
        "build_communicate",
        lambda **kwargs: _FakeCommunicate(kwargs["text"], controller),
    )

    text = (
        ("alpha beta gamma delta epsilon " * 12).strip()
        + "\n\n"
        + ("omega psi chi phi upsilon " * 12).strip()
    )
    cleaned = tts_worker.build_text_profile(text).cleaned_text.strip()
    cursor = tts_worker._ChunkCursor(cleaned)
    first_chunk, _payload, _start, _end = cursor.take_next(120, 400)

    output = tmp_path / "resume.mp3"
    worker = tts_worker.TTSWorker(
        text=text,
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(output),
    )

    with pytest.raises(tts_worker._ChunkError) as excinfo:
        asyncio.run(worker._stream_generate())

    fail_second_chunk["enabled"] = False
    resumed = tts_worker.TTSWorker(
        text=text,
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(output),
        job_id=excinfo.value.staging_dir.name if excinfo.value.staging_dir is not None else None,
        resume_staging_dir=excinfo.value.staging_dir,
    )
    asyncio.run(resumed._stream_generate())

    assert output.exists()
    assert output.read_bytes().startswith(first_chunk.encode("utf-8"))
    assert calls[first_chunk] == 1
    assert excinfo.value.staging_dir is not None
    assert not excinfo.value.staging_dir.exists()


def test_irrecoverable_subchunk_fails_closed_instead_of_skipping(tmp_path, monkeypatch):
    """A tiny sub-chunk that refuses to produce audio must fail the job
    (preserving progress), not silently drop the text. This is the regression
    we're protecting against for very long audiobook jobs."""
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr(tts_worker, "_chunk_plan_for", _small_plan)

    async def fake_list_voices(*, force_refresh=False):
        return [{"ShortName": "en-US-AvaNeural", "Locale": "en-US"}]

    # "omega" appears in a single small sentence that will always return
    # no-audio. The recovery loop will subdivide once or twice, then hit the
    # min-size floor with the same fragment still failing.
    def controller(text: str) -> str:
        if "omega" in text:
            return "no_audio"
        return "success"

    monkeypatch.setattr(tts_worker, "list_voices", fake_list_voices)
    monkeypatch.setattr(
        tts_worker,
        "build_communicate",
        lambda **kwargs: _FakeCommunicate(kwargs["text"], controller),
    )

    text = (
        ("alpha beta gamma delta epsilon " * 12).strip()
        + "\n\nomega irrecoverable fragment.\n\n"
        + ("zeta eta theta iota kappa " * 12).strip()
    )

    output = tmp_path / "fail_closed.mp3"
    worker = tts_worker.TTSWorker(
        text=text,
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(output),
    )

    with pytest.raises(tts_worker._ChunkError) as excinfo:
        asyncio.run(worker._stream_generate())

    # The job must NOT have produced a "completed" MP3 — the previous version
    # of SetupTTS would silently skip the irrecoverable fragment and write a
    # success file. We need the failure to be loud and preserve progress.
    assert not output.exists()
    assert excinfo.value.staging_dir is not None
    # At least one earlier chunk should still be staged for resume.
    assert excinfo.value.preserved_chunks >= 1


def test_recovery_subdivision_ranges_exactly_cover_parent():
    random.seed(42)

    for _ in range(200):
        parts = []
        for _ in range(random.randint(20, 80)):
            token = "".join(
                random.choice(string.ascii_lowercase)
                for _ in range(random.randint(1, 15))
            )
            parts.append(token + random.choice([" ", "  ", "\n", "\n\n", ". ", "? ", ""]))

        text = tts_worker.build_text_profile("".join(parts)).cleaned_text.strip()
        cursor = tts_worker._ChunkCursor(text)
        while cursor.has_more():
            _chunk, _payload, start, end = cursor.take_next(
                random.randint(60, 160),
                random.randint(180, 540),
            )
            sub_chunks = tts_worker._subdivide_range_for_recovery(
                text,
                start,
                end,
                random.randint(20, 80),
                random.randint(80, 260),
            )
            if len(sub_chunks) <= 1:
                continue

            ranges = [(sub_start, sub_end) for _text, sub_start, sub_end, _payload in sub_chunks]
            assert ranges[0][0] == start
            assert ranges[-1][1] == end
            assert all(ranges[i][1] == ranges[i + 1][0] for i in range(len(ranges) - 1))


def test_duration_sanity_failure_preserves_progress_without_completed_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr(tts_worker, "_chunk_plan_for", _small_plan)
    monkeypatch.setattr(tts_worker, "mp3_duration_seconds", lambda _path: 1.0)

    async def fake_list_voices(*, force_refresh=False):
        return [{"ShortName": "en-US-AvaNeural", "Locale": "en-US"}]

    monkeypatch.setattr(tts_worker, "list_voices", fake_list_voices)
    monkeypatch.setattr(
        tts_worker,
        "build_communicate",
        lambda **kwargs: _FakeCommunicate(kwargs["text"], lambda _text: "success"),
    )

    text = ("alpha beta gamma delta epsilon zeta eta theta iota kappa " * 80).strip()
    output = tmp_path / "too-short.mp3"
    worker = tts_worker.TTSWorker(
        text=text,
        voice="en-US-AvaNeural",
        rate="+0%",
        volume="+0%",
        output_path=str(output),
    )

    with pytest.raises(tts_worker._ChunkError) as excinfo:
        asyncio.run(worker._stream_generate())

    err = excinfo.value
    assert err.cause.kind == "duration_truncated"
    assert output.exists()
    assert err.staging_dir is not None
    manifest = json.loads((err.staging_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["measured_duration_s"] == 1.0


def test_estimate_duration_range_seconds_is_widely_bounded_but_realistic():
    rate = "+5%"
    min_s, max_s = tts_worker._estimate_duration_range_seconds(180_000, rate, "devanagari")
    # 180k chars of Devanagari at +5% rate has a plausible range somewhere
    # between roughly 3 and 8 hours — well over the "2-3 hours bug" zone.
    assert min_s < max_s
    assert min_s > 60 * 60      # > 1 hour minimum
    assert max_s > 3 * 60 * 60  # > 3 hours maximum


def test_chunk_error_messages_are_specific():
    no_audio = tts_worker._ChunkError(
        1,
        31,
        tts_worker._AttemptFailure("no_audio", "The speech service returned no audio."),
    )
    timeout = tts_worker._ChunkError(
        2,
        31,
        tts_worker._AttemptFailure(
            "timeout_waiting_for_audio",
            "The speech request timed out.",
        ),
    )
    dns = tts_worker._ChunkError(
        3,
        31,
        tts_worker._AttemptFailure("dns", "Could not resolve speech host."),
    )

    assert "returned no audio" in tts_worker.TTSWorker._user_message(no_audio)
    assert "timed out repeatedly" in tts_worker.TTSWorker._user_message(timeout)
    assert "resolve speech.platform.bing.com" in tts_worker.TTSWorker._user_message(dns)

    no_audio.preserved_chunks = 3
    message = tts_worker.TTSWorker._user_message(no_audio)
    assert "preserved" in message.lower()
    assert "retry/resume" in message.lower()

    coverage = tts_worker._ChunkError(
        9,
        9,
        tts_worker._AttemptFailure(
            "incomplete_coverage",
            "Generation finished but the recorded chunks do not cover the full source text.",
        ),
    )
    coverage_msg = tts_worker.TTSWorker._user_message(coverage)
    assert "do not cover" in coverage_msg.lower()
    assert "resume" in coverage_msg.lower()
