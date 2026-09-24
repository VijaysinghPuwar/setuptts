"""
History Panel — compact collapsible strip at the bottom.

Shows the last N conversion jobs. Click to open; right-click for more.
Stays visually quiet so it doesn't compete with the main content.
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QMessageBox,
    QSizePolicy,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.models.job import Job, JobStatus
from app.models.voice import persona_name
from app.services.history_service import HistoryService
from app.utils.paths import open_in_file_manager

logger = logging.getLogger(__name__)

_COLUMNS = ["When", "Text Preview", "Voice", "Took", "Length", "File"]
(_COL_WHEN, _COL_PREVIEW, _COL_VOICE,
 _COL_TOOK, _COL_LENGTH, _COL_FILE) = range(len(_COLUMNS))


class HistoryPanel(QWidget):
    """Compact recent-conversions table with open/delete actions."""

    def __init__(self, history: HistoryService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._history = history
        self._jobs: list[Job] = []
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------ #

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        # Only now is the header polished and its real height known.  Pinning
        # it as a hard minimum stops the layout shaving the header — and
        # clipping its Clear All button — when the strip is squeezed; the
        # table below absorbs the loss instead, which is what scrolls.
        self._header.setMinimumHeight(
            max(36, self._header.sizeHint().height())
        )

    def minimumSizeHint(self):  # type: ignore[override]
        """
        Report the height the strip genuinely needs: its header plus one row.

        The default hint is derived from the table, whose own minimum is tiny,
        so the strip could be given less height than its header alone needs
        and the header's Clear All button was clipped.  MainWindow uses this
        as the floor when sizing the split.
        """
        hint = super().minimumSizeHint()
        hint.setHeight(max(hint.height(), self._header.sizeHint().height() + 36))
        return hint

    def refresh(self) -> None:
        try:
            self._jobs = self._history.get_jobs(limit=50)
        except Exception:
            logger.warning("Could not load history", exc_info=True)
            self._jobs = []
        self._populate()

    def add_job(self, job: Job) -> None:
        self._jobs.insert(0, job)
        if len(self._jobs) > 50:
            self._jobs.pop()
        self._populate()

    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ─────────────────────────────────────────────────── #
        header = QWidget()
        header.setObjectName("historyHeader")
        header.setMinimumHeight(36)
        # Fixed, not the default Preferred: the table below is what should
        # absorb the strip's spare height, while the header keeps exactly the
        # height its own content needs — under a larger system font the
        # default let it sit at its 36 px minimum and clip its button.
        header.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._header = header
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 0, 12, 0)
        hl.setSpacing(8)

        title = QLabel("RECENT CONVERSIONS")
        title.setObjectName("sectionLabel")
        hl.addWidget(title)
        hl.addStretch()

        self._clear_btn = QPushButton("Clear All")
        self._clear_btn.setObjectName("dangerGhostButton")
        hl.addWidget(self._clear_btn)
        root.addWidget(header)

        # ── Table ──────────────────────────────────────────────────── #
        self._table = QTableWidget()
        self._table.setColumnCount(len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        header_view = self._table.horizontalHeader()
        # The short columns size to their content — fixed widths clipped
        # "25m 47s" and "3:05:48" under the Windows font.  Preview and File
        # share whatever is left.
        for col in (_COL_WHEN, _COL_VOICE, _COL_TOOK, _COL_LENGTH):
            header_view.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(_COL_PREVIEW, QHeaderView.Stretch)
        header_view.setSectionResizeMode(_COL_FILE, QHeaderView.Stretch)
        header_view.setMinimumSectionSize(56)
        self._table.horizontalHeaderItem(_COL_TOOK).setToolTip("How long generation took")
        self._table.horizontalHeaderItem(_COL_LENGTH).setToolTip("Length of the audio")
        self._table.setToolTip("Double-click to play · right-click for more")
        self._table.setAccessibleName("Recent conversions")
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.setObjectName("historyTable")
        # The table's own minimum height competes with the header's when the
        # strip is squeezed, and Qt then shaves the header instead — clipping
        # the Clear All button.  The table is the part that should give way:
        # it scrolls, the header does not.
        self._table.setMinimumHeight(0)
        root.addWidget(self._table, 1)

        self._table.doubleClicked.connect(self._open_selected)
        self._table.customContextMenuRequested.connect(self._show_menu)
        self._clear_btn.clicked.connect(self._clear_all)

    # ------------------------------------------------------------------ #

    def _populate(self) -> None:
        self._table.setRowCount(0)

        if not self._jobs:
            self._table.setRowCount(1)
            ph = QTableWidgetItem("No conversions yet — generate your first audio above")
            ph.setTextAlignment(Qt.AlignCenter)
            ph.setForeground(Qt.darkGray)
            ph.setFlags(ph.flags() & ~Qt.ItemIsSelectable)
            self._table.setItem(0, 0, ph)
            self._table.setSpan(0, 0, 1, len(_COLUMNS))
            self._table.setRowHeight(0, 36)
            return

        for job in self._jobs:
            row = self._table.rowCount()
            self._table.insertRow(row)

            when = _cell(job.created_at_display)
            when.setToolTip(job.created_at.strftime("%Y-%m-%d %H:%M"))
            preview = _cell(job.text_preview)
            preview.setToolTip(job.text_preview)
            voice = _cell(persona_name(job.voice))
            voice.setToolTip(f"{job.voice}  ·  speed {job.rate}")
            file_cell = _cell(job.output_filename)
            file_cell.setToolTip(job.output_path)

            self._table.setItem(row, _COL_WHEN, when)
            self._table.setItem(row, _COL_PREVIEW, preview)
            self._table.setItem(row, _COL_VOICE, voice)
            self._table.setItem(row, _COL_TOOK, _cell(_fmt_took(job.duration_seconds, job.status)))
            self._table.setItem(row, _COL_LENGTH, _cell(_fmt_length(job.audio_seconds)))
            self._table.setItem(row, _COL_FILE, file_cell)

            if job.status != JobStatus.COMPLETED:
                for col in range(len(_COLUMNS)):
                    item = self._table.item(row, col)
                    if item:
                        item.setForeground(Qt.darkGray)

            self._table.setRowHeight(row, 32)

    def _open_selected(self) -> None:
        row = self._table.currentRow()
        if 0 <= row < len(self._jobs):
            self._open_job_file(self._jobs[row])

    def _open_job_file(self, job: Job, *, reveal: bool = False) -> None:
        target = Path(job.output_path)
        if reveal and not target.exists() and target.parent.exists():
            open_in_file_manager(target.parent)
            return
        if not open_in_file_manager(target, reveal=reveal):
            QMessageBox.information(
                self, "File Not Found",
                f"“{target.name}” is no longer at\n\n{target.parent}\n\n"
                "It may have been moved, renamed, or deleted.",
            )

    def _show_menu(self, pos) -> None:
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._jobs):
            return
        job = self._jobs[row]
        menu = QMenu(self)
        open_a   = menu.addAction("Play / Open File")
        folder_a = menu.addAction("Show in Folder")
        copy_a   = menu.addAction("Copy File Path")
        menu.addSeparator()
        del_a    = menu.addAction("Remove from History")

        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == open_a:
            self._open_job_file(job)
        elif action == folder_a:
            self._open_job_file(job, reveal=True)
        elif action == copy_a:
            QApplication.clipboard().setText(job.output_path)
        elif action == del_a:
            if job.id is not None:
                self._history.delete_job(job.id)
            self._jobs.pop(row)
            self._populate()

    def _clear_all(self) -> None:
        if not self._jobs:
            return
        if QMessageBox.question(
            self, "Clear History",
            "Remove all recent conversions?\n\nAudio files will not be deleted.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) == QMessageBox.Yes:
            self._history.clear_history()
            self._jobs.clear()
            self._populate()


# ── Helpers ───────────────────────────────────────────────────────── #

def _fmt_took(secs: float, status: JobStatus) -> str:
    """Format generation time as a compact human string.

    12s  |  1m 24s  |  8m 12s  |  2h 14m
    Failed / Cancelled → status label instead of fake time.
    """
    if status == JobStatus.FAILED:
        return "Failed"
    if status == JobStatus.CANCELLED:
        return "Cancelled"
    if secs <= 0:
        return "—"
    total_s = int(secs)
    if total_s < 60:
        return f"{total_s}s"
    m = total_s // 60
    s = total_s % 60
    if m < 60:
        return f"{m}m {s:02d}s"
    h = m // 60
    m = m % 60
    return f"{h}h {m:02d}m"


def _fmt_length(secs: float | None) -> str:
    """Audio length: 4:05 · 1:02:33 — or a dash for jobs recorded before 1.6."""
    if not secs or secs <= 0:
        return "—"
    total = int(round(secs))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _cell(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


