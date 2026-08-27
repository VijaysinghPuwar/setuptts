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
  │  RECENT CONVERSIONS  (collapsible, ~20 % of window height)  │
  ├────────────────────────────────────────────────────────────┤
  │  status bar (24 px)                                        │
  └────────────────────────────────────────────────────────────┘

Responsive behaviour
--------------------
Both splits are re-derived from the window size on every resize
(_apply_responsive_layout), and the sidebar's control density steps down as
the window gets shorter (OutputPanel.set_density).  Without this the sidebar
kept a fixed footprint: at short heights its scroll viewport collapsed to
~160 px, slicing the voice card against the pinned CTA and hiding the speed,
export and running-job controls below the fold.  A drag of either splitter
pins that dimension to the user's choice, clamped so it can never starve the
other side.

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

# Verified floor: the whole layout renders without clipping at this size and
# the sidebar still shows the voice card above the pinned CTA.  Kept low so the
# window fits alongside other windows on a small laptop display.
_MIN_WINDOW_SIZE = (720, 480)

_RIGHT_PANEL_MIN = 310
_RIGHT_PANEL_MAX = 400
_RIGHT_PANEL_DEFAULT = 340
_INPUT_PANEL_MIN = 320

# History strip: a fixed height is wrong at both extremes — it swallows a
# quarter of a short window and looks stranded on a tall one.  It is sized as
# a fraction of the window instead, clamped so the header plus one row always
# fit and it never dominates.
_HISTORY_MIN     = 88
_HISTORY_MAX     = 240
_HISTORY_FRACTION = 0.20

# The editor + controls row never drops below this share of the split, however
# tall the history strip below it is asked to be.
_MAIN_ROW_MIN_FRACTION = 0.55

