"""
Tests for the 1.6.0 product fixes outside the generation core: voice naming,
export destination safety, error wording, text import, history, and the
supporting utilities.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
import sys
import warnings
from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config.settings import AppSettings  # noqa: E402
from app.models.job import Job  # noqa: E402
from app.models.voice import Voice, persona_name  # noqa: E402
from app.services.history_service import SCHEMA_VERSION, HistoryService  # noqa: E402
from app.utils.errors import friendly_error_text, split_error  # noqa: E402
from app.utils.mp3_duration import mp3_duration_from_bytes, mp3_duration_seconds  # noqa: E402
from app.utils.output_paths import check_output_path, next_free_path, normalise_filename  # noqa: E402
from app.utils.paths import AppPaths  # noqa: E402

pytest.importorskip("pytestqt")


# ------------------------------------------------------------------ #
# Voice naming                                                        #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("short_name, expected", [
    ("en-US-AndrewNeural", "Andrew"),
    ("en-US-AndrewMultilingualNeural", "Andrew (Multilingual)"),
    ("zh-CN-liaoning-XiaobeiNeural", "Xiaobei"),
    ("fr-FR-VivienneMultilingualNeural", "Vivienne (Multilingual)"),
])
def test_persona_name_keeps_the_multilingual_variant_visible(short_name, expected):
    assert persona_name(short_name) == expected


def test_standard_and_multilingual_voices_no_longer_look_identical():
    a = Voice("en-US-AndrewNeural", "Andrew", "en-US", "Male")
    b = Voice("en-US-AndrewMultilingualNeural", "Andrew ML", "en-US", "Male")
    assert a.display_name != b.display_name


# ------------------------------------------------------------------ #
# Export destination                                                  #
# ------------------------------------------------------------------ #

def test_filename_is_normalised_to_mp3():
    assert normalise_filename("  chapter 1 ") == "chapter 1.mp3"
    assert normalise_filename("book.MP3") == "book.MP3"
    assert normalise_filename("") == "output.mp3"


@pytest.mark.parametrize("name", ["part: one", "a/b", "what?", "tab\there", "CON", "nul.mp3", "trailing.", ". "])
def test_illegal_file_names_are_refused_up_front(tmp_path, name):
    problem = check_output_path(str(tmp_path), name)
    assert problem is not None
    assert problem.title in {"Invalid File Name"}


def test_blank_name_falls_back_to_output_mp3(tmp_path):
    assert check_output_path(str(tmp_path), "   ") is None
    assert normalise_filename("   ") == "output.mp3"


def test_binary_files_are_not_imported_as_text():
    from app.ui.panels.input_panel import decode_text_file

    assert decode_text_file(bytes(range(256)) * 20) is None


def test_valid_unicode_name_is_accepted(tmp_path):
    assert check_output_path(str(tmp_path), "अध्याय १ — Chapter 1") is None


def test_missing_folder_is_refused(tmp_path):
    problem = check_output_path(str(tmp_path / "gone"), "book")
    assert problem is not None and problem.title == "Save Location Not Found"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_read_only_folder_is_refused(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("running with privileges that ignore permission bits")
        problem = check_output_path(str(locked), "book")
        assert problem is not None and problem.title == "Can't Save Here"
    finally:
        locked.chmod(0o700)


def test_next_free_path_counts_up(tmp_path):
    (tmp_path / "book.mp3").write_bytes(b"1")
    (tmp_path / "book (2).mp3").write_bytes(b"2")
    assert next_free_path(tmp_path / "book.mp3").name == "book (3).mp3"
    assert next_free_path(tmp_path / "new.mp3").name == "new.mp3"


# ------------------------------------------------------------------ #
# Error wording                                                       #
# ------------------------------------------------------------------ #

def test_split_error_moves_technical_detail_behind_the_summary():
    err = split_error(
        "SetupTTS couldn't reach the service.\n\nPlease try again."
        "\n\nTechnical details: ClientConnectorError: Cannot connect to host"
    )
    assert "ClientConnectorError" not in err.summary
    assert err.summary.startswith("SetupTTS couldn't reach")
    assert "ClientConnectorError" in err.details


def test_split_error_keeps_the_what_next_paragraph_after_old_details_marker():
    err = split_error(
        "Generation failed on chunk 4/9 after recovery attempts.\n\n"
        "Details: An unexpected error occurred\n\n"
        "Completed audio up to chunk 3 has been preserved."
    )
    assert "preserved" in err.summary
    assert "unexpected error" in err.details


def test_raw_exception_text_never_becomes_the_headline():
    err = split_error("aiohttp.client_exceptions.SocketTimeoutError: Timeout on reading data")
    assert "aiohttp" not in err.summary
    assert "in time" in err.summary
    assert "aiohttp" in err.details


@pytest.mark.parametrize("raw, fragment", [
    (OSError(28, "No space left on device"), "not enough free disk space"),
    (PermissionError(13, "Permission denied"), "not allowed to save"),
    ("Cannot connect to host speech.platform.bing.com:443 [getaddrinfo failed]", "could not reach"),
    ("SSLCertVerificationError CERTIFICATE_VERIFY_FAILED", "date and time"),
    ("503 Service Unavailable, WSServerHandshakeError", "temporarily unavailable"),
])
def test_friendly_error_text(raw, fragment):
    assert fragment in friendly_error_text(raw).lower()


# ------------------------------------------------------------------ #
# Text import                                                         #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("encoding, bom", [
    ("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"),
    ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"),
    ("utf-16-le", b""), ("cp1252", b""),
])
def test_text_files_decode_however_notepad_saved_them(encoding, bom):
    from app.ui.panels.input_panel import decode_text_file

    text = "Café — “quoted” text, naïve résumé." if encoding == "cp1252" else \
        "Café — “quoted” text. हिंदी पाठ। 🙂"
    decoded = decode_text_file(bom + text.encode(encoding))
    assert decoded == text


def test_clear_text_can_be_undone(qtbot):
    from app.ui.panels.input_panel import InputPanel

    panel = InputPanel()
    qtbot.addWidget(panel)
    panel.set_text("A whole chapter the user did not mean to lose.")
    panel.clear()
    assert panel.get_text() == ""
    panel._editor.undo()
    assert panel.get_text() == "A whole chapter the user did not mean to lose."


def test_has_text_signal_is_immediate_and_full_text_is_debounced(qtbot):
    from app.ui.panels.input_panel import InputPanel

    panel = InputPanel()
    qtbot.addWidget(panel)
    has_text, full = [], []
    panel.has_text_changed.connect(has_text.append)
    panel.text_changed.connect(full.append)

    panel._editor.insertPlainText("abc")
    assert has_text == [True]          # immediately
    assert full == []                  # not yet: coalesced
    qtbot.waitUntil(lambda: full == ["abc"], timeout=2000)
    panel._editor.insertPlainText("def")
    panel.flush()
    assert full[-1] == "abcdef"


# ------------------------------------------------------------------ #
# History                                                             #
# ------------------------------------------------------------------ #

def _job(**kw) -> Job:
    base = dict(id=None, text_preview="Once upon a time", voice="en-US-AvaNeural",
                rate="+0%", output_path="/tmp/a.mp3", duration_seconds=12.0)
    base.update(kw)
    return Job(**base)


def test_history_closes_every_connection(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        history = HistoryService(tmp_path / "history.db")
        history.add_job(_job(audio_seconds=245.0))
        jobs = history.get_jobs()
        history.clear_history()
        import gc
        gc.collect()
    assert jobs[0].audio_seconds == 245.0


def test_history_migrates_a_pre_1_6_database(tmp_path):
    db = tmp_path / "history.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, text_preview TEXT NOT NULL,
          voice TEXT NOT NULL, rate TEXT NOT NULL, output_path TEXT NOT NULL,
          created_at TEXT NOT NULL, duration_secs REAL NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'completed', error_message TEXT NOT NULL DEFAULT '');
    """)
    conn.execute("INSERT INTO jobs (text_preview, voice, rate, output_path, created_at) "
                 "VALUES ('old', 'en-US-AvaNeural', '+0%', '/x.mp3', ?)",
                 (datetime.now().isoformat(),))
    conn.commit()
    conn.close()

    history = HistoryService(db)
    jobs = history.get_jobs()
    assert [j.text_preview for j in jobs] == ["old"]
    assert jobs[0].audio_seconds is None
    history.add_job(_job(audio_seconds=10.0))
    with closing(sqlite3.connect(db)) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_corrupt_history_is_set_aside_not_fatal(tmp_path):
    db = tmp_path / "history.db"
    db.write_bytes(b"this is not a database" * 100)
    history = HistoryService(db)
    history.add_job(_job())
    assert len(history.get_jobs()) == 1
    assert (tmp_path / "history.db.corrupt").exists()


