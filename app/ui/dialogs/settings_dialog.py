"""Settings dialog."""

import logging
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QClipboard
from PySide6.QtWidgets import (
    QApplication,
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
        self.setMinimumWidth(480)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._build_ui()
        self._load_values()

    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
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

        # ── Data ───────────────────────────────────────────────────── #
        root.addWidget(self._section_title("Data"))

        data_form = QFormLayout()
        data_form.setSpacing(12)
        data_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._data_dir_label = QLabel()
        self._data_dir_label.setObjectName("metaLabel")
        self._data_dir_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        data_form.addRow("App data folder:", self._data_dir_label)

        self._log_dir_label = QLabel()
        self._log_dir_label.setObjectName("metaLabel")
        self._log_dir_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
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

        # ── Buttons ────────────────────────────────────────────────── #
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------ #

    def _load_values(self) -> None:
        self._output_dir_edit.setText(self._settings.output_dir)
        self._data_dir_label.setText(str(self._paths.data_dir))
        self._log_dir_label.setText(str(self._paths.log_dir))

    def _browse_output_dir(self) -> None:
        current = self._output_dir_edit.text() or str(Path.home() / "Desktop")
        path = QFileDialog.getExistingDirectory(self, "Select Default Output Folder", current)
        if path:
            self._output_dir_edit.setText(path)

    def _save(self) -> None:
        self._settings.output_dir = self._output_dir_edit.text().strip()
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
        lbl.setStyleSheet(
            "font-size: 13px; font-weight: 600; color: #1D1D1F; "
            "border-bottom: 1px solid #E5E5EA; padding-bottom: 6px;"
        )
        return lbl
