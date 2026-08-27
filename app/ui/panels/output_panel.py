"""
Output / Controls Panel — right sidebar.

Architecture
------------
All generation goes through a JobQueue (app/workers/job_queue.py).
The user can submit as many jobs as they like without waiting;
up to 2 run in parallel and the rest queue automatically.

The Generate button is always enabled whenever there is input text —
it never enters a global "busy" disabled state.

Per-job progress is shown in the ACTIVE JOBS section that appears
below the export card while at least one job is running or queued.
Completed jobs move to the history panel (bottom of main window).

Real progress
-------------
Progress goes from 3 % → 95 % driven by real WordBoundary events from
the edge_tts streaming API, then jumps to 100 % when the file is saved.
No fake static percentage.

Layout
------
Everything above the divider scrolls; the primary CTA is pinned below it so
it stays reachable at every window size, down to the 780x520 minimum.

  ┌─ VOICE ─────────────────────────────────────────────────┐ ╮
  │ [Search voices…_______________________________________] │ │
  │ [All Languages____________▾] [All▾]                     │ │
  │ [Ava · Female · English (US)__________________________▾]│ │
  │ [▶ Preview Voice]                                       │ │
  ├─ SPEED ─────────────────────────────────────────── +5% ─┤ │ scrolls
  │ Slower ●────────────────────────────────────── Faster   │ │
  ├─ EXPORT ────────────────────────────────────────────────┤ │
  │ File Name                                               │ │
  │ [output.mp3__________________________________________]  │ │
  │ Save To                                                 │ │
  │ [/Users/.../Desktop________________________________][Brw]│ │
  ├─ ACTIVE JOBS ──────────────────────────────── (hidden)  ┤ │
  │ ▶ chapter1.mp3                                    [✕]  │ │
  │   Ava · English (US)                             45%    │ │
  │   ▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  │ │
  │   [REMOTE] streaming audio from server                  │ │
  │   812 chars/s · ETA 1:20 · chunk 3/~7                   │ ╯
  ├─────────────────────────────────────────────────────────┤
  │ [           Generate & Export MP3                    ]  │   pinned
  └─────────────────────────────────────────────────────────┘

Styling note
------------
Container widgets in this panel must not carry inline stylesheets.  A
widget-level stylesheet overrides the application stylesheet for the widget
*and every descendant*, which silently strips the background from all the
cards, inputs and buttons nested inside.  Use an objectName plus a rule in
app/assets/styles/app.qss instead.
"""

import logging
import os
import subprocess
import sys
import warnings
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import AppSettings
from app.models.job import Job, JobStatus
from app.models.voice import Voice
from app.services.history_service import HistoryService
from app.services.tts_quality import (
    VoiceCompatibilityAssessment,
    assess_voice_compatibility,
    build_text_profile,
)
from app.utils.paths import AppPaths
from app.workers.job_queue import JobItem, JobQueue
from app.workers.chunk_store import ResumeCandidate, ChunkStore
from app.workers.preview_worker import PreviewWorker
from app.workers.voice_loader import VoiceLoaderWorker

logger = logging.getLogger(__name__)

_ROLE_SHORT_NAME = Qt.UserRole
_ROLE_IS_RECENT  = Qt.UserRole + 1


# ══════════════════════════════════════════════════════════════════════ #
#  Main panel                                                            #
# ══════════════════════════════════════════════════════════════════════ #

