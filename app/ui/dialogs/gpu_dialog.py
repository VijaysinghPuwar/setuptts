"""
GPU support — run offline (Piper) voices on an NVIDIA graphics card (Windows).

Finds the card, downloads the optional CUDA runtime (~1.3 GB), then times
the same voice on the processor and on the card in a separate process
(``SetupTTS --gpu-check``).  The card is switched on only when it is clearly
faster.  The choice takes effect the next time SetupTTS starts, because the
GPU build of onnxruntime has to be wired in before anything imports it.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

from PySide6.QtCore import QProcess, QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import AppSettings
from app.services import gpu_accel
from app.ui.dialogs.voice_manager_dialog import _adopt, _friendly
from app.utils.paths import AppPaths

logger = logging.getLogger(__name__)

#: (setting value, radio label) — order matches app.config.settings.GPU_MODES.
MODE_CHOICES = (
    ("auto", "Automatic — use whichever is faster on this PC (recommended)"),
    ("gpu", "Graphics card"),
    ("cpu", "Processor"),
)
_MODE_TITLES = {"auto": "Automatic", "gpu": "Graphics card", "cpu": "Processor"}

#: Windows PCs with no NVIDIA card (processor only, AMD or Intel graphics).
NO_NVIDIA_TEXT = ("No NVIDIA graphics card was found on this PC. Offline voices run on "
                  "the processor, which is the right choice here.")

#: The speed check normally takes one to three minutes.
CHECK_TIMEOUT_MS = 6 * 60 * 1000


class _Detector(QThread):
    """Runs nvidia-smi off the UI thread (it can take seconds)."""

    found = Signal(object)   # GpuInfo | None

    def run(self) -> None:
        try:
            gpus = gpu_accel.detect_gpus()
        except Exception:  # noqa: BLE001
            logger.warning("GPU detection failed", exc_info=True)
            gpus = []
        self.found.emit(gpus[0] if gpus else None)


class _PackDownloader(QThread):
    # object, not int: byte counts can pass 2**31.
    progress = Signal(object, object)
    done = Signal()
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            gpu_accel.download_pack(
                progress=lambda d, t: self.progress.emit(d, t),
                cancelled=lambda: self._cancel,
            )
            self.done.emit()
        except gpu_accel.DownloadCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("GPU support download failed", exc_info=True)
            self.failed.emit(_friendly(exc))


def _speedup_text(result: dict) -> str:
    try:
        return f"{float(result.get('speedup', 0)):.1f}×"
    except (TypeError, ValueError):
        return "?"


def _voice_name(key: str) -> str:
    if not key:
        return ""
    try:
        from app.services import piper_tts

        return piper_tts.display_name(key)
    except Exception:  # noqa: BLE001
        return key


def mode_line(mode: str, installed: bool, check: dict | None) -> tuple[bool, str]:
    """(use the card?, one plain sentence) for *mode*, based on gpu_accel.decide()."""
    use, why = gpu_accel.decide(mode, installed, check)
    title = _MODE_TITLES.get(mode, "Automatic")
    where = "using the graphics card" if use else "using the processor"
    if mode == "cpu":
        return use, "Processor: offline voices run on the processor."
    if mode == "auto" and installed and check and check.get("ok"):
        speed = _speedup_text(check)
        if use:
            return use, f"Automatic: {where} — it was {speed} faster than the processor."
        try:
            card = f"{float(check.get('speedup', 0)):.2f}×"
        except (TypeError, ValueError):
            card = "?"
        return use, f"Automatic: {where} — it was faster on this PC (card {card})."
    if mode == "gpu" and use:
        return use, (f"Graphics card: {where} (offline voices fall back to the "
                     "processor if it fails).")
    return use, f"{title}: {where} — {why}."


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e8 else f"{n / 1e6:.0f} MB"


class GpuDialog(QDialog):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("Graphics Card")
        self.setMinimumWidth(520)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        self._gpu: gpu_accel.GpuInfo | None = None
        self._detected = False
        self._removal_pending = False
        self._detector: _Detector | None = None
        self._downloader: _PackDownloader | None = None
        self._proc: QProcess | None = None
        self._check_out: Path | None = None

        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(CHECK_TIMEOUT_MS)
        self._check_timer.timeout.connect(self._on_check_timeout)

        self._build_ui()
        self._start_detection()

    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        ly = QVBoxLayout(self)
        ly.setContentsMargins(24, 20, 24, 16)
        ly.setSpacing(8)

        title = QLabel("Graphics card")
        title.setObjectName("dialogTitle")
        ly.addWidget(title)

        self._card_label = QLabel()
        self._card_label.setObjectName("dialogFieldLabel")
        self._card_label.setWordWrap(True)
        self._card_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ly.addWidget(self._card_label)

        self._intro = QLabel(
            "Offline voices can run on your graphics card instead of the "
            "processor. This needs a one-time download of about "
            f"{gpu_accel.DOWNLOAD_BYTES / 1e9:.1f} GB (NVIDIA CUDA libraries)."
        )
        self._intro.setObjectName("metaLabel")
        self._intro.setWordWrap(True)
        ly.addWidget(self._intro)

        self._failure_note = QLabel()
        self._failure_note.setObjectName("metaLabel")
        self._failure_note.setWordWrap(True)
        ly.addWidget(self._failure_note)

        self._mode_title = QLabel("Run offline voices on")
        self._mode_title.setObjectName("dialogFieldLabel")
        ly.addWidget(self._mode_title)
        self._mode_group = QButtonGroup(self)
        self._mode_buttons: dict[str, QRadioButton] = {}
        for i, (mode, label) in enumerate(MODE_CHOICES):
            btn = QRadioButton(label)
            self._mode_group.addButton(btn, i)
            self._mode_buttons[mode] = btn
            ly.addWidget(btn)
        self._mode_group.idClicked.connect(self._on_mode_clicked)
        self._mode_line = QLabel()
        self._mode_line.setObjectName("metaLabel")
        self._mode_line.setWordWrap(True)
        ly.addWidget(self._mode_line)

        self._result_label = QLabel()
        self._result_label.setObjectName("metaLabel")
        self._result_label.setWordWrap(True)
        ly.addWidget(self._result_label)

        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ly.addWidget(self._status)

        prog = QHBoxLayout()
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(False)
        self._progress.setAccessibleName("Progress")
        prog.addWidget(self._progress, 1)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setObjectName("ghostButton")
        self._cancel_btn.clicked.connect(self._cancel_download)
        prog.addWidget(self._cancel_btn)
        ly.addLayout(prog)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self._download_btn = QPushButton("Download GPU support")
        self._download_btn.clicked.connect(self._start_download)
        actions.addWidget(self._download_btn)
        self._test_btn = QPushButton("Test speed again")
        self._test_btn.setObjectName("ghostButton")
        self._test_btn.clicked.connect(self._start_check)
        actions.addWidget(self._test_btn)
        self._remove_btn = QPushButton("Remove GPU support")
        self._remove_btn.setObjectName("dangerGhostButton")
        self._remove_btn.clicked.connect(self._remove)
        actions.addWidget(self._remove_btn)
        actions.addStretch()
        ly.addLayout(actions)

        ly.addStretch()
        foot = QHBoxLayout()
        foot.addStretch()
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        foot.addWidget(close)
        ly.addLayout(foot)

        for w in (self._card_label, self._intro, self._failure_note, self._mode_title,
                  *self._mode_buttons.values(), self._mode_line, self._result_label, self._progress, self._cancel_btn,
                  self._download_btn, self._test_btn, self._remove_btn):
            w.hide()

    # ------------------------------------------------------------------ #
    #  State                                                              #
    # ------------------------------------------------------------------ #

    def _busy(self) -> bool:
        return self._download_running() or self._check_running()

    def _download_running(self) -> bool:
        return self._downloader is not None and self._downloader.isRunning()

    def _check_running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    def _refresh(self, message: str | None = None) -> None:
        """Show the controls that fit the current state; *message* goes in the status line."""
        failed = gpu_accel.failure()
        self._failure_note.setVisible(bool(failed))
        if failed:
            self._failure_note.setText(
                f"The graphics card stopped working during this session ({failed}); "
                "offline voices are using the processor.")

        gpu = self._gpu
        self._card_label.setVisible(gpu is not None)
        if gpu is not None:
            self._card_label.setText(f"{gpu.name} · driver {gpu.driver}")

        busy = self._busy()
        installed = gpu_accel.pack_installed() and not self._removal_pending
        check = gpu_accel.load_check() if installed else None
        problem = gpu_accel.requirement_problem(gpu) if self._detected else None
        usable = self._detected and problem is None and not self._removal_pending

        self._intro.setVisible(usable and not installed and not busy)
        self._download_btn.setVisible(usable and not installed and not busy)
        show_check = installed and check is not None and not busy
        show_mode = (usable or installed) and self._detected and not self._removal_pending
        for w in (self._mode_title, *self._mode_buttons.values(), self._mode_line):
            w.setVisible(show_mode)
        if show_mode:
            self._update_mode_ui(installed, check)
        self._result_label.setVisible(show_check)
        self._test_btn.setVisible(show_check and usable)
        # Removing stays possible even when the card no longer qualifies.
        self._remove_btn.setVisible(installed and not busy and self._detected)
        self._progress.setVisible(busy)
        self._cancel_btn.setVisible(self._download_running())

        if show_check:
            self._result_label.setText(self._result_line(check))

        if message is not None:
            self._status.setText(message)
        elif not self._detected:
            self._status.setText("Looking for an NVIDIA graphics card…")
        elif gpu is None:
            self._status.setText(NO_NVIDIA_TEXT)
        elif problem:
            self._status.setText(problem)
        elif not busy:
            self._status.setText("")
        self._status.setVisible(bool(self._status.text()))

    def _result_line(self, check: dict) -> str:
        when = str(check.get("started") or "")[:10]
        when = f" ({when})" if when else ""
        voice = _voice_name(str(check.get("voice") or ""))
        with_voice = f" with the voice {voice}" if voice else ""
        if gpu_accel.check_recommends(check):
            line = (f"Last test{when}: {_speedup_text(check)} faster than the "
                    f"processor{with_voice}.")
        elif check.get("ok"):
            line = (f"Last test{when}: {_speedup_text(check)} the processor's speed"
                    f"{with_voice} — not enough to be worth using.")
        else:
            line = f"Last test{when} did not finish: {check.get('error') or 'unknown error'}"
        if gpu_accel.activated() and not gpu_accel.failure():
            name = gpu_accel.device_name() or "the graphics card"
            line += f" Offline voices are running on {name} now."
        return line

    # ------------------------------------------------------------------ #
    #  Detection                                                          #
    # ------------------------------------------------------------------ #

    def _start_detection(self) -> None:
        self._refresh()
        self._detector = _Detector()
        self._detector.found.connect(self._on_detected)
        self._detector.start()

    def _on_detected(self, gpu) -> None:
        self._gpu = gpu
        self._detected = True
        if (gpu_accel.requirement_problem(gpu) is None and gpu_accel.pack_installed()
                and gpu_accel.load_check() is None):
            self._start_check()
            return
        self._refresh()

    # ------------------------------------------------------------------ #
    #  Download                                                           #
    # ------------------------------------------------------------------ #

    def _start_download(self) -> None:
        if self._busy():
            return
        self._downloader = _PackDownloader()
        self._downloader.progress.connect(self._on_progress)
        self._downloader.done.connect(self._on_downloaded)
        self._downloader.failed.connect(self._on_download_failed)
        self._downloader.cancelled.connect(self._on_download_cancelled)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._cancel_btn.setEnabled(True)
        self._downloader.start()
        self._refresh("Downloading GPU support…")

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            pct = min(100, int(done * 100 / total))
            self._progress.setValue(pct)
            self._status.setText(
                f"Downloading GPU support — {pct}% "
                f"({done / 1e6:.0f} of {total / 1e6:.0f} MB)")

    def _on_downloaded(self) -> None:
        self._downloader_finished()
        self._start_check()

    def _on_download_failed(self, message: str) -> None:
        self._downloader_finished()
        self._refresh(f"The download failed: {message} Click Download to try again; "
                      "it continues where it stopped.")

    def _on_download_cancelled(self) -> None:
        self._downloader_finished()
        self._refresh("Download cancelled. Click Download to continue it later.")

    def _downloader_finished(self) -> None:
        # The signal arrives just before run() returns; wait so the state
        # checks below see the thread as finished.
        if self._downloader is not None:
            self._downloader.wait(2_000)

    def _cancel_download(self) -> None:
        if self._downloader is not None:
            self._downloader.cancel()
            self._cancel_btn.setEnabled(False)

    # ------------------------------------------------------------------ #
    #  Speed check                                                        #
    # ------------------------------------------------------------------ #

    def _start_check(self) -> None:
        if self._busy():
            return
        out = AppPaths().cache_dir / f"gpu-check-{int(time.time())}.json"
        out.unlink(missing_ok=True)
        self._check_out = out
        cmd = gpu_accel.check_command(out)
        proc = QProcess(self)
        proc.setProgram(cmd[0])
        proc.setArguments(cmd[1:])
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.setStandardOutputFile(QProcess.nullDevice())
        proc.finished.connect(self._on_check_finished)
        proc.errorOccurred.connect(self._on_check_error)
        self._proc = proc
        self._progress.setRange(0, 0)   # busy indicator
        proc.start()
        self._check_timer.start()
        self._refresh("Testing speed on your graphics card… (about a minute)")

    def _read_check_output(self) -> dict | None:
        if self._check_out is None:
            return None
        try:
            data = json.loads(self._check_out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        finally:
            try:
                self._check_out.unlink(missing_ok=True)
            except OSError:
                pass
        return data if isinstance(data, dict) else None

    def _end_check(self) -> None:
        self._check_timer.stop()
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.deleteLater()
        self._progress.setRange(0, 100)

    def _on_check_finished(self, *_args) -> None:
        result = self._read_check_output()
        self._end_check()
        if result is None:
            result = {"ok": False, "recommended": False,
                      "error": "The speed test stopped unexpectedly.",
                      "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._handle_check_result(result)

    def _on_check_error(self, error) -> None:
        if error != QProcess.FailedToStart:
            return   # crashes and the like also emit finished
        self._end_check()
        self._handle_check_result({
            "ok": False, "recommended": False,
            "error": "The speed test could not be started.",
            "started": time.strftime("%Y-%m-%d %H:%M:%S")})

    def _on_check_timeout(self) -> None:
        if not self._check_running():
            return
        self._kill_check()
        self._handle_check_result({
            "ok": False, "recommended": False,
            "error": "The speed test took too long and was stopped.",
            "started": time.strftime("%Y-%m-%d %H:%M:%S")})

    def _kill_check(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.finished.disconnect()
            proc.errorOccurred.disconnect()
        except (RuntimeError, TypeError):
            pass
        # The portable build is a launcher that unpacks and starts the real
        # app as a child; kill the whole tree so no check keeps running.
        pid = proc.processId()
        if sys.platform == "win32" and pid:
            import subprocess

            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        proc.kill()
        proc.waitForFinished(2_000)
        self._end_check()

    def _handle_check_result(self, result: dict) -> None:
        """Store a speed-check verdict and report it; the chosen mode is left alone."""
        try:
            gpu_accel.save_check(result)
        except OSError:
            logger.warning("Could not save the GPU speed check result", exc_info=True)
        mode = self._settings.gpu_mode
        speed = _speedup_text(result)
        if gpu_accel.check_recommends(result):
            message = (f"Your graphics card made offline voices {speed} faster than "
                       "the processor.")
            if mode == "auto":
                message += " Automatic will use it"
                message += ("." if gpu_accel.activated()
                            else " — restart SetupTTS to start using it.")
        elif result.get("ok"):
            message = f"Your graphics card was not faster than the processor ({speed})"
            message += (", so Automatic will keep using the processor." if mode == "auto"
                        else ".")
        else:
            error = result.get("error") or "unknown error"
            if mode == "gpu":
                message = (f"Warning: the graphics card failed the speed test ({error}) "
                           "Graphics card is selected, so SetupTTS will still try it and "
                           "fall back to the processor if it fails.")
            else:
                message = (f"The speed test did not finish: {error} "
                           "SetupTTS will keep using the processor.")
        self._refresh(message)

    # ------------------------------------------------------------------ #
    #  Settings                                                           #
    # ------------------------------------------------------------------ #

    def _update_mode_ui(self, installed: bool, check: dict | None) -> None:
        mode = self._settings.gpu_mode
        btn = self._mode_buttons.get(mode)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
        use, line = mode_line(mode, installed, check)
        if use != gpu_accel.activated():
            line += " Takes effect the next time you start SetupTTS."
        self._mode_line.setText(line)

    def _on_mode_clicked(self, index: int) -> None:
        mode = MODE_CHOICES[index][0]
        if mode != self._settings.gpu_mode:
            self._settings.gpu_mode = mode
            self._settings.save()
        installed = gpu_accel.pack_installed() and not self._removal_pending
        self._update_mode_ui(installed, gpu_accel.load_check() if installed else None)

    def _remove(self) -> None:
        size = gpu_accel.pack_size_on_disk()
        answer = QMessageBox.question(
            self, "Remove GPU Support",
            f"Remove GPU support and free {_gb(size)} of disk space?\n\n"
            "Offline voices will run on the processor. You can download it "
            "again at any time.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if gpu_accel.remove_pack():
            self._refresh(f"GPU support was removed ({_gb(size)} freed).")
        else:
            self._removal_pending = True
            self._refresh("GPU support is in use right now, so it will be removed the "
                          "next time SetupTTS starts.")

    # ------------------------------------------------------------------ #

    def done(self, result: int) -> None:  # noqa: D401 - Qt override
        if self._download_running():
            answer = QMessageBox.question(
                self, "Download in Progress",
                "GPU support is still downloading. Stop the download and close?\n\n"
                "You can continue it later.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._downloader.cancel()
            self._downloader.wait(2_000)
        elif self._check_running():
            answer = QMessageBox.question(
                self, "Speed Test in Progress",
                "The speed test is still running. Stop it and close?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._kill_check()
        for t in (self._detector, self._downloader):
            if t is not None and t.isRunning():
                for sig in ("found", "progress", "done", "failed", "cancelled"):
                    try:
                        getattr(t, sig).disconnect()
                    except (AttributeError, RuntimeError, TypeError):
                        pass
                _adopt(t)
        super().done(result)
