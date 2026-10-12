"""
GPU support UI: the Windows-only "Graphics card" section in Settings and the
GpuDialog states.  Every gpu_accel entry point that would touch nvidia-smi,
the network or the app-data folder is replaced, so these run on any OS.
"""

import os

import pytest

from PySide6.QtWidgets import QLabel, QMessageBox

from app.config.settings import AppSettings
from app.services import gpu_accel
from app.utils.paths import AppPaths

pytest.importorskip("pytestqt", reason="pytest-qt is required for UI tests")

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture(scope="session", autouse=True)
def _offscreen():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


RTX = gpu_accel.GpuInfo("NVIDIA GeForce RTX 4070", "581.42", 12282, (8, 9))
OLD_DRIVER = gpu_accel.GpuInfo("NVIDIA GeForce RTX 3060", "531.18", 12288, (8, 6))

GOOD = {"ok": True, "recommended": True, "speedup": 4.32, "gpu_name": RTX.name,
        "driver": RTX.driver, "voice": "en_US-lessac-medium", "error": "",
        "started": "2026-10-12 10:00:00", "pack": gpu_accel.PACK_ID}
SLOW = dict(GOOD, recommended=False, speedup=1.08)
FAILED = {"ok": False, "recommended": False, "error": "CUDA failed to start.",
          "started": "2026-10-12 10:00:00", "pack": gpu_accel.PACK_ID}


@pytest.fixture
def app_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "data"))
    return AppPaths()


@pytest.fixture
def settings(app_paths):
    return AppSettings(app_paths)


@pytest.fixture
def gpu(monkeypatch):
    """Configurable fake of gpu_accel's machine-facing functions."""

    class Fake:
        gpus = [RTX]
        installed = False
        check = None
        activated = False
        failure = None
        remove_result = True
        removed = 0

    fake = Fake()
    monkeypatch.setattr(gpu_accel, "detect_gpus", lambda *a, **k: list(fake.gpus))
    monkeypatch.setattr(gpu_accel, "pack_installed", lambda: fake.installed)
    monkeypatch.setattr(gpu_accel, "load_check", lambda: fake.check)
    monkeypatch.setattr(gpu_accel, "save_check",
                        lambda r: setattr(fake, "check", dict(r, pack=gpu_accel.PACK_ID)))
    monkeypatch.setattr(gpu_accel, "activated", lambda: fake.activated)
    monkeypatch.setattr(gpu_accel, "failure", lambda: fake.failure)
    monkeypatch.setattr(gpu_accel, "pack_size_on_disk", lambda: 2_900_000_000)

    def remove():
        fake.removed += 1
        return fake.remove_result

    monkeypatch.setattr(gpu_accel, "remove_pack", remove)

    def no_download(*a, **k):
        raise AssertionError("download_pack must not run in this test")

    monkeypatch.setattr(gpu_accel, "download_pack", no_download)
    return fake


def _open(settings, qtbot, monkeypatch=None, *, no_check=True):
    from app.ui.dialogs import gpu_dialog

    if monkeypatch is not None and no_check:
        started = []
        monkeypatch.setattr(gpu_dialog.GpuDialog, "_start_check",
                            lambda self: started.append(True))
    dialog = gpu_dialog.GpuDialog(settings)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._detected, timeout=3000)
    return dialog


def _shown(widget) -> bool:
    return not widget.isHidden()


# ------------------------------------------------------------------ #
# Settings dialog                                                      #
# ------------------------------------------------------------------ #

def _texts(dialog):
    return [lbl.text() for lbl in dialog.findChildren(QLabel)]


def test_settings_has_no_gpu_section_on_mac(monkeypatch, app_paths, settings, qtbot, gpu):
    from app.ui.dialogs.settings_dialog import SettingsDialog

    monkeypatch.setattr("sys.platform", "darwin")
    dialog = SettingsDialog(settings, app_paths)
    qtbot.addWidget(dialog)
    assert "Graphics card" not in _texts(dialog)
    assert dialog._gpu_status_label is None


def test_settings_on_windows_without_nvidia_is_cheap(monkeypatch, app_paths, settings,
                                                     qtbot, gpu):
    """Opening Settings never runs nvidia-smi; with nothing downloaded it just says Processor."""
    from app.ui.dialogs.settings_dialog import SettingsDialog

    monkeypatch.setattr("sys.platform", "win32")

    def no_smi(*a, **k):
        raise AssertionError("Settings must not run nvidia-smi")

    monkeypatch.setattr(gpu_accel, "detect_gpus", no_smi)
    dialog = SettingsDialog(settings, app_paths)
    qtbot.addWidget(dialog)
    assert dialog._gpu_status_label.text() == "Processor"


