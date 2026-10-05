"""Structured file logging with rotation. Raw tracebacks stay in log files only."""

import faulthandler
import logging
import os
import sys
import threading
import uuid
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


#: Native crash dumps (segfaults in Qt/C extensions) go here; Python logging
#: cannot record a crash that kills the interpreter.
CRASH_FILENAME = "crash.log"

_crash_file = None   # kept open for the life of the process (faulthandler needs it)


def install_crash_logging(log_dir: Path) -> str:
    """
    Route every kind of failure into the log, and return this run's session id.

    A windowed build has no console, so without this an exception raised in a
    Qt slot or a worker thread is printed to a stderr that does not exist and
    the user's report arrives with nothing in the log to explain it.
    """
    global _crash_file
    session = uuid.uuid4().hex[:8]
    log = logging.getLogger("app.crash")

    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Uncaught exception (session %s)", session,
                     exc_info=(exc_type, exc, tb))

    def _thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.critical("Uncaught exception in thread %s (session %s)", name, session,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook

    try:
        _crash_file = open(log_dir / CRASH_FILENAME, "a", encoding="utf-8")
        _crash_file.write(f"\n=== session {session} pid {os.getpid()} ===\n")
        _crash_file.flush()
        faulthandler.enable(_crash_file, all_threads=True)
    except Exception:  # noqa: BLE001 — diagnostics must never stop startup
        log.warning("Native crash logging unavailable", exc_info=True)

    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        qt_log = logging.getLogger("qt")
        levels = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.CRITICAL,
        }

        def _qt_handler(mode, context, message):
            where = f" ({context.file}:{context.line})" if context.file else ""
            qt_log.log(levels.get(mode, logging.WARNING), "%s%s", message, where)

        qInstallMessageHandler(_qt_handler)
    except Exception:  # noqa: BLE001
        log.debug("Qt message handler not installed", exc_info=True)

    return session


def log_environment(session: str) -> None:
    """Write one block describing the build and machine at the top of a run."""
    from app.utils.diagnostics import environment_info

    log = logging.getLogger("app.env")
    log.info("──── session %s ────", session)
    for key, value in environment_info().items():
        log.info("%s: %s", key, value)