class OutputPanel(QWidget):
    """
    Right-side controls: voice picker, speed, export form, job queue.

    Signals
    -------
    job_completed(Job)   Forwarded to the history panel.
    status_message(str)  Short message for the window status bar.
    """

    job_completed  = Signal(object)   # Job model instance
    status_message = Signal(str)

    def __init__(
        self,
        settings: AppSettings,
        history:  HistoryService,
        parent:   QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._history  = history

        # Voice state
        self._all_voices:      list[Voice] = []
        self._filtered_voices: list[Voice] = []
        self._voice_loader: VoiceLoaderWorker | None = None
        self._preview_worker: PreviewWorker | None = None
        self._current_text = ""
        self._compatibility: VoiceCompatibilityAssessment | None = None
        self._visible_recommended_voice: str | None = None
        self._resume_candidates: list[ResumeCandidate] = []
        # Keep old voice-loader workers alive until their thread exits.
        # Without this, replacing self._voice_loader on retry can cause
        # the old QThread object to be GC-collected mid-run → crash.
        self._finishing_workers: list[QThread] = []

        # Job queue — allows multiple concurrent exports
        self._queue    = JobQueue(parent=self)
        self._job_rows: dict[str, "_JobRowWidget"] = {}

        # Failure dialog queue — prevents stacking multiple error dialogs when
        # two concurrent jobs fail near-simultaneously (exec() re-enters the
        # event loop, which can fire a second _on_job_failed before the first
        # dialog is dismissed).
        self._failure_dialog_queue: list = []   # list[JobItem]
        self._failure_dialog_active = False

        # Debounce voice-filter rebuilds so rapid search typing doesn't
        # hammer the combo (400+ addItem() calls per keystroke).
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(150)
        self._filter_timer.timeout.connect(self._apply_filters)

        self.setObjectName("sidePanel")
        # Density level; MainWindow re-derives it from the window height on
        # every resize.  Comfortable until then.
        self._density = self.DENSITY_COMFORTABLE
        self._build_ui()
        self._connect_signals()
        self._apply_settings()
        self._start_voice_load()
        self._refresh_resume_jobs()

    # ------------------------------------------------------------------ #
    # Public API (used by MainWindow)                                     #
    # ------------------------------------------------------------------ #

    def get_selected_voice(self) -> str:
        idx = self._voice_combo.currentIndex()
        if 0 <= idx < self._voice_combo.count():
            name = self._voice_combo.itemData(idx, _ROLE_SHORT_NAME)
            if name:
                return name
        return self._settings.voice

    def get_rate_string(self) -> str:
        return self._settings.rate_string()

    def get_volume_string(self) -> str:
        return self._settings.volume_string()

    def get_output_path(self) -> str:
        folder = self._folder_edit.text().strip()
        name   = self._filename_edit.text().strip() or "output.mp3"
        if not name.lower().endswith(".mp3"):
            name += ".mp3"
        return str(Path(folder) / name) if folder else str(Path.home() / "Desktop" / name)

    # ---- Lifecycle / queue state (used by MainWindow) -----------------

    def is_busy(self) -> bool:
        """True if any TTS jobs are running or pending."""
        return self._queue.is_busy()

    @property
    def running_count(self) -> int:
        return self._queue.running_count

    @property
    def pending_count(self) -> int:
        return self._queue.pending_count

    def shutdown(self) -> None:
        """
        Stop all workers cleanly.  Called from MainWindow.closeEvent before
        accepting the close — blocks briefly waiting for threads to exit.

        Signals are disconnected first so that queued cross-thread signals
        cannot fire on destroyed widget objects after we return.
        """
        self._queue.cancel_all()

        for name, worker in [
            ("voice_loader", self._voice_loader),
            ("preview",      self._preview_worker),
        ]:
            if worker is None or not isinstance(worker, QThread):
                continue
            if not worker.isRunning():
                continue
            logger.info("Stopping worker: %s", name)

            # Disconnect all signals first.  Any signals already queued in
            # the Qt event loop will become no-ops after disconnection, so
            # they cannot call back into widgets that are being destroyed.
            #
            # Disconnecting a signal that has no connections makes PySide6
            # emit a RuntimeWarning (via warnings.warn, so try/except cannot
            # catch it).  That is expected here — we disconnect defensively,
            # without tracking which signals were wired up — so the warning is
            # suppressed rather than left to spam the log on every shutdown.
            with warnings.catch_warnings():
                # PySide6 6.11 prefixes this with "libpyside: ", so match
                # anywhere in the message rather than anchoring at the start.
                warnings.filterwarnings(
                    "ignore",
                    message=".*Failed to disconnect.*",
                    category=RuntimeWarning,
                )
                for sig_name in ("loaded", "failed", "started_playing",
                                  "finished", "progress", "status_changed",
                                  "completed"):
                    sig = getattr(worker, sig_name, None)
                    if sig is None:
                        continue
                    try:
                        sig.disconnect()
                    except (RuntimeError, TypeError):
                        pass

            if hasattr(worker, "cancel"):
                worker.cancel()
            if hasattr(worker, "stop_playback"):
                worker.stop_playback()
            worker.quit()
            if not worker.wait(4_000):
                logger.warning("Worker %s timed out — terminating", name)
                worker.terminate()
                worker.wait(1_000)
            else:
                logger.info("Worker %s stopped cleanly", name)

        # Also keep finishing_workers alive until they exit
        for w in list(self._finishing_workers):
            if w.isRunning():
                w.quit()
                if not w.wait(2_000):
                    w.terminate()
                    w.wait(500)
        self._finishing_workers.clear()

    # ------------------------------------------------------------------ #
    # UI construction                                                      #
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # NOTE: do *not* set an inline stylesheet on the scroll area or its
        # inner widget.  A widget-level stylesheet takes precedence over the
        # application stylesheet for the widget *and all of its descendants*,
        # which silently strips every background in this panel (cards, inputs,
        # and the green Generate CTA all fall back to the bare window colour).
        # Transparency is expressed in app.qss via #sideScroll / #sideScrollInner.
        scroll = QScrollArea()
        scroll.setObjectName("sideScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        self._scroll = scroll

        inner = QWidget()
        inner.setObjectName("sideScrollInner")
        il = QVBoxLayout(inner)
        il.setContentsMargins(8, 8, 8, 8)
        il.setSpacing(6)
        self._scroll_inner_layout = il

        il.addWidget(self._build_voice_section())
        il.addWidget(self._build_speed_section())
        il.addWidget(self._build_export_section())

        # Active jobs card — hidden until first job is submitted
        self._jobs_card = self._build_jobs_section()
        il.addWidget(self._jobs_card)

        il.addStretch()
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        # The primary CTA lives *outside* the scroll area so it can never be
        # pushed below the fold on a short window.
        root.addWidget(self._build_action_footer())

    # ── Voice ─────────────────────────────────────────────────────────── #

    def _build_voice_section(self) -> QFrame:
        card = _card()
        ly = QVBoxLayout(card)
        ly.setContentsMargins(12, 10, 12, 10)
        ly.setSpacing(5)

        # Header: label + count
        hdr = QHBoxLayout()
        hdr.addWidget(_section_label("VOICE"))
        hdr.addStretch()
        self._voice_count_label = QLabel("")
        self._voice_count_label.setObjectName("metaLabel")
        hdr.addWidget(self._voice_count_label)
        ly.addLayout(hdr)

        # Search gets its own full-width row; the filters share the next one.
        # Packing all three onto one row let the language combo's sizeHint
        # (driven by its longest locale name) squeeze search down to a stub.
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search voices…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setMinimumWidth(120)
        ly.addWidget(self._search_edit)

        sf = QHBoxLayout()
        sf.setSpacing(5)

        self._lang_combo = QComboBox()
        self._lang_combo.addItem("All Languages", userData="")
        self._lang_combo.setMaxVisibleItems(18)
        # Do not let the widest locale name dictate the layout width.
        self._lang_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._lang_combo.setMinimumContentsLength(10)
        self._lang_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        sf.addWidget(self._lang_combo, 1)

        self._gender_combo = QComboBox()
        self._gender_combo.addItems(["All", "Female", "Male"])
        self._gender_combo.setMinimumWidth(76)
        self._gender_combo.setMaximumWidth(96)
        sf.addWidget(self._gender_combo)
        ly.addLayout(sf)

        # Voice selector
        self._voice_combo = QComboBox()
        self._voice_combo.addItem("Loading voices…")
        self._voice_combo.setEnabled(False)
        self._voice_combo.setMaxVisibleItems(16)
        self._voice_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._voice_combo.setMinimumContentsLength(12)
        self._voice_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        ly.addWidget(self._voice_combo)

        self._voice_warning = QFrame()
        self._voice_warning.setObjectName("voiceWarning")
        warning_layout = QVBoxLayout(self._voice_warning)
        warning_layout.setContentsMargins(10, 8, 10, 8)
        warning_layout.setSpacing(6)

        self._voice_warning_label = QLabel("")
        self._voice_warning_label.setObjectName("voiceWarningText")
        self._voice_warning_label.setWordWrap(True)
        warning_layout.addWidget(self._voice_warning_label)

        self._use_recommended_voice_btn = QPushButton("Use Recommended Voice")
        self._use_recommended_voice_btn.setObjectName("ghostButton")
        warning_layout.addWidget(self._use_recommended_voice_btn, alignment=Qt.AlignLeft)
        self._voice_warning.hide()
        ly.addWidget(self._voice_warning)

        # Error state
        self._voice_error_label = QLabel()
        self._voice_error_label.setObjectName("statusError")
        self._voice_error_label.setWordWrap(True)
        self._voice_error_label.hide()
        ly.addWidget(self._voice_error_label)

        self._retry_voices_btn = QPushButton("Retry")
        self._retry_voices_btn.setObjectName("ghostButton")
        self._retry_voices_btn.hide()
        ly.addWidget(self._retry_voices_btn, alignment=Qt.AlignLeft)

        # Preview row
        prev = QHBoxLayout()
        prev.setSpacing(6)

        self._preview_btn = QPushButton("▶  Preview Voice")
        self._preview_btn.setObjectName("previewButton")
        self._preview_btn.setEnabled(False)
        self._preview_btn.setToolTip("Play a short sample through your speakers")
        prev.addWidget(self._preview_btn)

        self._stop_preview_btn = QPushButton("■  Stop")
        self._stop_preview_btn.setObjectName("cancelButton")
        self._stop_preview_btn.setFixedHeight(30)
        self._stop_preview_btn.hide()
        prev.addWidget(self._stop_preview_btn)

        self._preview_status = QLabel("")
        self._preview_status.setObjectName("metaLabel")
        prev.addWidget(self._preview_status, 1)
        ly.addLayout(prev)

        return card

    # ── Speed ─────────────────────────────────────────────────────────── #

    def _build_speed_section(self) -> QFrame:
        card = _card()
        ly = QVBoxLayout(card)
        ly.setContentsMargins(12, 8, 12, 8)
        ly.setSpacing(3)

        hdr = QHBoxLayout()
        hdr.addWidget(_section_label("SPEED"))
        hdr.addStretch()
        self._rate_value_label = QLabel("+5%")
        self._rate_value_label.setStyleSheet(
            "font-size: 12px; font-weight: 700; color: #1DB954; background: transparent;"
        )
        hdr.addWidget(self._rate_value_label)
        ly.addLayout(hdr)

        self._rate_slider = QSlider(Qt.Horizontal)
        self._rate_slider.setRange(-50, 100)
        self._rate_slider.setValue(5)
        self._rate_slider.setSingleStep(5)
        self._rate_slider.setPageStep(10)
        ly.addWidget(self._rate_slider)

        hints = QHBoxLayout()
        slow = QLabel("Slower"); slow.setObjectName("metaLabel")
        fast = QLabel("Faster"); fast.setObjectName("metaLabel")
        hints.addWidget(slow); hints.addStretch(); hints.addWidget(fast)
        ly.addLayout(hints)

        return card

    # ── Export form ───────────────────────────────────────────────────── #

    def _build_export_section(self) -> QFrame:
        card = _card()
        ly = QVBoxLayout(card)
        ly.setContentsMargins(12, 10, 12, 12)
        ly.setSpacing(0)

        ly.addWidget(_section_label("EXPORT"))
        ly.addSpacing(8)

        # File name
        ly.addWidget(_field_label("File Name"))
        ly.addSpacing(3)
        self._filename_edit = QLineEdit()
        self._filename_edit.setPlaceholderText("output.mp3")
        self._filename_edit.setText("output.mp3")
        ly.addWidget(self._filename_edit)
        ly.addSpacing(8)

        # Save location
        ly.addWidget(_field_label("Save To"))
        ly.addSpacing(3)
        folder_row = QHBoxLayout()
        folder_row.setSpacing(5)
        self._folder_edit = QLineEdit()
        self._folder_edit.setPlaceholderText("Choose folder…")
        self._folder_edit.setReadOnly(True)
        self._folder_edit.setMinimumWidth(120)
        self._folder_edit.setStyleSheet(
            "QLineEdit { color: #9A9A9F; } QLineEdit:focus { border-color: #2C2C30; }"
        )
        folder_row.addWidget(self._folder_edit, 1)
        self._browse_btn = QPushButton("Browse")
        folder_row.addWidget(self._browse_btn)
        ly.addLayout(folder_row)

        return card

    # ── Pinned action footer (never scrolls away) ─────────────────────── #

    def _build_action_footer(self) -> QWidget:
        """
        Primary CTA + resume controls, pinned to the bottom of the sidebar.

        Kept outside the QScrollArea so that "Generate & Export MP3" stays
        reachable at every window height, including the window minimum.

        Everything below the CTA is *secondary*: it must never grow tall
        enough to starve the scroll viewport above it.  The footer is what
        the scroll area has to give way to, so a wrapping paragraph in here
        costs three lines of voice/speed/export controls.  Both hints are
        therefore single-line and elided, with the full text on the tooltip,
        and both collapse entirely in compact mode (see set_compact).
        """
        footer = QWidget()
        footer.setObjectName("actionFooter")
        ly = QVBoxLayout(footer)
        ly.setContentsMargins(12, 10, 12, 10)
        ly.setSpacing(0)
        self._footer_layout = ly

        # Generate button — always available when text exists.
        # "&&" renders as a literal ampersand; a single "&" would be swallowed
        # as a keyboard mnemonic and show up as "Generate Export MP3".
        self._generate_btn = _AdaptiveLabelButton([
            "Generate && Export MP3",
            "Generate MP3",
            "Generate",
        ])
        self._generate_btn.setObjectName("generateButton")
        self._generate_btn.setEnabled(False)
        ly.addWidget(self._generate_btn)

        # Hint shown when no text.  Elided rather than wrapped: a second line
        # here is a second line stolen from the controls above.
        self._generate_hint = _ElidingLabel(
            "Type or paste text on the left to get started", mode=Qt.ElideRight
        )
        self._generate_hint.setObjectName("hintLabel")
        self._generate_hint.setAlignment(Qt.AlignCenter)
        self._generate_hint.setToolTip("Type or paste text on the left to get started")
        ly.addSpacing(5)
        ly.addWidget(self._generate_hint)

        ly.addSpacing(6)
        self._resume_job_btn = _AdaptiveLabelButton(
            ["Resume Saved Job", "Resume Job", "Resume"]
        )
        self._resume_job_btn.setObjectName("ghostButton")
        self._resume_job_btn.hide()
        ly.addWidget(self._resume_job_btn)

        self._resume_job_hint = _ElidingLabel("", mode=Qt.ElideRight)
        self._resume_job_hint.setObjectName("resumeHint")
        self._resume_job_hint.hide()
        ly.addWidget(self._resume_job_hint)

        return footer

    # ── Height-aware density ──────────────────────────────────────────── #

    #: Density levels, loosest first.  MainWindow picks one from the window
    #: height; see DENSITY_* in app/ui/main_window.py for the thresholds.
    DENSITY_COMFORTABLE = "comfortable"
    DENSITY_COMPACT     = "compact"
    DENSITY_MINIMAL     = "minimal"

    _DENSITIES = (DENSITY_COMFORTABLE, DENSITY_COMPACT, DENSITY_MINIMAL)

    def set_density(self, level: str) -> None:
        """
        Trade padding, then secondary chrome, for scroll-viewport height.

        The sidebar is a scrollable column of controls above a pinned CTA, so
        the viewport between them is what a short window takes its space from.
        At full density that viewport held barely two controls and sliced the
        voice card in half against the footer edge.

        Space is given up in the order that costs the user least:

        ``comfortable``  full padding, both explanatory hints shown.
        ``compact``      tight control padding (app.qss ``[compact="true"]``),
                         hints kept — roughly two more controls fit.
        ``minimal``      also drops the two hints, whose text stays reachable
                         as the tooltip of the widget each one explains.

        The primary CTA keeps a large touch target at every level; it is the
        one control that must stay easy to hit.
        """
        if level not in self._DENSITIES:
            raise ValueError(f"unknown density level: {level!r}")
        if getattr(self, "_density", None) == level:
            return

        previous     = getattr(self, "_density", None)
        self._density = level
        tight        = level != self.DENSITY_COMFORTABLE
        show_hints   = level != self.DENSITY_MINIMAL

        margin = 6 if tight else 10
        self._footer_layout.setContentsMargins(12, margin, 12, margin)
        self._scroll_inner_layout.setSpacing(4 if tight else 6)

        self._generate_hint.setVisible(
            show_hints and not self._generate_btn.isEnabled()
        )
        self._resume_job_hint.setVisible(
            show_hints and bool(self._resume_job_hint.full_text())
        )

        # Tighter control padding — see the compact block in app.qss.  Skip
        # the re-polish when only the hint visibility changed: it walks the
        # whole subtree and runs on every resize that crosses a threshold.
        was_tight = previous is not None and previous != self.DENSITY_COMFORTABLE
        if previous is None or was_tight != tight:
            # A property change only re-evaluates the stylesheet for widgets
            # that are re-polished, and the rules target descendants, so the
            # whole subtree has to be re-polished, not just this panel.
            self.setProperty("compact", "true" if tight else "false")
            style = self.style()
            for widget in [self, *self.findChildren(QWidget)]:
                style.unpolish(widget)
                style.polish(widget)
            # The CTA font size changes with density, so its label has to be
            # re-chosen against the new metrics.
            self._generate_btn.refresh_label()
            self.updateGeometry()

    @property
    def density(self) -> str:
        return self._density

    # ── Active jobs list ──────────────────────────────────────────────── #

    def _build_jobs_section(self) -> QFrame:
        card = _card()
        self._jobs_outer_layout = QVBoxLayout(card)
        self._jobs_outer_layout.setContentsMargins(12, 8, 12, 8)
        self._jobs_outer_layout.setSpacing(4)

        hdr = QHBoxLayout()
        hdr.addWidget(_section_label("ACTIVE JOBS"))
        hdr.addStretch()
        self._jobs_count_label = QLabel("")
        self._jobs_count_label.setObjectName("metaLabel")
        hdr.addWidget(self._jobs_count_label)
        self._jobs_outer_layout.addLayout(hdr)

        # Job rows are inserted here
        self._jobs_list_layout = QVBoxLayout()
        self._jobs_list_layout.setSpacing(4)
        self._jobs_outer_layout.addLayout(self._jobs_list_layout)

        card.hide()
        return card

    # ------------------------------------------------------------------ #
    # Signal wiring                                                        #
    # ------------------------------------------------------------------ #

    def _connect_signals(self) -> None:
        # Voice / filter — search is debounced; lang/gender fire immediately
        self._search_edit.textChanged.connect(
            lambda: self._filter_timer.start()   # restart 150 ms window
        )
        self._lang_combo.currentIndexChanged.connect(self._on_filter_changed)
        self._gender_combo.currentIndexChanged.connect(self._on_filter_changed)
        self._voice_combo.currentIndexChanged.connect(self._on_voice_selection_changed)

        # Speed
        self._rate_slider.valueChanged.connect(self._on_rate_changed)

        # Export form
        self._browse_btn.clicked.connect(self._browse_folder)
        self._generate_btn.clicked.connect(self._on_generate)
        self._resume_job_btn.clicked.connect(self._on_resume_saved_job)
        self._retry_voices_btn.clicked.connect(self._start_voice_load)

        # Preview
        self._preview_btn.clicked.connect(self._on_preview)
        self._stop_preview_btn.clicked.connect(self._on_stop_preview)
        self._use_recommended_voice_btn.clicked.connect(self._on_use_recommended_voice)

        # Job queue signals
        self._queue.job_submitted.connect(self._on_job_submitted)
        self._queue.job_started.connect(self._on_job_started)
        self._queue.job_progress.connect(self._on_job_progress)
        self._queue.job_status_changed.connect(self._on_job_status_changed)
        self._queue.job_stage_changed.connect(self._on_job_stage_changed)
        self._queue.job_speed_updated.connect(self._on_job_speed_updated)
        self._queue.job_telemetry_updated.connect(self._on_job_telemetry_updated)
        self._queue.job_resumable.connect(self._on_job_resumable)
        self._queue.job_completed.connect(self._on_job_completed)
        self._queue.job_failed.connect(self._on_job_failed)
        self._queue.job_cancelled.connect(self._on_job_cancelled)

    # ------------------------------------------------------------------ #
    # Called by MainWindow when input text changes                        #
    # ------------------------------------------------------------------ #

    def on_text_changed(self, text: str) -> None:
        self._current_text = text
        has_text = bool(text.strip())
        self._generate_btn.setEnabled(has_text)
        self._generate_hint.setVisible(
            not has_text and self._density != self.DENSITY_MINIMAL
        )
        self._refresh_voice_guidance()

    # ------------------------------------------------------------------ #
    # Settings                                                             #
    # ------------------------------------------------------------------ #

    def _apply_settings(self) -> None:
        self._rate_slider.setValue(self._settings.rate)
        folder = self._settings.output_dir or str(Path.home() / "Desktop")
        self._set_folder_text(folder)
        idx = self._gender_combo.findText(self._settings.gender_filter)
        if idx >= 0:
            self._gender_combo.setCurrentIndex(idx)

    # ------------------------------------------------------------------ #
    # Voice loading                                                        #
    # ------------------------------------------------------------------ #

    def _start_voice_load(self) -> None:
        # If a previous loader is still running (e.g. retry clicked quickly),
        # disconnect its callbacks so we don't get duplicate _on_voices_loaded
        # calls, and keep a strong Python reference until the thread exits to
        # prevent "QThread destroyed while running" crashes.
        old = self._voice_loader
        if old is not None and old.isRunning():
            try:
                old.loaded.disconnect(self._on_voices_loaded)
                old.failed.disconnect(self._on_voices_failed)
            except Exception:
                pass
            self._finishing_workers.append(old)
            old.finished.connect(
                lambda w=old: (
                    self._finishing_workers.remove(w)
                    if w in self._finishing_workers else None
                )
            )

        self._voice_error_label.hide()
        self._retry_voices_btn.hide()
        self._hide_voice_guidance()
        self._voice_combo.clear()
        self._voice_combo.addItem("Loading voices…")
        self._voice_combo.setEnabled(False)
        self._lang_combo.setEnabled(False)
        self._gender_combo.setEnabled(False)
        self._preview_btn.setEnabled(False)
        self._voice_count_label.setText("Connecting…")

        self._voice_loader = VoiceLoaderWorker()
        self._voice_loader.loaded.connect(self._on_voices_loaded)
        self._voice_loader.failed.connect(self._on_voices_failed)
        self._voice_loader.start()

    def _on_voices_loaded(self, voices: list[Voice]) -> None:
        self._all_voices = voices
        seen: dict[str, str] = {}
        for v in voices:
            if v.locale not in seen:
                seen[v.locale] = _locale_label(v.locale)

        self._lang_combo.blockSignals(True)
        self._lang_combo.clear()
        self._lang_combo.addItem("All Languages", userData="")
        for locale, label in sorted(seen.items(), key=lambda x: x[1]):
            self._lang_combo.addItem(label, userData=locale)
        saved = self._settings.language_filter
        if saved:
            idx = self._lang_combo.findData(saved)
            if idx >= 0:
                self._lang_combo.setCurrentIndex(idx)
        self._lang_combo.blockSignals(False)

        self._lang_combo.setEnabled(True)
        self._gender_combo.setEnabled(True)
        self._apply_filters()

    def _on_voices_failed(self, message: str) -> None:
        self._voice_combo.clear()
        self._voice_combo.addItem("Could not load voices")
        self._voice_count_label.setText("")
        self._voice_error_label.setText(message)
        self._voice_error_label.show()
        self._retry_voices_btn.show()
        self._hide_voice_guidance()
        self.status_message.emit("Voice load failed — check internet")

    # ------------------------------------------------------------------ #
    # Filtering                                                            #
    # ------------------------------------------------------------------ #

    def _on_filter_changed(self) -> None:
        self._settings.language_filter = self._lang_combo.currentData(Qt.UserRole) or ""
        self._settings.gender_filter   = self._gender_combo.currentText()
        self._filter_timer.start()   # also debounced — consistent path

    def _apply_filters(self) -> None:
        query  = self._search_edit.text().lower().strip()
        locale = self._lang_combo.currentData(Qt.UserRole) or ""
        gender = self._gender_combo.currentText()

        filtered = self._all_voices
        if locale:
            filtered = [v for v in filtered if v.locale == locale]
        if gender != "All":
            filtered = [v for v in filtered if v.gender == gender]
        if query:
            filtered = [
                v for v in filtered
                if (query in v.short_name.lower()
                    or query in v.friendly_name.lower()
                    or query in v.locale.lower()
                    or query in _locale_label(v.locale).lower()
                    or query in v.gender.lower())
            ]

        self._filtered_voices = filtered
        self._rebuild_voice_combo()

    def _rebuild_voice_combo(self) -> None:
        recent      = self._settings.recently_used_voices
        saved_voice = self._settings.voice

        self._voice_combo.blockSignals(True)
        self._voice_combo.clear()

        if not self._filtered_voices:
            self._voice_combo.addItem("No voices match")
            self._voice_combo.setEnabled(False)
            self._preview_btn.setEnabled(False)
            self._voice_count_label.setText("0 voices")
            self._voice_combo.blockSignals(False)
            return

        recent_in = [v for v in self._filtered_voices if v.short_name in recent]
        rest      = [v for v in self._filtered_voices if v.short_name not in recent]
        restore   = 0

        if recent_in:
            self._voice_combo.addItem("── Recently Used ──")
            item = self._voice_combo.model().item(self._voice_combo.count() - 1)
            if item:
                item.setEnabled(False)
                item.setForeground(Qt.darkGray)
            for v in recent_in:
                self._voice_combo.addItem(_voice_display(v) + "  ★")
                self._voice_combo.setItemData(
                    self._voice_combo.count() - 1, v.short_name, _ROLE_SHORT_NAME
                )
                if v.short_name == saved_voice:
                    restore = self._voice_combo.count() - 1

        for v in rest:
            self._voice_combo.addItem(_voice_display(v))
            self._voice_combo.setItemData(
                self._voice_combo.count() - 1, v.short_name, _ROLE_SHORT_NAME
            )
            if v.short_name == saved_voice and restore == 0:
                restore = self._voice_combo.count() - 1

        self._voice_combo.setCurrentIndex(restore)
        self._voice_combo.setEnabled(True)
        self._voice_combo.blockSignals(False)

        total = len(self._filtered_voices)
        all_n = len(self._all_voices)
        self._voice_count_label.setText(
            f"{total} voices" if total == all_n else f"{total} / {all_n}"
        )
        self._preview_btn.setEnabled(True)
        self._refresh_voice_guidance()

    def _on_voice_selection_changed(self) -> None:
        selected = self.get_selected_voice()
        if selected:
            self._settings.voice = selected
        self._refresh_voice_guidance()

    def _refresh_voice_guidance(self) -> None:
        if not self._all_voices:
            self._hide_voice_guidance()
            return

        selected_voice = self.get_selected_voice()
        if not selected_voice or not self._current_text.strip():
            self._hide_voice_guidance()
            return

        profile = build_text_profile(self._current_text)
        assessment = assess_voice_compatibility(profile, selected_voice, self._all_voices)
        self._compatibility = assessment

        warning_message = ""
        recommended_voice = None

        if assessment.requires_confirmation:
            warning_message = assessment.message
            recommended_voice = assessment.recommended_voice
        else:
            long_job_warning = self._long_job_voice_warning(profile, selected_voice)
            if long_job_warning is None:
                self._hide_voice_guidance()
                return
            warning_message, recommended_voice = long_job_warning

        self._voice_warning_label.setText(warning_message)
        self._visible_recommended_voice = recommended_voice
        self._use_recommended_voice_btn.setVisible(bool(recommended_voice))
        if recommended_voice:
            self._use_recommended_voice_btn.setText(
                f"Use {recommended_voice}"
            )
        self._voice_warning.show()

    def _hide_voice_guidance(self) -> None:
        self._compatibility = None
        self._visible_recommended_voice = None
        self._voice_warning.hide()
        self._voice_warning_label.clear()
        self._use_recommended_voice_btn.hide()

    def _long_job_voice_warning(
        self,
        profile,
        selected_voice: str,
    ) -> tuple[str, str | None] | None:
        cleaned = profile.cleaned_text.strip()
        if len(cleaned) < 45_000 or "multilingual" not in selected_voice.lower():
            return None
        if profile.language_code not in {None, "en"}:
            return None
        if profile.script_code not in {None, "latin", ""}:
            return None

        parts = selected_voice.split("-")
        locale = "-".join(parts[:2]) if len(parts) >= 2 else selected_voice
        recommended_voice = next(
            (
                voice.short_name
                for voice in self._all_voices
                if voice.short_name != selected_voice
                and voice.locale == locale
                and "multilingual" not in voice.short_name.lower()
            ),
            None,
        )
        message = (
            f"'{selected_voice}' is a multilingual model. For very long English narration jobs, "
            "SetupTTS treats it as more failure-prone than a same-locale non-multilingual voice "
            "and will use smaller chunks plus stronger recovery."
        )
        if recommended_voice:
            message += f"\nRecommended voice: {recommended_voice}"
        return message, recommended_voice

    def _on_use_recommended_voice(self) -> None:
        recommended_voice = self._visible_recommended_voice
        if not recommended_voice:
            return
        if self._select_voice_by_short_name(recommended_voice):
            self.status_message.emit(f"Using recommended voice: {recommended_voice}")

    def _select_voice_by_short_name(self, short_name: str) -> bool:
        voice = next((item for item in self._all_voices if item.short_name == short_name), None)
        if voice is None:
            return False

        self._search_edit.blockSignals(True)
        self._search_edit.clear()
        self._search_edit.blockSignals(False)

        locale_idx = self._lang_combo.findData(voice.locale)
        if locale_idx >= 0:
            self._lang_combo.setCurrentIndex(locale_idx)
        if self._gender_combo.currentText() not in {"All", voice.gender}:
            all_idx = self._gender_combo.findText("All")
            if all_idx >= 0:
                self._gender_combo.setCurrentIndex(all_idx)

        self._apply_filters()
        for idx in range(self._voice_combo.count()):
            if self._voice_combo.itemData(idx, _ROLE_SHORT_NAME) == short_name:
                self._voice_combo.setCurrentIndex(idx)
                return True
        return False

    # ------------------------------------------------------------------ #
    # Rate slider                                                          #
    # ------------------------------------------------------------------ #

    def _on_rate_changed(self, value: int) -> None:
        self._rate_value_label.setText(f"+{value}%" if value >= 0 else f"{value}%")
        self._settings.rate = value

    # ------------------------------------------------------------------ #
    # Folder browse                                                        #
    # ------------------------------------------------------------------ #

    def _set_folder_text(self, folder: str) -> None:
        """Show a folder path from its start, with the full path as a tooltip."""
        self._folder_edit.setText(folder)
        self._folder_edit.setToolTip(folder)
        self._folder_edit.setCursorPosition(0)

    def _browse_folder(self) -> None:
        current = self._folder_edit.text() or str(Path.home() / "Desktop")
        folder = QFileDialog.getExistingDirectory(self, "Choose Save Location", current)
        if folder:
            self._set_folder_text(folder)
            self._settings.output_dir = folder

    # ------------------------------------------------------------------ #
    # Preview                                                              #
    # ------------------------------------------------------------------ #

    def _on_preview(self) -> None:
        if self._preview_worker and self._preview_worker.isRunning():
            return
        voice = self.get_selected_voice()
        rate  = self.get_rate_string()
        self._preview_btn.hide()
        self._stop_preview_btn.show()
        self._preview_status.setText("Generating preview…")
        self._preview_worker = PreviewWorker(voice=voice, rate=rate)
        self._preview_worker.started_playing.connect(
            lambda: self._preview_status.setText("Playing…")
        )
        self._preview_worker.finished.connect(self._on_preview_done)
        self._preview_worker.failed.connect(self._on_preview_failed)
        self._preview_worker.start()

    def _on_stop_preview(self) -> None:
        if self._preview_worker:
            self._preview_worker.stop_playback()

    def _on_preview_done(self) -> None:
        self._stop_preview_btn.hide()
        self._preview_btn.show()
        self._preview_status.setText("")

    def _on_preview_failed(self, message: str) -> None:
        logger.warning("Preview failed: %s", message)
        self._on_preview_done()
        self._preview_status.setText("Preview unavailable")

    # ------------------------------------------------------------------ #
    # Submit generation job                                                #
    # ------------------------------------------------------------------ #

    def _on_generate(self) -> None:
        # ── Debounce: disable for 1.5 s to prevent rapid double-click ──── #
        # Button is re-enabled by _restore_generate_btn after the timer fires.
        self._generate_btn.setEnabled(False)
        QTimer.singleShot(1500, self._restore_generate_btn)

        parent_win = self.window()
        text = parent_win.get_input_text() if hasattr(parent_win, "get_input_text") else ""

        if not text:
            QMessageBox.warning(self, "No Text",
                "Please add some text on the left before generating.")
            return

        voice        = self.get_selected_voice()
        output_path  = self.get_output_path()
        rate         = self.get_rate_string()
        volume       = self.get_volume_string()
        allow_voice_mismatch = False

        self._refresh_voice_guidance()
        assessment = self._compatibility
        if assessment and assessment.requires_confirmation:
            if (
                self._settings.auto_switch_recommended_voice
                and assessment.recommended_voice
                and self._select_voice_by_short_name(assessment.recommended_voice)
            ):
                self.status_message.emit(
                    f"Auto-switched to recommended voice: {assessment.recommended_voice}"
                )
                voice = self.get_selected_voice()
                self._refresh_voice_guidance()
            else:
                prompt = QMessageBox(self)
                prompt.setIcon(QMessageBox.Icon.Warning)
                prompt.setWindowTitle("Voice May Not Match Text")
                prompt.setText(assessment.message)

                use_recommended_btn = None
                if assessment.recommended_voice:
                    use_recommended_btn = prompt.addButton(
                        "Use Recommended Voice",
                        QMessageBox.ButtonRole.AcceptRole,
                    )
                generate_anyway_btn = prompt.addButton(
                    "Generate Anyway",
                    QMessageBox.ButtonRole.ActionRole,
                )
                cancel_btn = prompt.addButton(QMessageBox.StandardButton.Cancel)
                prompt.exec()

                clicked = prompt.clickedButton()
                if clicked == cancel_btn:
                    return
                if use_recommended_btn is not None and clicked == use_recommended_btn:
                    if not self._select_voice_by_short_name(assessment.recommended_voice or ""):
                        return
                    voice = self.get_selected_voice()
                    self._refresh_voice_guidance()
                elif clicked == generate_anyway_btn:
                    allow_voice_mismatch = True
                else:
                    return

        # ── Duplicate output-path guard ─────────────────────────────────── #
        if self._queue.has_active_output_path(output_path):
            QMessageBox.warning(
                self,
                "Job Already Active",
                f"A job writing to\n\n"
                f"  {Path(output_path).name}\n\n"
                "is already running or queued.\n\n"
                "Please wait for it to finish, cancel it first, or choose "
                "a different output file name.",
            )
            return

        # Build compact voice display for the job row
        parts        = voice.split("-")
        persona      = parts[-1].replace("Neural", "").replace("Multilingual", "") if parts else voice
        locale_key   = "-".join(parts[:2]) if len(parts) >= 2 else voice
        voice_display = f"{persona} · {_locale_label(locale_key)}"

        # Persist settings
        self._settings.voice      = voice
        self._settings.output_dir = str(Path(output_path).parent)
        self._settings.add_recently_used_voice(voice)
        self._settings.save()

        logger.info(
            "Submitting job: voice=%s output=%s rate=%s",
            voice, output_path, rate,
        )
        try:
            self._queue.submit(
                text=text, voice=voice, voice_display=voice_display,
                rate=rate, volume=volume, output_path=output_path,
                allow_voice_mismatch=allow_voice_mismatch,
            )
        except ValueError:
            QMessageBox.warning(
                self,
                "Job Already Active",
                f"A job writing to\n\n"
                f"  {Path(output_path).name}\n\n"
                "is already running or queued.\n\n"
                "Please wait for it to finish, cancel it first, or choose "
                "a different output file name.",
            )

    def _restore_generate_btn(self) -> None:
        """Re-enable the Generate button after the debounce timer fires."""
        parent_win = self.window()
        has_text = bool(
            parent_win.get_input_text()
            if hasattr(parent_win, "get_input_text") else ""
        )
        self._generate_btn.setEnabled(has_text)

    def _refresh_resume_jobs(self) -> None:
        try:
            self._resume_candidates = ChunkStore.list_resume_candidates(AppPaths().staging_dir)
        except Exception:
            logger.warning("Could not load resumable jobs", exc_info=True)
            self._resume_candidates = []

        if not self._resume_candidates:
            self._resume_job_btn.hide()
            self._resume_job_btn.setToolTip("")
            self._resume_job_hint.hide()
            self._resume_job_hint.setText("")
            return

        latest = self._resume_candidates[0]
        resume_chunk = latest.failed_at_chunk or (latest.completed_count + 1)
        latest_name = Path(latest.output_path).name
        count = len(self._resume_candidates)
        suffix = "" if count == 1 else f" ({count})"
        self._resume_job_btn.set_labels([
            f"Resume Saved Job{suffix}",
            f"Resume Job{suffix}",
            f"Resume{suffix}",
        ])
        detail = (
            f"{count} resumable job(s) saved locally. Latest: {latest_name} — "
            f"{latest.completed_count} chunk(s) preserved, resume at chunk {resume_chunk}."
        )
        self._resume_job_hint.setText(detail)
        # The hint elides to one line and collapses in compact mode, so the
        # full detail has to stay reachable from the button it belongs to.
        self._resume_job_btn.setToolTip(detail)
        self._resume_job_hint.setToolTip(detail)
        self._resume_job_btn.show()
        self._resume_job_hint.setVisible(self._density != self.DENSITY_MINIMAL)

    def _on_resume_saved_job(self) -> None:
        if not self._resume_candidates:
            self._refresh_resume_jobs()
        if not self._resume_candidates:
            QMessageBox.information(
                self,
                "No Saved Job",
                "No resumable SetupTTS job was found.",
            )
            return

        if len(self._resume_candidates) == 1:
            self._resume_candidate(self._resume_candidates[0])
            return

        menu = QMenu(self)
        for candidate in self._resume_candidates:
            resume_chunk = candidate.failed_at_chunk or (candidate.completed_count + 1)
            label = (
                f"{Path(candidate.output_path).name} — "
                f"resume at chunk {resume_chunk} ({candidate.completed_count} preserved)"
            )
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, c=candidate: self._resume_candidate(c)
            )
        menu.exec(self._resume_job_btn.mapToGlobal(self._resume_job_btn.rect().bottomLeft()))

    def _resume_candidate(self, candidate: ResumeCandidate) -> None:
        if self._queue.has_active_output_path(candidate.output_path):
            QMessageBox.warning(
                self,
                "Job Already Active",
                "A job for this output file is already running or queued.",
            )
            return

        voice = candidate.voice
        self._select_voice_by_short_name(voice)
        output_path = Path(candidate.output_path)
        self._filename_edit.setText(output_path.name)
        self._set_folder_text(str(output_path.parent))
        self._settings.output_dir = str(output_path.parent)

        parent_win = self.window()
        if hasattr(parent_win, "set_input_text"):
            try:
                parent_win.set_input_text(candidate.text)
            except Exception:
                logger.warning("Could not load resumable text into the editor", exc_info=True)

        parts = voice.split("-")
        persona = parts[-1].replace("Neural", "").replace("Multilingual", "") if parts else voice
        locale_key = "-".join(parts[:2]) if len(parts) >= 2 else voice
        voice_display = f"{persona} · {_locale_label(locale_key)}"

        try:
            self._queue.submit(
                text=candidate.text,
                voice=voice,
                voice_display=voice_display,
                rate=candidate.rate,
                volume=candidate.volume,
                output_path=candidate.output_path,
                allow_voice_mismatch=False,
                job_id=candidate.job_id,
                resume_staging_dir=str(candidate.staging_dir),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Job Already Active", str(exc))
            return

        self.status_message.emit(f"Resuming saved job: {output_path.name}")
        self._refresh_resume_jobs()

    # ------------------------------------------------------------------ #
    # Job queue event handlers                                             #
    # ------------------------------------------------------------------ #

    def _on_job_submitted(self, item: JobItem) -> None:
        row = _JobRowWidget(item, parent=self)
        row.cancel_requested.connect(self._queue.cancel)
        self._job_rows[item.id] = row
        self._jobs_list_layout.addWidget(row)
        first_job = not self._jobs_card.isVisible()
        self._jobs_card.setVisible(True)
        self._update_jobs_header()
        self.status_message.emit(f"Queued: {item.filename}")

        # ACTIVE JOBS sits at the bottom of the scrollable column, so on a
        # short window it lands below the fold and the user watches a blank
        # sidebar while their export runs.  Bring it into view for the first
        # job of a batch.  Deferred: the card has just been shown, so its
        # geometry is only valid after the pending layout pass.
        if first_job:
            QTimer.singleShot(0, self._scroll_to_jobs)

    def _scroll_to_jobs(self) -> None:
        """Scroll the sidebar so the ACTIVE JOBS card is visible."""
        if self._jobs_card.isVisible():
            self._scroll.ensureWidgetVisible(self._jobs_card, 0, 0)

    def _on_job_started(self, item: JobItem) -> None:
        if item.id in self._job_rows:
            self._job_rows[item.id].set_running()
        self._update_jobs_header()

    def _on_job_progress(self, job_id: str, pct: int) -> None:
        if job_id in self._job_rows:
            self._job_rows[job_id].update_progress(pct)

    def _on_job_status_changed(self, job_id: str, text: str) -> None:
        if job_id in self._job_rows:
            self._job_rows[job_id].update_status(text)

    def _on_job_stage_changed(self, job_id: str, kind: str, text: str) -> None:
        if job_id in self._job_rows:
            self._job_rows[job_id].update_stage(kind, text)

    def _on_job_speed_updated(self, job_id: str, cps: float) -> None:
        if job_id in self._job_rows:
            self._job_rows[job_id].update_speed(cps)

    def _on_job_telemetry_updated(self, job_id: str, telemetry: object) -> None:
        if job_id in self._job_rows:
            self._job_rows[job_id].update_telemetry(telemetry)

    def _on_job_resumable(self, item: JobItem) -> None:
        self.status_message.emit(
            f"Partial progress preserved for {item.filename} — resume from chunk {item.failed_chunk or (item.preserved_chunks + 1)}"
        )
        QTimer.singleShot(0, self._refresh_resume_jobs)

    def _on_job_completed(self, item: JobItem) -> None:
        self._remove_job_row(item.id)

        # Persist to SQLite history
        job = Job(
            id=None,
            text_preview=item.text[:80],
            voice=item.voice,
            rate=item.rate,
            output_path=item.output_path,
            duration_seconds=item.duration,
            status=JobStatus.COMPLETED,
        )
        try:
            job = self._history.add_job(job)
        except Exception:
            logger.warning("History write failed", exc_info=True)

        self.job_completed.emit(job)
        self.status_message.emit(f"Saved: {item.filename}")
        self._refresh_resume_jobs()

    def _on_job_failed(self, item: JobItem) -> None:
        self._remove_job_row(item.id)
        self._refresh_resume_jobs()
        self._failure_dialog_queue.append(item)
        if not self._failure_dialog_active:
            self._show_next_failure_dialog()

    def _show_next_failure_dialog(self) -> None:
        if not self._failure_dialog_queue:
            self._failure_dialog_active = False
            return

        self._failure_dialog_active = True
        item = self._failure_dialog_queue.pop(0)

        prompt = QMessageBox(self)
        prompt.setIcon(QMessageBox.Icon.Critical)
        prompt.setWindowTitle("Generation Failed")
        prompt.setText(item.error)
        resume_btn = None
        if item.resumable and item.resume_staging_dir:
            resume_btn = prompt.addButton(
                "Resume Failed Job",
                QMessageBox.ButtonRole.AcceptRole,
            )
        prompt.addButton(QMessageBox.StandardButton.Ok)
        prompt.exec()

        if resume_btn is not None and prompt.clickedButton() == resume_btn:
            candidate = next(
                (
                    saved
                    for saved in self._resume_candidates
                    if str(saved.staging_dir) == item.resume_staging_dir
                ),
                None,
            )
            if candidate is not None:
                self._resume_candidate(candidate)
                # Show next failure dialog (if any) after a brief delay so the
                # resume job can start before the next error dialog interrupts.
                QTimer.singleShot(400, self._show_next_failure_dialog)
                return

        if item.resumable and item.preserved_chunks > 0:
            self.status_message.emit(
                f"Generation failed at chunk {item.failed_chunk}; {item.preserved_chunks} chunk(s) preserved"
            )
        else:
            self.status_message.emit(f"Generation failed: {item.filename}")

        # Show the next queued failure dialog, if any.
        self._show_next_failure_dialog()

    def _on_job_cancelled(self, item: JobItem) -> None:
        self._remove_job_row(item.id)
        self.status_message.emit(f"Cancelled: {item.filename}")
        QTimer.singleShot(300, self._refresh_resume_jobs)

    def _remove_job_row(self, job_id: str) -> None:
        row = self._job_rows.pop(job_id, None)
        if row:
            self._jobs_list_layout.removeWidget(row)
            row.deleteLater()
        self._update_jobs_header()
        if not self._job_rows:
            self._jobs_card.setVisible(False)

    def _update_jobs_header(self) -> None:
        n = len(self._job_rows)
        self._jobs_count_label.setText(f"{n}" if n else "")


# ══════════════════════════════════════════════════════════════════════ #
#  Per-job row widget                                                    #
# ══════════════════════════════════════════════════════════════════════ #

class _JobRowWidget(QWidget):
    """
    Compact two-line widget representing one job in the active jobs list.

    Line 1: [icon]  filename.mp3                     [Cancel ✕]
    Line 2:         Voice · Locale  ▓▓▓░░░  45%  Status text
    """

    cancel_requested = Signal(str)   # job_id

    def __init__(self, item: JobItem, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._job_id    = item.id
        self._has_stage = False   # True once first stage_changed event arrives
        self._telemetry = None
        self._build(item)

    def _build(self, item: JobItem) -> None:
        self.setObjectName("jobRow")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 2)
        root.setSpacing(2)

        # ── Line 1 ─────────────────────────────────────────────────── #
        top = QHBoxLayout()
        top.setSpacing(4)
        top.setContentsMargins(0, 0, 0, 0)

        self._icon_lbl = QLabel("·")
        self._icon_lbl.setFixedWidth(14)
        self._icon_lbl.setStyleSheet(
            "font-size: 11px; color: #5A5A60; background: transparent;"
        )
        top.addWidget(self._icon_lbl)

        self._name_lbl = _ElidingLabel(item.filename)
        self._name_lbl.setStyleSheet(
            "font-size: 12px; font-weight: 600; color: #F2F2F4; background: transparent;"
        )
        self._name_lbl.setToolTip(item.output_path)
        top.addWidget(self._name_lbl, 1)

        self._cancel_btn = QPushButton("✕")
        self._cancel_btn.setObjectName("ghostButton")
        self._cancel_btn.setFixedSize(22, 22)
        self._cancel_btn.setToolTip("Cancel this job")
        self._cancel_btn.setStyleSheet(
            "QPushButton { color: #5A5A60; font-size: 11px; padding: 0; }"
            "QPushButton:hover { color: #FF453A; }"
        )
        self._cancel_btn.clicked.connect(
            lambda: self.cancel_requested.emit(self._job_id)
        )
        top.addWidget(self._cancel_btn)
        root.addLayout(top)

        # ── Line 2 ─────────────────────────────────────────────────── #
        bot = QHBoxLayout()
        bot.setSpacing(5)
        bot.setContentsMargins(18, 0, 0, 0)  # indent to align under filename

        self._voice_lbl = _ElidingLabel(item.voice_display)
        self._voice_lbl.setStyleSheet(
            "font-size: 10px; color: #5A5A60; background: transparent;"
        )
        self._voice_lbl.setToolTip(item.voice_display)
        bot.addWidget(self._voice_lbl, 1)

        self._pct_lbl = QLabel("")
        self._pct_lbl.setStyleSheet(
            "font-size: 10px; font-weight: 700; color: #1DB954; "
            "background: transparent; min-width: 28px;"
        )
        self._pct_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._pct_lbl.hide()
        bot.addWidget(self._pct_lbl)
        root.addLayout(bot)

        # ── Line 3: full-width progress bar ─────────────────────────── #
        bar_row = QHBoxLayout()
        bar_row.setContentsMargins(18, 1, 0, 1)
        bar_row.setSpacing(0)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(4)
        self._progress_bar.hide()
        bar_row.addWidget(self._progress_bar)
        root.addLayout(bar_row)

        # ── Line 4: status text ─────────────────────────────────────── #
        status_row = QHBoxLayout()
        status_row.setContentsMargins(18, 0, 0, 0)
        status_row.setSpacing(0)
        self._status_lbl = QLabel(item.status_text)
        self._status_lbl.setStyleSheet(
            "font-size: 10px; color: #5A5A60; background: transparent;"
        )
        self._status_lbl.setWordWrap(True)
        status_row.addWidget(self._status_lbl, 1)
        root.addLayout(status_row)

        # ── Line 5: real-time speed / ETA ───────────────────────────── #
        spd_row = QHBoxLayout()
        spd_row.setContentsMargins(18, 0, 0, 0)
        spd_row.setSpacing(0)

        self._speed_lbl = QLabel("")
        self._speed_lbl.setStyleSheet(
            "font-size: 11px; font-weight: 700; color: #1DB954;"
            " background: transparent;"
        )
        self._speed_lbl.setWordWrap(True)
        self._speed_lbl.hide()
        spd_row.addWidget(self._speed_lbl, 1)
        root.addLayout(spd_row)

    # ------------------------------------------------------------------ #

    # Stage-kind → (hex color, badge label)
    _STAGE_STYLE: dict[str, tuple[str, str]] = {
        "local":   ("#5A8A6A", "LOCAL"),    # muted green — work on your machine
        "remote":  ("#4A8CC2", "REMOTE"),   # blue — network / Microsoft servers
        "waiting": ("#C2944A", "WAIT"),     # amber — blocked on server/retry
    }

    def set_running(self) -> None:
        self._icon_lbl.setText("▶")
        self._icon_lbl.setStyleSheet(
            "font-size: 10px; color: #1DB954; background: transparent;"
        )
        self._progress_bar.show()
        self._pct_lbl.show()
        self._status_lbl.setTextFormat(Qt.PlainText)
        self._status_lbl.setText("Connecting…")

    def update_progress(self, pct: int) -> None:
        self._progress_bar.setValue(pct)
        self._pct_lbl.setText(f"{pct}%")

    def update_status(self, text: str) -> None:
        # Only update with plain text if no stage event has arrived yet.
        # Once stage events are flowing they carry richer information.
        if not self._has_stage:
            self._status_lbl.setTextFormat(Qt.PlainText)
            self._status_lbl.setText(text)

    def update_stage(self, kind: str, text: str) -> None:
        """Show a color-coded LOCAL / REMOTE / WAIT badge + detail text."""
        self._has_stage = True
        color, badge = self._STAGE_STYLE.get(kind, ("#7A7A80", kind.upper()))
        html = (
            f'<span style="color:{color};font-weight:bold;font-size:9px">'
            f'[{badge}]</span>'
            f'<span style="color:#7A7A80;font-size:10px"> {text}</span>'
        )
        self._status_lbl.setTextFormat(Qt.RichText)
        self._status_lbl.setText(html)

    def update_speed(self, cps: float) -> None:
        """Show real-time generation speed below the progress bar."""
        if cps > 0:
            self._speed_lbl.setText(f"{cps:,.0f} chars/s")
            self._speed_lbl.show()

    def update_telemetry(self, telemetry: object) -> None:
        self._telemetry = telemetry
        cps = getattr(telemetry, "rolling_chars_per_second", 0.0) or 0.0
        current_chunk = getattr(telemetry, "current_chunk", 0) or 0
        estimated_total = getattr(telemetry, "estimated_total_chunks", None)
        chunk_chars = getattr(telemetry, "chunk_chars", 0) or 0
        eta_seconds = getattr(telemetry, "eta_seconds", None)

        parts: list[str] = []
        if cps > 0:
            parts.append(f"{cps:,.0f} chars/s")
        if eta_seconds is not None and eta_seconds > 1:
            parts.append(f"ETA {_format_eta(eta_seconds)}")
        if current_chunk > 0:
            if estimated_total and estimated_total >= current_chunk:
                parts.append(f"chunk {current_chunk}/~{estimated_total}")
            else:
                parts.append(f"chunk {current_chunk}")
        if chunk_chars > 0:
            parts.append(f"{chunk_chars:,} chars")

        if parts:
            self._speed_lbl.setText(" · ".join(parts))
            self._speed_lbl.show()


# ══════════════════════════════════════════════════════════════════════ #
#  Module-level helpers                                                  #
# ══════════════════════════════════════════════════════════════════════ #

# Built once at import time — previously recreated on every _locale_label() call.
_LOCALE_MAP: dict[str, str] = {
    "af-ZA": "Afrikaans (South Africa)", "am-ET": "Amharic",
    "ar-AE": "Arabic (UAE)", "ar-BH": "Arabic (Bahrain)",
    "ar-DZ": "Arabic (Algeria)", "ar-EG": "Arabic (Egypt)",
    "ar-IQ": "Arabic (Iraq)", "ar-JO": "Arabic (Jordan)",
    "ar-KW": "Arabic (Kuwait)", "ar-LB": "Arabic (Lebanon)",
    "ar-LY": "Arabic (Libya)", "ar-MA": "Arabic (Morocco)",
    "ar-OM": "Arabic (Oman)", "ar-QA": "Arabic (Qatar)",
    "ar-SA": "Arabic (Saudi Arabia)", "ar-SY": "Arabic (Syria)",
    "ar-TN": "Arabic (Tunisia)", "ar-YE": "Arabic (Yemen)",
    "az-AZ": "Azerbaijani", "bg-BG": "Bulgarian",
    "bn-BD": "Bengali (Bangladesh)", "bn-IN": "Bengali (India)",
    "bs-BA": "Bosnian", "ca-ES": "Catalan", "cs-CZ": "Czech",
    "cy-GB": "Welsh", "da-DK": "Danish",
    "de-AT": "German (Austria)", "de-CH": "German (Switzerland)",
    "de-DE": "German", "el-GR": "Greek",
    "en-AU": "English (Australia)", "en-CA": "English (Canada)",
    "en-GB": "English (UK)", "en-HK": "English (Hong Kong)",
    "en-IE": "English (Ireland)", "en-IN": "English (India)",
    "en-KE": "English (Kenya)", "en-NG": "English (Nigeria)",
    "en-NZ": "English (New Zealand)", "en-PH": "English (Philippines)",
    "en-SG": "English (Singapore)", "en-TZ": "English (Tanzania)",
    "en-US": "English (US)", "en-ZA": "English (South Africa)",
    "es-AR": "Spanish (Argentina)", "es-BO": "Spanish (Bolivia)",
    "es-CL": "Spanish (Chile)", "es-CO": "Spanish (Colombia)",
    "es-CR": "Spanish (Costa Rica)", "es-CU": "Spanish (Cuba)",
    "es-DO": "Spanish (Dom. Rep.)", "es-EC": "Spanish (Ecuador)",
    "es-ES": "Spanish (Spain)", "es-GT": "Spanish (Guatemala)",
    "es-HN": "Spanish (Honduras)", "es-MX": "Spanish (Mexico)",
    "es-NI": "Spanish (Nicaragua)", "es-PA": "Spanish (Panama)",
    "es-PE": "Spanish (Peru)", "es-PR": "Spanish (Puerto Rico)",
    "es-PY": "Spanish (Paraguay)", "es-SV": "Spanish (El Salvador)",
    "es-US": "Spanish (US)", "es-UY": "Spanish (Uruguay)",
    "es-VE": "Spanish (Venezuela)", "et-EE": "Estonian",
    "eu-ES": "Basque", "fa-IR": "Persian", "fi-FI": "Finnish",
    "fil-PH": "Filipino", "fr-BE": "French (Belgium)",
    "fr-CA": "French (Canada)", "fr-CH": "French (Switzerland)",
    "fr-FR": "French", "ga-IE": "Irish", "gl-ES": "Galician",
    "gu-IN": "Gujarati", "he-IL": "Hebrew", "hi-IN": "Hindi",
    "hr-HR": "Croatian", "hu-HU": "Hungarian", "hy-AM": "Armenian",
    "id-ID": "Indonesian", "is-IS": "Icelandic",
    "it-CH": "Italian (Switzerland)", "it-IT": "Italian",
    "ja-JP": "Japanese", "jv-ID": "Javanese", "ka-GE": "Georgian",
    "kk-KZ": "Kazakh", "km-KH": "Khmer", "kn-IN": "Kannada",
    "ko-KR": "Korean", "lo-LA": "Lao", "lt-LT": "Lithuanian",
    "lv-LV": "Latvian", "mk-MK": "Macedonian", "ml-IN": "Malayalam",
    "mn-MN": "Mongolian", "mr-IN": "Marathi", "ms-MY": "Malay",
    "mt-MT": "Maltese", "my-MM": "Burmese", "nb-NO": "Norwegian",
    "ne-NP": "Nepali", "nl-BE": "Dutch (Belgium)", "nl-NL": "Dutch",
    "or-IN": "Odia", "pa-IN": "Punjabi", "pl-PL": "Polish",
    "ps-AF": "Pashto", "pt-BR": "Portuguese (Brazil)",
    "pt-PT": "Portuguese (Portugal)", "ro-RO": "Romanian",
    "ru-RU": "Russian", "si-LK": "Sinhala", "sk-SK": "Slovak",
    "sl-SI": "Slovenian", "so-SO": "Somali", "sq-AL": "Albanian",
    "sr-RS": "Serbian", "su-ID": "Sundanese", "sv-SE": "Swedish",
    "sw-KE": "Swahili (Kenya)", "sw-TZ": "Swahili (Tanzania)",
    "ta-IN": "Tamil (India)", "ta-LK": "Tamil (Sri Lanka)",
    "ta-MY": "Tamil (Malaysia)", "ta-SG": "Tamil (Singapore)",
    "te-IN": "Telugu", "th-TH": "Thai", "tr-TR": "Turkish",
    "uk-UA": "Ukrainian", "ur-IN": "Urdu (India)",
    "ur-PK": "Urdu (Pakistan)", "uz-UZ": "Uzbek", "vi-VN": "Vietnamese",
    "wuu-CN": "Shanghainese", "yue-CN": "Cantonese",
    "zh-CN": "Chinese (Mainland)", "zh-CN-liaoning": "Chinese (Liaoning)",
    "zh-CN-shaanxi": "Chinese (Shaanxi)", "zh-HK": "Chinese (HK)",
    "zh-TW": "Chinese (Taiwan)", "zu-ZA": "Zulu",
}


class _AdaptiveLabelButton(QPushButton):
    """
    Push button that steps down to a shorter label rather than clipping.

    Qt neither elides nor wraps a button label: a button narrower than its
    text simply cuts the text off.  The sidebar is narrow by design and font
    metrics differ sharply by platform — "Generate & Export MP3" needs about
    330 px in Windows' Segoe UI against roughly 290 px of usable sidebar,
    while the same string fits comfortably in macOS's SF Pro — so the button
    carries labels from longest to shortest and shows the longest that fits.

    Labels use Qt's "&&" escape for a literal ampersand; the mnemonic form is
    what gets measured, since that is what is drawn.
    """

    #: Stylesheet padding either side, plus a little slack for the border.
    _CHROME_WIDTH = 36

    def __init__(self, labels: list[str], parent: QWidget | None = None) -> None:
        super().__init__(labels[0], parent)
        self._labels = list(labels)
        # Ignored, not Expanding: the button fills the width it is given but
        # never reports a text-driven minimum that would widen the sidebar.
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def set_labels(self, labels: list[str]) -> None:
        """Replace the label set, then re-choose from it."""
        self._labels = list(labels)
        self.refresh_label()

    def refresh_label(self) -> None:
        """Re-choose the label — call after anything that changes the font."""
        metrics   = QFontMetrics(self.font())
        available = self.width() - self._CHROME_WIDTH
        chosen    = self._labels[-1]
        for label in self._labels:
            if metrics.horizontalAdvance(label.replace("&&", "&")) <= available:
                chosen = label
                break
        if self.text() != chosen:
            self.setText(chosen)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.refresh_label()


class _ElidingLabel(QLabel):
    """
    Single-line label that elides overflow instead of forcing its parent wider.

    A plain QLabel reports its full text width as a minimum, so one long
    filename can push the whole sidebar past its maximum width and clip.

    ``mode`` picks where the ellipsis goes: the default middle elide keeps both
    ends of a filename readable, while running prose should elide at the end
    (a sentence cut in the middle reads as two fragments).
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        mode: Qt.TextElideMode = Qt.ElideMiddle,
    ) -> None:
        super().__init__(parent)
        self._full_text = text
        self._elide_mode = mode
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)
        self._apply_elide()

    def setText(self, text: str) -> None:  # type: ignore[override]
        self._full_text = text
        self._apply_elide()

    def full_text(self) -> str:
        return self._full_text

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        metrics = QFontMetrics(self.font())
        super().setText(
            metrics.elidedText(self._full_text, self._elide_mode, max(30, self.width()))
        )


def _card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    # Minimum, not the default Preferred: a QScrollArea sizes its inner widget
    # to the viewport expanded to the widget's *minimum* hint, not its size
    # hint.  With Preferred the cards absorbed the difference by shrinking
    # below their content — clipping the Browse and Preview buttons by a few
    # pixels — instead of the scroll area growing and offering a scrollbar.
    f.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
    return f


def _format_eta(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("sectionLabel")
    return lbl


def _field_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet("color: #9A9A9F; font-size: 11px; background: transparent;")
    return lbl


def _voice_display(v: Voice) -> str:
    parts   = v.short_name.split("-")
    persona = parts[-1].replace("Neural", "").replace("Multilingual", "")
    return f"{persona}  ·  {v.gender}  ·  {_locale_label(v.locale)}"


def _open_path(path: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        logger.error("Failed to open %s: %s", path, exc)


def _locale_label(locale: str) -> str:
    return _LOCALE_MAP.get(locale, locale)
