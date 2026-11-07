"""Audiobook compression: settings, the re-encode itself, and both workers."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.services import compression
from app.services.local_tts import Mp3Writer
from app.utils.mp3_duration import mp3_duration_seconds

RATE = 24_000


def _speechlike(seconds: float, rate: int = RATE) -> bytes:
    """Voiced, syllable-paced audio with pauses: closer to narration than a tone."""
    rng = np.random.default_rng(7)
    t = np.arange(int(seconds * rate)) / rate
    pitch = 140 + 30 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / rate
    voiced = sum(np.sin(k * phase) / k for k in range(1, 12))
    syllables = np.clip(np.sin(2 * np.pi * 4 * t), 0, None) ** 2
    pauses = (np.sin(2 * np.pi * 0.25 * t) > -0.7).astype(float)
    noise = rng.normal(0, 0.05, t.size)
    signal = (voiced * syllables + noise * syllables) * pauses
    signal = signal / np.max(np.abs(signal)) * 12_000
    return signal.astype("<i2").tobytes()


def _make_mp3(path: Path, seconds: float = 20.0, kbps: int = 48) -> Path:
    writer = Mp3Writer(path, RATE, kbps)
    writer.write(_speechlike(seconds))
    return writer.finish()


def _decode(path: Path, rate: int = RATE) -> np.ndarray:
    import miniaudio

    decoded = miniaudio.decode_file(str(path), nchannels=1, sample_rate=rate)
    return np.frombuffer(decoded.samples, dtype="<i2").astype(np.float64)


def _similarity(a: np.ndarray, b: np.ndarray, max_lag: int = 4_000) -> float:
    """Best normalised correlation of *b* against *a* (encoder delay aligned).

    A coarse search finds the encoder delay, then a sample-exact one: being
    a few samples off reads as a quality loss that isn't there.
    """
    n = min(a.size, b.size) - 2 * max_lag
    ref = a[max_lag:max_lag + n]
    ref = ref - ref.mean()
    ref_norm = np.linalg.norm(ref)

    def corr(lag: int) -> float:
        seg = b[lag:lag + n]
        seg = seg - seg.mean()
        denom = ref_norm * np.linalg.norm(seg)
        return float(ref @ seg / denom) if denom else 0.0

    coarse = max(range(0, 2 * max_lag, 8), key=corr)
    return max(corr(lag) for lag in range(max(0, coarse - 8), coarse + 9))


# ------------------------------------------------------------------ #
# Levels and settings                                                  #
# ------------------------------------------------------------------ #

def test_three_levels_plus_off_with_falling_bitrates():
    assert compression.LEVELS == ("off", "light", "balanced", "smallest")
    rates = [compression.profile_for(lvl).bitrate_kbps for lvl in compression.LEVELS[1:]]
    assert rates == sorted(rates, reverse=True)
    assert all(r < 48 for r in rates), "every level must shrink online audio"
    assert compression.profile_for("off") is None
    assert compression.profile_for(None) is None
    assert compression.profile_for("bogus") is None


def test_normalize_level_rejects_junk():
    assert compression.normalize_level("light") == "light"
    assert compression.normalize_level("off") == "off"
    for junk in ("", "LIGHT", 3, None, ["balanced"]):
        assert compression.normalize_level(junk) == compression.DEFAULT_LEVEL


def test_local_bitrate_never_exceeds_native():
    assert compression.local_bitrate_kbps("off", 64) == 64
    assert compression.local_bitrate_kbps("balanced", 64) == 32
    assert compression.local_bitrate_kbps("smallest", 64) == 24
    assert compression.local_bitrate_kbps("light", 32) == 32


def _settings(tmp_path):
    from app.config.settings import AppSettings

    class Paths:
        settings_path = tmp_path / "settings.json"

    return AppSettings(Paths())


def test_compression_is_on_by_default_at_the_light_level(tmp_path):
    # Light, not Balanced: stronger levels re-encode the already-compressed
    # online audio hard enough to sound echoey (v2.2.1).
    assert _settings(tmp_path).compression == compression.DEFAULT_LEVEL == "light"


def _write_settings(tmp_path, data: dict) -> None:
    (tmp_path / "settings.json").write_text(json.dumps(data))


def test_untouched_v220_default_moves_to_light(tmp_path):
    # v2.2.0 saved its default ("balanced") like any other value.
    _write_settings(tmp_path, {"compression": "balanced", "rate": 12})
    s = _settings(tmp_path)
    assert s.compression == "light"
    assert s.rate == 12
    s.save()
    stored = json.loads((tmp_path / "settings.json").read_text())
    assert stored["compression"] == "light"
    assert stored["compression_user_set"] is False


@pytest.mark.parametrize("level", ["off", "light", "smallest"])
def test_other_saved_levels_are_kept(tmp_path, level):
    # Anything other than the old default was a deliberate choice.
    _write_settings(tmp_path, {"compression": level})
    assert _settings(tmp_path).compression == level


def test_a_level_the_user_picked_is_never_migrated(tmp_path):
    _write_settings(tmp_path, {"compression": "balanced", "compression_user_set": True})
    assert _settings(tmp_path).compression == "balanced"


def test_choosing_a_level_in_settings_marks_it_as_the_users(qtbot, tmp_path):
    from app.ui.dialogs.settings_dialog import SettingsDialog
    from app.utils.paths import AppPaths

    settings = _settings(tmp_path)
    dialog = SettingsDialog(settings, AppPaths())
    qtbot.addWidget(dialog)
    dialog._save()          # saving without touching the level ...
    assert _settings(tmp_path).compression_user_set is False   # ... is not a choice

    settings = _settings(tmp_path)
    dialog = SettingsDialog(settings, AppPaths())
    qtbot.addWidget(dialog)
    dialog._compression_combo.setCurrentIndex(dialog._compression_combo.findData("balanced"))
    dialog._save()
    reloaded = _settings(tmp_path)
    assert reloaded.compression == "balanced"
    assert reloaded.compression_user_set is True


def test_compression_setting_round_trips_and_survives_bad_values(tmp_path):
    s = _settings(tmp_path)
    s.compression = "smallest"
    s.save()
    assert _settings(tmp_path).compression == "smallest"
    s.compression = "off"
    s.save()
    assert _settings(tmp_path).compression == "off"

    (tmp_path / "settings.json").write_text(json.dumps({"compression": 42, "rate": 9}))
    loaded = _settings(tmp_path)
    assert loaded.compression == "light"
    assert loaded.rate == 9, "a bad compression value must not reset other settings"
    (tmp_path / "settings.json").write_text(json.dumps({"compression": "ultra"}))
    assert _settings(tmp_path).compression == "light"


# ------------------------------------------------------------------ #
# The re-encode                                                        #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("level", ["light", "balanced", "smallest"])
def test_compress_shrinks_keeps_length_and_sound(tmp_path, level):
    src = _make_mp3(tmp_path / "book.mp3", seconds=20)
    original = _decode(src)
    before_bytes = src.stat().st_size
    before_len = mp3_duration_seconds(src)
    progress = []

    result = compression.compress_mp3_in_place(
        src, level, source_bitrate_kbps=48, on_progress=progress.append)

    assert result is not None
    profile = compression.profile_for(level)
    after_bytes = src.stat().st_size
    assert after_bytes == result.compressed_bytes < before_bytes
    expected_ratio = profile.bitrate_kbps / 48
    assert after_bytes / before_bytes == pytest.approx(expected_ratio, abs=0.06)
    assert mp3_duration_seconds(src) == pytest.approx(before_len, abs=0.15)
    assert progress and progress[-1] == 100 and progress == sorted(progress)
    assert not list(tmp_path.glob("*.part")), "no temporary file may be left behind"
    # Not just smaller: it must still be the same audio.
    assert _similarity(original, _decode(src)) > 0.95


def _band_db(x: np.ndarray, lo: float, hi: float, rate: int = RATE) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(x.size))) ** 2
    freqs = np.fft.rfftfreq(x.size, 1 / rate)
    return 10 * np.log10(spectrum[(freqs >= lo) & (freqs < hi)].sum() + 1e-12)


def test_default_level_keeps_the_sample_rate_and_the_treble(tmp_path):
    # The echoey sound of v2.2.0's default came from re-encoding the 48 kbit/s
    # online audio at 32 kbit/s: the encoder resamples to 22 kHz and rolls
    # off everything above ~7 kHz.  The default must keep 24 kHz and the
    # speech band intact.
    import miniaudio

    src = _make_mp3(tmp_path / "book.mp3", seconds=20)
    original = _decode(src)
    assert compression.compress_mp3_in_place(
        src, compression.DEFAULT_LEVEL, source_bitrate_kbps=48) is not None
    assert miniaudio.mp3_get_file_info(str(src)).sample_rate == RATE
    compressed = _decode(src)
    n = min(original.size, compressed.size)
    for lo, hi in [(0, 4_000), (4_000, 7_000), (7_000, 10_000)]:
        assert _band_db(compressed[:n], lo, hi) == pytest.approx(
            _band_db(original[:n], lo, hi), abs=1.5), f"{lo}-{hi} Hz band changed"
    assert _similarity(original, compressed) > 0.98


def test_off_and_already_small_files_are_left_alone(tmp_path):
    src = _make_mp3(tmp_path / "a.mp3", seconds=3)
    data = src.read_bytes()
    assert compression.compress_mp3_in_place(src, "off", source_bitrate_kbps=48) is None
    assert compression.compress_mp3_in_place(src, "balanced", source_bitrate_kbps=32) is None
    assert src.read_bytes() == data


def test_cancel_keeps_the_original_untouched(tmp_path):
    src = _make_mp3(tmp_path / "book.mp3", seconds=12)
    data = src.read_bytes()
    calls = {"n": 0}

    def should_cancel():
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(compression.CompressionCancelled):
        compression.compress_mp3_in_place(src, "balanced", source_bitrate_kbps=48,
                                          should_cancel=should_cancel)
    assert src.read_bytes() == data
    assert not list(tmp_path.glob("*.part"))


def test_unreadable_file_is_kept_as_is(tmp_path):
    src = tmp_path / "junk.mp3"
    src.write_bytes(b"not an mp3 at all" * 500)
    data = src.read_bytes()
    assert compression.compress_mp3_in_place(src, "balanced", source_bitrate_kbps=48) is None
    assert src.read_bytes() == data
    assert not list(tmp_path.glob("*.part"))


def test_locked_target_keeps_the_original(tmp_path, monkeypatch):
    src = _make_mp3(tmp_path / "book.mp3", seconds=3)
    data = src.read_bytes()

    def locked(a, b, attempts=5):
        raise PermissionError("in use by a media player")

    monkeypatch.setattr(compression, "replace_with_retry", locked)
    assert compression.compress_mp3_in_place(src, "balanced", source_bitrate_kbps=48) is None
    assert src.read_bytes() == data
    assert not list(tmp_path.glob("*.part"))


def test_length_mismatch_is_rejected(tmp_path, monkeypatch):
    src = _make_mp3(tmp_path / "book.mp3", seconds=5)
    data = src.read_bytes()
    real = compression.mp3_duration_seconds

    def short_after_encode(path):
        seconds = real(path)
        return seconds / 2 if str(path).endswith(".part") else seconds

    monkeypatch.setattr(compression, "mp3_duration_seconds", short_after_encode)
    assert compression.compress_mp3_in_place(src, "balanced", source_bitrate_kbps=48) is None
    assert src.read_bytes() == data
    assert not list(tmp_path.glob("*.part"))


def test_verify_rules():
    assert compression._verify(100, 50, 10.0, 10.05) == ""
    assert "not smaller" in compression._verify(100, 100, 10.0, 10.0)
    assert "empty" in compression._verify(100, 0, 10.0, 10.0)
    assert "unreadable" in compression._verify(100, 50, 10.0, None)
    assert "length" in compression._verify(100, 50, 3600.0, 3590.0)
    # Long books get a proportional tolerance (encoder padding adds up).
    assert compression._verify(100, 50, 36_000.0, 36_000.0 + 5) == ""


# ------------------------------------------------------------------ #
# Workers                                                              #
# ------------------------------------------------------------------ #

class _ToneSynth:
    sample_rate = 22_050

    def synthesize(self, text, rate):
        return _speechlike(0.5 + len(text) / 200, self.sample_rate)

    def cancel(self):
        pass

    def close(self):
        pass


@pytest.mark.parametrize("level,kbps", [("off", 64), ("balanced", 32), ("smallest", 24)])
def test_local_worker_encodes_at_the_compression_bitrate(qtbot, monkeypatch, tmp_path, level, kbps):
    from app.workers import local_tts_worker

    monkeypatch.setattr(local_tts_worker, "make_synthesizer", lambda name: _ToneSynth())
    out = tmp_path / f"{level}.mp3"
    worker = local_tts_worker.LocalTTSWorker(
        "A sentence to read aloud. " * 40, "piper:en_US-fake-medium", "+0%", "+0%",
        str(out), compression=level)
    done = {}
    worker.completed.connect(lambda p, e: done.update(path=p))
    worker.failed.connect(lambda m: done.update(error=m))
    with qtbot.waitSignal(worker.finished, timeout=30_000):
        worker.start()
    qtbot.wait(20)
    assert done.get("path") == str(out), done
    seconds = mp3_duration_seconds(out)
    measured_kbps = out.stat().st_size * 8 / seconds / 1000
    assert measured_kbps == pytest.approx(kbps, rel=0.08)


def _online_worker(tmp_path, level, out):
    from app.workers.tts_worker import TTSWorker

    return TTSWorker("Hello there.", "en-US-AvaNeural", "+0%", "+0%", str(out),
                     job_id="cmp", compression=level)


def _fake_generation(worker, out, seconds=8.0):
    """Replace the network pipeline with one that writes a verified 48 kbit/s file."""
    async def fake_run():
        _make_mp3(out, seconds=seconds, kbps=48)
        worker._output_written = True

    worker._run_with_task = fake_run


@pytest.mark.parametrize("level", ["off", "balanced"])
def test_online_worker_compresses_the_finished_file(qtbot, tmp_path, level):
    out = tmp_path / "online.mp3"
    worker = _online_worker(tmp_path, level, out)
    _fake_generation(worker, out)
    done, stages = {}, []
    worker.completed.connect(lambda p, e: done.update(path=p))
    worker.failed.connect(lambda m: done.update(error=m))
    worker.stage_changed.connect(lambda k, t: stages.append(t))
    worker._run_job()
    assert done.get("path") == str(out), done
    kbps = out.stat().st_size * 8 / mp3_duration_seconds(out) / 1000
    if level == "off":
        assert kbps == pytest.approx(48, rel=0.05)
        assert not any("Compress" in s for s in stages)
    else:
        assert kbps == pytest.approx(32, rel=0.08)
        assert any("compressed" in s and "smaller" in s for s in stages), stages


def test_online_worker_cancel_during_compression_keeps_full_file(qtbot, tmp_path, monkeypatch):
    out = tmp_path / "online.mp3"
    worker = _online_worker(tmp_path, "balanced", out)
    _fake_generation(worker, out, seconds=12)
    snapshot = {}
    real = compression._transcode

    def cancel_midway(src, dst, kbps, duration, should_cancel, on_progress):
        snapshot["data"] = Path(src).read_bytes()

        def progress(pct):
            on_progress(pct)
            if pct >= 20:
                worker.cancel()
        return real(src, dst, kbps, duration, should_cancel, progress)

    monkeypatch.setattr(compression, "_transcode", cancel_midway)
    done = {}
    worker.completed.connect(lambda p, e: done.update(path=p))
    worker.failed.connect(lambda m: done.update(error=m))
    worker._run_job()
    # The finished book is reported and kept whole, just not compressed.
    assert done.get("path") == str(out), done
    assert out.read_bytes() == snapshot["data"]
    assert not list(tmp_path.glob("*.part"))


def test_online_worker_never_fails_a_job_over_compression(qtbot, tmp_path, monkeypatch):
    out = tmp_path / "online.mp3"
    worker = _online_worker(tmp_path, "smallest", out)
    _fake_generation(worker, out)

    def boom(*a, **k):
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr(compression, "_transcode", boom)
    done = {}
    worker.completed.connect(lambda p, e: done.update(path=p))
    worker.failed.connect(lambda m: done.update(error=m))
    worker._run_job()
    assert done.get("path") == str(out), done
    assert mp3_duration_seconds(out) > 7


def test_job_queue_passes_compression_to_the_worker(qtbot, monkeypatch, tmp_path):
    from app.workers import job_queue, local_tts_worker

    seen = []
    monkeypatch.setattr(local_tts_worker.LocalTTSWorker, "start",
                        lambda self: seen.append(("local", self._bitrate_kbps)))
    monkeypatch.setattr(job_queue.TTSWorker, "start",
                        lambda self: seen.append(("online", self._compression)))
    q = job_queue.JobQueue()
    q.submit(text="hi", voice="system:Alex", rate="+0%", volume="+0%",
             output_path=str(tmp_path / "a.mp3"), voice_display="Alex",
             compression="smallest")
    q.submit(text="hi", voice="en-US-AvaNeural", rate="+0%", volume="+0%",
             output_path=str(tmp_path / "b.mp3"), voice_display="Ava",
             compression="light")
    assert seen == [("local", 24), ("online", "light")]
    q.cancel_all()


# ------------------------------------------------------------------ #
# Settings dialog                                                      #
# ------------------------------------------------------------------ #

def test_settings_dialog_offers_and_saves_compression(qtbot, tmp_path):
    from app.ui.dialogs.settings_dialog import SettingsDialog
    from app.utils.paths import AppPaths

    settings = _settings(tmp_path)
    dialog = SettingsDialog(settings, AppPaths())
    qtbot.addWidget(dialog)
    combo = dialog._compression_combo
    assert [combo.itemData(i) for i in range(combo.count())] == list(compression.LEVELS)
    assert combo.currentData() == "light"
    assert "recommended" in combo.currentText()
    assert "MB per hour" in combo.currentText()
    assert "40 kbit/s" in dialog._compression_note.text()
    # Only the default is marked recommended.
    assert [combo.itemData(i) for i in range(combo.count())
            if "recommended" in combo.itemText(i)] == ["light"]

    combo.setCurrentIndex(combo.findData("off"))
    assert "native quality" in dialog._compression_note.text()
    dialog._save()
    assert _settings(tmp_path).compression == "off"



def test_export_card_shows_the_level_and_links_to_settings(qtbot, tmp_path, monkeypatch):
    from app.services.history_service import HistoryService
    from app.ui.panels.output_panel import OutputPanel
    from app.workers import voice_loader

    monkeypatch.setattr(voice_loader.VoiceLoaderWorker, "start", lambda self: None)
    settings = _settings(tmp_path)
    panel = OutputPanel(settings=settings, history=HistoryService(tmp_path / "h.db"))
    qtbot.addWidget(panel)
    label = panel._compression_label
    assert "Light" in label.text() and "18 MB/hour" in label.text()

    settings.compression = "off"
    panel.refresh_compression_label()
    assert "Off" in label.text()

    with qtbot.waitSignal(panel.settings_requested, timeout=1000):
        label.linkActivated.emit("settings")


def test_submitted_jobs_use_the_saved_compression(qtbot, tmp_path, monkeypatch):
    from app.services.history_service import HistoryService
    from app.ui.panels.output_panel import OutputPanel
    from app.workers import voice_loader

    monkeypatch.setattr(voice_loader.VoiceLoaderWorker, "start", lambda self: None)
    settings = _settings(tmp_path)
    settings.compression = "smallest"
    panel = OutputPanel(settings=settings, history=HistoryService(tmp_path / "h.db"))
    qtbot.addWidget(panel)
    seen = {}

    def fake_submit(**kwargs):
        seen.update(kwargs)
        return "job"

    monkeypatch.setattr(panel._queue, "submit", fake_submit)
    monkeypatch.setattr(panel, "get_input_text", lambda: "Chapter one. It was a quiet night.",
                        raising=False)
    monkeypatch.setattr(panel, "_has_visible_voice", lambda: True)
    monkeypatch.setattr(panel, "get_selected_voice", lambda: "en-US-AvaNeural")
    monkeypatch.setattr(panel, "get_output_path", lambda: str(tmp_path / "book.mp3"))
    monkeypatch.setattr(panel, "_refresh_voice_guidance", lambda: None)
    panel._compatibility = None
    panel._on_generate()
    assert seen.get("compression") == "smallest", seen
    assert seen.get("output_path") == str(tmp_path / "book.mp3")


# ------------------------------------------------------------------ #
# Stylesheet fonts (log noise found while reviewing user logs)         #
# ------------------------------------------------------------------ #

def test_px_font_sizes_convert_to_identical_points():
    from app.ui import style

    qss = "QComboBox { font-size: 13px; padding: 6px 12px; } QLabel { font-size: 10.5px; }"
    out = style.px_fonts_to_points(qss)
    assert "font-size: 9.75pt" in out and "font-size: 7.875pt" in out
    assert "padding: 6px 12px" in out, "only font sizes change"


def test_windows_stylesheet_has_no_pixel_font_sizes(monkeypatch):
    from app.ui import style

    monkeypatch.setattr(style.sys, "platform", "win32")
    qss = style.stylesheet_text()
    assert "font-size" in qss
    assert not style._PX_FONT_SIZE.search(qss)
    monkeypatch.setattr(style.sys, "platform", "darwin")
    assert style._PX_FONT_SIZE.search(style.stylesheet_text()), "macOS keeps pixel sizes"


def test_combo_popup_logs_no_font_warning(qtbot):
    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtWidgets import QApplication, QComboBox

    from app.ui import style

    app = QApplication.instance()
    if app.platformName() != "windows":
        # The warning comes from Qt's windows11 style.  The headless test
        # platform logs a different font warning with or without this fix.
        pytest.skip("needs the real Windows platform (QT_QPA_PLATFORM=windows)")
    old_sheet = app.styleSheet()
    msgs = []
    previous = qInstallMessageHandler(lambda mode, ctx, msg: msgs.append(msg))
    try:
        app.setStyleSheet(style.px_fonts_to_points(style.stylesheet_text()))
        combo = QComboBox()
        qtbot.addWidget(combo)
        combo.addItems(["Off", "Light", "Balanced"])
        combo.show()
        combo.showPopup()
        qtbot.wait(50)
        combo.hidePopup()
    finally:
        qInstallMessageHandler(previous)
        app.setStyleSheet(old_sheet)
    assert not [m for m in msgs if "setPointSize" in m], msgs
