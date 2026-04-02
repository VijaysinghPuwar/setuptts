import asyncio

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
        chunk, payload = cursor.take_next(char_limit, payload_limit)
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