def test_settings_gpu_section_on_windows(monkeypatch, app_paths, settings, qtbot, gpu):
    from PySide6.QtWidgets import QPushButton
    from app.ui.dialogs.settings_dialog import SettingsDialog, gpu_status_text

    monkeypatch.setattr("sys.platform", "win32")
    dialog = SettingsDialog(settings, app_paths)
    qtbot.addWidget(dialog)
    texts = _texts(dialog)
    assert "Graphics card" in texts
    # Placed after the Voice section, before Logs.
    assert texts.index("Voice") < texts.index("Graphics card") < texts.index("Logs & Troubleshooting")
    assert dialog._gpu_status_label.text() == "Processor"
    assert any(b.text() == "GPU Settings…" for b in dialog.findChildren(QPushButton))



@pytest.mark.parametrize("mode, installed, check, expected", [
    ("auto", False, None, "Processor"),
    ("gpu", False, None, "Processor"),
    ("cpu", False, None, "Processor"),
    ("auto", True, None, "Automatic — processor"),
    ("auto", True, GOOD, f"Automatic — graphics card ({RTX.name})"),
    ("auto", True, SLOW, "Automatic — processor"),
    ("auto", True, FAILED, "Automatic — processor"),
    ("gpu", True, GOOD, "Graphics card"),
    ("gpu", True, SLOW, "Graphics card"),
    ("gpu", True, FAILED, "Graphics card"),
    ("cpu", True, GOOD, "Processor"),
])
def test_settings_gpu_status_text(settings, gpu, mode, installed, check, expected):
    from app.ui.dialogs.settings_dialog import gpu_status_text

    gpu.installed = installed
    gpu.check = check
    settings.gpu_mode = mode
    assert gpu_status_text(settings) == expected


# ------------------------------------------------------------------ #
# GpuDialog states                                                     #
# ------------------------------------------------------------------ #

def test_no_card(settings, qtbot, gpu, monkeypatch):
    """Processor-only PC, or AMD / Intel graphics: plain, calm, nothing to do."""
    from app.ui.dialogs.gpu_dialog import NO_NVIDIA_TEXT

    gpu.gpus = []
    dialog = _open(settings, qtbot, monkeypatch)
    assert dialog._status.text() == NO_NVIDIA_TEXT
    assert "right choice here" in dialog._status.text()
    assert not _shown(dialog._card_label)
    assert not _shown(dialog._intro)
    for w in (dialog._download_btn, *dialog._mode_buttons.values(), dialog._test_btn,
              dialog._remove_btn, dialog._progress):
        assert not _shown(w)


def test_driver_too_old(settings, qtbot, gpu, monkeypatch):
    monkeypatch.setattr(gpu_accel, "PYTHON_TAG", tuple(__import__("sys").version_info[:2]))
    gpu.gpus = [OLD_DRIVER]
    dialog = _open(settings, qtbot, monkeypatch)
    assert "driver is too old" in dialog._status.text()
    assert not _shown(dialog._download_btn)
    assert RTX.name not in dialog._card_label.text()
    assert OLD_DRIVER.name in dialog._card_label.text()


@pytest.fixture
def right_python(monkeypatch):
    import sys
    monkeypatch.setattr(gpu_accel, "PYTHON_TAG", tuple(sys.version_info[:2]))


def test_not_installed_offers_download(settings, qtbot, gpu, monkeypatch, right_python):
    dialog = _open(settings, qtbot, monkeypatch)
    assert _shown(dialog._download_btn)
    assert _shown(dialog._intro)
    assert "1.3 GB" in dialog._intro.text()
    assert RTX.name in dialog._card_label.text() and RTX.driver in dialog._card_label.text()
    assert not _shown(dialog._remove_btn)


