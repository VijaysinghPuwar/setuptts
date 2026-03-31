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
  ┌─ VOICE ─────────────────────────────────────────────────┐
  │ [Search…_______] [All Languages▾] [All▾]               │
  │ [Ava · Female · English US_____________________________▾]│
  │ [▶ Preview Voice]                                       │
  ├─ SPEED ─────────────────────────────────────────── +5% ─┤
  │ Slower ●────────────────────────────────────── Faster   │
  ├─ EXPORT ────────────────────────────────────────────────┤
  │ File Name                                               │
  │ [output.mp3__________________________________________]  │
  │ Save To                                                 │
  │ [/Users/.../Desktop________________________________][Brw]│
  │ ─────────────────────────────────────────────────────── │
  │ [           Generate & Export MP3                    ]  │
  ├─ ACTIVE JOBS ──────────────────────────────── (hidden)  ┤
  │ ▶ chapter1.mp3  Ava·EN  ▓▓▓▓▓░░░  45%  Generating  [✕]│
  │ · chapter2.mp3  Guy·EN              Queued           [✕]│
  └─────────────────────────────────────────────────────────┘
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
from app.workers.job_queue import JobItem, JobQueue
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
        # Keep old voice-loader workers alive until their thread exits.
        # Without this, replacing self._voice_loader on retry can cause
        # the old QThread object to be GC-collected mid-run → crash.
        self._finishing_workers: list[QThread] = []

        # Job queue — allows multiple concurrent exports
        self._queue    = JobQueue(parent=self)
        self._job_rows: dict[str, "_JobRowWidget"] = {}

        # Debounce voice-filter rebuilds so rapid search typing doesn't
        # hammer the combo (400+ addItem() calls per keystroke).
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(150)
        self._filter_timer.timeout.connect(self._apply_filters)

        self.setObjectName("sidePanel")
        self._build_ui()
        self._connect_signals()
        self._apply_settings()
        self._start_voice_load()

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
            for sig_name in ("loaded", "failed", "started_playing",
                              "finished", "progress", "status_changed",
                              "completed"):
                sig = getattr(worker, sig_name, None)
                if sig is not None:
                    try:
                        sig.disconnect()
                    except Exception:
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

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        il = QVBoxLayout(inner)
        il.setContentsMargins(8, 8, 8, 8)
        il.setSpacing(6)

        il.addWidget(self._build_voice_section())
        il.addWidget(self._build_speed_section())
        il.addWidget(self._build_export_section())

        # Active jobs card — hidden until first job is submitted
        self._jobs_card = self._build_jobs_section()
        il.addWidget(self._jobs_card)

        il.addStretch()
        scroll.setWidget(inner)
        root.addWidget(scroll)

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

        # Search + filters on one row
        sf = QHBoxLayout()
        sf.setSpacing(5)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search voices…")
        self._search_edit.setClearButtonEnabled(True)
        sf.addWidget(self._search_edit, 2)

        self._lang_combo = QComboBox()
        self._lang_combo.addItem("All Languages", userData="")
        self._lang_combo.setMaxVisibleItems(18)
        sf.addWidget(self._lang_combo, 3)

        self._gender_combo = QComboBox()
        self._gender_combo.addItems(["All", "Female", "Male"])
        self._gender_combo.setFixedWidth(76)
        sf.addWidget(self._gender_combo)
        ly.addLayout(sf)

        # Voice selector
        self._voice_combo = QComboBox()
        self._voice_combo.addItem("Loading voices…")
        self._voice_combo.setEnabled(False)
        self._voice_combo.setMaxVisibleItems(16)
        self._voice_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        ly.addWidget(self._voice_combo)

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
        self._folder_edit.setStyleSheet(
            "QLineEdit { color: #9A9A9F; } QLineEdit:focus { border-color: #2C2C30; }"
        )
        folder_row.addWidget(self._folder_edit, 1)
        self._browse_btn = QPushButton("Browse")
        folder_row.addWidget(self._browse_btn)
        ly.addLayout(folder_row)
        ly.addSpacing(12)

        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        div.setStyleSheet("background: #2C2C30; border: none; max-height: 1px;")
        ly.addWidget(div)
        ly.addSpacing(12)

        # Generate button — always available when text exists
        self._generate_btn = QPushButton("  Generate & Export MP3")
        self._generate_btn.setObjectName("generateButton")
        self._generate_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._generate_btn.setEnabled(False)
        ly.addWidget(self._generate_btn)
        ly.addSpacing(5)

        # Hint shown when no text
        self._generate_hint = QLabel("Type or paste text on the left to get started")
        self._generate_hint.setObjectName("hintLabel")
        self._generate_hint.setAlignment(Qt.AlignCenter)
        self._generate_hint.setStyleSheet(
            "color: #3A3A40; font-size: 11px; background: transparent;"
        )
        ly.addWidget(self._generate_hint)

        return card

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

        # Speed
        self._rate_slider.valueChanged.connect(self._on_rate_changed)

        # Export form
        self._browse_btn.clicked.connect(self._browse_folder)
        self._generate_btn.clicked.connect(self._on_generate)
        self._retry_voices_btn.clicked.connect(self._start_voice_load)

        # Preview
        self._preview_btn.clicked.connect(self._on_preview)
        self._stop_preview_btn.clicked.connect(self._on_stop_preview)

        # Job queue signals
        self._queue.job_submitted.connect(self._on_job_submitted)
        self._queue.job_started.connect(self._on_job_started)
        self._queue.job_progress.connect(self._on_job_progress)
        self._queue.job_status_changed.connect(self._on_job_status_changed)
        self._queue.job_stage_changed.connect(self._on_job_stage_changed)
        self._queue.job_speed_updated.connect(self._on_job_speed_updated)
        self._queue.job_completed.connect(self._on_job_completed)
        self._queue.job_failed.connect(self._on_job_failed)
        self._queue.job_cancelled.connect(self._on_job_cancelled)

    # ------------------------------------------------------------------ #
    # Called by MainWindow when input text changes                        #
    # ------------------------------------------------------------------ #

    def on_text_changed(self, text: str) -> None:
        has_text = bool(text.strip())
        self._generate_btn.setEnabled(has_text)
        self._generate_hint.setVisible(not has_text)

    # ------------------------------------------------------------------ #
    # Settings                                                             #
    # ------------------------------------------------------------------ #

    def _apply_settings(self) -> None:
        self._rate_slider.setValue(self._settings.rate)
        folder = self._settings.output_dir or str(Path.home() / "Desktop")
        self._folder_edit.setText(folder)
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

    # ------------------------------------------------------------------ #
    # Rate slider                                                          #
    # ------------------------------------------------------------------ #

    def _on_rate_changed(self, value: int) -> None:
        self._rate_value_label.setText(f"+{value}%" if value >= 0 else f"{value}%")
        self._settings.rate = value

    # ------------------------------------------------------------------ #
    # Folder browse                                                        #
    # ------------------------------------------------------------------ #

    def _browse_folder(self) -> None:
        current = self._folder_edit.text() or str(Path.home() / "Desktop")
        folder = QFileDialog.getExistingDirectory(self, "Choose Save Location", current)
        if folder:
            self._folder_edit.setText(folder)
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
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "No Text",
                "Please add some text on the left before generating.")
            return

        voice        = self.get_selected_voice()
        output_path  = self.get_output_path()
        rate         = self.get_rate_string()
        volume       = self.get_volume_string()

        # ── Duplicate output-path guard ─────────────────────────────────── #
        if self._queue.has_active_output_path(output_path):
            from PySide6.QtWidgets import QMessageBox
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
        self._queue.submit(
            text=text, voice=voice, voice_display=voice_display,
            rate=rate, volume=volume, output_path=output_path,
        )

    def _restore_generate_btn(self) -> None:
        """Re-enable the Generate button after the debounce timer fires."""
        parent_win = self.window()
        has_text = bool(
            parent_win.get_input_text()
            if hasattr(parent_win, "get_input_text") else ""
        )
        self._generate_btn.setEnabled(has_text)

    # ------------------------------------------------------------------ #
    # Job queue event handlers                                             #
    # ------------------------------------------------------------------ #

    def _on_job_submitted(self, item: JobItem) -> None:
        row = _JobRowWidget(item, parent=self)
        row.cancel_requested.connect(self._queue.cancel)
        self._job_rows[item.id] = row
        self._jobs_list_layout.addWidget(row)
        self._jobs_card.setVisible(True)
        self._update_jobs_header()
        self.status_message.emit(f"Queued: {item.filename}")

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

    def _on_job_failed(self, item: JobItem) -> None:
        self._remove_job_row(item.id)
        # Use a non-blocking (modeless) dialog so that simultaneous failures
        # from concurrent jobs don't stack up and freeze the UI.
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QMessageBox as _QMB
        msg = _QMB(self)
        msg.setIcon(_QMB.Icon.Critical)
        msg.setWindowTitle("Generation Failed")
        msg.setText(item.error)
        msg.setStandardButtons(_QMB.StandardButton.Ok)
        msg.setModal(False)
        msg.setAttribute(_Qt.WidgetAttribute.WA_DeleteOnClose)
        msg.show()
        self.status_message.emit(f"Generation failed: {item.filename}")

    def _on_job_cancelled(self, item: JobItem) -> None:
        self._remove_job_row(item.id)
        self.status_message.emit(f"Cancelled: {item.filename}")

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
        self._build(item)

    def _build(self, item: JobItem) -> None:
        self.setStyleSheet("background: transparent;")
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

        self._name_lbl = QLabel(item.filename)
        self._name_lbl.setStyleSheet(
            "font-size: 12px; font-weight: 600; color: #F2F2F4; background: transparent;"
        )
        self._name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
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

        self._voice_lbl = QLabel(item.voice_display)
        self._voice_lbl.setStyleSheet(
            "font-size: 10px; color: #5A5A60; background: transparent;"
        )
        self._voice_lbl.setFixedWidth(100)
        bot.addWidget(self._voice_lbl)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(4)
        self._progress_bar.hide()
        bot.addWidget(self._progress_bar, 1)

        self._pct_lbl = QLabel("")
        self._pct_lbl.setStyleSheet(
            "font-size: 10px; font-weight: 700; color: #1DB954; "
            "background: transparent; min-width: 28px;"
        )
        self._pct_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._pct_lbl.hide()
        bot.addWidget(self._pct_lbl)

        self._status_lbl = QLabel(item.status_text)
        self._status_lbl.setStyleSheet(
            "font-size: 10px; color: #5A5A60; background: transparent;"
        )
        bot.addWidget(self._status_lbl)

        root.addLayout(bot)

        # ── Line 3: real-time speed ─────────────────────────────────── #
        # Indented 123 px (18 left margin + 100 voice label + 5 spacing)
        # so it sits directly below the progress bar.
        # Hidden until the first speed measurement arrives.
        spd_row = QHBoxLayout()
        spd_row.setContentsMargins(123, 0, 0, 0)
        spd_row.setSpacing(0)

        self._speed_lbl = QLabel("")
        self._speed_lbl.setStyleSheet(
            "font-size: 11px; font-weight: 700; color: #1DB954;"
            " background: transparent;"
        )
        self._speed_lbl.hide()
        spd_row.addWidget(self._speed_lbl)
        spd_row.addStretch()
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


def _card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    return f


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
