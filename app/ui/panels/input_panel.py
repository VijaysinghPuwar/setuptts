"""
Input Panel — full-height text editor on the left side.

Chrome is minimal so the text editor itself dominates the view.
A bottom action bar holds secondary controls (Open, Clear, word count).
Drag-and-drop a .txt/.md file onto the editor to import it.
"""

import locale
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
    # UTF-32 LE's BOM begins with UTF-16 LE's, so it must be checked first.
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return data.decode("utf-32", errors="replace")
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
        if encoding is not None:
            text = data.decode(encoding, errors="replace")
            if _looks_like_text(text):
                return text
        # CJK code units such as U+4E00 contain a zero byte too, so UTF-16
        # Chinese/Japanese without a BOM puts NULs in either or both halves
        # and the byte-order guess above can be wrong.
        for candidate in ("utf-16-le", "utf-16-be"):
            text = _strict(data, candidate)
            if _plausible_cjk(text):
                return text
        return None   # not a text file

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    return _decode_legacy(data)


# Legacy (pre-Unicode) encodings Windows editors still save in.  Each is only
# accepted when the result is plausible for its script, because nearly any
# byte string "decodes" in some code page.  Strict CJK decoders (they reject
# most foreign byte patterns) go before BOM-less UTF-16, lenient ones after:
# GB18030 decodes almost anything, and UTF-16 "decodes" Shift-JIS bytes.
_CYRILLIC = ("cp1251",)
_CJK_STRICT = ("cp932", "euc_kr")
_CJK_LENIENT = ("gb18030", "big5")


def _decode_legacy(data: bytes) -> str:
    """
    Decode a non-UTF-8 file.  Always assuming cp1252 turned Russian (cp1251)
    files into "Ãëàâà…" and Japanese Shift-JIS files into replacement
    characters, which were then read aloud as gibberish.
    """
    system = (locale.getpreferredencoding(False) or "").lower().replace("-", "")
    if system and system not in {"cp1252", "utf8", "ascii", "ansi_x3.41968"}:
        text = _strict(data, system)
        if text is not None and _looks_like_text(text):
            return text

    # Order matters: the most selective decoders go first.  cp932 / EUC-KR
    # reject most foreign byte patterns; cp1251 and cp1252 accept almost any
    # byte, so they come after (Shift-JIS read as cp1251 is "88 % Cyrillic").
    for encoding in _CJK_STRICT:
        text = _strict(data, encoding)
        # Modern Korean is almost all Hangul; Chinese GBK read as EUC-KR
        # comes out as a Hangul/Hanja mix.
        if _plausible_cjk(text) and (encoding != "euc_kr" or _letter_share(text, _is_hangul) >= 0.9):
            return text
    # Pure CJK text in UTF-16 has no ASCII, hence no NUL bytes at all.
    text = _bomless_utf16_cjk(data)
    if text is not None:
        return text

    western = _strict(data, "cp1252")
    if western is not None and _looks_like_text(western) and _plausible_western(western):
        return western
    for encoding in _CYRILLIC:
        text = _strict(data, encoding)
        # Measured against *all* letters: one "й" among ASCII letters is
        # what cp1251 makes of "é", not evidence of Russian.
        if (text is not None and _looks_like_text(text)
                and _letter_share(text, _is_cyrillic) >= 0.8 and _has_word_spaces(text)):
            return text
    for encoding in _CJK_LENIENT:
        text = _strict(data, encoding)
        if _plausible_cjk(text):
            return text
    if western is not None:
        return western
    # Central European (Czech, Polish, Hungarian…) uses bytes cp1252 leaves
    # undefined, so it lands here.
    central = _strict(data, "cp1250")
    if central is not None and _plausible_western(central):
        return central
    return data.decode("utf-8", errors="replace")


