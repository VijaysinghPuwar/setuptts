"""
Get Voices — download, remove and discover voices that run on this computer.

Lists the public Piper catalog (free offline neural voices).  Downloads run
in a background thread with progress and cancel; nothing blocks the window.
Also links to the OS settings page where more built-in voices are added.
"""

from __future__ import annotations

import logging
import subprocess
import sys

from PySide6.QtCore import QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.services import piper_tts
from app.services.piper_tts import CatalogVoice

logger = logging.getLogger(__name__)

_COL_VOICE, _COL_LANG, _COL_QUALITY, _COL_SIZE, _COL_ACTION = range(5)

# Threads still running when their dialog closed (a download stuck on a slow
# connection, a catalog fetch).  Destroying a running QThread aborts the
# process, so they are kept alive here until they finish.
_ORPHANS: list[QThread] = []


def _adopt(thread: QThread | None) -> None:
    if thread is not None and thread.isRunning() and thread not in _ORPHANS:
        thread.setParent(None)
        _ORPHANS.append(thread)
        thread.finished.connect(lambda t=thread: _ORPHANS.remove(t) if t in _ORPHANS else None)
_QUALITY_LABELS = {"x_low": "Basic", "low": "Good", "medium": "Better", "high": "Best"}


class _CatalogLoader(QThread):
    loaded = Signal(list)
    failed = Signal(str)

    def __init__(self, force: bool = False) -> None:
        super().__init__()
        self._force = force

    def run(self) -> None:
        try:
            self.loaded.emit(piper_tts.fetch_catalog(force_refresh=self._force))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Voice catalog failed to load", exc_info=True)
            self.failed.emit(_friendly(exc))


class _Downloader(QThread):
    progress = Signal(int, int)
    done = Signal(str)        # key
    failed = Signal(str, str)  # key, message
    cancelled = Signal(str)

    def __init__(self, voice: CatalogVoice) -> None:
        super().__init__()
        self.voice = voice
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            piper_tts.download_voice(
                self.voice,
                progress=lambda d, t: self.progress.emit(d, t),
                cancelled=lambda: self._cancel,
            )
            self.done.emit(self.voice.key)
        except piper_tts.DownloadCancelled:
            self.cancelled.emit(self.voice.key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Voice download failed: %s", self.voice.key, exc_info=True)
            self.failed.emit(self.voice.key, _friendly(exc))


def _friendly(exc: BaseException) -> str:
    import socket
    import urllib.error

    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError)):
        return "couldn't reach the download server — check your internet connection and try again."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "not enough free disk space."
    if isinstance(exc, PermissionError):
        return "SetupTTS can't write to its voice folder."
    return str(exc)


def _size_label(n: int) -> str:
    return f"{n / 1e6:.0f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB"


class VoiceManagerDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Get Voices")
        self.setMinimumSize(520, 420)
        self.resize(720, 560)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        #: True when voices were added or removed; the caller reloads its list.
        self.changed = False
        #: ShortName of the last voice downloaded (selected after closing).
        self.last_installed: str | None = None

        self._catalog: list[CatalogVoice] = []
        self._installed: set[str] = set()
        self._loader: _CatalogLoader | None = None
        self._downloader: _Downloader | None = None
        self._old_threads: list[QThread] = []

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(120)
        self._filter_timer.timeout.connect(self._populate)

        self._build_ui()
        self._refresh_installed()
        self._load_catalog()

    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        ly = QVBoxLayout(self)
        ly.setContentsMargins(20, 18, 20, 16)
        ly.setSpacing(8)

        title = QLabel("Free offline voices")
        title.setObjectName("dialogTitle")
        ly.addWidget(title)

        intro = QLabel(
            "These natural-sounding voices run on your computer — no internet, "
            "account or setup needed once downloaded. Each voice is a one-time "
            "download (most are 20–75 MB)."
        )
        intro.setWordWrap(True)
        intro.setObjectName("metaLabel")
        ly.addWidget(intro)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search voices or languages…")
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumWidth(120)
        row.addWidget(self._search, 2)
        self._lang = QComboBox()
        self._lang.addItem("All Languages", userData="")
        self._lang.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._lang.setMinimumContentsLength(10)
        self._lang.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        row.addWidget(self._lang, 1)
        self._installed_only = QCheckBox("Installed")
        row.addWidget(self._installed_only)
        ly.addLayout(row)

        self._table = QTableWidget(0, 5)
        self._table.setObjectName("voiceCatalog")
        self._table.setHorizontalHeaderLabels(["Voice", "Language", "Quality", "Size", ""])
        self._table.verticalHeader().hide()
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(_COL_VOICE, QHeaderView.Stretch)
        hdr.setSectionResizeMode(_COL_LANG, QHeaderView.Stretch)
        for col in (_COL_QUALITY, _COL_SIZE):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        # Cell widgets don't take part in ResizeToContents, so the action
        # column is sized for its widest label ("Downloading…").
        hdr.setSectionResizeMode(_COL_ACTION, QHeaderView.Fixed)
        fm = self.fontMetrics()
        hdr.resizeSection(_COL_ACTION, fm.horizontalAdvance("Downloading…") + 40)
        self._table.verticalHeader().setDefaultSectionSize(34)
        ly.addWidget(self._table, 1)

        self._status = QLabel("Loading the voice catalog…")
        self._status.setObjectName("metaLabel")
        self._status.setWordWrap(True)
        ly.addWidget(self._status)

        prog = QHBoxLayout()
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress.hide()
        prog.addWidget(self._progress, 1)
        self._cancel_btn = QPushButton("Cancel download")
        self._cancel_btn.setObjectName("ghostButton")
        self._cancel_btn.hide()
        prog.addWidget(self._cancel_btn)
        self._retry_btn = QPushButton("Retry")
        self._retry_btn.setObjectName("ghostButton")
        self._retry_btn.hide()
        prog.addWidget(self._retry_btn)
        ly.addLayout(prog)

        foot = QHBoxLayout()
        self._os_btn = QPushButton(
            "Add Windows voices…" if sys.platform == "win32" else "Add macOS voices…")
        self._os_btn.setObjectName("quietGhostButton")
        self._os_btn.setToolTip(
            "Open system settings to install more built-in voices; they appear "
            "under Built-in after you restart SetupTTS or reload voices."
        )
        self._os_btn.setVisible(sys.platform in ("win32", "darwin"))
        foot.addWidget(self._os_btn)
        foot.addStretch()
        close = QPushButton("Done")
        close.setDefault(True)
        foot.addWidget(close)
        ly.addLayout(foot)

        self._search.textChanged.connect(lambda: self._filter_timer.start())
        self._lang.currentIndexChanged.connect(lambda: self._populate())
        self._installed_only.toggled.connect(lambda: self._populate())
        self._cancel_btn.clicked.connect(self._cancel_download)
        self._retry_btn.clicked.connect(lambda: self._load_catalog(force=True))
        self._os_btn.clicked.connect(_open_os_voice_settings)
        close.clicked.connect(self.accept)

    # ------------------------------------------------------------------ #

    def _keep(self, thread: QThread | None) -> None:
        """Hold a reference until the thread exits (see OutputPanel)."""
        if thread is not None and thread.isRunning():
            self._old_threads.append(thread)
            thread.finished.connect(
                lambda t=thread: self._old_threads.remove(t) if t in self._old_threads else None)

    def _load_catalog(self, force: bool = False) -> None:
        if not piper_tts.piper_available():
            self._status.setText("Offline voices are not available in this build.")
            return
        self._retry_btn.hide()
        self._status.setText("Loading the voice catalog…")
        self._keep(self._loader)
        self._loader = _CatalogLoader(force)
        self._loader.loaded.connect(self._on_catalog)
        self._loader.failed.connect(self._on_catalog_failed)
        self._loader.start()

    def _on_catalog(self, catalog: list) -> None:
        self._catalog = catalog
        langs = sorted({(v.locale, v.language) for v in catalog}, key=lambda x: x[1])
        self._lang.blockSignals(True)
        current = self._lang.currentData()
        self._lang.clear()
        self._lang.addItem("All Languages", userData="")
        for locale, label in langs:
            self._lang.addItem(label, userData=locale)
        idx = self._lang.findData(current)
        self._lang.setCurrentIndex(max(0, idx))
        self._lang.blockSignals(False)
        self._populate()

    def _on_catalog_failed(self, message: str) -> None:
        self._retry_btn.show()
        self._populate()   # still show what is installed
        self._status.setText(f"Couldn't load the list of voices: {message}")

    def _refresh_installed(self) -> None:
        self._installed = piper_tts.installed_keys()

    def _populate(self) -> None:
        query = self._search.text().strip().lower()
        locale = self._lang.currentData() or ""
        installed_only = self._installed_only.isChecked()
        by_key = {v.key: v for v in self._catalog}
        # Installed voices missing from the catalog (offline, or retired).
        rows: list[CatalogVoice] = list(self._catalog)
        for key in sorted(self._installed - set(by_key)):
            lang = key.split("-")[0]
            rows.append(CatalogVoice(key=key, locale=lang.replace("_", "-"), language=lang,
                                     quality=key.rsplit("-", 1)[-1], size_bytes=0, files={}))
        if installed_only:
            rows = [v for v in rows if v.key in self._installed]
        if locale:
            rows = [v for v in rows if v.locale == locale]
        if query:
            rows = [v for v in rows if query in v.key.lower() or query in v.language.lower()
                    or query in v.display_name.lower()]
        # Installed first, then by language.
        rows.sort(key=lambda v: (v.key not in self._installed, v.language, v.display_name))

        self._table.setUpdatesEnabled(False)
        self._table.setRowCount(len(rows))
        busy_key = self._downloader.voice.key if self._downloader and self._downloader.isRunning() else None
        for r, v in enumerate(rows):
            name = v.display_name + (f" · {v.gender}" if v.gender else "")
            item = QTableWidgetItem(name)
            item.setToolTip(v.key)
            self._table.setItem(r, _COL_VOICE, item)
            lang_item = QTableWidgetItem(v.language)
            lang_item.setToolTip(v.language)
            self._table.setItem(r, _COL_LANG, lang_item)
            self._table.setItem(r, _COL_QUALITY,
                                QTableWidgetItem(_QUALITY_LABELS.get(v.quality, v.quality)))
            self._table.setItem(r, _COL_SIZE,
                                QTableWidgetItem(_size_label(v.size_bytes) if v.size_bytes else ""))
            self._table.setCellWidget(r, _COL_ACTION, self._action_button(v, busy_key))
        self._table.setUpdatesEnabled(True)

        if self._catalog or self._installed:
            self._status.setText(f"{len(rows)} voices shown · {len(self._installed)} installed")

    def _action_button(self, v: CatalogVoice, busy_key: str | None) -> QWidget:
        btn = QPushButton()
        btn.setObjectName("ghostButton")
        if v.key in self._installed:
            if piper_tts.is_bundled(v.key):
                btn.setText("Included")
                btn.setEnabled(False)
                btn.setToolTip("Comes with SetupTTS")
            else:
                btn.setText("Remove")
                btn.setObjectName("dangerGhostButton")
                btn.clicked.connect(lambda _=False, key=v.key: self._remove(key))
        elif v.key == busy_key:
            btn.setText("Downloading…")
            btn.setEnabled(False)
        else:
            btn.setText("Download")
            btn.setEnabled(bool(v.files) and busy_key is None)
            btn.clicked.connect(lambda _=False, voice=v: self._download(voice))
        return btn

    # ------------------------------------------------------------------ #

    def _download(self, voice: CatalogVoice) -> None:
        if self._downloader is not None and self._downloader.isRunning():
            return
        self._keep(self._downloader)
        self._downloader = _Downloader(voice)
        self._downloader.progress.connect(self._on_progress)
        self._downloader.done.connect(self._on_downloaded)
        self._downloader.failed.connect(self._on_download_failed)
        self._downloader.cancelled.connect(self._on_download_cancelled)
        self._progress.setValue(0)
        self._progress.setFormat(f"{voice.display_name}: %p%")
        self._progress.show()
        self._cancel_btn.show()
        self._downloader.start()
        self._populate()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self._progress.setValue(min(100, int(done * 100 / total)))

    def _finish_download_ui(self) -> None:
        self._progress.hide()
        self._cancel_btn.hide()

    def _on_downloaded(self, key: str) -> None:
        self._finish_download_ui()
        self.changed = True
        self.last_installed = piper_tts.PIPER_PREFIX + key
        self._refresh_installed()
        self._populate()
        self._status.setText(f"{piper_tts.display_name(key)} is ready — it's selected when you close this window.")

    def _on_download_failed(self, key: str, message: str) -> None:
        self._finish_download_ui()
        self._populate()
        self._status.setText(f"Download of {piper_tts.display_name(key)} failed: {message}")

    def _on_download_cancelled(self, key: str) -> None:
        self._finish_download_ui()
        self._populate()
        self._status.setText("Download cancelled.")

    def _cancel_download(self) -> None:
        if self._downloader is not None:
            self._downloader.cancel()

    def _remove(self, key: str) -> None:
        answer = QMessageBox.question(
            self, "Remove Voice",
            f"Remove the offline voice “{piper_tts.display_name(key)}”?\n\n"
            "You can download it again at any time.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        piper_tts.remove_voice(key)
        self.changed = True
        if self.last_installed == piper_tts.PIPER_PREFIX + key:
            self.last_installed = None
        self._refresh_installed()
        self._populate()

    # ------------------------------------------------------------------ #

    def done(self, result: int) -> None:  # noqa: D401 - Qt override
        if self._downloader is not None and self._downloader.isRunning():
            answer = QMessageBox.question(
                self, "Download in Progress",
                "A voice is still downloading. Stop the download and close?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._downloader.cancel()
            self._downloader.wait(2_000)
        for t in [self._loader, self._downloader, *self._old_threads]:
            if t is not None and t.isRunning():
                for sig in ("loaded", "failed", "progress", "done", "cancelled"):
                    try:
                        getattr(t, sig).disconnect()
                    except (AttributeError, RuntimeError, TypeError):
                        pass
                _adopt(t)
        super().done(result)


def _open_os_voice_settings() -> None:
    if sys.platform == "win32":
        QDesktopServices.openUrl(QUrl("ms-settings:speech"))
    elif sys.platform == "darwin":
        try:
            subprocess.Popen(["open", "x-apple.systempreferences:"
                              "com.apple.preference.universalaccess?SpokenContent"])
        except OSError:
            logger.warning("Could not open System Settings", exc_info=True)
