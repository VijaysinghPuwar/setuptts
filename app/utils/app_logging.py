"""Structured file logging with rotation. Raw tracebacks stay in log files only."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: Current log file name.  Builds before 1.6.0 wrote "voicecraft.log" (a
#: leftover from the project's old name); that file is renamed on first run.
LOG_FILENAME = "setuptts.log"
_LEGACY_LOG_FILENAMES = ("voicecraft.log",)


def log_file_path(log_dir: Path) -> Path:
    return log_dir / LOG_FILENAME


def _migrate_legacy_log(log_dir: Path) -> None:
    target = log_dir / LOG_FILENAME
    if target.exists():
        return
    for legacy in _LEGACY_LOG_FILENAMES:
        old = log_dir / legacy
        if old.exists():
            try:
                old.replace(target)
            except OSError:
                pass   # e.g. still open in an old copy of the app — harmless
            return


def setup_logging(log_dir: Path, level: int = logging.DEBUG) -> None:
    """
    Configure application-wide logging.

    - DEBUG and above → rotating log file (10 MB × 3 backups)
    - WARNING and above → stderr (for crash reporting)
    - Never exposes raw tracebacks to the GUI; the UI catches errors itself.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_log(log_dir)
    log_file = log_file_path(log_dir)

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # A windowed (no-console) Windows build has no stderr at all.
    if sys.stderr is not None:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.WARNING)
        stderr_handler.setFormatter(fmt)
        root.addHandler(stderr_handler)

    logging.getLogger("edge_tts").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("aiosignal").setLevel(logging.WARNING)
