"""Settings dialog."""

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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

    @staticmethod
    def _section_title(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "font-size: 13px; font-weight: 600; color: #1D1D1F; "
            "border-bottom: 1px solid #E5E5EA; padding-bottom: 6px;"
        )
        return lbl
