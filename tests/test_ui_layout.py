"""
Regression tests for UI layout, theming and window-state handling.

Each test here pins a defect that shipped in a previous release:

* the right sidebar lost every stylesheet background (including the green
  primary CTA) because container widgets carried inline stylesheets;
* the primary CTA sat inside the scroll area and fell below the fold at the
  default and minimum window sizes;
* the CTA label lost its ampersand to Qt's mnemonic handling;
* the sidebar was pinned to a fixed width regardless of window size;
* the history panel height was persisted as 0 whenever the panel was hidden;
* saved window geometry was restored without checking it lands on a screen.
"""

import os

import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QScrollArea, QWidget

from app.config.settings import AppSettings
from app.models.voice import Voice
from app.utils.paths import AppPaths, resource_path


# These tests drive real widgets and need pytest-qt's qapp/qtbot fixtures.
# Skip cleanly rather than erroring if it is unavailable, so a missing test
# dependency degrades to "not run" instead of breaking the release build.
pytest.importorskip("pytestqt", reason="pytest-qt is required for UI layout tests")

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture(scope="session", autouse=True)
def _offscreen():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def app_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUPTTS_DATA_DIR", str(tmp_path / "data"))
    return AppPaths()


@pytest.fixture
def styled_app(qapp):
    """Apply the real application stylesheet, as app.main does at startup."""
    qss = resource_path("app/assets/styles/app.qss")
    qapp.setStyleSheet(qss.read_text(encoding="utf-8"))
    yield qapp
    qapp.setStyleSheet("")


@pytest.fixture
def window(styled_app, app_paths, qtbot):
    from app.ui.main_window import MainWindow

    settings = AppSettings(app_paths)
    win = MainWindow(settings=settings, paths=app_paths)
    qtbot.addWidget(win)
    win._output_panel._on_voices_loaded(
        [
            Voice(
                short_name="en-US-AvaNeural",
                friendly_name="Ava",
                gender="Female",
                locale="en-US",
            )
        ]
    )
    win.show()
    styled_app.processEvents()
    yield win
    win.ensure_workers_stopped()


# ------------------------------------------------------------------ #
# Stylesheet integrity                                                 #
# ------------------------------------------------------------------ #

def _pixel(window, widget, dx, dy):
    point = widget.mapTo(window, widget.rect().topLeft())
    return window.grab().toImage().pixelColor(point.x() + dx, point.y() + dy).name()


def test_sidebar_containers_carry_no_inline_stylesheet(window):
    """
    A widget-level stylesheet applies to the widget *and every descendant*,
    overriding the application stylesheet.  An inline sheet on the sidebar's
    scroll area or its inner widget silently strips the background from every
    card, input and button nested inside it.
    """
    panel = window._output_panel
    scroll = panel.findChild(QScrollArea)

    assert scroll.styleSheet() == ""
    assert scroll.widget().styleSheet() == ""


def test_generate_button_renders_its_accent_background(window, styled_app):
    """The primary CTA must actually paint green, not fall through to the
    bare window colour."""
    panel = window._output_panel
    window.resize(1100, 800)
    window.set_input_text("some text")
    styled_app.processEvents()

    button = panel._generate_btn
    assert button.isEnabled()
    assert _pixel(window, button, 30, 20).lower() == "#1db954"


def test_export_card_and_inputs_keep_their_surfaces(window, styled_app):
    window.resize(1100, 800)
    styled_app.processEvents()
    panel = window._output_panel

    card = panel._filename_edit.parentWidget()
    assert _pixel(window, card, 6, 6).lower() == "#161618"
    assert _pixel(window, panel._filename_edit, 30, 15).lower() == "#1e1e21"


# ------------------------------------------------------------------ #
# Primary CTA placement                                                #
# ------------------------------------------------------------------ #

def test_generate_button_is_outside_the_scroll_area(window):
    panel = window._output_panel
    scroll = panel.findChild(QScrollArea)
    assert not scroll.isAncestorOf(panel._generate_btn)


@pytest.mark.parametrize("height", [520, 560, 660, 800, 1000])
def test_generate_button_visible_at_every_window_height(window, styled_app, height):
    """The CTA previously needed a window taller than ~800 px to be reachable."""
    window.resize(1100, height)
    styled_app.processEvents()

    visible = window._output_panel._generate_btn.visibleRegion().boundingRect()
    assert visible.height() > 20, f"CTA not visible at window height {height}"
    assert visible.width() > 100


def test_generate_button_label_keeps_its_ampersand(window):
    """
    Qt eats a single '&' in a button label as a keyboard mnemonic, which
    rendered the CTA as "Generate Export MP3".  The literal form is '&&'.
    """
    text = window._output_panel._generate_btn.text()
    assert "&&" in text
    assert text.replace("&&", "&") == "Generate & Export MP3"


# ------------------------------------------------------------------ #
# Responsive sidebar                                                   #
# ------------------------------------------------------------------ #

def test_sidebar_width_scales_with_window(window, styled_app):
    panel = window._output_panel
    widths = {}
    for width in (780, 900, 1100, 1600):
        window.resize(width, 660)
        styled_app.processEvents()
        widths[width] = panel.width()

    # Never more than ~40% of a narrow window...
    assert widths[780] <= int(780 * 0.42)
    # ...but still grows toward the preferred width when there is room.
    assert widths[1600] > widths[780]
    # ...and always leaves the editor a usable amount of space.
    for width, sidebar in widths.items():
        assert width - sidebar >= 320, f"editor starved at window width {width}"


