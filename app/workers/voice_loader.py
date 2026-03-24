"""Background QThread worker for loading the voice list from edge_tts."""

import asyncio
import logging

from PySide6.QtCore import QThread, Signal

from app.models.voice import Voice
from app.services.tts_service import list_voices

logger = logging.getLogger(__name__)


class VoiceLoaderWorker(QThread):
    """
    Fetches the full voice list from edge_tts in a background thread.

    Signals
    -------
    loaded(list[Voice])    Full voice list on success
    failed(str)            User-friendly error message
    """

    loaded = Signal(list)
    failed = Signal(str)

    def run(self) -> None:
        try:
            raw = asyncio.run(list_voices())
            voices = [Voice.from_edge_dict(d) for d in raw]
            voices.sort(key=lambda v: (v.locale, v.display_name))
            logger.info("Loaded %d voices", len(voices))
            self.loaded.emit(voices)
        except Exception as exc:
            logger.exception("Voice loading failed")
            self.failed.emit(
                "Could not load voices from the speech service.\n"
                "Please check your internet connection.\n\n"
                f"Details: {exc}"
            )
