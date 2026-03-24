"""Platform-aware path resolution for app data, logs, cache, and bundled resources."""

import sys
from pathlib import Path
from platformdirs import user_data_dir, user_log_dir, user_cache_dir

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
        self.data_dir = Path(user_data_dir(APP_NAME, APP_ORG))
        self.log_dir = Path(user_log_dir(APP_NAME, APP_ORG))
        self.cache_dir = Path(user_cache_dir(APP_NAME, APP_ORG))

        for directory in (self.data_dir, self.log_dir, self.cache_dir):
            directory.mkdir(parents=True, exist_ok=True)

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
