"""
History Panel — compact collapsible strip at the bottom.

Shows the last N conversion jobs. Click to open; right-click for more.
Stays visually quiet so it doesn't compete with the main content.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
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
from app.services.history_service import HistoryService

logger = logging.getLogger(__name__)


class HistoryPanel(QWidget):
    """Compact recent-conversions table with open/delete actions."""

    def __init__(self, history: HistoryService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._history = history
        self._jobs: list[Job] = []
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------ #

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
        header.setFixedHeight(36)
        header.setStyleSheet(
            "background-color: #111113; border-top: 1px solid #2C2C30;"
        )
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 0, 12, 0)
        hl.setSpacing(8)

        title = QLabel("RECENT CONVERSIONS")
        title.setObjectName("sectionLabel")
        hl.addWidget(title)
        hl.addStretch()

        self._clear_btn = QPushButton("Clear All")
        self._clear_btn.setObjectName("ghostButton")
        self._clear_btn.setStyleSheet(
            "QPushButton { color: #FF453A; font-size: 11px; padding: 2px 6px; }"
            "QPushButton:hover { color: #FF6B61; }"
        )
        hl.addWidget(self._clear_btn)
        root.addWidget(header)

        # ── Table ──────────────────────────────────────────────────── #
        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(["When", "Text Preview", "Voice", "Took", "File"])
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self._table.setColumnWidth(0, 84)
        self._table.setColumnWidth(2, 120)
        self._table.setColumnWidth(3, 78)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.setStyleSheet(
            "QTableWidget { background: #0D0D0F; border: none; }"
            "QTableWidget::item { border-bottom: 1px solid #1A1A1E; }"
            "QHeaderView::section { background: #111113; }"
        )
        root.addWidget(self._table)

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
            self._table.setSpan(0, 0, 1, 5)
            self._table.setRowHeight(0, 36)
            return

        for job in self._jobs:
            row = self._table.rowCount()
            self._table.insertRow(row)

            parts   = job.voice.split("-")
            persona = parts[-1].replace("Neural", "") if parts else job.voice
            took    = _fmt_took(job.duration_seconds, job.status)

            self._table.setItem(row, 0, _cell(job.created_at_display))
            self._table.setItem(row, 1, _cell(job.text_preview))
            self._table.setItem(row, 2, _cell(persona))
            self._table.setItem(row, 3, _cell(took))
            self._table.setItem(row, 4, _cell(job.output_filename))

            if job.status != JobStatus.COMPLETED:
                for col in range(5):
                    item = self._table.item(row, col)
                    if item:
                        item.setForeground(Qt.darkGray)

            self._table.setRowHeight(row, 32)

    def _open_selected(self) -> None:
        row = self._table.currentRow()
        if 0 <= row < len(self._jobs):
            _open_path(self._jobs[row].output_path)

    def _show_menu(self, pos) -> None:
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._jobs):
            return
        job = self._jobs[row]
        menu = QMenu(self)
        open_a   = menu.addAction("Open File")
        folder_a = menu.addAction("Show in Folder")
        menu.addSeparator()
        del_a    = menu.addAction("Remove from History")

        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == open_a:
            _open_path(job.output_path)
        elif action == folder_a:
            _open_path(str(Path(job.output_path).parent))
        elif action == del_a:
            if job.id is not None:
                self._history.delete_job(job.id)
            self._jobs.pop(row)
            self._populate()

    def _clear_all(self) -> None:
        from PySide6.QtWidgets import QMessageBox
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


def _cell(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


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
