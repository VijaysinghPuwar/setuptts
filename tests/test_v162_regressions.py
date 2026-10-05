"""
Regression tests for the bugs fixed in 1.6.2.

Each test failed on 1.6.1 and documents the user-visible symptom it guards.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from app.workers import tts_worker


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

class _Echo:
    """Fake Communicate: echoes the chunk text back as its 'audio'."""

    def __init__(self, text: str, controller=None) -> None:
        self._text = text
        self._controller = controller

    async def stream(self):
        if self._controller is not None:
            result = self._controller(self._text)
            if isinstance(result, BaseException):
                raise result
        mid = max(1, len(self._text) // 2)
        yield {"type": "WordBoundary", "text": self._text[:mid]}
        yield {"type": "WordBoundary", "text": self._text[mid:]}
        yield {"type": "audio", "data": self._text.encode("utf-8")}


@pytest.fixture
def app_paths(tmp_path, monkeypatch):
    from app.utils.paths import AppPaths

    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "data"))
    return AppPaths()


@pytest.fixture
def styled_app(qapp):
    from app.ui.style import stylesheet_text

    qapp.setStyleSheet(stylesheet_text())
    return qapp


@pytest.fixture
def window(styled_app, app_paths, qtbot):
    from app.config.settings import AppSettings
    from app.ui.main_window import MainWindow

    win = MainWindow(settings=AppSettings(app_paths), paths=app_paths)
    qtbot.addWidget(win)
    win.show()
    styled_app.processEvents()
    yield win
    win.ensure_workers_stopped()


@pytest.fixture
def stub_service(monkeypatch, tmp_path):
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "data"))
    state = {"controller": None, "voices": [
        {"ShortName": "en-US-AvaNeural", "Locale": "en-US", "Gender": "Female"},
        {"ShortName": "hi-IN-SwaraNeural", "Locale": "hi-IN", "Gender": "Female"},
    ]}

    async def fake_list_voices(*, force_refresh=False):
        return state["voices"]

    real_sleep = asyncio.sleep

    async def fast_sleep(_t, *a, **k):
        await real_sleep(0)

    monkeypatch.setattr(tts_worker, "list_voices", fake_list_voices)
    monkeypatch.setattr(tts_worker, "build_communicate",
                        lambda **kw: _Echo(kw["text"], state["controller"]))
    monkeypatch.setattr(tts_worker.asyncio, "sleep", fast_sleep)
    return state


# ------------------------------------------------------------------ #
# Worker                                                              #
# ------------------------------------------------------------------ #

def test_resume_of_a_use_anyway_job_is_not_blocked_by_the_voice_check(stub_service, tmp_path):
    """Every Resume of a job started with 'Use anyway' failed the voice check."""
    from edge_tts import exceptions as edge_exceptions

    from app.utils.paths import AppPaths
    from app.workers.chunk_store import ChunkStore

    calls = {"n": 0}

    def drop_after_three(_text):
        calls["n"] += 1
        if calls["n"] > 3:
            return edge_exceptions.NoAudioReceived("down")
        return None

    stub_service["controller"] = drop_after_three
    text = "यह एक बहुत लंबा हिंदी वाक्य है। " * 600
    out = tmp_path / "hindi.mp3"
    first = tts_worker.TTSWorker(text=text, voice="en-US-AvaNeural", rate="+0%",
                                 volume="+0%", output_path=str(out),
                                 allow_voice_mismatch=True)
    with pytest.raises(Exception):
        asyncio.run(first._stream_generate())

    candidate = ChunkStore.list_resume_candidates(AppPaths().staging_dir)[0]
    stub_service["controller"] = None
    resumed = tts_worker.TTSWorker(
        text=candidate.text, voice=candidate.voice, rate=candidate.rate,
        volume=candidate.volume, output_path=str(out),
        allow_voice_mismatch=False,              # what the Resume button passes
        job_id=candidate.job_id, resume_staging_dir=candidate.staging_dir,
    )
    asyncio.run(resumed._stream_generate())
    assert out.exists()


@pytest.mark.parametrize("sample", [
    "这是一个关于城市生活的故事。人们每天早上很早起床，然后去上班。" * 200,
    "これは長い物語です。彼は毎朝早く起きて、仕事に行きました。" * 200,
], ids=["chinese", "japanese"])
def test_cjk_chunks_end_at_sentence_boundaries(sample):
    """CJK has no space after 。 so every chunk was hard-split mid-sentence."""
    cursor = tts_worker._ChunkCursor(sample)
    chunks = []
    while cursor.has_more():
        chunk, _, start, end = cursor.take_next(1_400, 4_000)
        chunks.append(chunk)
    assert len(chunks) > 3
    for chunk in chunks[:-1]:
        assert chunk.endswith(("。", "！", "？", "，", "、")), chunk[-12:]


def test_chunk_cutting_is_fast_on_text_without_paragraphs():
    """The longest-first scan re-escaped every prefix: ~1 s per chunk."""
    words = ("lorem ipsum dolor sit amet consectetur adipiscing elit " * 3000).strip()
    cursor = tts_worker._ChunkCursor(words)
    started = time.perf_counter()
    count = 0
    while cursor.has_more() and count < 20:
        cursor.take_next(9_000, 3_600)
        count += 1
    assert (time.perf_counter() - started) / count < 0.1


@pytest.mark.parametrize("n_chars,rate", [(1_474, "-5%"), (1_500, "-50%"), (9_000, "-5%")])
def test_chunk_deadline_outlasts_slow_streaming_voices(n_chars, rate):
    """
    Andrew streamed a 1,474-char chunk in 65 s, exactly the old fixed limit,
    so healthy streams were killed and logged as "stalled after partial audio".
    The deadline must allow streaming at 0.45x real time.
    """
    text = "a" * n_chars
    audio_s = n_chars / (15.8 * tts_worker._rate_multiplier(rate))
    timeout = tts_worker.TTSWorker._chunk_timeout_for(text, rate)
    assert timeout >= min(tts_worker._CHUNK_TIMEOUT_MAX_S, audio_s / 0.45)
    assert timeout > 65


def test_slower_speech_rate_gets_a_longer_deadline():
    t = "a" * 2_000
    slow = tts_worker.TTSWorker._chunk_timeout_for(t, "-50%")
    normal = tts_worker.TTSWorker._chunk_timeout_for(t, "+0%")
    fast = tts_worker.TTSWorker._chunk_timeout_for(t, "+100%")
    assert slow > normal > fast


def test_a_long_chunk_streaming_faster_than_realtime_is_not_slow(stub_service):
    """elapsed > 32 s alone shrank every chunk of a slow-streaming voice."""
    worker = tts_worker.TTSWorker(text="x", voice="en-US-AvaNeural", rate="+0%",
                                  volume="+0%", output_path="unused.mp3")
    plan = tts_worker._chunk_plan_for(200_000)
    healthy = tts_worker._ChunkOutcome(attempts=1, elapsed=60.0,
                                       first_audio_delay=1.5,
                                       audio_bytes=6000 * 120)   # 120 s of audio
    chars, _ = worker._retune_after_chunk(healthy, 5_000, 2_000, plan, chunk_index=3)
    assert chars >= 5_000

    struggling = tts_worker._ChunkOutcome(attempts=1, elapsed=60.0,
                                          first_audio_delay=1.5,
                                          audio_bytes=6000 * 30)  # 0.5x real time
    chars, _ = worker._retune_after_chunk(struggling, 5_000, 2_000, plan, chunk_index=3)
    assert chars < 5_000


# ------------------------------------------------------------------ #
# Text cleaning                                                       #
# ------------------------------------------------------------------ #

def test_normalisation_keeps_words_symbols_and_shaping_marks():
    from app.services.tts_quality import normalize_text_for_tts as norm

    assert "international committee" in norm("The inter­national com­mittee met.")
    assert "180°C" in norm("Bake at 180°C.")
    assert "©" in norm("Copyright © 2024") and "®" in norm("Acme®")
    assert "‌" in norm("می‌خواهم")                      # Persian ZWNJ
    assert "to .5 percent" in norm("fell to .5 percent")
    assert "use .NET" in norm("I use .NET daily")
    assert norm("Hello , world .") == "Hello, world."


def test_guidance_profile_is_sampled_for_huge_texts():
    from app.services.tts_quality import build_guidance_profile

    text = "This is an English sentence about a quiet town. " * 200_000   # ~10 MB
    started = time.perf_counter()
    profile = build_guidance_profile(text)
    assert time.perf_counter() - started < 3.0
    assert profile.language_code == "en"


# ------------------------------------------------------------------ #
# Files and paths                                                     #
# ------------------------------------------------------------------ #

def test_mp3_duration_skips_an_id3_tag_larger_than_one_block(tmp_path):
    from app.utils.mp3_duration import mp3_duration_from_bytes, mp3_duration_seconds

    # One MPEG-2 L3 frame, 48 kbit/s @ 24 kHz: 144 bytes, 576 samples (24 ms).
    frame = bytes([0xFF, 0xF3, 0x64, 0xC4]) + bytes(140)
    audio = frame * 2500                                  # 60 s
    size = 2 * 1024 * 1024
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    tag = b"ID3\x04\x00\x00" + syncsafe + bytes([0xFF, 0xFB] * (size // 2))
    path = tmp_path / "tagged.mp3"
    path.write_bytes(tag + audio)
    expected = mp3_duration_from_bytes(audio)
    assert expected and abs(expected - 60.0) < 0.1
    assert abs(mp3_duration_seconds(path) - expected) < 0.05


def test_keep_both_does_not_eat_a_number_that_is_part_of_the_name(tmp_path):
    from app.utils.output_paths import next_free_path

    real = tmp_path / "Annual Report (2024).mp3"
    real.write_bytes(b"x")
    assert next_free_path(real).name == "Annual Report (2024) (2).mp3"

    (tmp_path / "book.mp3").write_bytes(b"x")
    (tmp_path / "book (2).mp3").write_bytes(b"x")
    assert next_free_path(tmp_path / "book (2).mp3").name == "book (3).mp3"


@pytest.mark.parametrize("label,data,expected", [
    ("cp1251", "Глава первая. Была тёмная и бурная ночь.".encode("cp1251"), "Глава первая"),
    ("shift_jis", "第一章。それは暗い嵐の夜だった。".encode("shift_jis"), "第一章"),
    ("utf-32", "Chapter One. It was a dark night.".encode("utf-32"), "Chapter One"),
    ("cp1252", "Café crème, déjà vu — naïve.".encode("cp1252"), "Café crème"),
    ("utf-16-le-cjk", ("第一章。那是一个漆黑的暴风雨之夜。" * 20).encode("utf-16-le"), "第一章"),
], ids=lambda v: v if isinstance(v, str) and len(v) < 20 else "")
def test_imported_text_files_decode_in_their_real_encoding(label, data, expected):
    from app.ui.panels.input_panel import decode_text_file

    text = decode_text_file(data)
    assert text is not None and text.startswith(expected), (label, text[:40] if text else text)


def test_same_output_file_with_different_case_is_one_destination(tmp_path):
    from app.workers.job_queue import _path_key

    a = _path_key(str(tmp_path / "Book.mp3"))
    b = _path_key(str(tmp_path / "book.mp3"))
    if sys.platform in ("win32", "darwin"):
        assert a == b
    else:
        assert a != b


# ------------------------------------------------------------------ #
# Settings and history                                                #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("key,value", [
    ("rate", "abc"), ("window_width", None), ("window_x", "left"),
    ("history_panel_height", None), ("output_dir", 123), ("volume", None),
    ("show_history", "false"), ("rate", 10_000),
])
def test_damaged_settings_values_fall_back_per_key(app_paths, key, value):
    from app.config.settings import _DEFAULTS, AppSettings

    app_paths.settings_path.write_text(json.dumps({key: value, "voice": "en-US-AndrewNeural"}),
                                       encoding="utf-8")
    settings = AppSettings(app_paths)
    assert settings.voice == "en-US-AndrewNeural"          # good keys survive
    assert isinstance(settings.rate, int) and -50 <= settings.rate <= 100
    assert isinstance(settings.window_width, int)
    if key == "rate" and value == 10_000:
        assert settings.rate == 100                          # clamped
    elif key != "window_x":
        assert settings._data[key] == _DEFAULTS[key]


def test_settings_save_through_a_read_only_file(app_paths):
    from app.config.settings import AppSettings

    settings = AppSettings(app_paths)
    settings.save()
    os.chmod(app_paths.settings_path, stat.S_IREAD)
    try:
        settings.rate = 40
        settings.save()
        assert AppSettings(app_paths).rate == 40
    finally:
        os.chmod(app_paths.settings_path, stat.S_IWRITE | stat.S_IREAD)


def test_history_held_by_another_process_neither_crashes_nor_is_discarded(tmp_path):
    from app.models.job import Job, JobStatus
    from app.services.history_service import HistoryService
    from datetime import datetime

    db = tmp_path / "history.db"
    HistoryService(db).add_job(Job(id=None, text_preview="keep me", voice="v", rate="+0%",
                                   output_path="o.mp3", created_at=datetime.now(),
                                   duration_seconds=1.0, status=JobStatus.COMPLETED))

    holder = sqlite3.connect(str(db), check_same_thread=False)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        service = HistoryService(db)                    # must not raise
    finally:
        holder.rollback()
        holder.close()
    assert db.exists() and not (tmp_path / "history.db.corrupt").exists()
    assert [j.text_preview for j in HistoryService(db).get_jobs()] == ["keep me"]
    del service


def test_settings_save_folder_reaches_the_sidebar(window, tmp_path, monkeypatch):
    from app.ui.dialogs import settings_dialog

    target = tmp_path / "Audiobooks"
    target.mkdir()

    def fake_exec(dialog):
        dialog._output_dir_edit.setText(str(target))
        dialog._save()
        return 1

    monkeypatch.setattr(settings_dialog.SettingsDialog, "exec", fake_exec)
    window._open_settings()
    assert str(target) in window._output_panel.get_output_path()


def test_settings_dialog_shows_the_version_up_front(styled_app, app_paths, qtbot):
    from app import APP_VERSION
    from app.config.settings import AppSettings
    from app.ui.dialogs.settings_dialog import SettingsDialog

    dialog = SettingsDialog(AppSettings(app_paths), app_paths)
    qtbot.addWidget(dialog)
    assert APP_VERSION in dialog.windowTitle()
    assert APP_VERSION in dialog._version_banner.text()


# ------------------------------------------------------------------ #
# Logging                                                             #
# ------------------------------------------------------------------ #

def test_uncaught_thread_exceptions_reach_the_log(tmp_path):
    import logging

    from app.utils.app_logging import install_crash_logging

    records: list[logging.LogRecord] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Grab()
    logging.getLogger("app.crash").addHandler(handler)
    old_sys, old_thread = sys.excepthook, threading.excepthook
    try:
        install_crash_logging(tmp_path)
        t = threading.Thread(target=lambda: 1 / 0)
        t.start()
        t.join()
    finally:
        sys.excepthook, threading.excepthook = old_sys, old_thread
        logging.getLogger("app.crash").removeHandler(handler)
        import faulthandler
        faulthandler.disable()
    assert any(r.exc_info and r.exc_info[0] is ZeroDivisionError for r in records)
    assert (tmp_path / "crash.log").exists()


def test_job_summary_is_logged(stub_service, tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="app.workers.tts_worker")
    worker = tts_worker.TTSWorker(text="Hello there. " * 50, voice="en-US-AvaNeural",
                                  rate="+0%", volume="+0%",
                                  output_path=str(tmp_path / "s.mp3"))
    asyncio.run(worker._stream_generate())
    assert worker._stats.chunks_ok >= 1
    assert "chunks_ok=" in worker._stats.summary()


# ------------------------------------------------------------------ #
# Cycle-2 review findings                                             #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("word", [
    "Olé!", "été", "Grüße", "Zoë", "Þór", "café crème",
    "Hyvää päivää! Mitä kuuluu?", "Ég tala íslensku og þú?",
], ids=lambda w: w.encode("ascii", "replace").decode())
def test_short_accented_western_text_stays_western(word):
    from app.ui.panels.input_panel import decode_text_file

    assert decode_text_file(word.encode("cp1252")) == word


def test_central_european_text_decodes_as_cp1250():
    from app.ui.panels.input_panel import decode_text_file

    text = "Příliš žluťoučký kůň úpěl ďábelské ódy."
    assert decode_text_file(text.encode("cp1250")) == text


def test_bomless_utf16_cjk_without_any_ascii():
    from app.ui.panels.input_panel import decode_text_file

    text = "我们今天去公园散步。"
    assert decode_text_file(text.encode("utf-16-le")) == text


def test_emoji_joiners_do_not_survive_cleaning():
    from app.services.tts_quality import normalize_text_for_tts as norm

    cleaned = norm("Chapter one.\n\n👨‍👩‍👧\n\nThe family arrived.")
    assert "‍" not in cleaned
    assert "‍" in norm("क्‍ष")


def test_a_leading_comma_does_not_make_a_tiny_chunk():
    text = "Well, " + " ".join(["word"] * 2000)
    chunk, _, _, _ = tts_worker._ChunkCursor(text).take_next(3_000, 9_000)
    assert len(chunk) > 1_000


def test_closing_quote_stays_with_its_sentence():
    text = "「今日はいい天気ですね。」と彼は言った。" * 40
    for max_chars in range(20, 60):
        cursor = tts_worker._ChunkCursor(text)
        while cursor.has_more():
            chunk, _, _, _ = cursor.take_next(max_chars, 4_000)
            assert not chunk.startswith("」"), max_chars


# ------------------------------------------------------------------ #
# Cycle-2 GUI findings                                                #
# ------------------------------------------------------------------ #

def test_generate_refuses_when_no_voice_is_visible(window, qtbot, monkeypatch):
    """Filters matching nothing: Generate used the hidden saved voice."""
    from PySide6.QtWidgets import QMessageBox

    shown = []
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda parent, title, text, *a, **k: shown.append(title)))
    panel = window._output_panel
    window.set_input_text("Some English text to speak.")
    panel._search_edit.setText("zzzz-no-such-voice")
    qtbot.wait(400)
    submitted = []
    monkeypatch.setattr(panel._queue, "submit", lambda **kw: submitted.append(kw))
    panel._on_generate()
    assert "Choose a Voice" in shown
    assert not submitted


def test_resume_asks_before_replacing_a_file_at_the_original_path(window, tmp_path, monkeypatch):
    from types import SimpleNamespace

    panel = window._output_panel
    existing = tmp_path / "book.mp3"
    existing.write_bytes(b"the user's newer audiobook")
    asked, submitted = [], []
    monkeypatch.setattr(panel, "_confirm_existing_output",
                        lambda path: asked.append(path) or "")      # user cancels
    monkeypatch.setattr(panel._queue, "submit", lambda **kw: submitted.append(kw))
    candidate = SimpleNamespace(output_path=str(existing), voice="en-US-AvaNeural",
                                text="saved text", rate="+0%", volume="+0%",
                                job_id="abc", staging_dir=tmp_path / "staging")
    panel._resume_candidate(candidate)
    assert asked == [str(existing)]
    assert not submitted
    assert existing.read_bytes() == b"the user's newer audiobook"


def test_preview_stopped_mid_generation_always_resets_the_buttons(qapp, qtbot, monkeypatch):
    """Stop, then a generation error: the UI waited forever on 'Stop'."""
    from app.workers import preview_worker


    async def hang_then_fail(**kw):
        await asyncio.sleep(30)
        raise OSError("offline")

    monkeypatch.setattr(preview_worker, "generate_audio", hang_then_fail)
    worker = preview_worker.PreviewWorker("en-US-AvaNeural", "+0%")
    failures = []
    worker.failed.connect(failures.append)
    with qtbot.waitSignal(worker.playback_finished, timeout=5000):
        worker.start()
        qtbot.wait(200)
        began = time.monotonic()
        worker.stop_playback()
    assert worker.wait(3000)
    assert time.monotonic() - began < 3.0          # cancelled, not waited out
    assert not failures


def test_closing_settings_right_after_copy_raises_nothing(styled_app, app_paths, qtbot):
    from app.config.settings import AppSettings
    from app.ui.dialogs.settings_dialog import SettingsDialog

    errors = []
    old_hook = sys.excepthook
    sys.excepthook = lambda *exc: errors.append(exc)
    try:
        dialog = SettingsDialog(AppSettings(app_paths), app_paths)
        dialog._copy_diag_btn.click()
        dialog._copy_path_btn.click()
        dialog.deleteLater()
        qtbot.wait(1800)
    finally:
        sys.excepthook = old_hook
    assert not errors


def test_tiny_accented_files_are_not_read_as_cyrillic():
    from app.ui.panels.input_panel import decode_text_file

    for word in ("à", "ÄÖÜ", "Ça"):
        assert decode_text_file(word.encode("cp1252")) == word


def test_joiner_after_virama_is_kept_at_word_end():
    from app.services.tts_quality import normalize_text_for_tts as norm

    assert "‍" in norm("അവന്‍ വന്നു")      # Malayalam chillu (legacy form)