def test_history_from_a_newer_build_is_read_only(tmp_path):
    db = tmp_path / "history.db"
    HistoryService(db).add_job(_job())
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    history = HistoryService(db)
    assert history.read_only
    history.add_job(_job(text_preview="should not be written"))
    history.clear_history()
    assert [j.text_preview for j in history.get_jobs()] == ["Once upon a time"]


def test_history_table_shows_audio_length(qtbot, tmp_path):
    from app.ui.panels.history_panel import HistoryPanel, _COL_LENGTH, _COL_VOICE

    history = HistoryService(tmp_path / "h.db")
    history.add_job(_job(voice="en-US-AndrewMultilingualNeural", audio_seconds=3725.0))
    panel = HistoryPanel(history)
    qtbot.addWidget(panel)
    assert panel._table.item(0, _COL_LENGTH).text() == "1:02:05"
    assert panel._table.item(0, _COL_VOICE).text() == "Andrew (Multilingual)"


# ------------------------------------------------------------------ #
# MP3 duration streaming                                              #
# ------------------------------------------------------------------ #

def _frame_48k_24khz() -> bytes:
    # MPEG-2 Layer III, 48 kbit/s, 24 kHz, mono — the service's exact format.
    return bytes([0xFF, 0xF3, 0x64, 0xC0]) + b"\x00" * 140


