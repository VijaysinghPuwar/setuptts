"""
Generate app.png (and optionally app.icns / app.ico) programmatically.

Run once before building:
    python scripts/generate_icons.py

Requires: PySide6 (already a project dependency)
For .ico conversion: pip install Pillow
"""

import sys
from pathlib import Path

# Allow running from project root
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPixmap, QBrush, QPen
from PySide6.QtWidgets import QApplication

ICONS_DIR = ROOT / "app" / "assets" / "icons"
ICONS_DIR.mkdir(parents=True, exist_ok=True)


def render_icon(size: int) -> QPixmap:
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)

    # Rounded-rect background
    radius = size * 0.22
    p.setBrush(QBrush(QColor("#007AFF")))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(0, 0, size, size, radius, radius)

    # Sound-wave bars
    pen = QPen(QColor("white"))
    pen.setCapStyle(Qt.RoundCap)
    cx, cy = size / 2, size / 2
    bar_w = size * 0.07
    gap = size * 0.055
    heights = [0.28, 0.50, 0.70, 0.50, 0.28]
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


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)

    print("Generating app.png …")
    px = render_icon(1024)
    png_path = ICONS_DIR / "app.png"
    px.save(str(png_path), "PNG")
    print(f"  Saved: {png_path}")

    # Optional: generate .ico using Pillow
    try:
        from PIL import Image
        img = Image.open(str(png_path))
        ico_path = ICONS_DIR / "app.ico"
        img.save(
            str(ico_path),
            format="ICO",
            sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
        print(f"  Saved: {ico_path}")
    except ImportError:
        print("  Pillow not installed — skipping app.ico (install with: pip install Pillow)")

    print("\nDone. For app.icns on macOS, run:")
    print("  scripts/build_macos.sh  (it reads app.assets/icons/ automatically)")
    print("\nOr manually:")
    print("  bash scripts/generate_icons.sh    (see that file for instructions)")


if __name__ == "__main__":
    main()
