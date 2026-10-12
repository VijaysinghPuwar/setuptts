"""
Regression tests for the Windows fixes in 1.7.3.

Each test documents the user-visible symptom it guards.
"""

from __future__ import annotations

import ctypes
import sys
import types
import urllib.request
from pathlib import Path

import pytest

from app.services import local_tts, tts_service
from app.services.local_tts import Mp3Writer
from app.utils import file_ops, paths
from app.utils.output_paths import check_output_path


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(file_ops.time, "sleep", lambda _s: None)


# ------------------------------------------------------------------ #
# Default save folder                                                 #
# ------------------------------------------------------------------ #

def test_default_output_dir_follows_a_relocated_desktop(monkeypatch, tmp_path):
    """OneDrive moves the Desktop; output went to a ~/Desktop nobody sees."""
    moved = tmp_path / "OneDrive" / "Desktop"
    moved.mkdir(parents=True)
    monkeypatch.setattr(paths, "user_desktop_dir", lambda: str(moved))
    assert paths.default_output_dir() == str(moved)


def test_default_output_dir_falls_back_to_an_existing_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "user_desktop_dir", lambda: str(tmp_path / "gone"))
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.default_output_dir() == str(tmp_path)
    assert Path(paths.default_output_dir()).is_dir()


def test_no_ui_code_hardcodes_home_desktop():
    root = Path(__file__).resolve().parents[1] / "app" / "ui"
    offenders = [str(p) for p in root.rglob("*.py")
                 if 'Path.home() / "Desktop"' in p.read_text(encoding="utf-8")]
    assert not offenders


# ------------------------------------------------------------------ #
# Show in folder                                                      #
# ------------------------------------------------------------------ #

def test_reveal_quotes_the_path_for_explorer(monkeypatch, tmp_path):
    """A space or comma in the path opened Documents instead of selecting the file."""
    target = tmp_path / "My Books, vol 1" / "Chapter 1.mp3"
    target.parent.mkdir()
    target.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setattr(paths.subprocess, "Popen", lambda arg, *a, **k: calls.append(arg))
    assert paths.open_in_file_manager(target, reveal=True)
    assert calls == [f'explorer /select,"{target}"']


# ------------------------------------------------------------------ #
# Offline-voice MP3 whose target is open elsewhere                   #
# ------------------------------------------------------------------ #

def test_finished_render_is_kept_when_the_target_is_locked(monkeypatch, tmp_path, fast_retries):
    """Replacing a file open in a media player threw away the whole render."""
    out = tmp_path / "book.mp3"
    out.write_bytes(b"old")
    real = file_ops.replace_with_retry

    def locked(src, dst, attempts=5):
        if Path(dst) == out:
            raise PermissionError(32, "The process cannot access the file")
        real(src, dst, attempts)

    monkeypatch.setattr(local_tts, "replace_with_retry", locked)
    w = Mp3Writer(out, 22_050)
    w.write(b"\x00\x01" * 22_050)
    saved = w.finish()
    assert saved == tmp_path / "book (2).mp3"
    assert saved.stat().st_size > 0
    assert out.read_bytes() == b"old"
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file locking")
def test_finished_render_survives_a_real_open_handle(tmp_path, fast_retries):
    out = tmp_path / "book.mp3"
    out.write_bytes(b"old")
    with open(out, "rb"):                       # no FILE_SHARE_DELETE: rename fails
        w = Mp3Writer(out, 22_050)
        w.write(b"\x00\x01" * 22_050)
        saved = w.finish()
    assert saved.name == "book (2).mp3"
    assert out.read_bytes() == b"old"


def test_a_missing_target_still_reports_the_error(monkeypatch, tmp_path, fast_retries):
    """Only an existing, in-use target is saved aside — a real failure surfaces."""
    def denied(src, dst, attempts=5):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(local_tts, "replace_with_retry", denied)
    w = Mp3Writer(tmp_path / "new.mp3", 22_050)
    w.write(b"\x00\x01" * 100)
    with pytest.raises(PermissionError):
        w.finish()


def test_job_queue_records_the_path_the_worker_saved_to(qapp, tmp_path):
    from app.workers.job_queue import JobItem, JobQueue

    queue = JobQueue()
    item = JobItem(id="j1", text="hi", voice="v", voice_display="V", rate="+0%",
                   volume="+0%", output_path=str(tmp_path / "book.mp3"))
    worker = types.SimpleNamespace(audio_duration_seconds=1.0)
    queue._running["j1"] = (item, worker)
    done = []
    queue.job_completed.connect(done.append)
    queue._on_worker_completed("j1", str(tmp_path / "book (2).mp3"), 1.0)
    assert done[0].output_path == str(tmp_path / "book (2).mp3")
    assert done[0].filename == "book (2).mp3"


# ------------------------------------------------------------------ #
# Voice download rename                                               #
# ------------------------------------------------------------------ #

def test_replace_rides_out_a_brief_scanner_lock(monkeypatch, tmp_path, fast_retries):
    """Defender scanning a fresh voice model made the rename fail at once."""
    src, dst = tmp_path / "v.onnx.part", tmp_path / "v.onnx"
    src.write_bytes(b"model")
    real = Path.replace
    fails = [2]

    def flaky(self, target):
        if fails[0]:
            fails[0] -= 1
            raise PermissionError(32, "in use")
        return real(self, target)

    monkeypatch.setattr(file_ops.sys, "platform", "win32")
    monkeypatch.setattr(Path, "replace", flaky)
    file_ops.replace_with_retry(src, dst)
    assert dst.read_bytes() == b"model"


