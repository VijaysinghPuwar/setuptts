"""
Main application window.

Layout
------
  ┌─ header bar (44 px) ───────────────────────────────────────┐
  │ [●] SetupTTS                               [⚙ Settings]  │
  ├──────────────────────────────────┬─────────────────────────┤
  │                                  │                         │
  │  TEXT INPUT  (flex width)        │  VOICE                  │
  │                                  │  SPEED                  │
  │  big text editor                 │  EXPORT                 │
  │  drag & drop                     │                         │
  │                                  │  [Generate & Export MP3]│
  ├──────────────────────────────────┴─────────────────────────┤
  │  RECENT CONVERSIONS  (collapsible, 140 px default)         │
  ├────────────────────────────────────────────────────────────┤
  │  status bar (24 px)                                        │
  └────────────────────────────────────────────────────────────┘

Shutdown lifecycle
------------------
closeEvent performs a safe, ordered shutdown:
  1. Ask the user if a generation is in progress.
  2. Cancel/stop all background workers.
  3. Wait up to 4 s for each to finish (non-blocking prompt kept responsive).
  4. Save settings.
  5. Accept the close event → app exits cleanly.

This prevents the "QThread destroyed while running" abort that triggers
macOS's "quit unexpectedly" dialog.
"""

import logging

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QBrush
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import AppSettings
from app.services.history_service import HistoryService
from app.ui.panels.history_panel import HistoryPanel
from app.ui.panels.input_panel import InputPanel
from app.ui.panels.output_panel import OutputPanel
from app.utils.paths import AppPaths, resource_path
from app import APP_NAME, APP_VERSION

logger = logging.getLogger(__name__)

_RIGHT_PANEL_MIN = 310
_RIGHT_PANEL_MAX = 400
_RIGHT_PANEL_DEFAULT = 340
_INPUT_PANEL_MIN = 320


def _make_app_icon() -> QIcon:
    """Load real icon; fall back to a rendered placeholder."""
    path = resource_path("app/assets/icons/app.png")
    if path.exists():
        return QIcon(str(path))

    size = 64
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(QColor("#0A84FF")))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(0, 0, size, size, size * 0.22, size * 0.22)
    p.end()
    return QIcon(px)