def test_download_then_check(settings, qtbot, gpu, monkeypatch, right_python):
    from app.ui.dialogs import gpu_dialog

    def fake_download(progress=None, cancelled=None):
        progress(500_000_000, gpu_accel.DOWNLOAD_BYTES)
        gpu.installed = True

    monkeypatch.setattr(gpu_accel, "download_pack", fake_download)
    started = []
    monkeypatch.setattr(gpu_dialog.GpuDialog, "_start_check", lambda self: started.append(True))
    dialog = gpu_dialog.GpuDialog(settings)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._detected, timeout=3000)
    dialog._download_btn.click()
    qtbot.waitUntil(lambda: bool(started), timeout=3000)


def test_download_error_is_friendly(settings, qtbot, gpu, monkeypatch, right_python):
    def full_disk(progress=None, cancelled=None):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(gpu_accel, "download_pack", full_disk)
    dialog = _open(settings, qtbot, monkeypatch)
    dialog._start_download()
    qtbot.waitUntil(lambda: "failed" in dialog._status.text(), timeout=3000)
    assert "not enough free disk space" in dialog._status.text()
    assert _shown(dialog._download_btn)


def test_installed_without_check_runs_check(settings, qtbot, gpu, monkeypatch, right_python):
    from app.ui.dialogs import gpu_dialog

    gpu.installed = True
    started = []
    monkeypatch.setattr(gpu_dialog.GpuDialog, "_start_check", lambda self: started.append(True))
    dialog = gpu_dialog.GpuDialog(settings)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: bool(started), timeout=3000)


def _checked_mode(dialog):
    return next(m for m, b in dialog._mode_buttons.items() if b.isChecked())


def test_installed_mode_switching_saves(settings, qtbot, gpu, monkeypatch, right_python):
    gpu.installed = True
    gpu.check = GOOD
    dialog = _open(settings, qtbot, monkeypatch)
    assert all(_shown(b) for b in dialog._mode_buttons.values())
    assert _checked_mode(dialog) == "auto"
    assert "4.3×" in dialog._result_label.text()
    assert "2026-10-12" in dialog._result_label.text()
    assert _shown(dialog._test_btn) and _shown(dialog._remove_btn)
    assert dialog._remove_btn.objectName() == "dangerGhostButton"
    assert dialog._mode_line.text().startswith(
        "Automatic: using the graphics card — it was 4.3× faster than the processor.")
    assert "next time you start SetupTTS" in dialog._mode_line.text()

    dialog._mode_buttons["cpu"].click()
    assert settings.gpu_mode == "cpu"
    assert AppSettings(AppPaths()).gpu_mode == "cpu"      # saved to disk
    assert dialog._mode_line.text().startswith("Processor:")
    # Not activated and the processor chosen: nothing changes on restart.
    assert "next time" not in dialog._mode_line.text()

    dialog._mode_buttons["gpu"].click()
    assert AppSettings(AppPaths()).gpu_mode == "gpu"
    assert dialog._mode_line.text().startswith("Graphics card: using the graphics card")


def test_mode_line_auto_slow_card(settings, qtbot, gpu, monkeypatch, right_python):
    gpu.installed = True
    gpu.check = SLOW
    gpu.activated = False
    dialog = _open(settings, qtbot, monkeypatch)
    assert dialog._mode_line.text() == (
        "Automatic: using the processor — it was faster on this PC (card 1.08×).")


def test_mode_line_active_session_switch_to_cpu(settings, qtbot, gpu, monkeypatch,
                                                right_python):
    gpu.installed = True
    gpu.check = GOOD
    gpu.activated = True
    dialog = _open(settings, qtbot, monkeypatch)
    assert "next time" not in dialog._mode_line.text()
    dialog._mode_buttons["cpu"].click()
    assert "Takes effect the next time you start SetupTTS." in dialog._mode_line.text()


def test_modes_usable_before_download(settings, qtbot, gpu, monkeypatch, right_python):
    dialog = _open(settings, qtbot, monkeypatch)
    assert _shown(dialog._download_btn)
    dialog._mode_buttons["gpu"].click()
    assert settings.gpu_mode == "gpu"
    assert "GPU support is not downloaded" in dialog._mode_line.text()


@pytest.mark.parametrize("mode, check, start", [
    ("auto", GOOD, "Automatic: using the graphics card"),
    ("auto", SLOW, "Automatic: using the processor — it was faster"),
    ("auto", FAILED, "Automatic: using the processor — the graphics card failed"),
    ("auto", None, "Automatic: using the processor — the speed check has not run yet"),
    ("gpu", GOOD, "Graphics card: using the graphics card"),
    ("gpu", FAILED, "Graphics card: using the graphics card"),
    ("cpu", GOOD, "Processor:"),
])
def test_mode_line_text(mode, check, start):
    from app.ui.dialogs.gpu_dialog import mode_line

    assert mode_line(mode, True, check)[1].startswith(start)


