"""Background QThread worker for loading the voice list from edge_tts."""

import asyncio
import json
import logging
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.models.voice import Voice
from app.services.tts_service import list_voices

logger = logging.getLogger(__name__)

_ATTEMPTS = 3
_RETRY_DELAYS_S = (1.5, 4.0)


class VoiceLoaderWorker(QThread):
    """
    Fetches the full voice list from edge_tts in a background thread.

    A flaky connection at startup is common (the voice list is the first thing
    the app fetches), so the request is retried, and every successful list is
    saved to *cache_path*.  If the service can't be reached at all, the saved
    list is used instead — the voice picker stays usable and remembers the
    user's voice — and ``from_cache`` is set so the UI can say so.

    Signals
    -------
    loaded(list[Voice])    Voice list (live, or the saved copy)
    failed(str)            User-friendly error message
    """

    loaded = Signal(list)
    failed = Signal(str)

    def __init__(self, cache_path: Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self._cache_path = cache_path
        self.from_cache = False

    def run(self) -> None:
        last_exc: Exception | None = None
        for attempt in range(_ATTEMPTS):
            if attempt:
                time.sleep(_RETRY_DELAYS_S[attempt - 1])
            if self.isInterruptionRequested():
                return
            try:
                raw = asyncio.run(list_voices(force_refresh=attempt > 0))
                voices = _to_voices(raw)
                if not voices:
                    raise ValueError("The speech service returned an empty voice list")
                logger.info("Loaded %d voices", len(voices))
                self._save_cache(voices)
                self.loaded.emit(voices)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning("Voice loading failed (attempt %d/%d): %s",
                               attempt + 1, _ATTEMPTS, exc)

        logger.error("Voice loading failed", exc_info=last_exc)
        cached = self._load_cache()
        if cached:
            logger.info("Using %d voices from the saved voice list", len(cached))
            self.from_cache = True
            self.loaded.emit(cached)
            return
        self.failed.emit(
            "Couldn't load the voice list. Please check your internet "
            "connection, then click Retry."
            f"\n\nTechnical details: {type(last_exc).__name__}: {last_exc}"
            if last_exc else
            "Couldn't load the voice list. Please check your internet "
            "connection, then click Retry."
        )

    # ------------------------------------------------------------------ #

    def _save_cache(self, voices: list[Voice]) -> None:
        if self._cache_path is None:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_name(self._cache_path.name + ".tmp")
            tmp.write_text(json.dumps([v.to_edge_dict() for v in voices]), encoding="utf-8")
            tmp.replace(self._cache_path)
        except OSError:
            logger.warning("Could not save the voice list cache", exc_info=True)

    def _load_cache(self) -> list[Voice]:
        if self._cache_path is None or not self._cache_path.exists():
            return []
        try:
            return _to_voices(json.loads(self._cache_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            logger.warning("Saved voice list is unreadable", exc_info=True)
            return []


def _to_voices(raw: list[dict]) -> list[Voice]:
    voices = [Voice.from_edge_dict(d) for d in raw if isinstance(d, dict) and d.get("ShortName")]
    voices.sort(key=lambda v: (v.locale, v.display_name))
    return voices