class MainWindow(QMainWindow):
    """Top-level application window."""

    def __init__(
        self,
        settings: AppSettings,
        paths: AppPaths,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._paths    = paths
        self._history  = HistoryService(paths.db_path)
        self._closing  = False   # guard against re-entrant closeEvent
        self._workers_stopped = False   # guard against double shutdown
        self._sidebar_user_sized = False  # True once the splitter is dragged

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(_make_app_icon())
        self.setMinimumSize(780, 520)
        self._restore_geometry()

        self._build_menu()
        self._build_central_widget()
        self._build_status_bar()
        self._connect_signals()

        self.status_bar.showMessage("Ready")
        logger.info("MainWindow shown")

    # ------------------------------------------------------------------ #
    # Public API used by panels                                            #
    # ------------------------------------------------------------------ #

    def get_input_text(self) -> str:
        return self._input_panel.get_text()

    def set_input_text(self, text: str) -> None:
        self._input_panel.set_text(text)

    # ------------------------------------------------------------------ #
    # Menu                                                                 #
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> None:
        mb = self.menuBar()

        file_menu = mb.addMenu("File")
        open_a = QAction("Open Text File…", self)
        open_a.setShortcut("Ctrl+O")
        open_a.triggered.connect(lambda: self._input_panel._open_file_dialog())
        file_menu.addAction(open_a)

        file_menu.addSeparator()
        quit_a = QAction("Quit", self)
        quit_a.setShortcut("Ctrl+Q")
        quit_a.triggered.connect(self.close)
        file_menu.addAction(quit_a)

        edit_menu = mb.addMenu("Edit")
        clear_a = QAction("Clear Text", self)
        clear_a.triggered.connect(lambda: self._input_panel.clear())
        edit_menu.addAction(clear_a)

        view_menu = mb.addMenu("View")
        self._toggle_history_action = QAction("Show Recent Conversions", self)
        self._toggle_history_action.setCheckable(True)
        self._toggle_history_action.setChecked(self._settings.show_history)
        self._toggle_history_action.triggered.connect(self._toggle_history)
        view_menu.addAction(self._toggle_history_action)

        help_menu = mb.addMenu("Help")
        settings_a = QAction("Settings…", self)
        settings_a.setShortcut("Ctrl+,")
        settings_a.triggered.connect(self._open_settings)
        help_menu.addAction(settings_a)

        help_menu.addSeparator()
        about_a = QAction(f"About {APP_NAME}", self)
        about_a.triggered.connect(self._open_about)
        help_menu.addAction(about_a)

    # ------------------------------------------------------------------ #
    # Central widget                                                       #
    # ------------------------------------------------------------------ #

    def _build_central_widget(self) -> None:
        container = QWidget()
        root_layout = QVBoxLayout(container)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_header())

        # ── Vertical splitter: [main row] ╱ [history] ─────────────── #
        self._v_splitter = QSplitter(Qt.Vertical)
        self._v_splitter.setHandleWidth(1)
        self._v_splitter.setChildrenCollapsible(False)

        # Main row: input | output/controls
        self._h_splitter = QSplitter(Qt.Horizontal)
        self._h_splitter.setHandleWidth(1)
        self._h_splitter.setChildrenCollapsible(False)

        self._input_panel = InputPanel()
        self._input_panel.setMinimumWidth(_INPUT_PANEL_MIN)
        self._output_panel = OutputPanel(
            settings=self._settings,
            history=self._history,
        )
        self._output_panel.setMinimumWidth(_RIGHT_PANEL_MIN)
        self._output_panel.setMaximumWidth(_RIGHT_PANEL_MAX)

        self._h_splitter.addWidget(self._input_panel)
        self._h_splitter.addWidget(self._output_panel)
        self._h_splitter.setStretchFactor(0, 1)
        self._h_splitter.setStretchFactor(1, 0)
        self._h_splitter.splitterMoved.connect(self._on_sidebar_resized)

        self._v_splitter.addWidget(self._h_splitter)

        # History panel (wrapped for padding)
        self._history_panel = HistoryPanel(history=self._history)
        self._v_splitter.addWidget(self._history_panel)

        saved_h = min(self._settings.history_panel_height, 130)  # compact default
        total   = max(self._settings.window_height - 88, 400)
        self._v_splitter.setSizes([total - saved_h, saved_h])

        root_layout.addWidget(self._v_splitter, 1)
        self.setCentralWidget(container)
        self._set_history_visible(self._settings.show_history)

    def _build_header(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("headerBar")
        bar.setFixedHeight(40)
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(18, 0, 14, 0)
        hl.setSpacing(10)

        # App icon (small)
        icon_path = resource_path("app/assets/icons/app.png")
        icon_lbl = QLabel()
        if icon_path.exists():
            px = QPixmap(str(icon_path)).scaled(
                26, 26, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        else:
            px = QPixmap(26, 26)
            px.fill(Qt.transparent)
            p = QPainter(px)
            p.setRenderHint(QPainter.Antialiasing)
            p.setBrush(QBrush(QColor("#0A84FF")))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(0, 0, 26, 26, 6, 6)
            p.end()
        icon_lbl.setPixmap(px)
        hl.addWidget(icon_lbl)

        name_lbl = QLabel(APP_NAME)
        name_lbl.setObjectName("appName")
        hl.addWidget(name_lbl)

        hl.addStretch()

        settings_btn = QPushButton("Settings")
        settings_btn.setObjectName("ghostButton")
        settings_btn.clicked.connect(self._open_settings)
        hl.addWidget(settings_btn)

        return bar

    def _build_status_bar(self) -> None:
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    # ------------------------------------------------------------------ #
    # Responsive layout                                                    #
    # ------------------------------------------------------------------ #

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        """
        Keep the control sidebar proportional to the window.

        At the 780 px minimum width a fixed 400 px sidebar swallows more than
        half the window and squeezes the editor, so the sidebar is capped at
        ~40 % of the window.  Once the user drags the splitter their width is
        preserved and only clamped to that cap — otherwise the sidebar tracks
        the preferred width, growing back when the window is widened again.
        """
        panel = getattr(self, "_output_panel", None)
        if panel is None:
            return

        allowed = int(self.width() * 0.40)
        target  = max(_RIGHT_PANEL_MIN, min(_RIGHT_PANEL_MAX, allowed))
        if panel.maximumWidth() != target:
            panel.setMaximumWidth(target)

        sizes = self._h_splitter.sizes()
        if len(sizes) != 2 or sum(sizes) <= 0:
            return

        if self._sidebar_user_sized:
            # Respect the user's width, but never exceed the responsive cap.
            right = max(_RIGHT_PANEL_MIN, min(target, sizes[1]))
        else:
            right = target

        left = max(_INPUT_PANEL_MIN, sum(sizes) - right)
        if [left, right] != sizes:
            self._h_splitter.setSizes([left, right])

    def _on_sidebar_resized(self, *_args) -> None:
        """The user dragged the horizontal splitter — stop auto-sizing."""
        self._sidebar_user_sized = True

    # ------------------------------------------------------------------ #
    # Signals                                                              #
    # ------------------------------------------------------------------ #

    def _connect_signals(self) -> None:
        # Wire text changes → enable/disable generate button
        self._input_panel.text_changed.connect(self._output_panel.on_text_changed)
        # Job completed → update history panel
        self._output_panel.job_completed.connect(self._history_panel.add_job)
        # Status messages → status bar
        self._output_panel.status_message.connect(self.status_bar.showMessage)

    # ------------------------------------------------------------------ #
    # Actions                                                              #
    # ------------------------------------------------------------------ #

    def _open_settings(self) -> None:
        from app.ui.dialogs.settings_dialog import SettingsDialog
        SettingsDialog(self._settings, self._paths, self).exec()

    def _open_about(self) -> None:
        from app.ui.dialogs.about_dialog import AboutDialog
        AboutDialog(self).exec()

    def _toggle_history(self, checked: bool) -> None:
        self._set_history_visible(checked)
        self._settings.show_history = checked

    def _set_history_visible(self, visible: bool) -> None:
        widget = self._v_splitter.widget(1)
        if widget:
            widget.setVisible(visible)

    # ------------------------------------------------------------------ #
    # Geometry persistence                                                 #
    # ------------------------------------------------------------------ #

    def _restore_geometry(self) -> None:
        """
        Restore the saved size/position, clamped to a screen that exists.

        Without clamping, a window saved on a monitor that is no longer
        attached (or one that was larger than the current display) reopens
        partly or entirely off-screen, where the user cannot reach it.
        """
        min_size = self.minimumSize()
        w = max(min_size.width(),  self._settings.window_width)
        h = max(min_size.height(), self._settings.window_height)

        x, y = self._settings.window_x, self._settings.window_y

        screen = None
        if x is not None and y is not None:
            screen = QApplication.screenAt(QPoint(int(x), int(y)))
        if screen is None:
            screen = QApplication.primaryScreen()

        if screen is None:
            self.resize(w, h)
            return

        avail = screen.availableGeometry()
        w = min(w, avail.width())
        h = min(h, avail.height())
        self.resize(w, h)

        if x is None or y is None:
            self.move(avail.center().x() - w // 2, avail.center().y() - h // 2)
            return

        # Keep the window fully inside the target screen.
        x = max(avail.left(), min(int(x), avail.right()  - w + 1))
        y = max(avail.top(),  min(int(y), avail.bottom() - h + 1))
        self.move(x, y)

    def _save_window_state(self) -> None:
        self._settings.window_width  = self.width()
        self._settings.window_height = self.height()
        self._settings.window_x      = self.x()
        self._settings.window_y      = self.y()

        # sizes()[-1] is 0 whenever the history panel is hidden; persisting
        # that would make the panel unusable (0 px tall) after a restart.
        sizes = self._v_splitter.sizes()
        if len(sizes) > 1 and sizes[-1] > 0:
            self._settings.history_panel_height = sizes[-1]

    # ------------------------------------------------------------------ #
    # Shutdown lifecycle  ← THE FIX                                       #
    # ------------------------------------------------------------------ #

    def ensure_workers_stopped(self) -> None:
        """
        Idempotent worker shutdown, safe to call from QApplication.aboutToQuit.

        closeEvent already does this on the normal path; this guards the exit
        paths that bypass it, where a still-running QThread would be destroyed
        and abort the process.
        """
        if self._workers_stopped:
            return
        self._workers_stopped = True
        try:
            self._output_panel.shutdown()
        except Exception:
            logger.warning("Worker shutdown raised during exit", exc_info=True)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """
        Safe, ordered shutdown.

        1. Guard against re-entrant calls.
        2. Warn the user if audio generation is in progress and
           offer to cancel it.
        3. Stop *all* active workers and wait for them.
        4. Save settings.
        5. Accept the event.

        Nothing in here calls sys.exit() or QApplication.quit()
        directly — we let Qt's normal event loop wind down naturally
        after we accept the close.
        """
        if self._closing:
            event.accept()
            return
        self._closing = True
        logger.info("Close requested — beginning shutdown sequence")

        # ── 1. Warn if any jobs are active ────────────────────────── #
        op = self._output_panel
        if op.is_busy():
            n_running = op.running_count
            n_pending = op.pending_count
            parts = []
            if n_running:
                parts.append(f"{n_running} job{'s' if n_running > 1 else ''} generating")
            if n_pending:
                parts.append(f"{n_pending} queued")
            detail = " and ".join(parts)
            reply = QMessageBox.question(
                self,
                "Jobs in Progress",
                f"Audio generation is active ({detail}).\n\n"
                "Closing now will cancel all pending jobs. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.No:
                self._closing = False
                event.ignore()
                return

        # ── 2. Stop all workers ────────────────────────────────────── #
        self.ensure_workers_stopped()

        # ── 3. Save settings ───────────────────────────────────────── #
        self._save_window_state()
        self._settings.save()
        logger.info("Settings saved — accepting close event")

        event.accept()
