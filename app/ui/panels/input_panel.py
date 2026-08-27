"""
Input Panel — full-height text editor on the left side.

Chrome is minimal so the text editor itself dominates the view.
A bottom action bar holds secondary controls (Open, Clear, word count).
Drag-and-drop a .txt/.md file onto the editor to import it.
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QFontMetrics,
    QColor,
    QPalette,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QSizePolicy,
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
        top_bar.setObjectName("editorTopBar")
        top_bar.setMinimumHeight(36)
        top_bar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
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
        sep.setObjectName("toolbarSep")
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedWidth(1)
        tbl.addWidget(sep)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setObjectName("quietGhostButton")
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
        bottom_bar.setObjectName("editorBottomBar")
        bottom_bar.setMinimumHeight(24)
        bottom_bar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._bottom_bar = bottom_bar
        bbl = QHBoxLayout(bottom_bar)
        bbl.setContentsMargins(16, 0, 16, 0)
        bbl.setSpacing(0)

        self._count_label = QLabel("0 words  ·  0 characters")
        self._count_label.setObjectName("wordCountLabel")
        bbl.addWidget(self._count_label)
        bbl.addStretch()

        self._drop_hint = QLabel("or drag & drop a .txt file")
        self._drop_hint.setObjectName("dropHint")
        bbl.addWidget(self._drop_hint)

        root.addWidget(bottom_bar)

    # ------------------------------------------------------------------ #

    def _connect_signals(self) -> None:
        self._editor.textChanged.connect(self._on_text_changed)
        self._editor.file_dropped.connect(self._load_file)
        self._editor.drag_active.connect(self._on_drag_state)
        self._import_btn.clicked.connect(self._open_file_dialog)
        self._clear_btn.clicked.connect(self.clear)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_stats_bar()

    def _on_text_changed(self) -> None:
        self._update_stats_bar()
        self.text_changed.emit(self._editor.toPlainText())

    def _update_stats_bar(self) -> None:
        """
        Fit the word count and the drop hint to the width actually available.

        Qt clips a QLabel rather than eliding it, and the default UI font is
        materially wider on Windows than on macOS, so a bar that comfortably
        holds both on one platform cuts both in half on the other.  The count
        drops to an abbreviated form when the long one will not fit, and the
        drop hint — the more expendable of the two, and duplicated by the
        editor's own placeholder — gives way before the count does.
        """
        text  = self._editor.toPlainText()
        words = len(text.split()) if text.strip() else 0
        chars = len(text)

        # Bar width less the layout's 16 px margins either side.
        available = max(0, self._bottom_bar.width() - 32)
        metrics   = QFontMetrics(self._count_label.font())

        long_form  = f"{words:,} words  ·  {chars:,} characters"
        short_form = f"{words:,}w  ·  {chars:,}c"
        count = long_form if metrics.horizontalAdvance(long_form) <= available \
            else short_form
        self._count_label.setText(count)
        self._count_label.setToolTip(long_form)

        hint_width = QFontMetrics(self._drop_hint.font()).horizontalAdvance(
            self._drop_hint.text()
        )
        self._drop_hint.setVisible(
            metrics.horizontalAdvance(count) + hint_width + 24 <= available
        )

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