def _strict(data: bytes, encoding: str) -> str | None:
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def _plausible_western(text: str) -> bool:
    """Accented letters are a small minority in every Western language.

    Russian cp1251 read as cp1252 is ~100 % accented letters ("Ïðèâåò"); real
    Western text stays well under half even in short, accent-dense words such
    as "été" or "Hyvää päivää".
    """
    letters = [ch for ch in text[:20_000] if ch.isalpha()]
    if not letters:
        return True
    non_ascii = sum(1 for ch in letters if ord(ch) > 0x7F)
    limit = 0.3 if len(letters) >= 200 else 0.7
    return non_ascii <= limit * len(letters)


def _letter_share(text: str, predicate) -> float:
    letters = [ch for ch in text[:20_000] if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if predicate(ch)) / len(letters)


def _bomless_utf16_cjk(data: bytes) -> str | None:
    """
    UTF-16 Chinese/Japanese/Korean saved without a byte-order mark.

    Ordinary 8-bit text also "decodes" as UTF-16 into CJK-looking code units
    ("He" -> U+6548), but then both bytes of every unit are printable ASCII;
    real CJK code units have a low byte spread over the whole 0-255 range.
    """
    if len(data) < 20 or len(data) % 2:
        return None
    # CJK code units put a high (>= 0x80) byte in most units; Western 8-bit
    # text only has the odd accented letter ("café" is 1 byte in 4).
    head = data[:8192]
    if sum(1 for b in head if b >= 0x80) < 0.15 * len(head):
        return None
    for encoding, low_bytes in (("utf-16-le", data[0::2]), ("utf-16-be", data[1::2])):
        sample = low_bytes[:4096]
        unusual = sum(1 for b in sample if b < 0x20 or b > 0x7E)
        if unusual < 0.25 * len(sample):
            continue
        text = _strict(data, encoding)
        if _plausible_cjk(text):
            return text
    return None


def _plausible_cjk(text: str | None) -> bool:
    """Is this decode real Chinese/Japanese/Korean prose?

    Wrong decodes also produce ideographs ("oãn" -> "o縅", Russian read as
    GB18030), so require several CJK characters that dominate the letters,
    and few ASCII spaces — CJK prose does not separate words with spaces,
    while text in another script decoded this way keeps its spaces.
    """
    if text is None or not _looks_like_text(text):
        return False
    sample = text[:20_000]
    letters = [ch for ch in sample if ch.isalpha()]
    cjk = sum(1 for ch in letters if _is_cjk(ch))
    if cjk < 3 or cjk < 0.6 * len(letters) or not _mostly_in(text, _is_cjk):
        return False
    is_korean = sum(1 for ch in letters if _is_hangul(ch)) >= 0.5 * len(letters)
    return is_korean or sample.count(" ") <= 0.08 * len(sample)


def _has_word_spaces(text: str) -> bool:
    """Alphabetic prose (Russian included) separates words with spaces."""
    sample = text[:20_000]
    letters = sum(1 for ch in sample if ch.isalpha())
    return letters < 12 or sample.count(" ") >= 0.05 * len(sample)


def _mostly_in(text: str, predicate) -> bool:
    non_ascii = [ch for ch in text[:20_000] if ord(ch) > 0x7F and not ch.isspace()]
    if not non_ascii:
        return False
    return sum(1 for ch in non_ascii if predicate(ch)) >= 0.95 * len(non_ascii)


def _is_cyrillic(ch: str) -> bool:
    return 0x0400 <= ord(ch) <= 0x04FF or ch in "«»—–…№“”„‘’•"


def _is_hangul(ch: str) -> bool:
    return 0xAC00 <= ord(ch) <= 0xD7AF or 0x1100 <= ord(ch) <= 0x11FF


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3000 <= cp <= 0x30FF          # CJK punctuation, hiragana, katakana
        or 0x3400 <= cp <= 0x9FFF       # CJK ideographs
        or 0xAC00 <= cp <= 0xD7AF       # Hangul syllables
        or 0x1100 <= cp <= 0x11FF       # Hangul jamo
        or 0xFF01 <= cp <= 0xFF5E       # full-width ASCII forms
        or 0xF900 <= cp <= 0xFAFF       # CJK compatibility ideographs
        or ch in "“”‘’…—・·"
    )


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
