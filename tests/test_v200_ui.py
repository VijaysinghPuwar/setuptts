"""v2.0.0 UI / shutdown regressions (each test fails on v1.8.2)."""

from __future__ import annotations

import threading

import pytest
from PySide6.QtCore import QMimeData, QPoint, QThread, QUrl, Qt
from PySide6.QtGui import QDragEnterEvent
from PySide6.QtWidgets import QApplication

from app.models.voice import Voice


@pytest.fixture
def app_paths(tmp_path, monkeypatch):
    from app.utils.paths import AppPaths

    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "data"))
    return AppPaths()


VOICES = [
    Voice("en-US-AvaNeural", "Ava", "en-US", "Female"),
    Voice("en-US-AndrewNeural", "Andrew", "en-US", "Male"),
    Voice("en-GB-RyanNeural", "Ryan", "en-GB", "Male"),
    Voice("fr-FR-DeniseNeural", "Denise", "fr-FR", "Female"),
]


def _panel(qtbot, app_paths, tmp_path, monkeypatch, settings=None):
    from app.config.settings import AppSettings
    from app.services.history_service import HistoryService
    from app.ui.panels.output_panel import OutputPanel
    from app.workers import voice_loader

    monkeypatch.setattr(voice_loader.VoiceLoaderWorker, "start", lambda self: None)
    panel = OutputPanel(settings=settings or AppSettings(app_paths),
                        history=HistoryService(tmp_path / "h.db"))
    qtbot.addWidget(panel)
    return panel


# ── Voice picker settings ────────────────────────────────────────────── #

def test_saved_filters_survive_launch_with_a_gender_filter(qtbot, app_paths, tmp_path, monkeypatch):
    from app.config.settings import AppSettings

    settings = AppSettings(app_paths)
    settings.gender_filter = "Male"
    settings.language_filter = "en-GB"
    settings.source_filter = "online"
    panel = _panel(qtbot, app_paths, tmp_path, monkeypatch, settings)
    assert settings.language_filter == "en-GB"
    assert settings.source_filter == "online"
    panel._on_voices_loaded(VOICES)
    assert panel._lang_combo.currentData() == "en-GB"
    assert panel.get_selected_voice() == "en-GB-RyanNeural"


def test_search_does_not_replace_the_saved_voice(qtbot, app_paths, tmp_path, monkeypatch):
    panel = _panel(qtbot, app_paths, tmp_path, monkeypatch)
    panel._settings.language_filter = ""
    panel._settings.voice = "en-US-AvaNeural"
    panel._on_voices_loaded(VOICES)
    assert panel.get_selected_voice() == "en-US-AvaNeural"

    panel._search_edit.setText("denise")
    panel._apply_filters()
    assert panel.get_selected_voice() == "fr-FR-DeniseNeural"
    assert panel._settings.voice == "en-US-AvaNeural"

    panel._search_edit.setText("")
    panel._apply_filters()
    assert panel.get_selected_voice() == "en-US-AvaNeural"

    # A real pick is still saved.
    combo = panel._voice_combo
    idx = next(i for i in range(combo.count())
               if combo.itemData(i, Qt.UserRole) == "en-US-AndrewNeural")
    combo.setCurrentIndex(idx)
    assert panel._settings.voice == "en-US-AndrewNeural"


def test_result_of_a_replaced_voice_loader_is_ignored(qtbot, app_paths, tmp_path, monkeypatch):
    panel = _panel(qtbot, app_paths, tmp_path, monkeypatch)
    panel._settings.language_filter = ""
    old = panel._voice_loader
    panel._start_voice_load()          # Retry: a new loader replaces it
    new = panel._voice_loader
    assert new is not old
    new.loaded.emit(VOICES)
    assert len(panel._all_voices) == len(VOICES)
    old.loaded.emit(VOICES[:1])        # queued before the retry; arrives late
    old.failed.emit("boom")
    assert len(panel._all_voices) == len(VOICES)
    assert not panel._voice_error_label.isVisibleTo(panel)


# ── Preview ──────────────────────────────────────────────────────────── #