def test_streamed_duration_matches_whole_buffer_across_block_edges(tmp_path, monkeypatch):
    from app.utils import mp3_duration

    monkeypatch.setattr(mp3_duration, "_READ_BLOCK", 1000)   # force many block edges
    data = b"ID3\x03\x00\x00\x00\x00\x00\x05" + b"\x00" * 5 + _frame_48k_24khz() * 700 + b"\x12\x34"
    path = tmp_path / "a.mp3"
    path.write_bytes(data)
    assert mp3_duration_seconds(path) == mp3_duration_from_bytes(data)
    assert abs(mp3_duration_seconds(path) - 700 * 576 / 24000) < 1e-9


# ------------------------------------------------------------------ #
# Main-window behaviour                                               #
# ------------------------------------------------------------------ #

@pytest.fixture
def window(qapp, tmp_path, monkeypatch, qtbot):
    from app.ui.main_window import MainWindow
    from app.workers import voice_loader

    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "data"))
    # No network: the window is fed voices directly.
    monkeypatch.setattr(voice_loader.VoiceLoaderWorker, "start", lambda self: None)
    paths = AppPaths()
    settings = AppSettings(paths)
    win = MainWindow(settings=settings, paths=paths)
    qtbot.addWidget(win)
    win._output_panel._on_voices_loaded([
        Voice("en-US-AndrewNeural", "Andrew", "en-US", "Male"),
        Voice("en-US-AndrewMultilingualNeural", "Andrew ML", "en-US", "Male"),
        Voice("en-US-AvaNeural", "Ava", "en-US", "Female"),
        Voice("hi-IN-MadhurNeural", "Madhur", "hi-IN", "Male"),
    ])
    win.show()
    yield win
    win.ensure_workers_stopped()


def test_title_shows_the_running_version(window):
    from app import APP_VERSION
    assert APP_VERSION in window.windowTitle()


def test_voice_picker_never_leaves_the_recent_header_selected(window):
    panel = window._output_panel
    panel._settings.voice = "en-US-AvaNeural"
    panel._settings.add_recently_used_voice("hi-IN-MadhurNeural")
    # Filter to Hindi: the saved voice (Ava) is filtered out, a recent voice
    # is shown under the header.
    idx = panel._lang_combo.findData("hi-IN")
    panel._lang_combo.setCurrentIndex(idx)
    panel._apply_filters()
    combo = panel._voice_combo
    assert combo.itemData(combo.currentIndex(), 0x0100) == "hi-IN-MadhurNeural"
    assert panel.get_selected_voice() == "hi-IN-MadhurNeural"
    assert panel._settings.voice == "hi-IN-MadhurNeural"


def test_both_andrew_voices_are_distinguishable_in_the_picker(window):
    combo = window._output_panel._voice_combo
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert any(label.startswith("Andrew (Multilingual)") for label in labels)
    assert any(label.startswith("Andrew  ·") for label in labels)


def test_existing_output_offers_keep_both(window, tmp_path, monkeypatch):
    panel = window._output_panel
    folder = tmp_path / "out"
    folder.mkdir()
    (folder / "book.mp3").write_bytes(b"finished audiobook")
    panel._set_folder_text(str(folder))
    panel._filename_edit.setText("book")

    def choose_keep_both(box):
        box.clickedButton = lambda: next(
            b for b in box.buttons() if b.text() == "Keep Both"
        )
        return 0

    monkeypatch.setattr(QMessageBox, "exec", choose_keep_both)
    chosen = panel._confirm_existing_output(str(folder / "book.mp3"))
    assert Path(chosen).name == "book (2).mp3"
    assert panel._filename_edit.text() == "book (2).mp3"
    assert (folder / "book.mp3").read_bytes() == b"finished audiobook"


