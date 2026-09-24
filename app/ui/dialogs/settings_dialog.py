"""Settings dialog."""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app import APP_NAME, APP_VERSION
from app.config.settings import AppSettings
from app.utils.app_logging import log_file_path
from app.utils.paths import AppPaths, open_in_file_manager

logger = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    """
    Simple settings dialog.

    Changes are applied immediately to the settings object when the user
    clicks Save; the caller is responsible for reacting to changed values.
    """

    def __init__(
        self,
        settings: AppSettings,
        paths: AppPaths | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._paths = paths or AppPaths()
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._build_ui()
        self._load_values()

    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        # The settings content scrolls, with the button row pinned below it.
        # Word-wrapped labels report a height-for-width that a plain dialog
        # layout underestimates, which previously clipped both the note under
        # the checkbox and the Save/Cancel row. Scrolling also keeps the dialog
        # usable on short screens and at large system font sizes.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("dialogScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        content.setObjectName("dialogScrollInner")
        root = QVBoxLayout(content)
        root.setContentsMargins(28, 22, 28, 20)
        root.setSpacing(8)

        # ── General ────────────────────────────────────────────────── #
        root.addWidget(self._section_title("General"))

        folder_label = QLabel("Default save folder")
        folder_label.setObjectName("dialogFieldLabel")
        root.addWidget(folder_label)
        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        self._output_dir_edit = QLineEdit()
        self._output_dir_edit.setPlaceholderText(str(Path.home() / "Desktop"))
        self._output_dir_edit.setAccessibleName("Default save folder")
        dir_row.addWidget(self._output_dir_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_output_dir)
        dir_row.addWidget(browse_btn)
        root.addLayout(dir_row)
        root.addWidget(self._note(
            "New audio files are saved here unless you pick another folder "
            "in the Export section."
        ))

        # ── Voice ──────────────────────────────────────────────────── #
        root.addWidget(self._section_title("Voice"))
        root.addWidget(self._note(
            "Your voice and speed are remembered automatically between sessions."
        ))

        self._auto_voice_checkbox = QCheckBox("Switch to a matching voice automatically")
        root.addWidget(self._auto_voice_checkbox)
        root.addWidget(self._note(
            "If the selected voice doesn't suit the text's language (for "
            "example an English voice for Hindi text), use the recommended "
            "voice instead of asking first."
        ))

        # ── Logs ───────────────────────────────────────────────────── #
        root.addWidget(self._section_title("Logs & Troubleshooting"))
        root.addWidget(self._note(
            "If something goes wrong, the log file helps explain why. You can "
            "attach it when reporting a problem."
        ))

        self._log_file_label = _PathLabel()
        root.addWidget(self._log_file_label)

        # Two rows, not three buttons abreast: under Windows' Segoe UI the
        # single row needed ~565 px and clipped a 520 px dialog on the right.
        log_btn_row = QHBoxLayout()
        log_btn_row.setSpacing(8)

        open_folder_btn = QPushButton("Open Logs Folder")
        open_folder_btn.clicked.connect(self._open_logs_folder)
        log_btn_row.addWidget(open_folder_btn)

        open_file_btn = QPushButton("Open Current Log")
        open_file_btn.clicked.connect(self._open_log_file)
        log_btn_row.addWidget(open_file_btn)
        log_btn_row.addStretch()
        root.addLayout(log_btn_row)

        copy_row = QHBoxLayout()
        self._copy_path_btn = QPushButton("Copy Log Path")
        self._copy_path_btn.clicked.connect(self._copy_log_path)
        copy_row.addWidget(self._copy_path_btn)
        copy_row.addStretch()
        root.addLayout(copy_row)

        # ── About ──────────────────────────────────────────────────── #
        root.addWidget(self._section_title("About"))

        version_lbl = QLabel(f"{APP_NAME} {APP_VERSION}")
        version_lbl.setObjectName("dialogVersion")
        version_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(version_lbl)
        root.addWidget(self._note(
            "Speech is generated by Microsoft's online neural voices, so an "
            "internet connection is required."
        ))

        data_row = QHBoxLayout()
        data_row.setSpacing(8)
        data_caption = QLabel("App data:")
        data_caption.setObjectName("metaLabel")
        data_row.addWidget(data_caption)
        self._data_dir_label = _PathLabel()
        data_row.addWidget(self._data_dir_label, 1)
        root.addLayout(data_row)
        root.addStretch()

        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # ── Buttons (pinned below the scroll area) ─────────────────── #
        button_bar = QWidget()
        button_bar.setObjectName("dialogButtonBar")
        bl = QHBoxLayout(button_bar)
        bl.setContentsMargins(28, 12, 28, 16)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        bl.addWidget(buttons)
        outer.addWidget(button_bar)

        self.resize(560, 620)

    # ------------------------------------------------------------------ #

    def _load_values(self) -> None:
        self._output_dir_edit.setText(self._settings.output_dir)
        self._auto_voice_checkbox.setChecked(
            self._settings.auto_switch_recommended_voice
        )
        self._data_dir_label.set_path(str(self._paths.data_dir))
        self._log_file_label.set_path(str(self._log_file_path()))

    def _browse_output_dir(self) -> None:
        current = self._output_dir_edit.text() or str(Path.home() / "Desktop")
        path = QFileDialog.getExistingDirectory(self, "Select Default Output Folder", current)
        if path:
            self._output_dir_edit.setText(path)

    def _save(self) -> None:
        self._settings.output_dir = self._output_dir_edit.text().strip()
        self._settings.auto_switch_recommended_voice = self._auto_voice_checkbox.isChecked()
        self._settings.save()
        self.accept()

    # ------------------------------------------------------------------ #
    # Log shortcuts                                                        #
    # ------------------------------------------------------------------ #

    def _log_file_path(self) -> Path:
        return log_file_path(self._paths.log_dir)

    def _open_logs_folder(self) -> None:
        log_dir = self._paths.log_dir
        if not log_dir.exists():
            QMessageBox.information(
                self, "Logs Folder",
                f"The logs folder does not exist yet:\n\n{log_dir}"
            )
            return
        open_in_file_manager(log_dir)

    def _open_log_file(self) -> None:
        log_file = self._log_file_path()
        if not log_file.exists():
            QMessageBox.information(
                self, "Log File",
                "No log file has been created yet.\n\n"
                f"Expected location:\n{log_file}"
            )
            return
        # Open it in the default text viewer; fall back to showing it in its
        # folder if no app is associated with .log files.
        if not open_in_file_manager(log_file):
            open_in_file_manager(log_file, reveal=True)

    def _copy_log_path(self) -> None:
        log_file = self._log_file_path()
        QApplication.clipboard().setText(str(log_file))
        # Briefly rename button text as a visual confirmation
        btn = self._copy_path_btn
        btn.setText("Copied ✓")
        QTimer.singleShot(1500, lambda: btn.setText("Copy Log Path"))

    # ------------------------------------------------------------------ #

    @staticmethod
    def _section_title(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("dialogSectionTitle")
        return lbl

    @staticmethod
    def _note(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setObjectName("metaLabel")
        return lbl


# ---------------------------------------------------------------------- #
# Elide-aware path label                                                  #
# ---------------------------------------------------------------------- #

class _PathLabel(QLabel):
    """
    Single-line label that elides a long filesystem path in the middle.

    A plain QLabel reports the full path as its minimum width, which blows
    out the form layout and pushes the row labels out of the dialog.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pathLabel")
        self._full_path = ""
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(120)

    def sizeHint(self):  # type: ignore[override]
        # Elided text should never ask for more width than it is given —
        # the full path is what the tooltip is for.
        hint = super().sizeHint()
        hint.setWidth(self.minimumWidth())
        return hint

    def set_path(self, path: str) -> None:
        self._full_path = path
        self.setToolTip(path)
        self._apply_elide()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        if not self._full_path:
            self.setText("")
            return
        metrics = QFontMetrics(self.font())
        self.setText(
            metrics.elidedText(self._full_path, Qt.ElideMiddle, max(60, self.width()))
        )
