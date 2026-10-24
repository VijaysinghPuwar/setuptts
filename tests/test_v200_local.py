"""
v2.0.0 regressions for voices that run on this computer: the volume setting,
the MP3 writer, Piper text/rate handling and downloads, the system-voice
helper, and the start-up sweep of files left by killed runs.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import sys
import time
import types
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.services import local_tts, piper_tts, system_tts  # noqa: E402
from app.services.local_tts import Mp3Writer  # noqa: E402

DAY = 24 * 3600


def _tone(seconds: float, rate: int = 22_050, amp: int = 8000) -> bytes:
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *(int(amp * math.sin(i / 8)) for i in range(n)))


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


# ── 1. Volume for local voices ──────────────────────────────────────────── #

@pytest.mark.parametrize("volume, gain", [
    ("+0%", 1.0), ("-20%", 0.8), ("+50%", 1.5), ("-50%", 0.5), ("", 1.0), ("loud", 1.0),
    ("-200%", 0.0),
])
def test_volume_string_maps_to_gain(volume, gain):
    assert local_tts.volume_gain(volume) == pytest.approx(gain)


def test_gain_scales_and_clips_without_wrapping():
    import numpy as np
    pcm = np.array([1000, -1000, 30000, -30000], dtype="<i2").tobytes()
    half = np.frombuffer(local_tts.apply_gain(pcm, 0.5), dtype="<i2")
    assert list(half) == [500, -500, 15000, -15000]
    loud = np.frombuffer(local_tts.apply_gain(pcm, 1.5), dtype="<i2")
    assert list(loud) == [1500, -1500, 32767, -32768]
    assert local_tts.apply_gain(pcm, 1.0) is pcm


def test_zero_volume_writer_is_byte_identical(tmp_path):
    pcm = _tone(0.5)
    plain = Mp3Writer(tmp_path / "a.mp3", 22_050)
    plain.write(pcm)
    same = Mp3Writer(tmp_path / "b.mp3", 22_050, gain=local_tts.volume_gain("+0%"))
    same.write(pcm)
    quiet = Mp3Writer(tmp_path / "c.mp3", 22_050, gain=local_tts.volume_gain("-50%"))
    quiet.write(pcm)
    a, b, c = (w.finish().read_bytes() for w in (plain, same, quiet))
    assert a == b
    assert a != c


def test_local_worker_applies_the_volume_setting(qtbot, monkeypatch, tmp_path):
    from app.workers import local_tts_worker

    gains = []

    class RecordingWriter(Mp3Writer):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            gains.append(self._gain)

    class Synth:
        sample_rate = 22_050

        def synthesize(self, text, rate):
            return _tone(0.1)

        def cancel(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(local_tts_worker, "make_synthesizer", lambda name: Synth())
    monkeypatch.setattr(local_tts_worker, "Mp3Writer", RecordingWriter)
    worker = local_tts_worker.LocalTTSWorker(
        "Hello there.", "piper:en_US-fake-medium", "+0%", "-30%", str(tmp_path / "v.mp3"))
    done = {}
    worker.completed.connect(lambda p, e: done.update(path=p))
    with qtbot.waitSignal(worker.finished, timeout=20_000):
        worker.start()
    qtbot.wait(20)
    assert done.get("path")
    assert gains == [pytest.approx(0.7)]


# ── 2. Mp3Writer set-up failure leaks nothing ───────────────────────────── #

def test_writer_setup_failure_leaves_no_part_file(tmp_path, monkeypatch):
    class BadEncoder:
        def set_bit_rate(self, v):
            pass

        def set_in_sample_rate(self, v):
            raise RuntimeError("unsupported sample rate")

    monkeypatch.setitem(sys.modules, "lameenc", types.SimpleNamespace(Encoder=BadEncoder))
    with pytest.raises(RuntimeError, match="unsupported"):
        Mp3Writer(tmp_path / "x.mp3", 1)
    assert list(tmp_path.iterdir()) == []


# ── 3/4. Piper: "[[" is text, and the rate is capped ────────────────────── #

class _RecordingVoice:
    def __init__(self):
        self.config = types.SimpleNamespace(sample_rate=1000, length_scale=1.0)
        import threading
        self.setuptts_run_lock = threading.BoundedSemaphore(1)
        self.texts: list[str] = []
        self.scales: list[float] = []

    def phonemize(self, text):
        self.texts.append(text)
        return [["a", "b"]]

    def phonemes_to_ids(self, phonemes):
        return [1, 2]

    def phoneme_ids_to_audio(self, ids, cfg):
        import numpy as np
        self.scales.append(cfg.length_scale)
        return np.full(10, 0.5, dtype=np.float32)


def _synth(voice):
    synth = object.__new__(piper_tts.PiperSynthesizer)
    synth._voice = voice
    synth.sample_rate = 1000
    synth._run_lock = voice.setuptts_run_lock
    synth._cancelled = False
    synth._forced_device = True
    synth._pool = None
    return synth


def test_piper_never_sees_inline_phoneme_brackets():
    pytest.importorskip("piper")
    voice = _RecordingVoice()
    _synth(voice).synthesize("See [[Paris]] and [[[odd]]] text.", "+0%")
    assert voice.texts and "[[" not in voice.texts[0]
    assert "Paris" in voice.texts[0] and "odd" in voice.texts[0]
    # Plain text is passed through untouched.
    assert piper_tts._plain_text("A [single] bracket.") == "A [single] bracket."


def test_piper_inline_brackets_are_read_as_words():
    from piper import voice as piper_voice
    # The real splitter no longer finds a raw-phoneme block.
    parts = piper_voice._PHONEME_BLOCK_PATTERN.split(piper_tts._plain_text("x [[Paris]] y"))
    assert not any(p.startswith("[[") for p in parts)


@pytest.mark.parametrize("rate, speed", [
    ("+0%", 1.0), ("+100%", 2.0), ("-50%", 0.5), ("+900%", 2.0), ("-99%", 0.25), ("x", 1.0),
])
def test_piper_speed_is_bounded(rate, speed):
    assert piper_tts._speed(rate) == pytest.approx(speed)


def test_piper_length_scale_capped_for_huge_rate():
    pytest.importorskip("piper")
    voice = _RecordingVoice()
    _synth(voice).synthesize("Hello.", "+900%")
    assert voice.scales == [pytest.approx(0.5)]


# ── 5. System voice helper ──────────────────────────────────────────────── #

class _FakeProc:
    def __init__(self):
        self.stdin = types.SimpleNamespace(write=lambda b: None, flush=lambda: None,
                                           close=lambda: None)
        self.killed = False
        self.waited = 0

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited += 1
        return 0


@pytest.fixture
def sys_synth():
    synth = system_tts.SystemSynthesizer("system:TestVoice")
    yield synth
    synth.close()


def test_helper_death_mid_speech_is_retried_once(sys_synth, monkeypatch):
    procs = []

    def ensure():
        procs.append(_FakeProc())
        sys_synth._proc = procs[-1]
        return procs[-1]

    replies = iter([system_tts._HelperExited("exited"), "OK"])

    def readline(proc, timeout):
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(sys_synth, "_ensure_helper", ensure)
    monkeypatch.setattr(sys_synth, "_readline", readline)
    sys_synth._speak_windows(Path("t.txt"), Path("o.wav"), "+0%")
    assert len(procs) == 2 and procs[0].killed


def test_helper_dying_twice_reports_the_exit(sys_synth, monkeypatch):
    monkeypatch.setattr(sys_synth, "_ensure_helper", lambda: _FakeProc())

    def readline(proc, timeout):
        raise system_tts._HelperExited("The system speech helper exited unexpectedly")

    monkeypatch.setattr(sys_synth, "_readline", readline)
    with pytest.raises(system_tts.SystemSpeechError, match="exited unexpectedly"):
        sys_synth._speak_windows(Path("t.txt"), Path("o.wav"), "+0%")


def test_helper_timeout_is_not_retried(sys_synth, monkeypatch):
    calls = []

    def ensure():
        calls.append(1)
        return _FakeProc()

    def readline(proc, timeout):
        raise system_tts.SystemSpeechError("The system voice stopped responding")

    monkeypatch.setattr(sys_synth, "_ensure_helper", ensure)
    monkeypatch.setattr(sys_synth, "_readline", readline)
    with pytest.raises(system_tts.SystemSpeechError, match="stopped responding"):
        sys_synth._speak_windows(Path("t.txt"), Path("o.wav"), "+0%")
    assert len(calls) == 1


def test_lone_surrogates_do_not_fail_the_chunk(sys_synth, monkeypatch):
    import wave
    seen = []

    def speak(text_path, wav_path, rate):
        seen.append(text_path.read_text(encoding="utf-8"))
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(system_tts.SAMPLE_RATE)
            wf.writeframes(_tone(0.05))

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys_synth, "_speak_windows", speak)
    pcm = sys_synth.synthesize("Broken \ud800 text.", "+0%")
    assert pcm and seen == ["Broken ? text."]


def test_cancel_kills_without_waiting(sys_synth):
    proc = _FakeProc()
    sys_synth._proc = proc
    sys_synth.cancel()
    assert proc.killed and proc.waited == 0
    sys_synth.close()
    assert proc.waited == 1


# ── 6. Start-up sweep of stale files ────────────────────────────────────── #

def test_sweep_removes_only_old_system_temp_dirs(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    old = tmp_path / "setuptts_sys_old"
    old.mkdir()
    (old / "in_1.txt").write_text("book text")
    _age(old, 2 * DAY)
    fresh = tmp_path / "setuptts_sys_new"
    fresh.mkdir()
    other = tmp_path / "unrelated_dir"
    other.mkdir()
    _age(other, 2 * DAY)
    assert system_tts.sweep_stale_temp_dirs(DAY) == 1
    assert not old.exists() and fresh.exists() and other.exists()


def test_sweep_removes_only_old_voice_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(piper_tts, "user_dir", lambda: tmp_path)
    old_legacy = tmp_path / "v.onnx.part"
    old_tagged = tmp_path / "v.onnx.0123456789ab.part"
    fresh = tmp_path / "w.onnx.abcdef012345.part"
    model = tmp_path / "v.onnx"
    for p in (old_legacy, old_tagged, fresh, model):
        p.write_bytes(b"x")
    for p in (old_legacy, old_tagged, model):
        _age(p, 2 * DAY)
    assert piper_tts.sweep_stale_parts(DAY) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["v.onnx", "w.onnx.abcdef012345.part"]


def test_sweep_never_raises(monkeypatch):
    def boom(_age):
        raise OSError("disk gone")
    monkeypatch.setattr(system_tts, "sweep_stale_temp_dirs", boom)
    monkeypatch.setattr(piper_tts, "sweep_stale_parts", boom)
    local_tts.sweep_stale_files()


# ── 7. Concurrent downloads of one voice ────────────────────────────────── #

class _Resp:
    def __init__(self, body, on_read=None):
        self._body, self._pos, self._on_read = body, 0, on_read

    def read(self, n):
        if self._on_read:
            cb, self._on_read = self._on_read, None
            cb()
        block = self._body[self._pos:self._pos + n]
        self._pos += len(block)
        return block

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _catalog_voice(model, cfg, *, md5_ok=True):
    files = {
        "x/en_US-fake-low.onnx": {"size_bytes": len(model),
                                  "md5_digest": hashlib.md5(model).hexdigest() if md5_ok else "0" * 32},
        "x/en_US-fake-low.onnx.json": {"size_bytes": len(cfg),
                                       "md5_digest": hashlib.md5(cfg).hexdigest()},
    }
    return piper_tts.CatalogVoice("en_US-fake-low", "en-US", "English (US)", "low",
                                  len(model) + len(cfg), files)


def test_failed_download_does_not_delete_another_downloads_files(tmp_path, monkeypatch):
    monkeypatch.setattr(piper_tts, "user_dir", lambda: tmp_path)
    model, cfg = b"M" * 300_000, b'{"language": {"code": "en_US"}}'
    good = _catalog_voice(model, cfg)
    bad = _catalog_voice(model, cfg, md5_ok=False)
    state = {"nested": False}

    def other_download():
        # A second download of the same voice fails while the first has
        # its config staged and is fetching the model.
        state["nested"] = True
        with pytest.raises(OSError, match="corrupted"):
            piper_tts.download_voice(bad)

    def fake_open(url, timeout=30):
        if ".onnx.json" in url:
            return _Resp(cfg)
        return _Resp(model, None if state["nested"] else other_download)

    monkeypatch.setattr(piper_tts, "_open", fake_open)
    piper_tts.download_voice(good)
    assert (tmp_path / "en_US-fake-low.onnx").read_bytes() == model
    assert (tmp_path / "en_US-fake-low.onnx.json").read_bytes() == cfg
    assert not list(tmp_path.glob("*.part"))


# ── 8. Corrupt catalog cache ────────────────────────────────────────────── #

def test_corrupt_catalog_cache_keeps_the_network_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "d"))
    from app.utils.paths import AppPaths
    cache = AppPaths().cache_dir / "piper_voices.json"
    cache.write_text("{not json")
    os.utime(cache, (0, 0))

    def offline(url, timeout=30):
        raise OSError("offline")

    monkeypatch.setattr(piper_tts, "_open", offline)
    with pytest.raises(OSError, match="offline"):
        piper_tts.fetch_catalog()


def test_good_catalog_cache_still_used_when_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "d"))
    from app.utils.paths import AppPaths
    cache = AppPaths().cache_dir / "piper_voices.json"
    cache.write_text(json.dumps({}))
    os.utime(cache, (0, 0))
    monkeypatch.setattr(piper_tts, "_open", lambda url, timeout=30: (_ for _ in ()).throw(OSError("x")))
    assert piper_tts.fetch_catalog() == []
