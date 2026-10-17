"""Regressions fixed in v1.8.2 (each test fails on v1.8.1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.services import piper_tts


@pytest.fixture
def app_paths(tmp_path, monkeypatch):
    from app.utils.paths import AppPaths

    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "data"))
    return AppPaths()


# ── Offline voices: espeak-ng clause overflow ────────────────────────── #

NUMBERS = " ".join(str(n * 7919 % 100003) for n in range(1, 600)) + "."


def test_bound_clauses_leaves_prose_alone():
    prose = ("It was the best of times, it was the worst of times. And so it went on "
             "and on without a pause for a very long while indeed until the end.")
    assert piper_tts._bound_clauses(prose) == prose


def test_bound_clauses_keeps_every_word_and_bounds_each_clause():
    out = piper_tts._bound_clauses(NUMBERS)
    assert out.replace(",", "").split() == NUMBERS.replace(",", "").split()
    for clause in out.split(","):
        assert sum(piper_tts._token_cost(t) for t in clause.split()) <= piper_tts._CLAUSE_COST_BUDGET


def test_bound_clauses_groups_giant_digit_runs():
    out = piper_tts._bound_clauses("1234567890" * 30)
    assert "".join(out.replace(",", "").split()) == "1234567890" * 30
    assert max(len(t.strip(",")) for t in out.split()) <= 3


def _bundled_voice():
    if not piper_tts.piper_available() or piper_tts.model_path(piper_tts.BUNDLED_VOICE) is None:
        pytest.skip("bundled Piper voice not present")
    return piper_tts._load(piper_tts.model_path(piper_tts.BUNDLED_VOICE), False)


def test_long_number_run_is_phonemized_in_full():
    voice = _bundled_voice()

    def count(text):
        return sum(len(s) for s in voice.phonemize(piper_tts._bound_clauses(text)))

    words = NUMBERS.rstrip(".").split()
    whole = count(NUMBERS)
    separate = sum(count(w) for w in words)
    # v1.8.1 kept 7 435 of ~26 000 phonemes here.
    assert whole >= 0.95 * separate


# ── Resuming one saved job twice ─────────────────────────────────────── #

def _saved_job(paths, job_id="job12345"):
    from app.workers.chunk_store import ChunkStore, _text_hash

    text = "Chapter one. " * 40
    store = ChunkStore.create(paths.staging_dir, job_id, voice="en-US-AvaNeural", rate="+0%",
                              volume="+0%", output_path=str(paths.staging_dir / "gone" / "b.mp3"),
                              text=text)
    store.record_chunk(0, start_char=0, end_char=100, text_hash=_text_hash(text[:100]),
                       audio_bytes=b"ID3fake")
    store.mark_failed(1, 2)
    store.release()
    return store, text


def _queue_that_never_starts(monkeypatch):
    from app.workers.job_queue import JobQueue

    monkeypatch.setattr(JobQueue, "_try_start", lambda self: None)
    return JobQueue()


def _submit(queue, out, **kw):
    return queue.submit(text="t", voice="en-US-AvaNeural", voice_display="Ava", rate="+0%",
                        volume="+0%", output_path=str(out), **kw)


def test_queue_refuses_second_job_with_same_id_or_staging_dir(tmp_path, monkeypatch):
    queue = _queue_that_never_starts(monkeypatch)
    _submit(queue, tmp_path / "a.mp3", job_id="job1", resume_staging_dir=str(tmp_path / "s"))
    with pytest.raises(ValueError):
        _submit(queue, tmp_path / "b.mp3", job_id="job1")
    with pytest.raises(ValueError):
        _submit(queue, tmp_path / "c.mp3", job_id="job2", resume_staging_dir=str(tmp_path / "s"))
    assert queue.has_active_job("job1")
    assert not queue.has_active_job("other")


def test_try_resume_refuses_a_folder_another_worker_owns(app_paths):
    from app.workers.chunk_store import ChunkStore, is_live

    store, text = _saved_job(app_paths)
    first = ChunkStore.try_resume(store.staging_dir, text, "en-US-AvaNeural")
    assert first is not None
    assert ChunkStore.try_resume(store.staging_dir, text, "en-US-AvaNeural") is None
    first.release()
    assert not is_live(store.staging_dir)
    again = ChunkStore.try_resume(store.staging_dir, text, "en-US-AvaNeural")
    assert again is not None
    again.release()


def test_failed_resume_check_does_not_leave_folder_claimed(app_paths):
    from app.workers.chunk_store import ChunkStore, is_live

    store, _ = _saved_job(app_paths)
    assert ChunkStore.try_resume(store.staging_dir, "different text", None) is None
    assert not is_live(store.staging_dir)


def test_queued_resume_is_no_longer_offered_or_discardable(qtbot, app_paths, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from app.config.settings import AppSettings
    from app.services.history_service import HistoryService
    from app.ui.panels import output_panel as op
    from app.workers import voice_loader
    from app.workers.job_queue import JobQueue

    monkeypatch.setattr(voice_loader.VoiceLoaderWorker, "start", lambda self: None)
    monkeypatch.setattr(JobQueue, "_try_start", lambda self: None)
    store, _ = _saved_job(app_paths)
    answers = iter([str(tmp_path / "x.mp3"), str(tmp_path / "y.mp3")])
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (next(answers), "")))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[1])))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))

    panel = op.OutputPanel(settings=AppSettings(app_paths),
                           history=HistoryService(tmp_path / "h.db"))
    qtbot.addWidget(panel)
    candidate = panel._resume_candidates[0]
    panel._resume_candidate(candidate)
    assert panel._queue.pending_count == 1
    assert panel._resume_candidates == []          # v1.8.1 still offered it

    panel._resume_candidate(candidate)             # a stale menu entry
    panel._discard_candidate(candidate)
    assert panel._queue.pending_count == 1
    assert store.staging_dir.exists()               # v1.8.1 deleted it
    assert warnings == ["Job Already Active", "Job Already Active"]


# ── Online voices: stream cleanup and log noise ──────────────────────── #

def test_cancel_mid_stream_closes_the_stream(qapp, monkeypatch):
    from app.workers import tts_worker as tw

    state = {"opened": 0, "closed": 0}

    class Res:
        async def __aenter__(self):
            state["opened"] += 1
            return self

        async def __aexit__(self, *a):
            await asyncio.sleep(0)
            state["closed"] += 1

    worker = tw.TTSWorker("hello world", "en-US-AvaNeural", "+0%", "+0%", "x.mp3")

    class FakeComm:
        async def stream(self):
            async with Res():
                for i in range(50):
                    yield {"type": "audio", "data": b"\xff" * 100}
                    if i == 3:
                        worker._cancelled = True

    monkeypatch.setattr(tw, "build_communicate", lambda **kw: FakeComm())
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(asyncio.CancelledError):
            loop.run_until_complete(worker._synthesise_attempt(
                "hello world", chunk_label="c", total_chars=11, progress_state=None,
                timeout_s=60, first_audio_timeout_s=28))
    finally:
        loop.close()
    assert state == {"opened": 1, "closed": 1}


def test_attempt_failure_text_drops_websocket_query_string():
    from app.workers.tts_worker import _AttemptFailure, _redact_url_queries

    raw = ("429, message='Invalid response status', url='wss://api.example.com/tts/v1"
           "?Ocp-Apim-Subscription-Key=ABC&ConnectionId=d5&Sec-MS-GEC=3E95'")
    failure = _AttemptFailure("network", f"failed on chunk 2/2: {raw}")
    assert "Subscription-Key" not in str(failure)
    assert "wss://api.example.com/tts/v1?" in str(failure)
    plain = "see https://example.com/a for info"
    assert _redact_url_queries(plain) == plain


# ── Text cleanup ─────────────────────────────────────────────────────── #

def test_scene_break_line_keeps_paragraph_break():
    from app.services.tts_quality import normalize_text_for_tts

    out = normalize_text_for_tts("End of scene.\n*\n\n\nChapter Two begins here")
    assert "\n\n" in out
    assert normalize_text_for_tts("- item one\n- item two") == "item one\nitem two"


def test_citation_marks_dropped_and_brackets_not_glued():
    from app.services.tts_quality import normalize_text_for_tts

    assert normalize_text_for_tts("Paris[2] is the capital of France[3][4].") == \
        "Paris is the capital of France."
    assert normalize_text_for_tts("Use a[i] and b[j] here") == "Use a i and b j here"
    # CJK brackets are quotation marks: no spaces added.
    assert " " not in normalize_text_for_tts("「はい」と言った")


def test_skin_tone_modifiers_are_not_left_to_be_read_aloud():
    from app.services.tts_quality import normalize_text_for_tts

    assert normalize_text_for_tts("Hi \U0001F44B\U0001F3FF all") == "Hi all"
    assert normalize_text_for_tts("\U0001F44D\U0001F3FD") == ""


def test_spoken_symbols_and_fractions_survive_nfkc():
    from app.services.tts_quality import normalize_text_for_tts

    assert normalize_text_for_tts("Brand™ costs ⅔ more") == "Brand™ costs ⅔ more"
    # Ordinary compatibility folding still happens.
    assert normalize_text_for_tts("Ｆｕｌｌ ﬁ") == "Full fi"


def test_one_katakana_word_does_not_make_english_japanese():
    from app.services.tts_quality import build_text_profile

    english = build_text_profile("The sign read ラーメン. "
                                 "We went inside and ate a big bowl of noodles together.")
    assert english.language_code == "en"
    assert build_text_profile("今日はとても良い天気ですね。").language_code == "ja"


# ── Settings ─────────────────────────────────────────────────────────── #

def test_settings_with_utf8_bom_are_kept(app_paths):
    from app.config.settings import AppSettings

    app_paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    app_paths.settings_path.write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"voice": "en-GB-RyanNeural", "rate": 12}).encode())
    settings = AppSettings(app_paths)
    assert settings.voice == "en-GB-RyanNeural"
    assert settings.rate == 12
    assert not app_paths.settings_path.with_name("settings.json.corrupt").exists()


def test_unreadable_settings_file_is_not_overwritten(app_paths, monkeypatch):
    from app.config import settings as settings_mod
    from app.config.settings import AppSettings

    app_paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"voice": "en-GB-RyanNeural"})
    app_paths.settings_path.write_text(original, encoding="utf-8")
    real_read = Path.read_bytes

    def locked(self):
        if self == app_paths.settings_path:
            raise PermissionError(13, "locked by another process")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", locked)
    monkeypatch.setattr(settings_mod.time, "sleep", lambda s: None)
    settings = AppSettings(app_paths)
    settings.save()
    monkeypatch.setattr(Path, "read_bytes", real_read)
    assert app_paths.settings_path.read_text(encoding="utf-8") == original


# ── UI ───────────────────────────────────────────────────────────────── #

def test_history_remove_targets_the_clicked_job_when_rows_shift(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QMenu

    from app.models.job import Job, JobStatus
    from app.services.history_service import HistoryService
    from app.ui.panels import history_panel as hp

    svc = HistoryService(tmp_path / "h.db")

    def mk(name):
        return svc.add_job(Job(id=None, text_preview=name, voice="en-US-AvaNeural", rate="+0%",
                               output_path=str(tmp_path / f"{name}.mp3"), duration_seconds=3,
                               status=JobStatus.COMPLETED))

    mk("A")
    mk("B")
    panel = hp.HistoryPanel(svc)
    qtbot.addWidget(panel)
    panel.resize(800, 300)
    panel.show()

    class MenuWhileJobFinishes(QMenu):
        def exec(self, *a):
            panel.add_job(mk("C"))
            return self.actions()[-1]

    monkeypatch.setattr(hp, "QMenu", MenuWhileJobFinishes)
    assert [j.text_preview for j in panel._jobs] == ["B", "A"]
    panel._show_menu(QPoint(10, panel._table.rowViewportPosition(1) + 5))
    assert [j.text_preview for j in panel._jobs] == ["C", "B"]
    assert [j.text_preview for j in svc.get_jobs(limit=50)] == ["C", "B"]


def test_loading_a_file_over_typed_text_can_be_undone(qtbot):
    from app.ui.panels.input_panel import InputPanel

    panel = InputPanel()
    qtbot.addWidget(panel)
    panel._editor.insertPlainText("An hour of typing")
    panel.set_text("Hello from file")
    assert panel.get_text() == "Hello from file"
    panel._editor.undo()
    assert panel.get_text() == "An hour of typing"


def test_plain_text_drag_is_accepted(qtbot):
    from PySide6.QtCore import QMimeData, QPoint, Qt
    from PySide6.QtGui import QDragEnterEvent
    from PySide6.QtWidgets import QApplication

    from app.ui.panels.input_panel import _DropAwareTextEdit

    edit = _DropAwareTextEdit()
    qtbot.addWidget(edit)
    edit.show()
    mime = QMimeData()
    mime.setText("dragged words")
    event = QDragEnterEvent(QPoint(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(edit.viewport(), event)
    assert event.isAccepted()


def test_shutdown_waits_for_all_workers_together(monkeypatch):
    from app.workers import job_queue
    from app.workers.job_queue import JobItem, JobQueue

    waits = []

    class SlowWorker:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def isRunning(self):
            return True

        def wait(self, ms):
            # Every worker is told to stop before anything waits.
            assert all(w.cancelled for w in workers[:2])
            waits.append(ms)
            return False

        def terminate(self):
            pass

    workers = [SlowWorker(), SlowWorker(), SlowWorker()]
    queue = JobQueue()
    for i, w in enumerate(workers[:2]):
        queue._running[f"j{i}"] = (JobItem(text="t", voice="v", voice_display="v", rate="+0%",
                                           volume="+0%", output_path=f"{i}.mp3"), w)
    queue._finishing.append(workers[2])
    clock = iter([0.0, 3.9, 4.5, 4.5])
    monkeypatch.setattr(job_queue.time, "monotonic", lambda: next(clock))
    queue.cancel_all()
    stop_waits = [ms for ms in waits if ms != 1_000]
    # One shared 4 s budget, not 4 s per worker.
    assert len(stop_waits) == 3
    assert sum(stop_waits) <= 4_000


def test_preview_failure_note_survives_playback_finished(qtbot, app_paths, monkeypatch, tmp_path):
    from app.config.settings import AppSettings
    from app.services.history_service import HistoryService
    from app.ui.panels import output_panel as op
    from app.workers import voice_loader

    monkeypatch.setattr(voice_loader.VoiceLoaderWorker, "start", lambda self: None)
    panel = op.OutputPanel(settings=AppSettings(app_paths),
                           history=HistoryService(tmp_path / "h.db"))
    qtbot.addWidget(panel)
    panel._on_preview_failed("Could not generate preview.\n\nDetails: boom")
    panel._on_preview_done()          # the worker's playback_finished, right after
    assert panel._preview_status.text() == "Preview unavailable"


def test_generate_stays_disabled_during_debounce(qtbot, app_paths, monkeypatch, tmp_path):
    from app.config.settings import AppSettings
    from app.services.history_service import HistoryService
    from app.ui.panels import output_panel as op
    from app.workers import voice_loader

    monkeypatch.setattr(voice_loader.VoiceLoaderWorker, "start", lambda self: None)
    panel = op.OutputPanel(settings=AppSettings(app_paths),
                           history=HistoryService(tmp_path / "h.db"))
    qtbot.addWidget(panel)
    monkeypatch.setattr(op.QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    panel._on_generate()              # no text: warns and returns
    panel.on_has_text_changed(True)   # flushed typing arrives during the debounce
    assert not panel._generate_btn.isEnabled()