def test_preview_click_right_after_playback_is_not_dropped(qtbot, app_paths, tmp_path, monkeypatch):
    from app.ui.panels import output_panel as op

    panel = _panel(qtbot, app_paths, tmp_path, monkeypatch)
    panel._on_voices_loaded(VOICES)

    release = threading.Event()

    class Ending(QThread):
        def run(self):
            release.wait(5)

    ending = Ending()
    ending.start()
    panel._preview_worker = ending
    panel._on_preview_done()           # playback_finished arrived; run() still returning

    started = []
    monkeypatch.setattr(op.PreviewWorker, "start", lambda self: started.append(self))
    threading.Timer(0.1, release.set).start()
    panel._on_preview()
    assert started, "the click was ignored"
    ending.wait(2_000)


# ── Shutdown ─────────────────────────────────────────────────────────── #

def test_voice_loader_retry_sleep_is_interruptible(monkeypatch):
    import time

    from app.workers import voice_loader

    worker = voice_loader.VoiceLoaderWorker()
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: True)
    t0 = time.monotonic()
    assert worker._sleep(4.0) is False
    assert time.monotonic() - t0 < 0.5


def test_voice_loader_stops_promptly_when_interrupted(qtbot, monkeypatch, tmp_path):
    import time

    from app.workers import voice_loader

    calls = []

    async def offline(*, force_refresh=False):
        calls.append(1)
        raise OSError("network down")

    monkeypatch.setattr(voice_loader, "list_voices", offline)
    monkeypatch.setattr(voice_loader, "_RETRY_DELAYS_S", (30, 30))
    worker = voice_loader.VoiceLoaderWorker(cache_path=tmp_path / "v.json")
    emitted = []
    worker.loaded.connect(emitted.append)
    worker.failed.connect(emitted.append)
    worker.start()
    qtbot.waitUntil(lambda: bool(calls), timeout=3_000)
    t0 = time.monotonic()
    worker.requestInterruption()
    assert worker.wait(2_000)
    assert time.monotonic() - t0 < 1.5
    assert len(calls) == 1


