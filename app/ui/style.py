"""Loads the application stylesheet."""

from app.utils.paths import resource_path

#: Placeholder in app.qss for the bundled assets folder.  Qt resolves a
#: relative url() against the *working directory*, not the .qss file, so
#: image references must be made absolute at load time — in development and
#: in a PyInstaller bundle alike.
ASSETS_PLACEHOLDER = "@ASSETS@"


def stylesheet_text() -> str:
    """The stylesheet with asset URLs resolved; empty if it is missing."""
    qss_path = resource_path("app/assets/styles/app.qss")
    if not qss_path.exists():
        return ""
    assets = resource_path("app/assets").resolve().as_posix()
    return qss_path.read_text(encoding="utf-8").replace(ASSETS_PLACEHOLDER, assets)