def test_sidebar_returns_to_preferred_width_after_shrinking(window, styled_app):
    """Narrowing then re-widening the window must not pin the sidebar at its
    minimum width."""
    panel = window._output_panel
    window.resize(1600, 660)
    styled_app.processEvents()
    wide = panel.width()

    window.resize(780, 660)
    styled_app.processEvents()
    window.resize(1600, 660)
    styled_app.processEvents()

    assert panel.width() == wide


# ------------------------------------------------------------------ #
# Window state persistence                                             #
# ------------------------------------------------------------------ #

def test_hidden_history_panel_height_is_not_persisted_as_zero(window, styled_app):
    """
    QSplitter.sizes() reports 0 for a hidden widget.  Persisting that made the
    history panel come back 0 px tall on the next launch.
    """
    settings = window._settings
    window.resize(1100, 660)
    styled_app.processEvents()

    window._toggle_history(False)
    styled_app.processEvents()
    window._save_window_state()

    assert settings.history_panel_height > 0


def test_history_panel_survives_a_hide_show_round_trip(window, styled_app):
    window.resize(1100, 660)
    styled_app.processEvents()
    before = window._v_splitter.sizes()[-1]

    window._toggle_history(False)
    styled_app.processEvents()
    window._toggle_history(True)
    styled_app.processEvents()

    assert window._v_splitter.sizes()[-1] == before


def test_offscreen_geometry_is_clamped_to_a_real_screen(styled_app, app_paths, qtbot):
    """A window saved on a monitor that is no longer attached must not reopen
    somewhere the user cannot reach it."""
    from PySide6.QtWidgets import QApplication
    from app.ui.main_window import MainWindow

    settings = AppSettings(app_paths)
    settings.window_x, settings.window_y = 99_999, 99_999
    settings.window_width, settings.window_height = 9_000, 9_000

    win = MainWindow(settings=settings, paths=app_paths)
    qtbot.addWidget(win)
    try:
        available = QApplication.primaryScreen().availableGeometry()
        geometry = win.geometry()
        assert available.contains(geometry.topLeft())
        assert geometry.width() <= available.width()
        assert geometry.height() <= available.height()
    finally:
        win.ensure_workers_stopped()


def test_restored_size_respects_the_window_minimum(styled_app, app_paths, qtbot):
    from app.ui.main_window import MainWindow

    settings = AppSettings(app_paths)
    settings.window_width, settings.window_height = 100, 100
    settings.window_x = settings.window_y = None

    win = MainWindow(settings=settings, paths=app_paths)
    qtbot.addWidget(win)
    try:
        assert win.width() >= win.minimumWidth()
        assert win.height() >= win.minimumHeight()
    finally:
        win.ensure_workers_stopped()


# ------------------------------------------------------------------ #
# Shutdown                                                             #
# ------------------------------------------------------------------ #

def test_ensure_workers_stopped_is_idempotent(window):
    window.ensure_workers_stopped()
    window.ensure_workers_stopped()   # must not raise or re-enter shutdown
    assert window._workers_stopped is True


# ------------------------------------------------------------------ #
# Dialogs                                                              #
# ------------------------------------------------------------------ #

def test_dialogs_do_not_use_light_theme_text_colours(styled_app, app_paths, qtbot):
    """
    The dialogs are drawn on the dark #161618 surface.  Hard-coded near-black
    text (#1D1D1F / #3C3C43) left the section headings and the About blurb
    effectively invisible.
    """
    from app.ui.dialogs.about_dialog import AboutDialog
    from app.ui.dialogs.settings_dialog import SettingsDialog

    banned = ("#1d1d1f", "#3c3c43", "#e5e5ea")

    for factory in (
        lambda: AboutDialog(),
        lambda: SettingsDialog(AppSettings(app_paths), app_paths),
    ):
        dialog = factory()
        qtbot.addWidget(dialog)
        for child in dialog.findChildren(QWidget):
            sheet = child.styleSheet().lower()
            for colour in banned:
                assert colour not in sheet, f"{child} still uses {colour}"


def test_settings_dialog_content_is_reachable_when_short(styled_app, app_paths, qtbot):
    """Content scrolls rather than being clipped, and the buttons stay pinned."""
    from PySide6.QtWidgets import QDialogButtonBox
    from app.ui.dialogs.settings_dialog import SettingsDialog

    dialog = SettingsDialog(AppSettings(app_paths), app_paths)
    qtbot.addWidget(dialog)
    dialog.resize(520, 300)   # deliberately far too short
    dialog.show()
    styled_app.processEvents()

    assert dialog.findChild(QScrollArea) is not None

    buttons = dialog.findChild(QDialogButtonBox)
    visible = buttons.visibleRegion().boundingRect()
    assert visible.height() > 10, "Save/Cancel clipped on a short dialog"


def test_settings_dialog_elides_long_paths(styled_app, app_paths, qtbot):
    """A long data path must not blow out the form and push its label away."""
    from app.ui.dialogs.settings_dialog import SettingsDialog

    dialog = SettingsDialog(AppSettings(app_paths), app_paths)
    qtbot.addWidget(dialog)
    dialog.resize(520, 620)
    dialog.show()
    styled_app.processEvents()

    label = dialog._data_dir_label
    full = str(app_paths.data_dir)
    assert label.toolTip() == full
    assert label.sizeHint().width() <= dialog.width()