def test_stop_threads_gives_every_worker_a_fair_wait_and_never_terminates(monkeypatch):
    from app.workers import thread_shutdown

    now = [0.0]
    kept = []

    class Stuck:
        def __init__(self):
            self.waits = []
            self.terminated = False

        def cancel(self):
            pass

        def requestInterruption(self):
            pass

        def quit(self):
            pass

        def isRunning(self):
            return True

        def wait(self, ms):
            self.waits.append(ms)
            now[0] += ms / 1000
            return False

        def terminate(self):
            self.terminated = True

    monkeypatch.setattr(thread_shutdown.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(thread_shutdown, "keep_alive", kept.append)
    a, b = Stuck(), Stuck()
    left = thread_shutdown.stop_threads([("a", a), ("b", b)])
    assert left == [a, b] and kept == [a, b]
    assert b.waits[0] >= 2_000           # not wait(0) after the first used the budget
    assert sum(a.waits + b.waits) <= 8_000
    assert not a.terminated and not b.terminated


def test_cancel_all_keeps_a_reference_to_a_worker_that_will_not_stop(monkeypatch):
    from app.workers import job_queue, thread_shutdown
    from app.workers.job_queue import JobItem, JobQueue

    class Stuck:
        _output_path = "x.mp3"

        def cancel(self):
            pass

        def requestInterruption(self):
            pass

        def quit(self):
            pass

        def isRunning(self):
            return True

        def wait(self, ms):
            return False

        def terminate(self):
            raise AssertionError("terminate() must not be used")

    kept = []
    monkeypatch.setattr(thread_shutdown, "keep_alive", kept.append)
    worker = Stuck()
    queue = JobQueue()
    queue._running["j"] = (JobItem(text="t", voice="v", voice_display="v", rate="+0%",
                                   volume="+0%", output_path="x.mp3"), worker)
    queue._active_workers.append(worker)
    queue.cancel_all()
    assert worker in kept
    assert worker in queue._active_workers


def test_stop_orphans_waits_for_dialog_threads(qtbot):
    from app.ui.dialogs import voice_manager_dialog as vmd

    stop = threading.Event()

    class Download(QThread):
        def cancel(self):
            stop.set()

        def run(self):
            stop.wait(10)

    t = Download()
    t.start()
    vmd._adopt(t)
    assert t in vmd._ORPHANS
    vmd.stop_orphans()
    assert t.wait(1_000)
    assert stop.is_set()


# ── Text import ──────────────────────────────────────────────────────── #

def test_one_bad_byte_does_not_turn_utf8_into_cp1252():
    from app.ui.panels.input_panel import decode_text_file

    text = "Café au lait — “quoted” naïve résumé. " * 200
    data = text.encode("utf-8")
    data = data[:500] + b"\xff" + data[500:]
    decoded = decode_text_file(data)
    assert "Café" in decoded and "“quoted”" in decoded
    assert "Ã" not in decoded


def test_cp1252_files_still_decode_as_cp1252():
    from app.ui.panels.input_panel import decode_text_file

    text = "The café served a crème brûlée to every guest who came in. " * 50
    assert decode_text_file(text.encode("cp1252")) == text


def test_a_stray_nul_does_not_make_utf8_look_like_utf16():
    from app.ui.panels.input_panel import decode_text_file

    text = "It was a bright cold day in April, and the clocks were striking thirteen. " * 40
    data = text.encode("utf-8")
    data = data[:300] + b"\x00" + data[300:]
    assert decode_text_file(data) == text


def test_bomless_utf16_cyrillic_still_detected():
    from app.ui.panels.input_panel import decode_text_file

    text = "Привет, мир. Это короткий рассказ о жизни в большом городе. " * 30
    assert decode_text_file(text.encode("utf-16-le")) == text


def test_binary_with_sparse_nuls_is_still_refused():
    from app.ui.panels.input_panel import decode_text_file

    data = bytes(range(256)) * 40
    assert decode_text_file(data) is None


# ── Input panel ──────────────────────────────────────────────────────── #

def test_resize_does_not_rescan_the_document(qtbot):
    from app.ui.panels.input_panel import InputPanel

    panel = InputPanel()
    qtbot.addWidget(panel)
    panel.set_text("one two three")
    assert "3 words" in panel._count_label.toolTip()

    calls = []
    real = panel._editor.toPlainText
    panel._editor.toPlainText = lambda: (calls.append(1), real())[1]
    panel.resize(900, 500)
    panel.show()
    panel.resize(600, 400)
    QApplication.processEvents()
    assert calls == []
    assert "3 words" in panel._count_label.toolTip()


def _drag_enter(widget, mime):
    event = QDragEnterEvent(QPoint(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(widget.viewport(), event)
    return event


def test_dropping_a_docx_is_refused_with_a_message(qtbot, tmp_path):
    from app.ui.panels.input_panel import InputPanel

    panel = InputPanel()
    qtbot.addWidget(panel)
    messages = []
    panel.status_message.connect(messages.append)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path / "book.docx"))])
    event = _drag_enter(panel._editor, mime)
    assert not event.isAccepted()
    assert messages and "book.docx" in messages[0]


def test_text_and_web_links_still_drop_in(qtbot, tmp_path):
    from app.ui.panels.input_panel import InputPanel

    panel = InputPanel()
    qtbot.addWidget(panel)
    mime = QMimeData()
    mime.setText("words from a web page")
    mime.setUrls([QUrl("https://example.com/article")])
    assert _drag_enter(panel._editor, mime).isAccepted()

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path / "book.txt"))])
    assert _drag_enter(panel._editor, mime).isAccepted()


# ── Main window ──────────────────────────────────────────────────────── #

def test_maximized_state_is_saved_and_restored(qtbot, app_paths, monkeypatch):
    from app.config.settings import AppSettings
    from app.ui.main_window import MainWindow
    from app.workers import voice_loader

    monkeypatch.setattr(voice_loader.VoiceLoaderWorker, "start", lambda self: None)
    settings = AppSettings(app_paths)
    settings.window_maximized = True
    win = MainWindow(settings=settings, paths=app_paths)
    qtbot.addWidget(win)
    assert win.windowState() & Qt.WindowState.WindowMaximized
    win.ensure_workers_stopped()

    settings2 = AppSettings(app_paths)
    win2 = MainWindow(settings=settings2, paths=app_paths)
    qtbot.addWidget(win2)
    win2.show()
    win2._save_window_state()
    assert settings2.window_maximized is False
    win2.ensure_workers_stopped()
