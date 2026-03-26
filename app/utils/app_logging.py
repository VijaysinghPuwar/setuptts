"""Structured file logging with rotation. Raw tracebacks stay in log files only."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: Path, level: int = logging.DEBUG) -> None:
    """
    Configure application-wide logging.

    - DEBUG and above → rotating log file (10 MB × 3 backups)
    - WARNING and above → stderr (for crash reporting)
    - Never exposes raw tracebacks to the GUI; the UI catches errors itself.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "voicecraft.log"

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

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)

    logging.getLogger("edge_tts").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("aiosignal").setLevel(logging.WARNING)