def test_generate_refuses_an_illegal_file_name_before_queueing(window, tmp_path, monkeypatch):
    panel = window._output_panel
    panel._set_folder_text(str(tmp_path))
    panel._filename_edit.setText("Part 1: Intro")
    window.set_input_text("Some text to speak. " * 5)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[1]))
    panel._on_generate()
    assert warned == ["Invalid File Name"]
    assert not panel.is_busy()


def test_speed_reset_returns_to_normal(window):
    panel = window._output_panel
    panel._rate_slider.setValue(25)
    assert panel._rate_reset_btn.isVisibleTo(panel)
    panel._rate_reset_btn.click()
    assert panel._rate_slider.value() == 0
    assert panel._rate_value_label.text() == "Normal (0%)"
    assert not panel._rate_reset_btn.isVisibleTo(panel)


def test_generate_shortcut_respects_the_button_state(window, monkeypatch):
    panel = window._output_panel
    calls = []
    monkeypatch.setattr(panel, "_on_generate", lambda: calls.append(1))
    window.set_input_text("")
    panel.trigger_generate()
    assert calls == []
    window.set_input_text("Hello there, this is enough text.")
    panel.trigger_generate()
    assert calls == [1]


def test_job_row_shows_plain_language_stage(qtbot):
    from app.ui.panels.output_panel import _friendly_stage

    assert _friendly_stage("remote", "Sending chunk 3/~7 to Microsoft (8,960 chars / 3,400 bytes)") == "Generating audio"
    assert _friendly_stage("waiting", "Retry 2/4 on chunk 5/40 — waiting 4 s before a fresh connection") \
        == "Connection problem — retrying chunk 5 (2 of 4)"
    assert _friendly_stage("local", "Assembling final audio file from all chunks…") == "Finalizing the MP3 file"
    assert _friendly_stage("local", "Note: 'x' is a multilingual model") is None


# ------------------------------------------------------------------ #
# Single instance and voice-list cache                                #
# ------------------------------------------------------------------ #

def test_second_instance_is_refused_and_activates_the_first(qapp, tmp_path, qtbot):
    from app.utils.single_instance import SingleInstance

    first = SingleInstance(tmp_path)
    assert first.acquire()
    try:
        second = SingleInstance(tmp_path)
        assert not second.acquire()
        with qtbot.waitSignal(first.activation_requested, timeout=3000):
            assert second.notify_running_instance()
    finally:
        first.release()
    third = SingleInstance(tmp_path)
    assert third.acquire()
    third.release()


def test_voice_list_falls_back_to_the_saved_copy_when_offline(tmp_path, monkeypatch):
    from app.workers import voice_loader

    cache = tmp_path / "voices.json"
    cache.write_text(json.dumps([
        {"ShortName": "en-US-AvaNeural", "FriendlyName": "Ava", "Locale": "en-US", "Gender": "Female"},
    ]))

    async def offline(*, force_refresh=False):
        raise OSError("network down")

    monkeypatch.setattr(voice_loader, "list_voices", offline)
    monkeypatch.setattr(voice_loader, "_RETRY_DELAYS_S", (0, 0))
    worker = voice_loader.VoiceLoaderWorker(cache_path=cache)
    loaded, failed = [], []
    worker.loaded.connect(loaded.append)
    worker.failed.connect(failed.append)
    worker.run()
    assert failed == []
    assert [v.short_name for v in loaded[0]] == ["en-US-AvaNeural"]
    assert worker.from_cache


def test_voice_list_failure_without_cache_is_plain(tmp_path, monkeypatch):
    from app.workers import voice_loader

    async def offline(*, force_refresh=False):
        raise OSError("Cannot connect to host speech.platform.bing.com")

    monkeypatch.setattr(voice_loader, "list_voices", offline)
    monkeypatch.setattr(voice_loader, "_RETRY_DELAYS_S", (0, 0))
    worker = voice_loader.VoiceLoaderWorker(cache_path=tmp_path / "none.json")
    failed = []
    worker.failed.connect(failed.append)
    worker.run()
    assert split_error(failed[0]).summary.startswith("Couldn't load the voice list")


def test_legacy_log_file_is_renamed(tmp_path):
    from app.utils.app_logging import LOG_FILENAME, _migrate_legacy_log

    (tmp_path / "voicecraft.log").write_text("old entries")
    _migrate_legacy_log(tmp_path)
    assert (tmp_path / LOG_FILENAME).read_text() == "old entries"
    assert not (tmp_path / "voicecraft.log").exists()
