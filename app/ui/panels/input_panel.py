"""
Input Panel — full-height text editor on the left side.

Chrome is minimal so the text editor itself dominates the view.
A bottom action bar holds secondary controls (Open, Clear, word count).
Drag-and-drop a .txt/.md file onto the editor to import it.
"""

import logging
import unicodedata
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QFontMetrics,
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

# Text files larger than this are almost certainly not prose (a log, a binary
# renamed .txt).  ~12+ hours of narration is well under it.
_MAX_IMPORT_BYTES = 50 * 1024 * 1024

# Editor changes are coalesced before the heavy work (word count, text
# profiling for the voice check) runs.  Both scan the whole document, which on
# a 150k-character audiobook made every keystroke lag on a slow machine.
_TEXT_SETTLE_MS = 250


def decode_text_file(data: bytes) -> str | None:
    """
    Decode an imported text file the way the user's editor saved it.

    Windows Notepad saves UTF-16 ("Unicode") and, in older versions, the ANSI
    code page; decoding those as UTF-8 with replacement turned whole books
    into garbage.  A BOM is authoritative.  Without one, NUL bytes decide:
    real text never contains them, so NULs mean UTF-16 — the half of each
    byte pair they sit in tells little- from big-endian — or, scattered
    evenly, a binary file (returns None).  Otherwise strict UTF-8, then the
    common Windows code page as the last resort.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")

    sample = data[:8192]
    if b"\x00" in sample:
        odd_zeros = sample[1::2].count(0)
        even_zeros = sample[0::2].count(0)
        total = odd_zeros + even_zeros
        encoding = None
        if odd_zeros >= 0.9 * total:
            encoding = "utf-16-le"
        elif even_zeros >= 0.9 * total:
            encoding = "utf-16-be"
        if encoding is None:
            return None   # NULs in both halves: not a text file
        text = data.decode(encoding, errors="replace")
        return text if _looks_like_text(text) else None

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _looks_like_text(text: str) -> bool:
    """Few control / private-use / unassigned characters — true of prose in any script."""
    sample = text[:4096]
    if not sample:
        return True
    odd = sum(
        1 for ch in sample
        if ch not in "\t\n\r\f" and unicodedata.category(ch) in ("Cc", "Co", "Cn", "Cs")
    )
    return odd <= len(sample) * 0.02


class InputPanel(QWidget):
    """
    Full-height text input with drag-and-drop file import.

    Signals
    -------
    text_changed(str)   Fires on every keystroke / file load.
    """

    text_changed = Signal(str)
    #: Fires immediately on every edit with whether there is any text at all —
    #: cheap, so the Generate button never lags behind the editor.
    has_text_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(_TEXT_SETTLE_MS)
        self._settle_timer.timeout.connect(self._emit_settled_text)
        self._had_text = False
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
        # A programmatic load is a single change — no need to wait for typing
        # to settle before the rest of the UI catches up.
        self.flush()

    def flush(self) -> None:
        """Deliver any pending text change now (e.g. right before Generate)."""
        if self._settle_timer.isActive():
            self._settle_timer.stop()
            self._emit_settled_text()

    def open_file(self) -> None:
        """Show the Open File dialog (File ▸ Open…, Ctrl+O)."""
        self._open_file_dialog()

    def clear(self) -> None:
        # Select-all + delete rather than QTextEdit.clear(): clear() wipes the
        # undo stack, so an accidental click on Clear lost the whole book.
        cursor = self._editor.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()

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

        self._import_btn = QPushButton("Open File…")
        self._import_btn.setObjectName("ghostButton")
        self._import_btn.setToolTip("Import a text file (.txt or .md)  —  Ctrl+O")
        tbl.addWidget(self._import_btn)

        sep = QFrame()
        sep.setObjectName("toolbarSep")
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedWidth(1)
        tbl.addWidget(sep)

        self._clear_btn = QPushButton("Clear Text")
        self._clear_btn.setObjectName("quietGhostButton")
        self._clear_btn.setToolTip("Remove all text from the editor (Undo with Ctrl+Z)")
        self._clear_btn.setEnabled(False)
        tbl.addWidget(self._clear_btn)

        root.addWidget(top_bar)

        # ── Editor ──────────────────────────────────────────────────── #
        self._editor = _DropAwareTextEdit(self)
        self._editor.setPlaceholderText(
            "Paste or type your text here.\n\n"
            "You can also click Open File… above, or drag a .txt file onto this area."
        )
        self._editor.setAcceptRichText(False)
        self._editor.setAccessibleName("Text to convert")

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
        has_text = not self._editor.document().isEmpty()
        if has_text != self._had_text:
            self._had_text = has_text
            self._clear_btn.setEnabled(has_text)
            self.has_text_changed.emit(has_text)
        self._settle_timer.start()

    def _emit_settled_text(self) -> None:
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
        from PySide6.QtWidgets import QMessageBox
        name = Path(path).name
        try:
            size = Path(path).stat().st_size
            if size > _MAX_IMPORT_BYTES:
                QMessageBox.warning(
                    self, "File Too Large",
                    f"“{name}” is {size / 1_048_576:.0f} MB, which is too large "
                    "to be a text document.\n\nPlease choose a plain-text file "
                    "(.txt or .md).",
                )
                return
            data = Path(path).read_bytes()
        except Exception as exc:
            logger.error("Failed to read %s: %s", path, exc)
            QMessageBox.warning(
                self, "Could Not Open File",
                f"SetupTTS couldn't read “{name}”.\n\n"
                "It may have been moved, or another program may be using it.",
            )
            return

        text = decode_text_file(data)
        if text is None:
            QMessageBox.warning(
                self, "Not a Text File",
                f"“{name}” doesn't look like a plain-text file.\n\n"
                "Word, PDF, and e-book files need to be saved as .txt first.",
            )
            return
        if not text.strip():
            QMessageBox.information(
                self, "Empty File", f"“{name}” doesn't contain any text.",
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
