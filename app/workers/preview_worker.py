"""
Background worker for voice preview audio generation and playback.

Generates a short MP3 sample, plays it inline (no window opened),
then cleans up the temp file automatically.
"""

import asyncio
import ctypes
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.services.tts_service import generate_audio

logger = logging.getLogger(__name__)

_PREVIEW_TEXT = (
    "Hello! This is a preview of the selected voice. "
    "You can use this voice to generate full audio from any text."
)


class PreviewWorker(QThread):
    """
    Generate a short audio sample for the chosen voice and play it inline.

    Does NOT open any external window or app — audio plays directly
    through the system audio output.

    Signals
    -------
    started_playing()   Audio is playing; caller can show "stop" state.
    finished()          Playback complete or worker stopped.
    failed(str)         User-friendly error message.
    """

    started_playing = Signal()
    finished = Signal()
    failed = Signal(str)

    def __init__(self, voice: str, rate: str) -> None:
        super().__init__()
        self._voice = voice
        self._rate = rate
        self._stop_requested = False
        self._tmp_path: str | None = None

        # Platform playback handle (used by stop())
        self._afplay_proc: subprocess.Popen | None = None  # macOS
        self._mci_alias: str | None = None                  # Windows

    def stop_playback(self) -> None:
        """Request early stop of playback."""
        self._stop_requested = True
        self._kill_playback()

    # ------------------------------------------------------------------ #

    def run(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        self._tmp_path = tmp.name

        try:
            asyncio.run(
                generate_audio(
                    text=_PREVIEW_TEXT,
                    voice=self._voice,
                    rate=self._rate,
                    volume="+0%",
                    output_path=self._tmp_path,
                )
            )
        except Exception as exc:
            logger.exception("Preview generation failed")
            self._cleanup()
            if not self._stop_requested:
                self.failed.emit(
                    f"Could not generate preview.\n\nDetails: {exc}"
                )
            return

        if self._stop_requested:
            self._cleanup()
            self.finished.emit()   # always notify UI so buttons reset
            return

        self.started_playing.emit()
        try:
            self._play_blocking(self._tmp_path)
        except Exception as exc:
            logger.exception("Preview playback failed")
            if not self._stop_requested:
                self.failed.emit(f"Could not play preview: {exc}")
        finally:
            self._cleanup()
            self.finished.emit()

    # ------------------------------------------------------------------ #
    # Platform-specific blocking playback                                  #
    # ------------------------------------------------------------------ #

    def _play_blocking(self, path: str) -> None:
        if sys.platform == "darwin":
            self._play_macos(path)
        elif sys.platform == "win32":
            self._play_windows(path)
        else:
            self._play_linux(path)

    def _play_macos(self, path: str) -> None:
        """afplay is bundled with macOS — no external dependency."""
        self._afplay_proc = subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._afplay_proc.wait()
        self._afplay_proc = None

    def _play_windows(self, path: str) -> None:
        """
        Play MP3 via Windows MCI (Media Control Interface).
        Built into Windows — no extra install required.
        """
        try:
            winmm = ctypes.windll.winmm  # type: ignore[attr-defined]
        except AttributeError:
            # Shouldn't happen on Windows, but fall back gracefully
            self._play_windows_fallback(path)
            return

        alias = "vc_preview"
        self._mci_alias = alias
        win_path = Path(path).as_posix().replace("/", "\\")

        ret = winmm.mciSendStringW(
            f'open "{win_path}" type mpegvideo alias {alias}',
            None, 0, None,
        )
        if ret != 0:
            self._play_windows_fallback(path)
            return

        winmm.mciSendStringW(f"play {alias}", None, 0, None)

        # Poll until done or stop requested (max 60 s)
        buf = ctypes.create_unicode_buffer(256)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not self._stop_requested:
            winmm.mciSendStringW(
                f"status {alias} mode", buf, 256, None
            )
            if buf.value not in ("playing", ""):
                break
            time.sleep(0.2)

        # Only close if _kill_playback() hasn't already closed it.
        # _kill_playback() clears self._mci_alias; alias is a local copy.
        if self._mci_alias:
            winmm.mciSendStringW(f"close {self._mci_alias}", None, 0, None)
            self._mci_alias = None

    def _play_windows_fallback(self, path: str) -> None:
        """Last resort: open with OS default media player."""
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            time.sleep(8)
        except Exception as exc:
            raise RuntimeError(f"MCI and fallback both failed: {exc}") from exc

    def _play_linux(self, path: str) -> None:
        """Try common Linux audio players in order."""
        for player in ("mpg123", "mpg321", "ffplay", "cvlc"):
            try:
                proc = subprocess.Popen(
                    [player, path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                proc.wait()
                return
            except FileNotFoundError:
                continue
        # xdg-open as last resort
        proc = subprocess.Popen(
            ["xdg-open", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(8)

    # ------------------------------------------------------------------ #

    def _kill_playback(self) -> None:
        if self._afplay_proc and self._afplay_proc.poll() is None:
            try:
                self._afplay_proc.terminate()
            except Exception:
                pass

        if self._mci_alias:
            try:
                winmm = ctypes.windll.winmm  # type: ignore[attr-defined]
                winmm.mciSendStringW(
                    f"close {self._mci_alias}", None, 0, None
                )
            except Exception:
                pass
            self._mci_alias = None

    def _cleanup(self) -> None:
        if self._tmp_path:
            try:
                Path(self._tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
            self._tmp_path = None
