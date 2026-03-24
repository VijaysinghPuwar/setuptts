"""About dialog."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPixmap, QBrush, QPen, QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import APP_NAME, APP_VERSION, APP_DESCRIPTION


def _make_logo_pixmap(size: int = 64) -> QPixmap:
    """Load the real app icon; fall back to programmatic rendering."""
    from app.utils.paths import resource_path
    icon_path = resource_path("app/assets/icons/app.png")
    if icon_path.exists():
        return QPixmap(str(icon_path)).scaled(
            size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )

    # Fallback
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    radius = size * 0.22
    p.setBrush(QBrush(QColor("#007AFF")))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(0, 0, size, size, radius, radius)
    pen = QPen(QColor("white"))
    pen.setCapStyle(Qt.RoundCap)
    cx, cy = size / 2, size / 2
    bar_w = size * 0.07
    gap = size * 0.06
    heights = [0.30, 0.55, 0.75, 0.55, 0.30]
    total_w = len(heights) * bar_w + (len(heights) - 1) * gap
    x = cx - total_w / 2
    for h_ratio in heights:
        bar_h = size * h_ratio
        pen.setWidthF(bar_w)
        p.setPen(pen)
        p.drawLine(int(x + bar_w / 2), int(cy - bar_h / 2),
                   int(x + bar_w / 2), int(cy + bar_h / 2))
        x += bar_w + gap
    p.end()
    return px


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setFixedSize(380, 300)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 28)
        layout.setSpacing(0)

        # Logo + name row
        top = QHBoxLayout()
        top.setSpacing(16)

        logo_lbl = QLabel()
        logo_lbl.setPixmap(_make_logo_pixmap(56))
        logo_lbl.setFixedSize(56, 56)
        top.addWidget(logo_lbl, alignment=Qt.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(4)
        name_lbl = QLabel(APP_NAME)
        name_lbl.setStyleSheet("font-size: 20px; font-weight: 700; color: #1D1D1F;")
        info.addWidget(name_lbl)

        ver_lbl = QLabel(f"Version {APP_VERSION}")
        ver_lbl.setStyleSheet("font-size: 13px; color: #86868B;")
        info.addWidget(ver_lbl)

        top.addLayout(info)
        top.addStretch()
        layout.addLayout(top)

        layout.addSpacing(20)

        desc = QLabel(APP_DESCRIPTION)
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size: 13px; color: #3C3C43; line-height: 1.5;")
        layout.addWidget(desc)

        layout.addSpacing(12)

        built_with = QLabel("Built with PySide6 · Powered by Microsoft Edge TTS")
        built_with.setStyleSheet("font-size: 12px; color: #86868B;")
        layout.addWidget(built_with)

        layout.addStretch()

        # Close button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setFixedWidth(80)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)