# ------------------------------------------------------------------ #
# Reserved names                                                      #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("name", ["COM¹", "lpt³", "CONIN$", "conout$.mp3"])
def test_more_windows_device_names_are_refused(tmp_path, name):
    problem = check_output_path(str(tmp_path), name)
    assert problem is not None and "reserves" in problem.message


# ------------------------------------------------------------------ #
# Proxy bypass lookup                                                 #
# ------------------------------------------------------------------ #

def test_proxy_bypass_lookup_is_not_repeated_per_chunk(monkeypatch):
    """With a bypass list, each check did DNS lookups inside the event loop."""
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tts_service, "_bypass_cache", None)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://p:8080"})
    calls = []
    monkeypatch.setattr(urllib.request, "proxy_bypass",
                        lambda host: calls.append(host) or False)
    for _ in range(50):
        assert tts_service.system_proxy() == "http://p:8080"
    assert len(calls) == 1
    # A changed proxy setting is noticed straight away.
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://q:3128"})
    assert tts_service.system_proxy() == "http://q:3128"
    assert len(calls) == 2


# ------------------------------------------------------------------ #
# Preview playback (Windows MCI)                                      #
# ------------------------------------------------------------------ #

class _FakeWinmm:
    def __init__(self, on_open=None) -> None:
        self.sent: list[str] = []
        self.open_aliases: set[str] = set()
        self._on_open = on_open

    def mciSendStringW(self, cmd, buf, size, hwnd):
        self.sent.append(cmd)
        words = cmd.split()
        if words[0] == "open":
            alias = words[-1]
            if alias in self.open_aliases:
                return 289                      # MCIERR_DUPLICATE_ALIAS
            self.open_aliases.add(alias)
            if self._on_open:
                self._on_open()
        elif words[0] == "close":
            self.open_aliases.discard(words[1])
        elif words[0] == "status" and buf is not None:
            buf.value = "stopped"
        return 0


def _patch_winmm(monkeypatch, fake):
    from app.workers import preview_worker

    fake_ctypes = types.SimpleNamespace(
        windll=types.SimpleNamespace(winmm=fake),
        create_unicode_buffer=ctypes.create_unicode_buffer,
    )
    monkeypatch.setattr(preview_worker, "ctypes", fake_ctypes)
    return preview_worker


def test_stop_just_before_mci_open_still_closes_the_device(monkeypatch, tmp_path):
    """The device played on, locked the file and broke every later preview."""
    holder = {}

    def stop_from_gui():
        holder["w"].stop_playback()

    fake = _FakeWinmm(on_open=stop_from_gui)
    preview_worker = _patch_winmm(monkeypatch, fake)
    w = preview_worker.PreviewWorker("en-US-AvaNeural", "+0%")
    holder["w"] = w
    w._mci_alias = None
    monkeypatch.setattr(w, "_play_windows_fallback",
                        lambda p: pytest.fail("fell back to an external player"))
    w._play_windows(str(tmp_path / "a.mp3"))
    assert not fake.open_aliases
    assert not any(c.startswith("play") for c in fake.sent)


def test_back_to_back_previews_each_get_a_device(monkeypatch, tmp_path):
    fake = _FakeWinmm()
    preview_worker = _patch_winmm(monkeypatch, fake)
    for _ in range(3):
        w = preview_worker.PreviewWorker("en-US-AvaNeural", "+0%")
        monkeypatch.setattr(w, "_play_windows_fallback",
                            lambda p: pytest.fail("fell back to an external player"))
        w._play_windows(str(tmp_path / "a.mp3"))
    assert not fake.open_aliases
    assert sum(c.startswith("play") for c in fake.sent) == 3


def test_external_player_fallback_leaves_the_file_for_the_player(monkeypatch, tmp_path):
    from app.workers import preview_worker

    f = tmp_path / "p.mp3"
    f.write_bytes(b"x")
    w = preview_worker.PreviewWorker("en-US-AvaNeural", "+0%")
    w._tmp_path = str(f)
    monkeypatch.setattr(preview_worker.os, "startfile", lambda p: None, raising=False)
    monkeypatch.setattr(preview_worker.time, "sleep", lambda s: None)
    w._play_windows_fallback(str(f))
    w._cleanup()
    assert f.exists()


# ------------------------------------------------------------------ #
# Build script                                                        #
# ------------------------------------------------------------------ #

def test_build_script_strings_are_ascii():
    """Windows PowerShell 5.1 read an em dash as a closing quote: the script didn't parse."""
    script = Path(__file__).resolve().parents[1] / "build_windows.ps1"
    code_lines = [l for l in script.read_text(encoding="utf-8").splitlines()
                  if not l.lstrip().startswith("#")]
    assert all(l.isascii() for l in code_lines)


def test_proxy_bypass_is_checked_for_the_host_actually_contacted(monkeypatch):
    """Synthesis moved to api.msedgeservices.com; the bypass list was checked for the old host."""
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tts_service, "_bypass_cache", None)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://p:8080"})
    asked = []
    monkeypatch.setattr(urllib.request, "proxy_bypass",
                        lambda host: asked.append(host) or host.endswith(".bing.com"))
    assert tts_service.system_proxy() == "http://p:8080"            # synthesis host
    assert asked[-1] == tts_service.synthesis_host()
    assert tts_service.system_proxy(tts_service.SPEECH_HOST) is None  # voice list host
