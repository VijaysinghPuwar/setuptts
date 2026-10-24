"""
Get Voices — download, remove and discover voices that run on this computer.

Lists the offline voices: Kokoro HD voices (built-in list; one shared model
plus a small file per voice) and the public Piper catalog.  Downloads run in a
background thread with progress and cancel; nothing blocks the window.
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

from app.services import kokoro_tts, piper_tts
from app.services.kokoro_tts import KokoroCatalogVoice
from app.services.piper_tts import CatalogVoice
from app.workers.thread_shutdown import stop_threads

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


def stop_orphans(budget_s: float = 3.0) -> None:
    """At app exit: cancel orphaned threads and wait briefly for them.

    _ORPHANS alone was not enough — it is cleared with the module during
    interpreter teardown, destroying a thread still running a download.
    Any that outlive the wait are kept alive by stop_threads until exit.
    """
    running = [t for t in _ORPHANS if t.isRunning()]
    if running:
        stop_threads([(type(t).__name__, t) for t in running],
                     budget_s=budget_s, min_wait_s=0.5)


_QUALITY_LABELS = {"x_low": "Basic", "low": "Good", "medium": "Better", "high": "Best"}

_ENGINES = (("", "All voices"), ("kokoro", "Kokoro HD"), ("piper", "Piper"))


def _engine(v) -> str:
    return getattr(v, "engine", "piper")


def _short_name(v) -> str:
    """The row's identity: engines' keys could otherwise collide."""
    if _engine(v) == "kokoro":
        return v.short_name
    return piper_tts.PIPER_PREFIX + v.key


def _name_of(short_name: str) -> str:
    if kokoro_tts.is_kokoro_voice(short_name):
        return kokoro_tts.display_name(short_name[len(kokoro_tts.KOKORO_PREFIX):])
    return piper_tts.display_name(short_name[len(piper_tts.PIPER_PREFIX):])


def _installed_short_names() -> set[str]:
    return ({piper_tts.PIPER_PREFIX + k for k in piper_tts.installed_keys()}
            | {kokoro_tts.KOKORO_PREFIX + k for k in kokoro_tts.installed_keys()})


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
    done = Signal(str)        # ShortName
    failed = Signal(str, str)  # ShortName, message
    cancelled = Signal(str)

    def __init__(self, voice: CatalogVoice | KokoroCatalogVoice) -> None:
        super().__init__()
        self.voice = voice
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        engine = kokoro_tts if _engine(self.voice) == "kokoro" else piper_tts
        name = _short_name(self.voice)
        try:
            engine.download_voice(
                self.voice,
                progress=lambda d, t: self.progress.emit(d, t),
                cancelled=lambda: self._cancel,
            )
            self.done.emit(name)
        except (piper_tts.DownloadCancelled, kokoro_tts.DownloadCancelled):
            self.cancelled.emit(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Voice download failed: %s", name, exc_info=True)
            self.failed.emit(name, _friendly(exc))


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
        self._kokoro: list[KokoroCatalogVoice] = (
            kokoro_tts.catalog() if kokoro_tts.kokoro_available() else [])
        self._installed: set[str] = set()
        self._loader: _CatalogLoader | None = None
        self._downloader: _Downloader | None = None
        self._dl_name = ""
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
            "Natural-sounding voices that run on your computer — no internet or "
            "account needed once downloaded.<br>"
            "<b>Kokoro HD</b> — the most lifelike. The first one brings the shared "
            "325 MB model; each one after is under 1 MB.<br>"
            "<b>Piper</b> — lighter, in more languages. Most are 20–75 MB."
        )
        intro.setTextFormat(Qt.RichText)
        intro.setToolTip(
            "Kokoro's Japanese and Chinese voices are not offered: they need "
            "pronunciation data that SetupTTS does not include, and without it "
            "they read most words wrongly.")
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
        self._engine_combo = QComboBox()
        for key, label in _ENGINES:
            self._engine_combo.addItem(label, userData=key)
        self._engine_combo.setToolTip("Show Kokoro HD voices, Piper voices or both")
        row.addWidget(self._engine_combo)
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
        # Headers line up with the left-aligned cells below them; Qt centres
        # them by default, which left each label floating off its column.
        hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
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
        # The bar is a thin 8 px track: its own text would spill out of it.
        # Progress is spelled out in the status line instead.
        self._progress.setTextVisible(False)
        self._progress.setAccessibleName("Download progress")
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
        self._engine_combo.currentIndexChanged.connect(lambda: self._populate())
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
        if self._kokoro and not self._catalog:
            # Kokoro's list is built in: show it while Piper's loads.
            self._on_catalog([])
        self._retry_btn.hide()
        self._status.setText("Loading the voice catalog…")
        self._keep(self._loader)
        self._loader = _CatalogLoader(force)
        self._loader.loaded.connect(self._on_catalog)
        self._loader.failed.connect(self._on_catalog_failed)
        self._loader.start()

    def _on_catalog(self, catalog: list) -> None:
        self._catalog = catalog
        labels: dict[str, str] = {}
        for v in [*catalog, *self._kokoro]:           # Piper's wording wins
            labels.setdefault(v.locale, v.language)
        langs = sorted(labels.items(), key=lambda x: x[1])
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
        self._status.setText(
            f"Couldn't load the list of Piper voices: {message}"
            + (" Kokoro HD voices are listed." if self._kokoro else ""))

    def _refresh_installed(self) -> None:
        self._installed = _installed_short_names()

    def _populate(self) -> None:
        query = self._search.text().strip().lower()
        locale = self._lang.currentData() or ""
        installed_only = self._installed_only.isChecked()
        engine = self._engine_combo.currentData() or ""
        piper_names = {_short_name(v) for v in self._catalog}
        # Installed Piper voices missing from the catalog (offline, or retired).
        rows: list = [*self._kokoro, *self._catalog]
        for name in sorted(self._installed - piper_names):
            if not piper_tts.is_piper_voice(name):
                continue
            key = name[len(piper_tts.PIPER_PREFIX):]
            lang = key.split("-")[0]
            rows.append(CatalogVoice(key=key, locale=lang.replace("_", "-"), language=lang,
                                     quality=key.rsplit("-", 1)[-1], size_bytes=0, files={}))
        if engine:
            rows = [v for v in rows if _engine(v) == engine]
        if installed_only:
            rows = [v for v in rows if _short_name(v) in self._installed]
        if locale:
            rows = [v for v in rows if v.locale == locale]
        if query:
            rows = [v for v in rows if query in v.key.lower() or query in v.language.lower()
                    or query in v.display_name.lower()
                    or (_engine(v) == "kokoro" and query in "kokoro hd")]
        # Installed first, then Kokoro (best-rated first), then by language.
        rows.sort(key=lambda v: (
            _short_name(v) not in self._installed,
            _engine(v) != "kokoro",
            kokoro_tts.grade_rank(v.key) if _engine(v) == "kokoro" else 0,
            v.language, v.display_name))

        self._table.setUpdatesEnabled(False)
        self._table.setRowCount(len(rows))
        busy = (_short_name(self._downloader.voice)
                if self._downloader and self._downloader.isRunning() else None)
        model_ready = kokoro_tts.model_installed()
        for r, v in enumerate(rows):
            kokoro = _engine(v) == "kokoro"
            name = v.display_name + (f" · {v.gender}" if v.gender else "")
            item = QTableWidgetItem(name)
            note = getattr(v, "note", "")
            item.setToolTip(v.key + (f"\n{note}" if note else ""))
            self._table.setItem(r, _COL_VOICE, item)
            lang_item = QTableWidgetItem(v.language)
            lang_item.setToolTip(v.language)
            self._table.setItem(r, _COL_LANG, lang_item)
            quality = _QUALITY_LABELS.get(v.quality, v.quality)
            if kokoro:
                grade = kokoro_tts.grade_for(v.key)
                quality = f"HD · {quality}"
                q_item = QTableWidgetItem(quality)
                q_item.setToolTip("Kokoro HD voice" + (f" · rated {grade} by its authors"
                                                       if grade else ""))
            else:
                q_item = QTableWidgetItem(quality)
            self._table.setItem(r, _COL_QUALITY, q_item)
            size_item = QTableWidgetItem(_size_label(v.size_bytes) if v.size_bytes else "")
            if kokoro and not model_ready:
                size_item.setToolTip("Includes the shared Kokoro model, downloaded once")
            self._table.setItem(r, _COL_SIZE, size_item)
            self._table.setCellWidget(r, _COL_ACTION, self._action_button(v, busy))
        self._table.setUpdatesEnabled(True)

        # Typing in the search box mid-download must not wipe out the
        # download's progress line.
        if (self._catalog or self._kokoro or self._installed) and not self._dl_name:
            self._status.setText(f"{len(rows)} voices shown · {len(self._installed)} installed")

    def _action_button(self, v, busy: str | None) -> QWidget:
        btn = QPushButton()
        btn.setObjectName("ghostButton")
        name = _short_name(v)
        if name in self._installed:
            if _engine(v) == "piper" and piper_tts.is_bundled(v.key):
                btn.setText("Included")
                btn.setEnabled(False)
                btn.setToolTip("Comes with SetupTTS")
            else:
                btn.setText("Remove")
                btn.setObjectName("dangerGhostButton")
                # Not while a download runs: removing the last Kokoro voice
                # deletes the model the download is about to rely on.
                btn.setEnabled(busy is None)
                btn.clicked.connect(lambda _=False, n=name: self._remove(n))
        elif name == busy:
            btn.setText("Downloading…")
            btn.setEnabled(False)
        else:
            btn.setText("Download")
            btn.setEnabled(bool(v.files) and busy is None)
            btn.clicked.connect(lambda _=False, voice=v: self._download(voice))
        return btn

    # ------------------------------------------------------------------ #

    def _download(self, voice) -> None:
        if self._downloader is not None and self._downloader.isRunning():
            return
        self._keep(self._downloader)
        self._downloader = _Downloader(voice)
        self._downloader.progress.connect(self._on_progress)
        self._downloader.done.connect(self._on_downloaded)
        self._downloader.failed.connect(self._on_download_failed)
        self._downloader.cancelled.connect(self._on_download_cancelled)
        self._progress.setValue(0)
        self._progress.show()
        self._dl_name = voice.display_name
        if _engine(voice) == "kokoro" and not kokoro_tts.model_installed():
            self._dl_name = f"the Kokoro model and {voice.display_name}"
        self._status.setText(f"Downloading {self._dl_name}…")
        self._cancel_btn.show()
        self._downloader.start()
        self._populate()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            pct = min(100, int(done * 100 / total))
            self._progress.setValue(pct)
            self._status.setText(
                f"Downloading {self._dl_name} — {pct}% "
                f"({done / 1e6:.0f} of {total / 1e6:.0f} MB)"
            )

    def _finish_download_ui(self) -> None:
        self._dl_name = ""
        self._progress.hide()
        self._cancel_btn.hide()

    def _on_downloaded(self, name: str) -> None:
        self._finish_download_ui()
        self.changed = True
        self.last_installed = name
        self._refresh_installed()
        self._populate()
        self._status.setText(f"{_name_of(name)} is ready — it's selected when you close this window.")

    def _on_download_failed(self, name: str, message: str) -> None:
        self._finish_download_ui()
        self._populate()
        self._status.setText(f"Download of {_name_of(name)} failed: {message}")

    def _on_download_cancelled(self, key: str) -> None:
        self._finish_download_ui()
        self._populate()
        self._status.setText("Download cancelled.")

    def _cancel_download(self) -> None:
        if self._downloader is not None:
            self._downloader.cancel()

    def _remove(self, name: str) -> None:
        kokoro = kokoro_tts.is_kokoro_voice(name)
        extra = ""
        if kokoro and not {n for n in self._installed if kokoro_tts.is_kokoro_voice(n)} - {name}:
            extra = ("\n\nIt is your last Kokoro voice, so the shared Kokoro model "
                     "(325 MB) is removed too.")
        answer = QMessageBox.question(
            self, "Remove Voice",
            f"Remove the offline voice “{_name_of(name)}”?{extra}\n\n"
            "You can download it again at any time.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            if kokoro:
                kokoro_tts.remove_voice(name[len(kokoro_tts.KOKORO_PREFIX):])
            else:
                piper_tts.remove_voice(name[len(piper_tts.PIPER_PREFIX):])
        except OSError:
            # Windows: the file is open in a running job or an antivirus scan.
            logger.warning("Could not remove voice %s", name, exc_info=True)
            QMessageBox.warning(
                self, "Remove Voice",
                "This voice is in use right now. Wait for the conversion "
                "using it to finish, then try again.",
            )
            return
        self.changed = True
        if self.last_installed == name:
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
