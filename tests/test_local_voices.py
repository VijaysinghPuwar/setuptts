"""
Tests for voices that run on this computer: OS built-in voices (SAPI /
OneCore on Windows, `say` on macOS) and offline Piper voices — the engines,
the MP3 writer, the local job worker, voice-list loading, job routing and the
voice picker's source filter.

Tests marked ``real_local_voices`` drive the engines installed on the
machine and skip when there are none.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import struct
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.models.voice import Voice, persona_name  # noqa: E402
from app.services import local_tts, piper_tts, system_tts  # noqa: E402
from app.services.local_tts import Mp3Writer  # noqa: E402
from app.utils.mp3_duration import mp3_duration_seconds  # noqa: E402

pytest.importorskip("pytestqt")


def _tone(seconds: float, rate: int = 22_050) -> bytes:
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *(int(8000 * math.sin(i / 8)) for i in range(n)))


# ------------------------------------------------------------------ #
# Names, rates, parsing                                               #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("rate, expected", [
    ("+0%", 0), ("+5%", 0), ("-50%", -6), ("+100%", 6), ("garbage", 0), ("+900%", 10),
])
def test_sapi_rate_maps_percent_to_sapi_scale(rate, expected):
    assert system_tts.sapi_rate(rate) == expected


def test_say_rate_scales_words_per_minute():
    assert system_tts.say_wpm("+0%") == 180
    assert system_tts.say_wpm("+100%") == 360
    assert system_tts.say_wpm("-50%") == 90


@pytest.mark.parametrize("desc, name", [
    ("Microsoft David Desktop - English (United States)", "David (Desktop)"),
    ("Microsoft Zira - English (United States)", "Zira"),
    ("Cortana", "Cortana"),
])
def test_sapi_descriptions_become_short_names(desc, name):
    assert system_tts._clean_label(desc)[0] == name


@pytest.mark.parametrize("token, name", [
    (r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-US_HAZEL_11.0", "Hazel"),
    (r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens\MSTTS_V110_enUS_MarkM", "Mark"),
    (r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_DE-DE_HEDDA_11.0", "Hedda"),
    (r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_Cortana", "Cortana"),
    ("Eddy (English (UK))", "Eddy (English (UK))"),
])
def test_unlisted_system_voice_still_gets_a_readable_name(token, name):
    # History rows render before the voice list loads, from the id alone.
    assert system_tts.system_display_name("system:" + token) == name


def test_windows_listing_parses_powershell_json(monkeypatch):
    payload = json.dumps([
        {"id": r"HKLM\Tokens\TTS_MS_Cortana", "desc": "Cortana", "locale": "en-US",
         "gender": "Female", "onecore": False},
        {"id": r"HKLM\OneCore\MSTTS_V110_deDE_KatjaM", "desc": "Microsoft Katja - German (Germany)",
         "locale": "de-DE", "gender": "Female", "onecore": True},
        {"id": r"HKLM\Tokens\Odd", "desc": "Odd Voice", "locale": "", "gender": "Neutral"},
    ]).encode()

    class Done:
        returncode = 0
        stdout = payload
        stderr = b""

    monkeypatch.setattr(system_tts.subprocess, "run", lambda *a, **k: Done())
    voices = system_tts._list_windows()
    assert [v["Locale"] for v in voices] == ["en-US", "de-DE", "en-US"]
    assert voices[0]["Gender"] == "Female" and voices[2]["Gender"] == ""
    assert all(v["Source"] == "system" and v["ShortName"].startswith("system:") for v in voices)
    assert persona_name(voices[1]["ShortName"]) == "Katja"


def test_windows_listing_accepts_a_single_object(monkeypatch):
    class Done:
        returncode = 0
        stdout = json.dumps({"id": "X", "desc": "Cortana", "locale": "en-US", "gender": "Female"}).encode()
        stderr = b""

    monkeypatch.setattr(system_tts.subprocess, "run", lambda *a, **k: Done())
    assert len(system_tts._list_windows()) == 1


def test_macos_say_listing_parses_names_with_spaces(monkeypatch):
    out = (
        "Alex                en_US    # Most people recognize me by my voice.\n"
        "Eddy (English (UK)) en_GB    # Hello! My name is Eddy.\n"
        "Amélie              fr_CA    # Bonjour, je m’appelle Amélie.\n"
        "garbage line\n"
    ).encode()

    class Done:
        returncode = 0
        stdout = out

    monkeypatch.setattr(system_tts.subprocess, "run", lambda *a, **k: Done())
    voices = system_tts._list_macos()
    assert [v["ShortName"] for v in voices] == ["system:Alex", "system:Eddy (English (UK))", "system:Amélie"]
    assert [v["Locale"] for v in voices] == ["en-US", "en-GB", "fr-CA"]


def test_system_listing_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("no powershell")
    monkeypatch.setattr(system_tts.subprocess, "run", boom)
    assert system_tts.list_system_voices() == [] or sys.platform not in ("win32", "darwin")


def test_duplicate_system_names_are_made_distinct():
    voices = system_tts._dedupe_names([
        {"ShortName": "system:a", "Locale": "en-US", "_display": "Zira"},
        {"ShortName": "system:b", "Locale": "en-US", "_display": "Zira"},
    ])
    assert {system_tts.system_display_name(v["ShortName"]) for v in voices} == {"Zira", "Zira (2)"}


@pytest.mark.parametrize("key, name", [
    ("en_US-lessac-medium", "Lessac"),
    ("en_US-amy-low", "Amy (low)"),
    ("en_GB-northern_english_male-medium", "Northern English Male"),
    ("en_US-ryan-x_low", "Ryan (x-low)"),
])
def test_piper_display_names(key, name):
    assert piper_tts.display_name(key) == name
    assert persona_name("piper:" + key) == name


def test_voice_model_round_trips_source():
    v = Voice.from_edge_dict({"ShortName": "piper:en_US-amy-low", "FriendlyName": "Amy",
                              "Locale": "en-US", "Gender": "", "Source": "piper"})
    assert v.is_local and v.source_label == "Offline" and v.display_name == "Amy (low)"
    assert Voice.from_edge_dict(v.to_edge_dict()) == v
    online = Voice.from_edge_dict({"ShortName": "en-US-AvaNeural", "Locale": "en-US", "Gender": "Female"})
    assert online.source == "online" and not online.is_local


def test_source_of_and_is_local():
    assert local_tts.source_of("en-US-AvaNeural") == "online"
    assert local_tts.source_of("piper:en_US-amy-low") == "piper"
    assert local_tts.source_of("system:Alex") == "system"
    assert local_tts.is_local_voice("system:Alex") and not local_tts.is_local_voice("en-US-AvaNeural")
    assert not local_tts.is_local_voice(None) and not local_tts.is_local_voice("")


def test_recommendations_stay_within_the_same_kind_of_voice():
    from app.services.tts_quality import build_text_profile, recommend_voice

    voices = [
        Voice("en-US-AvaNeural", "Ava", "en-US", "Female"),
        Voice("piper:en_US-amy-low", "Amy", "en-US", "Female", source="piper"),
        Voice("system:Zira", "Zira", "en-US", "Female", source="system"),
    ]
    profile = build_text_profile("This is a plain English sentence for the test. " * 4)
    assert recommend_voice(profile, voices, exclude="system:Zira",
                           preferred_gender="Female") == "piper:en_US-amy-low"
    assert recommend_voice(profile, voices, exclude="en-US-JennyNeural",
                           preferred_gender="Female") == "en-US-AvaNeural"


# ------------------------------------------------------------------ #
# WAV reading                                                         #
# ------------------------------------------------------------------ #

def _write_wav(path: Path, frames: bytes, rate: int, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames)


def test_read_wav_downmixes_and_resamples(tmp_path):
    stereo = struct.pack("<4h", 100, 300, -100, -300)
    p = tmp_path / "s.wav"
    _write_wav(p, stereo, system_tts.SAMPLE_RATE, channels=2)
    assert struct.unpack("<2h", system_tts._read_wav(p)) == (200, -200)

    p2 = tmp_path / "r.wav"
    _write_wav(p2, _tone(1.0, 44_100), 44_100)
    out = system_tts._read_wav(p2)
    assert abs(len(out) // 2 - system_tts.SAMPLE_RATE) <= 1


def test_read_wav_accepts_an_empty_result(tmp_path):
    p = tmp_path / "e.wav"
    _write_wav(p, b"", system_tts.SAMPLE_RATE)
    assert system_tts._read_wav(p) == b""


# ------------------------------------------------------------------ #
# MP3 writer                                                          #
# ------------------------------------------------------------------ #

def test_mp3_writer_streams_pcm_into_a_valid_mp3(tmp_path):
    out = tmp_path / "a.mp3"
    w = Mp3Writer(out, 22_050)
    for _ in range(3):
        w.write(_tone(1.0))
    w.write_silence(0.5)
    assert not out.exists(), "nothing may appear under the final name before finish()"
    w.finish()
    assert out.exists() and not (tmp_path / "a.mp3.part").exists()
    assert w.duration_seconds == pytest.approx(3.5, abs=0.01)
    assert mp3_duration_seconds(out) == pytest.approx(3.5, abs=0.15)


def test_mp3_writer_abort_keeps_the_existing_file(tmp_path):
    out = tmp_path / "book.mp3"
    out.write_bytes(b"previous good audio")
    w = Mp3Writer(out, 22_050)
    w.write(_tone(0.5))
    w.abort()
    assert out.read_bytes() == b"previous good audio"
    assert not (tmp_path / "book.mp3.part").exists()
    w.abort()   # idempotent


def test_mp3_writer_tolerates_odd_byte_counts(tmp_path):
    w = Mp3Writer(tmp_path / "o.mp3", 22_050)
    w.write(b"\x01\x02\x03")
    assert w.samples == 1
    w.finish()


def test_synthesize_to_mp3_rejects_silence(tmp_path):
    class Silent:
        sample_rate = 22_050
        def synthesize(self, text, rate): return b""
        def cancel(self): pass
        def close(self): pass

    with pytest.raises(RuntimeError):
        local_tts.synthesize_to_mp3("piper:x", "hi", "+0%", tmp_path / "x.mp3", synth=Silent())
    assert not (tmp_path / "x.mp3").exists()


# ------------------------------------------------------------------ #
# Local job worker                                                    #
# ------------------------------------------------------------------ #

class FakeSynth:
    sample_rate = 22_050

    def __init__(self, *, fail_times=0, silent=False, delay=0.0, fail_always=False):
        self.calls: list[str] = []
        self.fail_times = fail_times
        self.fail_always = fail_always
        self.silent = silent
        self.delay = delay
        self.cancelled = False
        self.closed = False

    def synthesize(self, text, rate):
        if self.cancelled:
            raise system_tts.SystemSpeechCancelled("cancelled")
        self.calls.append(text)
        if self.delay:
            time.sleep(self.delay)
        if self.fail_always or self.fail_times > 0:
            self.fail_times -= 1
            raise system_tts.SystemSpeechError("voice hiccup")
        return b"" if self.silent else _tone(0.05 + len(text) / 2000)

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


def _run_worker(qtbot, monkeypatch, synth, text, out, *, cancel_after=None):
    from app.workers import local_tts_worker

    monkeypatch.setattr(local_tts_worker, "make_synthesizer", lambda name: synth)
    monkeypatch.setattr(local_tts_worker.time, "sleep", lambda s: None)
    worker = local_tts_worker.LocalTTSWorker(text, "piper:en_US-fake-medium", "+0%", "+0%", str(out))
    result = {"progress": []}
    worker.completed.connect(lambda p, e: result.update(done=p))
    worker.failed.connect(lambda m: result.update(error=m))
    worker.progress.connect(result["progress"].append)
    if cancel_after is not None:
        def watch(v):
            if v >= cancel_after:
                worker.cancel()
        worker.progress.connect(watch)
    with qtbot.waitSignal(worker.finished, timeout=20_000):
        worker.start()
    qtbot.wait(20)
    return worker, result


def test_local_worker_writes_every_chunk_in_order(qtbot, monkeypatch, tmp_path):
    paragraphs = [f"Paragraph {i}. " + "Words go here and here. " * 30 for i in range(12)]
    text = "\n\n".join(paragraphs)
    synth = FakeSynth()
    out = tmp_path / "book.mp3"
    worker, result = _run_worker(qtbot, monkeypatch, synth, text, out)
    assert result.get("done") == str(out), result
    assert len(synth.calls) > 1
    joined = " ".join(synth.calls)
    assert [joined.index(f"Paragraph {i}.") for i in range(12)] == sorted(
        joined.index(f"Paragraph {i}.") for i in range(12))
    assert all(len(c) <= 1_200 for c in synth.calls)
    assert result["progress"][-1] == 100
    assert worker.audio_duration_seconds == pytest.approx(mp3_duration_seconds(out), abs=0.2)
    assert synth.closed


def test_local_worker_retries_a_failed_chunk(qtbot, monkeypatch, tmp_path):
    synth = FakeSynth(fail_times=2)
    _, result = _run_worker(qtbot, monkeypatch, synth, "Hello there. " * 10, tmp_path / "r.mp3")
    assert "done" in result and len(synth.calls) == 3


def test_local_worker_failure_keeps_the_previous_file(qtbot, monkeypatch, tmp_path):
    out = tmp_path / "keep.mp3"
    out.write_bytes(b"old audiobook")
    _, result = _run_worker(qtbot, monkeypatch, FakeSynth(fail_always=True), "Hi. " * 50, out)
    assert "error" in result and "done" not in result
    assert out.read_bytes() == b"old audiobook"
    assert not (tmp_path / "keep.mp3.part").exists()


def test_local_worker_rejects_an_all_silent_result(qtbot, monkeypatch, tmp_path):
    _, result = _run_worker(qtbot, monkeypatch, FakeSynth(silent=True), "Hello world.", tmp_path / "s.mp3")
    assert "language" in result.get("error", "")
    assert not (tmp_path / "s.mp3").exists()


def test_local_worker_cancel_stops_quickly_and_leaves_nothing(qtbot, monkeypatch, tmp_path):
    text = "\n\n".join("Sentence number one is here. " * 40 for _ in range(30))
    synth = FakeSynth(delay=0.01)
    out = tmp_path / "c.mp3"
    _, result = _run_worker(qtbot, monkeypatch, synth, text, out, cancel_after=20)
    assert "done" not in result and "error" not in result
    assert not out.exists() and not (tmp_path / "c.mp3.part").exists()
    assert synth.closed


def test_local_worker_reports_empty_text(qtbot, monkeypatch, tmp_path):
    _, result = _run_worker(qtbot, monkeypatch, FakeSynth(), "   \n\n  ", tmp_path / "e.mp3")
    assert "no readable text" in result.get("error", "")


def test_local_worker_reports_a_missing_voice(qtbot, monkeypatch, tmp_path):
    from app.workers import local_tts_worker

    def missing(name):
        raise piper_tts.PiperError("The offline voice “Fake” is not installed.")

    monkeypatch.setattr(local_tts_worker, "make_synthesizer", missing)
    worker = local_tts_worker.LocalTTSWorker("Hello.", "piper:x-fake-low", "+0%", "+0%",
                                             str(tmp_path / "m.mp3"))
    errors = []
    worker.failed.connect(errors.append)
    with qtbot.waitSignal(worker.finished, timeout=10_000):
        worker.start()
    qtbot.wait(20)
    assert errors and "not installed" in errors[0]


def test_job_queue_routes_local_voices_to_the_local_worker(qtbot, monkeypatch, tmp_path):
    from app.workers import job_queue, local_tts_worker

    started = []
    monkeypatch.setattr(local_tts_worker.LocalTTSWorker, "start", lambda self: started.append("local"))
    monkeypatch.setattr(job_queue.TTSWorker, "start", lambda self: started.append("online"))
    q = job_queue.JobQueue()
    q.submit(text="hi", voice="system:Alex", rate="+0%", volume="+0%",
             output_path=str(tmp_path / "a.mp3"), voice_display="Alex")
    q.submit(text="hi", voice="en-US-AvaNeural", rate="+0%", volume="+0%",
             output_path=str(tmp_path / "b.mp3"), voice_display="Ava")
    assert started == ["local", "online"]
    q.cancel_all()


# ------------------------------------------------------------------ #
# Piper catalog / download                                            #
# ------------------------------------------------------------------ #

def _catalog_entry(key, files):
    lang, name, quality = piper_tts._key_parts(key)
    return {
        "key": key, "name": name, "quality": quality,
        "language": {"code": lang, "family": lang.split("_")[0], "region": lang.split("_")[-1],
                     "name_english": "English", "country_english": "United States"},
        "files": files,
    }


def test_catalog_drops_unsupported_and_incomplete_voices():
    raw = {
        "en_US-amy-low": _catalog_entry("en_US-amy-low", {
            "en/en_US/amy/low/en_US-amy-low.onnx": {"size_bytes": 10, "md5_digest": "x"},
            "en/en_US/amy/low/en_US-amy-low.onnx.json": {"size_bytes": 2, "md5_digest": "y"},
            "en/en_US/amy/low/MODEL_CARD": {"size_bytes": 1, "md5_digest": "z"},
        }),
        "zh_CN-huayan-medium": _catalog_entry("zh_CN-huayan-medium", {
            "a.onnx": {}, "a.onnx.json": {}}),
        "en_US-broken-low": _catalog_entry("en_US-broken-low", {"b.onnx": {}}),
        "junk": {"nope": 1},
    }
    voices = piper_tts._parse_catalog(raw)
    assert [v.key for v in voices] == ["en_US-amy-low"]
    assert voices[0].size_bytes == 12 and voices[0].locale == "en-US"
    assert voices[0].gender == "Female"


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@pytest.fixture
def piper_user_dir(tmp_path, monkeypatch):
    d = tmp_path / "voices"
    d.mkdir()
    monkeypatch.setattr(piper_tts, "user_dir", lambda: d)
    monkeypatch.setattr(piper_tts, "bundled_dir", lambda: tmp_path / "bundled")
    return d


def _fake_voice(model=b"M" * 600_000, cfg=b'{"language": {"code": "en_US"}}', *, md5_ok=True):
    files = {
        "en/en_US/fake/low/en_US-fake-low.onnx":
            {"size_bytes": len(model), "md5_digest": hashlib.md5(model).hexdigest() if md5_ok else "0" * 32},
        "en/en_US/fake/low/en_US-fake-low.onnx.json":
            {"size_bytes": len(cfg), "md5_digest": hashlib.md5(cfg).hexdigest()},
    }
    voice = piper_tts.CatalogVoice("en_US-fake-low", "en-US", "English (US)", "low",
                                   len(model) + len(cfg), files)
    return voice, {".onnx": model, ".json": cfg}


def _serve(monkeypatch, bodies):
    def fake_open(url, timeout=30):
        return _FakeResp(bodies[".json"] if ".onnx.json" in url else bodies[".onnx"])
    monkeypatch.setattr(piper_tts, "_open", fake_open)


def test_download_verifies_and_installs(piper_user_dir, monkeypatch):
    voice, bodies = _fake_voice()
    _serve(monkeypatch, bodies)
    seen = []
    piper_tts.download_voice(voice, progress=lambda d, t: seen.append((d, t)))
    assert (piper_user_dir / "en_US-fake-low.onnx").read_bytes() == bodies[".onnx"]
    assert (piper_user_dir / "en_US-fake-low.onnx.json").exists()
    assert seen[-1][0] == seen[-1][1] == voice.size_bytes
    assert not list(piper_user_dir.glob("*.part"))
    assert "en_US-fake-low" in piper_tts.installed_keys()


def test_corrupt_download_installs_nothing(piper_user_dir, monkeypatch):
    voice, bodies = _fake_voice(md5_ok=False)
    _serve(monkeypatch, bodies)
    with pytest.raises(OSError, match="corrupted"):
        piper_tts.download_voice(voice)
    assert list(piper_user_dir.iterdir()) == []


def test_truncated_download_installs_nothing(piper_user_dir, monkeypatch):
    voice, bodies = _fake_voice()
    bodies[".onnx"] = bodies[".onnx"][:1000]
    _serve(monkeypatch, bodies)
    with pytest.raises(OSError, match="incomplete"):
        piper_tts.download_voice(voice)
    assert list(piper_user_dir.iterdir()) == []


def test_cancelled_download_installs_nothing(piper_user_dir, monkeypatch):
    voice, bodies = _fake_voice()
    _serve(monkeypatch, bodies)
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(piper_tts.DownloadCancelled):
        piper_tts.download_voice(voice, cancelled=cancelled)
    assert list(piper_user_dir.iterdir()) == []


def test_remove_voice_only_touches_downloads(piper_user_dir, monkeypatch):
    voice, bodies = _fake_voice()
    _serve(monkeypatch, bodies)
    piper_tts.download_voice(voice)
    assert piper_tts.remove_voice("en_US-fake-low")
    assert not piper_tts.remove_voice("en_US-fake-low")
    assert list(piper_user_dir.iterdir()) == []


def test_catalog_falls_back_to_cache_when_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "d"))
    from app.utils.paths import AppPaths
    cache = AppPaths().cache_dir / "piper_voices.json"
    raw = {"en_US-amy-low": _catalog_entry("en_US-amy-low", {
        "x/en_US-amy-low.onnx": {"size_bytes": 1}, "x/en_US-amy-low.onnx.json": {"size_bytes": 1}})}
    cache.write_text(json.dumps(raw))
    os.utime(cache, (0, 0))   # stale, so a refresh is attempted

    def offline(url, timeout=30):
        raise OSError("offline")

    monkeypatch.setattr(piper_tts, "_open", offline)
    assert [v.key for v in piper_tts.fetch_catalog()] == ["en_US-amy-low"]
    cache.unlink()
    with pytest.raises(OSError):
        piper_tts.fetch_catalog()


def test_long_phoneme_runs_are_capped_at_word_gaps():
    words = ["a", "b", "c", " "] * 200
    pieces = piper_tts._cap_phonemes(words, limit=50)
    assert all(len(p) <= 50 for p in pieces)
    assert all(p[0] != " " for p in pieces)
    # Nothing lost except the gaps it split at.
    assert sum(len(p) for p in pieces) >= len(words) - len(pieces)
    giant = ["x"] * 175
    assert [len(p) for p in piper_tts._cap_phonemes(giant, limit=50)] == [50, 50, 50, 25]
    assert piper_tts._cap_phonemes(["a"] * 10, limit=50) == [["a"] * 10]


@pytest.mark.real_local_voices
def test_real_piper_memory_stays_bounded_on_unpunctuated_text():
    if not piper_tts.piper_available() or piper_tts.model_path(piper_tts.BUNDLED_VOICE) is None:
        pytest.skip("bundled Piper voice not present")
    synth = piper_tts.PiperSynthesizer(piper_tts.PIPER_PREFIX + piper_tts.BUNDLED_VOICE)
    # Without the phoneme cap this one call held ~3 GB.
    pcm = synth.synthesize("1234567890 " * 60, "+0%")
    assert len(pcm) / 2 / synth.sample_rate > 30


def test_piper_voice_cache_is_bounded(monkeypatch, tmp_path):
    import types

    loads = []
    fake = types.SimpleNamespace(PiperVoice=types.SimpleNamespace(
        load=lambda path, download_dir=None: loads.append(path) or types.SimpleNamespace()))
    monkeypatch.setitem(sys.modules, "piper", fake)
    monkeypatch.setattr(piper_tts, "_LOADED", piper_tts.OrderedDict())
    monkeypatch.setattr(piper_tts, "user_dir", lambda: tmp_path)
    monkeypatch.setattr(piper_tts, "_MAX_LOADED", 2)
    for name in ("a", "b", "a", "c", "a", "b"):
        piper_tts._load(tmp_path / f"{name}.onnx")
    assert len(piper_tts._LOADED) == 2
    # "a" was kept hot by reuse; "b" was evicted by "c" and loaded again.
    assert [Path(p).stem for p in loads] == ["a", "b", "c", "b"]


def test_piper_synthesizer_reports_a_missing_model(piper_user_dir):
    with pytest.raises(piper_tts.PiperError, match="not installed"):
        piper_tts.PiperSynthesizer("piper:en_US-nothere-low")


# ------------------------------------------------------------------ #
# Voice list loading                                                  #
# ------------------------------------------------------------------ #

LOCAL = [
    {"ShortName": "piper:en_US-lessac-medium", "FriendlyName": "Lessac", "Locale": "en-US",
     "Gender": "Female", "Source": "piper"},
    {"ShortName": "system:Cortana", "FriendlyName": "Cortana", "Locale": "en-US",
     "Gender": "Female", "Source": "system"},
]


def _loader(monkeypatch, tmp_path, *, online):
    from app.workers import voice_loader

    async def fetch(*, force_refresh=False):
        if online is None:
            raise OSError("network down")
        return online

    monkeypatch.setattr(voice_loader, "list_voices", fetch)
    monkeypatch.setattr(voice_loader, "list_local_voices", lambda: LOCAL)
    monkeypatch.setattr(voice_loader, "_RETRY_DELAYS_S", (0, 0))
    worker = voice_loader.VoiceLoaderWorker(cache_path=tmp_path / "voices.json")
    loaded, failed = [], []
    worker.loaded.connect(loaded.append)
    worker.failed.connect(failed.append)
    worker.run()
    return worker, loaded, failed


def test_loader_merges_online_and_local_voices_but_caches_only_online(tmp_path, monkeypatch):
    online = [{"ShortName": "en-US-AvaNeural", "FriendlyName": "Ava", "Locale": "en-US", "Gender": "Female"}]
    worker, loaded, failed = _loader(monkeypatch, tmp_path, online=online)
    assert not failed
    assert {v.source for v in loaded[0]} == {"online", "piper", "system"}
    cached = json.loads((tmp_path / "voices.json").read_text())
    assert [c["ShortName"] for c in cached] == ["en-US-AvaNeural"]


def test_loader_offers_local_voices_with_no_internet_and_no_cache(tmp_path, monkeypatch):
    worker, loaded, failed = _loader(monkeypatch, tmp_path, online=None)
    assert not failed and worker.online_unavailable
    assert {v.short_name for v in loaded[0]} == {v["ShortName"] for v in LOCAL}


def test_loader_still_fails_plainly_with_nothing_at_all(tmp_path, monkeypatch):
    from app.workers import voice_loader

    async def fetch(*, force_refresh=False):
        raise OSError("network down")

    monkeypatch.setattr(voice_loader, "list_voices", fetch)
    monkeypatch.setattr(voice_loader, "_RETRY_DELAYS_S", (0, 0))
    worker = voice_loader.VoiceLoaderWorker(cache_path=tmp_path / "voices.json")
    failed = []
    worker.failed.connect(failed.append)
    worker.run()
    assert failed


# ------------------------------------------------------------------ #
# Voice picker                                                        #
# ------------------------------------------------------------------ #

@pytest.fixture
def window(qapp, tmp_path, monkeypatch, qtbot):
    from app.config.settings import AppSettings
    from app.ui.main_window import MainWindow
    from app.utils.paths import AppPaths
    from app.workers import voice_loader

    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(voice_loader.VoiceLoaderWorker, "start", lambda self: None)
    paths = AppPaths()
    settings = AppSettings(paths)
    settings.language_filter = ""
    win = MainWindow(settings=settings, paths=paths)
    qtbot.addWidget(win)
    win.show()
    yield win
    win.ensure_workers_stopped()


VOICES = [
    Voice("en-US-AvaNeural", "Ava", "en-US", "Female"),
    Voice("en-US-AndrewNeural", "Andrew", "en-US", "Male"),
    Voice("piper:en_US-lessac-medium", "Lessac", "en-US", "Female", source="piper"),
    Voice("system:Cortana", "Cortana", "en-US", "Female", source="system"),
    Voice("system:Hazel", "Hazel", "en-GB", "", source="system"),
]


def _rows(panel):
    combo = panel._voice_combo
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def test_picker_groups_voices_by_source_under_headers(window):
    panel = window._output_panel
    panel._on_voices_loaded(list(VOICES))
    texts = [t for t, _ in _rows(panel)]
    headers = [t for t, d in _rows(panel) if d is None]
    assert headers == ["── Microsoft Online ──", "── Offline Neural ──", "── Built into this computer ──"]
    assert texts.index("── Offline Neural ──") < next(i for i, t in enumerate(texts) if "Lessac" in t)
    assert any("Cortana" in t and "Built-in" in t for t in texts)
    # A header is never the selection.
    assert panel._voice_combo.itemData(panel._voice_combo.currentIndex())


def test_source_tabs_filter_the_list_and_persist(window, qtbot):
    panel = window._output_panel
    panel._on_voices_loaded(list(VOICES))
    panel._source_buttons["system"].click()
    qtbot.waitUntil(lambda: all(d is None or d.startswith("system:") for _, d in _rows(panel)), timeout=2000)
    assert {d for _, d in _rows(panel) if d} == {"system:Cortana", "system:Hazel"}
    assert panel._settings.source_filter == "system"
    # One source: no group headers.
    assert all(d for _, d in _rows(panel))
    panel._source_buttons[""].click()
    qtbot.waitUntil(lambda: len([d for _, d in _rows(panel) if d]) == len(VOICES), timeout=2000)


def test_recommended_voice_on_another_tab_is_still_selectable(window):
    panel = window._output_panel
    panel._on_voices_loaded(list(VOICES))
    panel._source_buttons["system"].click()
    panel._apply_filters()
    assert panel._select_voice_by_short_name("piper:en_US-lessac-medium")
    assert panel.get_selected_voice() == "piper:en_US-lessac-medium"
    assert panel._current_source_filter() == ""


def test_empty_offline_tab_points_to_get_voices(window):
    panel = window._output_panel
    panel._on_voices_loaded([v for v in VOICES if v.source != "piper"])
    panel._source_buttons["piper"].click()
    panel._apply_filters()
    assert "Get voices" in panel._voice_combo.itemText(0)
    assert not panel._has_visible_voice()


def test_unknown_gender_voices_show_under_all_only(window, qtbot):
    panel = window._output_panel
    panel._on_voices_loaded(list(VOICES))
    panel._gender_combo.setCurrentText("Female")
    qtbot.waitUntil(lambda: "system:Hazel" not in [d for _, d in _rows(panel)], timeout=2000)
    assert "Hazel" not in " ".join(t for t, _ in _rows(panel))


def test_saved_local_voice_is_restored(window):
    panel = window._output_panel
    panel._settings.voice = "system:Cortana"
    panel._on_voices_loaded(list(VOICES))
    assert panel.get_selected_voice() == "system:Cortana"
    assert "works offline" in panel._voice_combo.toolTip()


def test_offline_mode_explains_itself_and_leaves_online_filter(window):
    panel = window._output_panel
    panel._source_buttons["online"].setChecked(True)
    panel._settings.source_filter = "online"

    class Loader:
        from_cache = False
        online_unavailable = True

    panel._voice_loader = Loader()
    panel._on_voices_loaded([v for v in VOICES if v.is_local])
    assert "No internet" in panel._voice_error_label.text()
    assert panel._current_source_filter() == ""
    assert panel._has_visible_voice()


def test_job_label_for_local_voices(window):
    from app.ui.panels.output_panel import _job_voice_display

    assert _job_voice_display("piper:en_US-lessac-medium") == "Lessac · Offline"
    assert _job_voice_display("system:Cortana").endswith("· Built-in")


def test_voice_manager_dialog_lists_and_marks_installed(qapp, qtbot, monkeypatch, piper_user_dir):
    from app.ui.dialogs import voice_manager_dialog as vmd

    voice, bodies = _fake_voice()
    monkeypatch.setattr(vmd._CatalogLoader, "start", lambda self: None)
    dialog = vmd.VoiceManagerDialog()
    qtbot.addWidget(dialog)
    dialog._on_catalog([voice])
    assert dialog._table.rowCount() == 1
    btn = dialog._table.cellWidget(0, vmd._COL_ACTION)
    assert btn.text() == "Download" and btn.isEnabled()

    _serve(monkeypatch, bodies)
    btn.click()
    qtbot.waitUntil(lambda: dialog.changed, timeout=10_000)
    assert dialog.last_installed == "piper:en_US-fake-low"
    assert dialog._table.cellWidget(0, vmd._COL_ACTION).text() == "Remove"

    monkeypatch.setattr(vmd.QMessageBox, "question",
                        lambda *a, **k: vmd.QMessageBox.StandardButton.Yes)
    dialog._table.cellWidget(0, vmd._COL_ACTION).click()
    assert dialog.last_installed is None
    assert dialog._table.cellWidget(0, vmd._COL_ACTION).text() == "Download"
    dialog._search.setText("zzz-no-match")
    qtbot.waitUntil(lambda: dialog._table.rowCount() == 0, timeout=2000)


# ------------------------------------------------------------------ #
# Real engines on this machine                                        #
# ------------------------------------------------------------------ #

@pytest.mark.real_local_voices
def test_real_system_voices_speak(tmp_path):
    voices = system_tts.list_system_voices()
    if not voices:
        pytest.skip("no system voices on this machine")
    synth = system_tts.SystemSynthesizer(voices[0]["ShortName"])
    try:
        pcm = synth.synthesize("Hello <b>world</b> & friends.", "+0%")
        assert len(pcm) / 2 / system_tts.SAMPLE_RATE > 0.5
        # The helper process is reused between calls.
        assert len(synth.synthesize("Second call.", "+50%")) > 0
    finally:
        synth.close()


@pytest.mark.real_local_voices
def test_real_system_voice_cancel_is_prompt():
    voices = system_tts.list_system_voices()
    if not voices:
        pytest.skip("no system voices on this machine")
    synth = system_tts.SystemSynthesizer(voices[0]["ShortName"])
    errors = []

    def speak():
        try:
            synth.synthesize("This is a long passage. " * 400, "-50%")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=speak)
    t.start()
    time.sleep(1.0)
    start = time.monotonic()
    synth.cancel()
    t.join(10)
    synth.close()
    assert not t.is_alive() and time.monotonic() - start < 5
    assert errors and isinstance(errors[0], system_tts.SystemSpeechCancelled)


@pytest.mark.real_local_voices
def test_real_bundled_piper_voice_speaks():
    if not piper_tts.piper_available() or piper_tts.model_path(piper_tts.BUNDLED_VOICE) is None:
        pytest.skip("bundled Piper voice not present")
    synth = piper_tts.PiperSynthesizer(piper_tts.PIPER_PREFIX + piper_tts.BUNDLED_VOICE)
    pcm = synth.synthesize("Testing one two three. Second sentence!", "+0%")
    assert len(pcm) / 2 / synth.sample_rate > 1.0
    fast = synth.synthesize("Testing one two three. Second sentence!", "+100%")
    assert len(fast) < len(pcm) * 0.8


@pytest.mark.real_local_voices
def test_real_piper_is_safe_from_two_threads():
    if not piper_tts.piper_available() or piper_tts.model_path(piper_tts.BUNDLED_VOICE) is None:
        pytest.skip("bundled Piper voice not present")
    out, errors = [], []

    def go(i):
        try:
            s = piper_tts.PiperSynthesizer(piper_tts.PIPER_PREFIX + piper_tts.BUNDLED_VOICE)
            for _ in range(3):
                out.append(len(s.synthesize(f"Thread {i} says hello. And again.", "+0%")))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not errors and len(out) == 9 and all(n > 0 for n in out)
