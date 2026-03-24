"""
Input Panel — full-height text editor on the left side.

Chrome is minimal so the text editor itself dominates the view.
A bottom action bar holds secondary controls (Open, Clear, word count).
Drag-and-drop a .txt/.md file onto the editor to import it.
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont, QColor, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


class InputPanel(QWidget):
    """
    Full-height text input with drag-and-drop file import.

    Signals
    -------
    text_changed(str)   Fires on every keystroke / file load.
    """

    text_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get_text(self) -> str:
        return self._editor.toPlainText().strip()

    def set_text(self, text: str) -> None:
        self._editor.setPlainText(text)
        self._editor.moveCursor(QTextCursor.MoveOperation.Start)
        self._editor.verticalScrollBar().setValue(0)

    def clear(self) -> None:
        self._editor.clear()

    # ------------------------------------------------------------------ #
    # UI                                                                   #
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top bar ─────────────────────────────────────────────────── #
        top_bar = QWidget()
        top_bar.setFixedHeight(36)
        top_bar.setStyleSheet(
            "background-color: #161618; border-bottom: 1px solid #2C2C30;"
        )
        tbl = QHBoxLayout(top_bar)
        tbl.setContentsMargins(16, 0, 12, 0)
        tbl.setSpacing(8)

        title = QLabel("TEXT INPUT")
        title.setObjectName("sectionLabel")
        tbl.addWidget(title)
        tbl.addStretch()

        self._import_btn = QPushButton("Open File")
        self._import_btn.setObjectName("ghostButton")
        self._import_btn.setToolTip("Import a text file (.txt or .md)")
        tbl.addWidget(self._import_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedWidth(1)
        sep.setStyleSheet("background: #2C2C30; border: none;")
        tbl.addWidget(sep)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setObjectName("ghostButton")
        self._clear_btn.setStyleSheet(
            "QPushButton { color: #5A5A60; } QPushButton:hover { color: #F2F2F4; }"
        )
        tbl.addWidget(self._clear_btn)

        root.addWidget(top_bar)

        # ── Editor ──────────────────────────────────────────────────── #
        self._editor = _DropAwareTextEdit(self)
        self._editor.setPlaceholderText(
            "Paste text here, or use Open File above, or drag and drop a .txt file…"
        )

        f = QFont()
        f.setPointSize(13)
        f.setStyleStrategy(QFont.PreferAntialias)
        self._editor.setFont(f)
        root.addWidget(self._editor, 1)

        # ── Bottom stats bar ─────────────────────────────────────────── #
        bottom_bar = QWidget()
        bottom_bar.setFixedHeight(24)
        bottom_bar.setStyleSheet(
            "background-color: #111113; border-top: 1px solid #1E1E22;"
        )
        bbl = QHBoxLayout(bottom_bar)
        bbl.setContentsMargins(16, 0, 16, 0)
        bbl.setSpacing(0)

        self._count_label = QLabel("0 words  ·  0 characters")
        self._count_label.setObjectName("wordCountLabel")
        bbl.addWidget(self._count_label)
        bbl.addStretch()

        self._drop_hint = QLabel("or drag & drop a .txt file")
        self._drop_hint.setObjectName("hintLabel")
        self._drop_hint.setStyleSheet("color: #3A3A40; font-size: 11px;")
        bbl.addWidget(self._drop_hint)

        root.addWidget(bottom_bar)

    # ------------------------------------------------------------------ #

    def _connect_signals(self) -> None:
        self._editor.textChanged.connect(self._on_text_changed)
        self._editor.file_dropped.connect(self._load_file)
        self._editor.drag_active.connect(self._on_drag_state)
        self._import_btn.clicked.connect(self._open_file_dialog)
        self._clear_btn.clicked.connect(self.clear)

    def _on_text_changed(self) -> None:
        text = self._editor.toPlainText()
        words = len(text.split()) if text.strip() else 0
        chars = len(text)
        self._count_label.setText(f"{words:,} words  ·  {chars:,} characters")
        self.text_changed.emit(text)

    def _on_drag_state(self, active: bool) -> None:
        """Dim the stats bar when a drop is in progress."""
        if active:
            self._editor.setStyleSheet(
                "QTextEdit { background-color: #0D1520; border: 2px solid #0A84FF; }"
            )
        else:
            self._editor.setStyleSheet("")

    def _open_file_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Text File",
            "",
            "Text Files (*.txt *.md);;All Files (*)",
        )
        if path:
            self._load_file(path)

    def _load_file(self, path: str) -> None:
        # ── 1. Read the file (I/O errors reported to user) ──────────── #
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.error("Failed to read %s: %s", path, exc)
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "Could Not Open File",
                f"The file could not be read.\n\n{exc}",
            )
            return

        # ── 2. Update the editor (bugs here are code errors, not I/O) ─ #
        self.set_text(text)
        logger.info("Loaded: %s", path)


# ------------------------------------------------------------------ #
# Drop-aware text edit                                                #
# ------------------------------------------------------------------ #

class _DropAwareTextEdit(QTextEdit):
    """QTextEdit that emits signals for .txt/.md file drops."""

    file_dropped = Signal(str)
    drag_active  = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls()]
            if any(p.lower().endswith((".txt", ".md")) for p in paths):
                event.acceptProposedAction()
                self.drag_active.emit(True)
                return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        self.drag_active.emit(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self.drag_active.emit(False)
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith((".txt", ".md")):
                self.file_dropped.emit(path)
                event.acceptProposedAction()
                return
        super().dropEvent(event)
