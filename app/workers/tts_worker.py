"""
Background QThread worker for TTS generation.

Uses edge_tts streaming API so we get real word-boundary progress events
instead of a fake static percentage.  Progress goes 3 % → 95 % as words
are synthesised, then jumps to 100 % once the file is fully written.

Cancellation is handled by cancelling the asyncio Task that owns the
streaming loop, which causes the async-for to raise CancelledError and
tears down the WebSocket connection cleanly.
"""

import asyncio
import logging
import time
from pathlib import Path

import edge_tts
from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)


class TTSWorker(QThread):
    """
    Signals
    -------
    progress(int)            0-100 based on words processed
    status_changed(str)      Short status string for the UI
    completed(str, float)    output_path, elapsed seconds
    failed(str)              User-friendly error message
    """

    progress       = Signal(int)
    status_changed = Signal(str)
    completed      = Signal(str, float)
    failed         = Signal(str)

    def __init__(
        self,
        text: str,
        voice: str,
        rate: str,
        volume: str,
        output_path: str,
    ) -> None:
        super().__init__()
        self._text        = text
        self._voice       = voice
        self._rate        = rate
        self._volume      = volume
        self._output_path = output_path
        self._cancelled   = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Public                                                               #
    # ------------------------------------------------------------------ #

    def cancel(self) -> None:
        """Cancel mid-stream.  Interrupts the async Task cleanly."""
        self._cancelled = True
        self.requestInterruption()
        loop = self._loop
        task = self._async_task
        if loop and not loop.is_closed() and task:
            loop.call_soon_threadsafe(task.cancel)

    # ------------------------------------------------------------------ #
    # QThread.run                                                          #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        start = time.monotonic()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._run_with_task())

            if self._cancelled:
                return

            elapsed = time.monotonic() - start
            logger.info(
                "Generation complete: voice=%s output=%s elapsed=%.2fs",
                self._voice, self._output_path, elapsed,
            )
            self.completed.emit(self._output_path, elapsed)

        except asyncio.CancelledError:
            logger.info("Generation cancelled: %s", self._output_path)

        except Exception as exc:
            logger.exception("TTS generation failed")
            if not self._cancelled:
                self.failed.emit(self._user_message(exc))

        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop      = None
            self._async_task = None
            asyncio.set_event_loop(None)

    # ------------------------------------------------------------------ #
    # Async internals                                                      #
    # ------------------------------------------------------------------ #

    async def _run_with_task(self) -> None:
        """Wrap generation in a named Task so cancel() can reach it."""
        self._async_task = asyncio.current_task()
        await self._stream_generate()

    async def _stream_generate(self) -> None:
        """
        Stream from the TTS service, writing audio to disk in real time.

        WordBoundary events carry the synthesised word text; we use the
        cumulative character count to derive a smooth 3 %–95 % progress.
        """
        logger.info(
            "Starting: voice=%s rate=%s chars=%d output=%s",
            self._voice, self._rate, len(self._text), self._output_path,
        )

        self.status_changed.emit("Connecting…")
        self.progress.emit(3)

        total_chars     = max(len(self._text.strip()), 1)
        processed_chars = 0

        communicate = edge_tts.Communicate(
            text   = self._text,
            voice  = self._voice,
            rate   = self._rate,
            volume = self._volume,
        )

        output_path = Path(self._output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        self.status_changed.emit("Generating audio…")

        try:
            with open(output_path, "wb") as audio_file:
                async for chunk in communicate.stream():
                    if self._cancelled:
                        raise asyncio.CancelledError()

                    if chunk["type"] == "audio":
                        audio_file.write(chunk["data"])

                    elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                        word = chunk.get("text", "")
                        processed_chars = min(
                            processed_chars + len(word) + 1,
                            total_chars,
                        )
                        # Reserve 3 %–95 % for streaming; 95–100 for file finalise
                        pct = int(3 + (processed_chars / total_chars) * 92)
                        self.progress.emit(pct)

        except asyncio.CancelledError:
            logger.info("Stream cancelled — removing partial file")
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

        if self._cancelled:
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise asyncio.CancelledError()

        size = output_path.stat().st_size
        logger.info("File written: %s  size=%d bytes", output_path, size)

        self.status_changed.emit("Done")
        self.progress.emit(100)

    # ------------------------------------------------------------------ #
    # Error messages                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _user_message(exc: Exception) -> str:
        msg = str(exc).lower()
        if any(k in msg for k in ("connection", "network", "resolve", "ssl", "wss", "websocket")):
            return (
                "Could not reach the speech service.\n"
                "Please check your internet connection and try again."
            )
        if "timeout" in msg:
            return "The speech service timed out. Please try again in a moment."
        if any(k in msg for k in ("permission", "access denied", "read-only")):
            return (
                "Cannot write to the selected output location.\n"
                "Please choose a different folder."
            )
        if any(k in msg for k in ("no such file", "directory")):
            return (
                "The output folder does not exist.\n"
                "Please select a valid save location."
            )
        return (
            f"An unexpected error occurred while generating audio.\n\n"
            f"Details: {exc}\n\n"
            "Please check the log file for more information."
        )
