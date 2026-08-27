"""Settings dialog."""

import logging
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QClipboard, QFontMetrics
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
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

from app.config.settings import AppSettings
from app.utils.paths import AppPaths

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
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(20)

        # ── Output Defaults ────────────────────────────────────────── #
        root.addWidget(self._section_title("Output"))

        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        self._output_dir_edit = QLineEdit()
        self._output_dir_edit.setPlaceholderText("Desktop")
        dir_row.addWidget(self._output_dir_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_output_dir)
        dir_row.addWidget(browse_btn)
        form.addRow("Default output folder:", dir_row)

        root.addLayout(form)

        # ── Audio Defaults ─────────────────────────────────────────── #
        root.addWidget(self._section_title("Audio Defaults"))

        audio_note = QLabel(
            "Voice and speed defaults are remembered automatically "
            "from your last session."
        )
        audio_note.setWordWrap(True)
        audio_note.setObjectName("metaLabel")
        root.addWidget(audio_note)

        self._auto_voice_checkbox = QCheckBox("Auto-switch to a recommended voice")
        root.addWidget(self._auto_voice_checkbox)

        auto_voice_note = QLabel(
            "When the selected voice looks incompatible with the text "
            "(for example an English voice for Hindi script), switch to the "
            "recommended voice automatically instead of asking."
        )
        auto_voice_note.setWordWrap(True)
        auto_voice_note.setObjectName("metaLabel")
        root.addWidget(auto_voice_note)

        # ── Data ───────────────────────────────────────────────────── #
        root.addWidget(self._section_title("Data"))

        data_form = QFormLayout()
        data_form.setSpacing(12)
        data_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._data_dir_label = _PathLabel()
        data_form.addRow("App data folder:", self._data_dir_label)

        self._log_dir_label = _PathLabel()
        data_form.addRow("Log folder:", self._log_dir_label)

        root.addLayout(data_form)

        # ── Logs ───────────────────────────────────────────────────── #
        root.addWidget(self._section_title("Logs"))

        log_note = QLabel("Use these shortcuts for troubleshooting.")
        log_note.setObjectName("metaLabel")
        root.addWidget(log_note)

        log_btn_row = QHBoxLayout()
        log_btn_row.setSpacing(8)

        open_folder_btn = QPushButton("Open Logs Folder")
        open_folder_btn.clicked.connect(self._open_logs_folder)
        log_btn_row.addWidget(open_folder_btn)

        open_file_btn = QPushButton("Open Log File")
        open_file_btn.clicked.connect(self._open_log_file)
        log_btn_row.addWidget(open_file_btn)

        copy_path_btn = QPushButton("Copy Log Path")
        copy_path_btn.clicked.connect(self._copy_log_path)
        log_btn_row.addWidget(copy_path_btn)

        log_btn_row.addStretch()
        root.addLayout(log_btn_row)
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
        self._log_dir_label.set_path(str(self._paths.log_dir))

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
        return self._paths.log_dir / "voicecraft.log"

    def _open_logs_folder(self) -> None:
        log_dir = self._paths.log_dir
        if not log_dir.exists():
            QMessageBox.information(
                self, "Logs Folder",
                f"The logs folder does not exist yet:\n\n{log_dir}"
            )
            return
        self._reveal_in_explorer(log_dir, is_dir=True)

    def _open_log_file(self) -> None:
        log_file = self._log_file_path()
        if not log_file.exists():
            QMessageBox.information(
                self, "Log File",
                "No log file has been created yet.\n\n"
                f"Expected location:\n{log_file}"
            )
            return
        self._reveal_in_explorer(log_file, is_dir=False)

    def _copy_log_path(self) -> None:
        log_file = self._log_file_path()
        QApplication.clipboard().setText(str(log_file))
        # Briefly rename button text as a visual confirmation
        btn = self.sender()
        if btn:
            btn.setText("Copied!")
            from PySide6.QtCore import QTimer
            QTimer.singleShot(1500, lambda: btn.setText("Copy Log Path"))

    @staticmethod
    def _reveal_in_explorer(path: Path, is_dir: bool) -> None:
        """
        Open *path* in the platform file manager.

        - macOS  : ``open -R <file>`` reveals the file in Finder;
                   ``open <dir>``  opens the folder directly.
        - Windows: ``explorer /select,<file>`` selects the file;
                   ``explorer <dir>``          opens the folder.
        - Linux  : ``xdg-open <dir>`` opens the parent folder.
        """
        try:
            if sys.platform == "darwin":
                if is_dir:
                    subprocess.run(["open", str(path)], check=False)
                else:
                    subprocess.run(["open", "-R", str(path)], check=False)
            elif sys.platform == "win32":
                if is_dir:
                    os.startfile(str(path))          # type: ignore[attr-defined]
                else:
                    subprocess.run(
                        ["explorer", f"/select,{path}"], check=False
                    )
            else:
                # Linux / other — open the parent folder
                target = path if is_dir else path.parent
                subprocess.run(["xdg-open", str(target)], check=False)
        except Exception as exc:
            logger.warning("Could not open path in file manager: %s", exc)

    # ------------------------------------------------------------------ #

    @staticmethod
    def _section_title(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("dialogSectionTitle")
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