# Sidebar density thresholds (window height, px).  Padding is given up first
# and only then the explanatory hints — see OutputPanel.set_density.
_DENSITY_COMPACT_BELOW = 760
_DENSITY_MINIMAL_BELOW = 640


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
        self._history_user_sized = False  # True once the history split is dragged
        self._history_user_height = 0     # the height they dragged it to

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(_make_app_icon())
        self.setMinimumSize(*_MIN_WINDOW_SIZE)
        self._restore_geometry()

        self._build_menu()
        self._build_central_widget()
        self._build_status_bar()
        self._connect_signals()

        self._sync_window_minimum()

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

        self._v_splitter.splitterMoved.connect(self._on_history_resized)
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
        # A minimum, not a fixed height: a fixed 40 px clips the Settings
        # button outright under a larger system font or accessibility text
        # scaling, where the button alone wants more than that.
        bar.setMinimumHeight(40)
        # Fixed, not Maximum: Maximum treats the size hint as a ceiling and
        # lets the bar shrink back to its 40 px minimum, which is the clipping
        # this was meant to prevent.  Fixed pins the height to what the
        # content actually needs, with 40 px as the floor.
        bar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
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
        Keep the layout proportional to the window in both axes.

        Width — at the minimum width a fixed 400 px sidebar swallows more than
        half the window and squeezes the editor, so the sidebar is capped at
        ~40 % of the window.  Once the user drags the splitter their width is
        preserved and only clamped to that cap — otherwise the sidebar tracks
        the preferred width, growing back when the window is widened again.

        Height — the sidebar's pinned footer is subtracted from the scrollable
        controls above it, and the history strip is subtracted from both.  A
        fixed history height plus a full-size footer left barely 160 px of
        scroll viewport on a short window, which sliced the voice card in half
        and pushed speed, export and the running-job list below the fold.  The
        history strip is therefore sized as a fraction of the window and the
        sidebar drops its optional hints once the window is short.
        """
        panel = getattr(self, "_output_panel", None)
        if panel is None:
            return

        # Same reasoning as the editor and history floors: 310 px is only
        # enough for the sidebar's controls at the font size we assumed.
        sidebar_min = max(_RIGHT_PANEL_MIN, panel.minimumSizeHint().width())
        if panel.minimumWidth() != sidebar_min:
            panel.setMinimumWidth(sidebar_min)

        allowed = int(self.width() * 0.40)
        target  = max(sidebar_min, min(max(_RIGHT_PANEL_MAX, sidebar_min), allowed))
        if panel.maximumWidth() != target:
            panel.setMaximumWidth(target)

        panel.set_density(self._density_for_height(self.height()))
        self._apply_responsive_history()

        sizes = self._h_splitter.sizes()
        if len(sizes) != 2 or sum(sizes) <= 0:
            return

        if self._sidebar_user_sized:
            # Respect the user's width, but never exceed the responsive cap.
            right = max(sidebar_min, min(target, sizes[1]))
        else:
            right = target

        # Same reasoning as the history floor: the editor's own chrome (the
        # TEXT INPUT / Open File / Clear bar) needs more than 320 px once the
        # system font grows, and squeezing below that clips the toolbar.  The
        # minimum is pushed onto the panel as well as used here, so the
        # splitter itself refuses to go below it.
        editor_min = max(_INPUT_PANEL_MIN,
                         self._input_panel.minimumSizeHint().width())
        if self._input_panel.minimumWidth() != editor_min:
            self._input_panel.setMinimumWidth(editor_min)
        left = max(editor_min, sum(sizes) - right)
        if [left, right] != sizes:
            self._h_splitter.setSizes([left, right])

        self._sync_window_minimum()

    def _sync_window_minimum(self) -> None:
        """
        Keep the window's minimum at least as large as the layout needs.

        An explicit minimum overrides the layout's own minimumSizeHint, so the
        constant alone promises a size the layout cannot render once the system
        font is larger than we assumed — and a window allowed below what the
        layout needs pushes the editor off the right-hand edge.  The panels'
        minimums are themselves derived from their content, so this has to be
        re-derived whenever they change, not only at construction.

        Never goes below _MIN_WINDOW_SIZE, and is skipped when unchanged, so
        the resize it can trigger settles on the next pass.
        """
        needed = self.minimumSizeHint()
        floor_w = max(_MIN_WINDOW_SIZE[0], needed.width())
        floor_h = max(_MIN_WINDOW_SIZE[1], needed.height())
        if (self.minimumWidth(), self.minimumHeight()) != (floor_w, floor_h):
            self.setMinimumSize(floor_w, floor_h)

    @staticmethod
    def _density_for_height(height: int) -> str:
        if height < _DENSITY_MINIMAL_BELOW:
            return OutputPanel.DENSITY_MINIMAL
        if height < _DENSITY_COMPACT_BELOW:
            return OutputPanel.DENSITY_COMPACT
        return OutputPanel.DENSITY_COMFORTABLE

    def _apply_responsive_history(self) -> None:
        """Size the history strip as a fraction of the window height."""
        # The sidebar is constructed before the history strip, so a resize
        # delivered between the two would land here with no strip to size.
        history = getattr(self, "_history_panel", None)
        if history is None or not history.isVisible():
            return

        sizes = self._v_splitter.sizes()
        if len(sizes) != 2 or sum(sizes) <= 0:
            return

        total = sum(sizes)
        # Derived, not the bare constant: under a larger system font the strip
        # needs more than 88 px just to draw its own header without clipping.
        floor = max(_HISTORY_MIN, history.minimumSizeHint().height())

        if self._history_user_sized:
            # Respect the height the user dragged to.  Clamping it to the
            # automatic target would mean the strip could never be dragged
            # taller than its default, which is the whole point of the drag;
            # reading it back from sizes() would let QSplitter's proportional
            # redistribution walk it a little further on every resize.
            bottom = max(floor, self._history_user_height)
        else:
            bottom = max(floor,
                         min(_HISTORY_MAX, int(self.height() * _HISTORY_FRACTION)))

        # The editor and controls are the main event: they keep the majority of
        # the window whatever the strip below asks for.  This is also what
        # claws back a height the user dragged to on a window that has since
        # been made much shorter.
        main_min = max(self._h_splitter.minimumSizeHint().height(),
                       int(total * _MAIN_ROW_MIN_FRACTION))
        top = max(main_min, total - bottom)
        bottom = max(0, total - top)
        if [top, bottom] != sizes:
            self._v_splitter.setSizes([top, bottom])

    def _on_sidebar_resized(self, *_args) -> None:
        """The user dragged the horizontal splitter — stop auto-sizing."""
        self._sidebar_user_sized = True

    def _on_history_resized(self, *_args) -> None:
        """The user dragged the vertical splitter — stop auto-sizing."""
        sizes = self._v_splitter.sizes()
        if len(sizes) != 2 or sizes[1] <= 0:
            return
        self._history_user_sized  = True
        self._history_user_height = sizes[1]

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
        if visible:
            # Re-derive the strip's share of the window; it was excluded from
            # responsive sizing while hidden.
            self._apply_responsive_history()

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
