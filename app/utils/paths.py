"""Platform-aware path resolution for app data, logs, cache, and bundled resources."""

import os
import sys
import tempfile
from pathlib import Path

from platformdirs import user_cache_dir, user_data_dir, user_log_dir

from app import APP_NAME, APP_ORG


def resource_path(relative: str) -> Path:
    """
    Resolve a path to a bundled resource.

    In development: resolves relative to the project root.
    In a PyInstaller bundle: resolves relative to sys._MEIPASS.
    """
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / relative
    return Path(__file__).parent.parent.parent / relative


class AppPaths:
    """
    All application data directories, resolved correctly per platform.

    macOS : ~/Library/Application Support/SetupTTS/
    Windows: %APPDATA%\\SetupTTS\\
    Linux  : ~/.local/share/SetupTTS/
    """

    def __init__(self) -> None:
        override_root = os.environ.get("SETUPTTS_DATA_DIR", "").strip()
        if override_root:
            base = Path(override_root).expanduser()
            self.data_dir = self._ensure_dir(base)
            self.log_dir = self._ensure_dir(self.data_dir / "logs")
            self.cache_dir = self._ensure_dir(self.data_dir / "cache")
            return

        self.data_dir = self._ensure_dir(Path(user_data_dir(APP_NAME, APP_ORG)))
        self.log_dir = self._ensure_dir(
            Path(user_log_dir(APP_NAME, APP_ORG)),
            fallback=self.data_dir / "logs",
        )
        self.cache_dir = self._ensure_dir(
            Path(user_cache_dir(APP_NAME, APP_ORG)),
            fallback=self.data_dir / "cache",
        )

    @staticmethod
    def _ensure_dir(directory: Path, *, fallback: Path | None = None) -> Path:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            return directory
        except OSError:
            safe_fallback = fallback or (Path(tempfile.gettempdir()) / APP_NAME)
            safe_fallback.mkdir(parents=True, exist_ok=True)
            return safe_fallback

    @property
    def staging_dir(self) -> Path:
        """Per-job chunk staging area for checkpoint / resume support."""
        d = self.data_dir / "staging"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> Path:
        return self.data_dir / "history.db"

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def stylesheet_path(self) -> Path:
        return resource_path("app/assets/styles/app.qss")

    @property
    def icon_path(self) -> Path:
        return resource_path("app/assets/icons/app.png")
