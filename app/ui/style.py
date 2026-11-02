"""Loads the application stylesheet."""

import re
import sys

from app.utils.paths import resource_path

#: Placeholder in app.qss for the bundled assets folder.  Qt resolves a
#: relative url() against the *working directory*, not the .qss file, so
#: image references must be made absolute at load time — in development and
#: in a PyInstaller bundle alike.
ASSETS_PLACEHOLDER = "@ASSETS@"

_PX_FONT_SIZE = re.compile(r"(font-size:\s*)(\d+(?:\.\d+)?)px")


def stylesheet_text() -> str:
    """The stylesheet with asset URLs resolved; empty if it is missing."""
    qss_path = resource_path("app/assets/styles/app.qss")
    if not qss_path.exists():
        return ""
    assets = resource_path("app/assets").resolve().as_posix()
    qss = qss_path.read_text(encoding="utf-8").replace(ASSETS_PLACEHOLDER, assets)
    if sys.platform == "win32":
        qss = px_fonts_to_points(qss)
    return qss


def px_fonts_to_points(qss: str) -> str:
    """
    Rewrite ``font-size: Npx`` as the identical size in points (Windows).

    A pixel-sized font has ``pointSize() == -1``.  Qt's windows11 style copies
    a combo box's point size when it builds the first popup, so every session
    logged "QFont::setPointSize: Point size <= 0 (-1)".  Qt 6 on Windows
    always reports 96 logical DPI (high-DPI screens scale by device pixel
    ratio instead), so N px is exactly 0.75·N pt there and nothing changes
    on screen.  macOS reports 72 DPI, where the same rewrite would shrink
    the text, so it is applied on Windows only.
    """
    return _PX_FONT_SIZE.sub(lambda m: f"{m.group(1)}{float(m.group(2)) * 0.75:g}pt", qss)