def test_failure_note(settings, qtbot, gpu, monkeypatch, right_python):
    gpu.installed = True
    gpu.check = GOOD
    gpu.activated = True
    gpu.failure = "CUDA error 700"
    dialog = _open(settings, qtbot, monkeypatch)
    assert _shown(dialog._failure_note)
    assert "CUDA error 700" in dialog._failure_note.text()
    assert "using the processor" in dialog._failure_note.text()


# ------------------------------------------------------------------ #
# Speed-check results                                                  #
# ------------------------------------------------------------------ #

def test_check_recommended_leaves_mode(settings, qtbot, gpu, monkeypatch, right_python):
    gpu.installed = True
    gpu.check = FAILED
    dialog = _open(settings, qtbot, monkeypatch)
    dialog._handle_check_result(dict(GOOD))
    assert settings.gpu_mode == "auto"
    assert "4.3× faster" in dialog._status.text()
    assert "Automatic will use it" in dialog._status.text()
    assert "restart SetupTTS" in dialog._status.text()
    assert gpu.check["speedup"] == 4.32
    assert dialog._mode_line.text().startswith("Automatic: using the graphics card")


def test_check_not_faster_keeps_processor(settings, qtbot, gpu, monkeypatch, right_python):
    gpu.installed = True
    gpu.check = GOOD
    dialog = _open(settings, qtbot, monkeypatch)
    dialog._handle_check_result(dict(SLOW))
    assert settings.gpu_mode == "auto"
    assert "not faster than the processor (1.1×)" in dialog._status.text()
    assert "Automatic will keep using the processor" in dialog._status.text()
    assert dialog._mode_line.text().startswith("Automatic: using the processor")


def test_check_error_is_shown(settings, qtbot, gpu, monkeypatch, right_python):
    gpu.installed = True
    gpu.check = GOOD
    dialog = _open(settings, qtbot, monkeypatch)
    dialog._handle_check_result(dict(FAILED))
    assert settings.gpu_mode == "auto"
    assert "CUDA failed to start." in dialog._status.text()
    assert "did not finish" in dialog._result_label.text()


def test_check_failed_with_gpu_mode_warns(settings, qtbot, gpu, monkeypatch, right_python):
    gpu.installed = True
    gpu.check = GOOD
    settings.gpu_mode = "gpu"
    dialog = _open(settings, qtbot, monkeypatch)
    dialog._handle_check_result(dict(FAILED))
    assert settings.gpu_mode == "gpu"
    text = dialog._status.text()
    assert text.startswith("Warning: the graphics card failed the speed test")
    assert "fall back to the processor" in text


def test_check_output_missing_is_an_error(settings, qtbot, gpu, monkeypatch, right_python,
                                          tmp_path):
    gpu.installed = True
    gpu.check = GOOD
    dialog = _open(settings, qtbot, monkeypatch)
    dialog._check_out = tmp_path / "missing.json"
    dialog._on_check_finished(1, None)
    assert "stopped unexpectedly" in dialog._status.text()


# ------------------------------------------------------------------ #
# Removing                                                             #
# ------------------------------------------------------------------ #

def test_remove_while_in_use_says_next_time(settings, qtbot, gpu, monkeypatch, right_python):
    gpu.installed = True
    gpu.check = GOOD
    gpu.remove_result = False
    asked = []

    def yes(parent, title, text, *a, **k):
        asked.append(text)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", yes)
    dialog = _open(settings, qtbot, monkeypatch)
    dialog._remove_btn.click()
    assert gpu.removed == 1
    assert "2.9 GB" in asked[0]
    assert "next time SetupTTS starts" in dialog._status.text()
    for w in (*dialog._mode_buttons.values(), dialog._remove_btn, dialog._download_btn,
              dialog._test_btn):
        assert not _shown(w)


def test_remove_cancelled_keeps_everything(settings, qtbot, gpu, monkeypatch, right_python):
    gpu.installed = True
    gpu.check = GOOD
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No)
    dialog = _open(settings, qtbot, monkeypatch)
    dialog._remove_btn.click()
    assert gpu.removed == 0
    assert _shown(dialog._remove_btn)
