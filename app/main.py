"""
Application entry point.

Sets up the QApplication, loads the stylesheet, configures logging,
and launches the main window.
"""

import logging
import os
import sys

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from app import APP_NAME, APP_VERSION, APP_ORG
from app.config.settings import AppSettings
from app.utils.app_logging import setup_logging
from app.utils.paths import AppPaths, resource_path

logger = logging.getLogger(__name__)


def _load_stylesheet(app: QApplication) -> None:
    from app.ui.style import stylesheet_text
    qss = stylesheet_text()
    if qss:
        app.setStyleSheet(qss)
    else:
        logger.warning("Stylesheet not found at %s",
                       resource_path("app/assets/styles/app.qss"))


def _set_platform_font(app: QApplication) -> None:
    """
    Set a clean system font per platform.

    On macOS, Qt already defaults to the native San Francisco font;
    we only nudge the point size. On Windows we explicitly request
    Segoe UI. Neither uses the CSS-only '-apple-system' trick, which
    is not a valid Qt font family name.
    """
    from PySide6.QtGui import QFontDatabase
    # Start from the actual system default
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)

    if sys.platform == "darwin":
        font.setPointSize(13)
    elif sys.platform == "win32":
        font = QFont("Segoe UI", 10)
    else:
        font.setPointSize(11)

    app.setFont(font)


def main() -> None:
    # ── SSL certificate path for packaged builds ────────────────────── #
    # In a PyInstaller bundle, Python cannot find the certifi CA bundle
    # through the normal package-data path.  Setting these env vars before
    # anything else ensures aiohttp (used by edge_tts) can validate TLS
    # certificates on both macOS and Windows.  Without this, voice loading
    # and audio generation silently fail with SSL errors in packaged builds.
    try:
        import certifi as _certifi
        os.environ.setdefault("SSL_CERT_FILE",       _certifi.where())
        os.environ.setdefault("REQUESTS_CA_BUNDLE",  _certifi.where())
        os.environ.setdefault("WEB_CONCURRENCY",     "1")  # aiohttp safety
    except Exception:
        pass  # certifi not installed — network ops may fail on some builds

    # Packaged-build check used by CI (see app/selftest.py).  Runs before the
    # single-instance guard so it works while the app is open.
    if "--selftest" in sys.argv:
        from app.selftest import run as run_selftest
        sys.exit(run_selftest(sys.argv))

    # Required before QApplication on some platforms
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    # On macOS, prevent the app icon from bouncing endlessly in the Dock
    if sys.platform == "darwin":
        os.environ.setdefault("QT_MAC_WANTS_LAYER", "1")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_ORG)
    app.setQuitOnLastWindowClosed(True)

    _set_platform_font(app)

    # Initialise paths + logging before anything else
    paths = AppPaths()

    # One copy per user: a second launch hands over to the running window.
    # Checked before logging is set up so the second copy never touches the
    # shared log file.
    from app.utils.single_instance import SingleInstance
    instance = SingleInstance(paths.data_dir)
    if not instance.acquire():
        instance.notify_running_instance()
        sys.exit(0)

    setup_logging(paths.log_dir)
    logger.info("Starting %s %s", APP_NAME, APP_VERSION)

    # Load settings
    settings = AppSettings(paths)

    # Set app-wide icon (taskbar, dock, dialogs)
    icon_path = paths.icon_path
    if icon_path.exists():
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path)))

    # Apply stylesheet
    _load_stylesheet(app)

    # Import here to avoid circular imports at module level
    from app.ui.main_window import MainWindow

    window = MainWindow(settings=settings, paths=paths)

    # Safety net: closeEvent handles the normal path, but any exit that does
    # not route through it (Cmd+Q on some platforms, a session logout, an
    # unhandled exception unwinding main) would otherwise destroy live
    # QThreads and abort with "Python quit unexpectedly".
    app.aboutToQuit.connect(window.ensure_workers_stopped)
    app.aboutToQuit.connect(instance.release)
    instance.activation_requested.connect(window.bring_to_front)

    window.show()

    exit_code = app.exec()
    logger.info("Application exiting with code %d", exit_code)
    sys.exit(exit_code)
