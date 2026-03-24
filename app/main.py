"""
Application entry point.

Sets up the QApplication, loads the stylesheet, configures logging,
and launches the main window.
"""

import logging
import os
import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from app import APP_NAME, APP_VERSION, APP_ORG
from app.config.settings import AppSettings
from app.utils.app_logging import setup_logging
from app.utils.paths import AppPaths, resource_path

logger = logging.getLogger(__name__)


def _load_stylesheet(app: QApplication) -> None:
    qss_path = resource_path("app/assets/styles/app.qss")
    if qss_path.exists():
        app.setStyleSheet(qss_path.read_text(encoding="utf-8"))
    else:
        logger.warning("Stylesheet not found at %s", qss_path)


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
    window.show()

    exit_code = app.exec()
    logger.info("Application exiting with code %d", exit_code)
    sys.exit(exit_code)
