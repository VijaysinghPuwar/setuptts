"""Persistent JSON-backed application settings with sane defaults."""

import json
import logging
import os
from pathlib import Path
from typing import Any

from app.utils.paths import AppPaths

logger = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "voice": "en-US-AvaNeural",
    "rate": 5,                      # integer -50..+100  (percent offset)
    "volume": 0,                    # integer -50..+50
    "language_filter": "en-US",
    "gender_filter": "All",         # "All" | "Female" | "Male"
    "voice_search": "",             # last search query (cleared on start)
    "recently_used_voices": [],     # list of ShortName strings, max 5
    "auto_switch_recommended_voice": False,
    "output_dir": "",               # empty = user's Desktop
    "window_width": 1100,
    "window_height": 660,
    "window_x": None,
    "window_y": None,
    "show_history": True,
    "history_panel_height": 200,
}


class AppSettings:
    """
    Thin wrapper around a JSON settings file.

    Usage
    -----
    settings = AppSettings(paths)
    settings.voice          # read
    settings.voice = "..."  # write (call .save() to persist)
    settings.save()
    """

    def __init__(self, paths: AppPaths) -> None:
        self._path: Path = paths.settings_path
        self._data: dict[str, Any] = dict(_DEFAULTS)
        self._load()

    # ------------------------------------------------------------------ #
    # Convenience properties                                               #
    # ------------------------------------------------------------------ #

    @property
    def voice(self) -> str:
        return self._data["voice"]

    @voice.setter
    def voice(self, value: str) -> None:
        self._data["voice"] = value

    @property
    def rate(self) -> int:
        return int(self._data["rate"])

    @rate.setter
    def rate(self, value: int) -> None:
        self._data["rate"] = int(value)

    @property
    def volume(self) -> int:
        return int(self._data["volume"])

    @volume.setter
    def volume(self, value: int) -> None:
        self._data["volume"] = int(value)

    @property
    def language_filter(self) -> str:
        return self._data["language_filter"]

    @language_filter.setter
    def language_filter(self, value: str) -> None:
        self._data["language_filter"] = value

    @property
    def output_dir(self) -> str:
        return self._data["output_dir"]

    @output_dir.setter
    def output_dir(self, value: str) -> None:
        self._data["output_dir"] = value

    @property
    def window_width(self) -> int:
        return int(self._data.get("window_width", 1100))

    @window_width.setter
    def window_width(self, v: int) -> None:
        self._data["window_width"] = v

    @property
    def window_height(self) -> int:
        return int(self._data.get("window_height", 720))

    @window_height.setter
    def window_height(self, v: int) -> None:
        self._data["window_height"] = v

    @property
    def window_x(self) -> int | None:
        v = self._data.get("window_x")
        return int(v) if v is not None else None

    @window_x.setter
    def window_x(self, v: int | None) -> None:
        self._data["window_x"] = v

    @property
    def window_y(self) -> int | None:
        v = self._data.get("window_y")
        return int(v) if v is not None else None

    @window_y.setter
    def window_y(self, v: int | None) -> None:
        self._data["window_y"] = v

    @property
    def show_history(self) -> bool:
        return bool(self._data.get("show_history", True))

    @show_history.setter
    def show_history(self, v: bool) -> None:
        self._data["show_history"] = v

    @property
    def history_panel_height(self) -> int:
        return int(self._data.get("history_panel_height", 200))

    @history_panel_height.setter
    def history_panel_height(self, v: int) -> None:
        self._data["history_panel_height"] = v

    # ------------------------------------------------------------------ #
    # Rate/volume as edge_tts format strings                               #
    # ------------------------------------------------------------------ #

    @property
    def gender_filter(self) -> str:
        return self._data.get("gender_filter", "All")

    @gender_filter.setter
    def gender_filter(self, value: str) -> None:
        self._data["gender_filter"] = value

    @property
    def recently_used_voices(self) -> list[str]:
        return list(self._data.get("recently_used_voices", []))

    def add_recently_used_voice(self, short_name: str) -> None:
        recent: list[str] = self.recently_used_voices
        if short_name in recent:
            recent.remove(short_name)
        recent.insert(0, short_name)
        self._data["recently_used_voices"] = recent[:5]

    @property
    def auto_switch_recommended_voice(self) -> bool:
        return bool(self._data.get("auto_switch_recommended_voice", False))

    @auto_switch_recommended_voice.setter
    def auto_switch_recommended_voice(self, value: bool) -> None:
        self._data["auto_switch_recommended_voice"] = bool(value)

    def rate_string(self) -> str:
        """Return rate as edge_tts expects: '+5%' or '-10%'."""
        r = self.rate
        return f"+{r}%" if r >= 0 else f"{r}%"

    def volume_string(self) -> str:
        """Return volume as edge_tts expects: '+0%'."""
        v = self.volume
        return f"+{v}%" if v >= 0 else f"{v}%"

    # ------------------------------------------------------------------ #
    # Persistence                                                          #
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                # Merge; unknown keys from future versions are dropped,
                # missing keys keep the default.
                for key in _DEFAULTS:
                    if key in loaded:
                        self._data[key] = loaded[key]
            except Exception:
                logger.warning("Could not load settings; using defaults.", exc_info=True)
                try:
                    self._path.replace(self._path.with_name(self._path.name + ".corrupt"))
                except OSError:
                    pass

    def save(self) -> None:
        """
        Persist settings atomically.

        settings.save() runs on every generate and again during shutdown.  A
        plain write_text() truncates the file first, so a crash or power loss
        mid-write leaves a half-written file that fails to parse on the next
        launch and silently resets every preference.  Writing to a temp file
        in the same directory and renaming makes the swap atomic.
        """
        payload = json.dumps(self._data, indent=2, ensure_ascii=False)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            logger.error("Failed to save settings.", exc_info=True)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def reset(self) -> None:
        self._data = dict(_DEFAULTS)
        self.save()
