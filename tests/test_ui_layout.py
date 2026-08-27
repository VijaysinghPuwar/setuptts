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
* saved window geometry was restored without checking it lands on a screen;
* the sidebar's pinned footer grew without bound — a wrapping resume hint and
  a wrapping CTA hint left ~160 px of scroll viewport on a short window, which
  sliced the voice card in half and pushed speed, export and the running-job
  list below the fold;
* the history strip kept a fixed height, swallowing a quarter of a short
  window;
* the ACTIVE JOBS card sat below the fold while jobs ran, so the sidebar
  looked idle during an export;
* the primary CTA and the editor's stats bar were clipped on Windows, whose
  default UI font is materially wider than macOS's — layout that is checked
  on one platform only is not checked at all.
"""

import os

import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QScrollArea, QWidget

from app.config.settings import AppSettings
from app.models.voice import Voice
from app.ui.main_window import _MAIN_ROW_MIN_FRACTION
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
    # Asserted on the label set, not the label currently shown: the button
    # steps down to a shorter label on a narrow sidebar or a wide font, and
    # the escaping has to be right for every label it can show.
    for label in window._output_panel._generate_btn._labels:
        assert "&" not in label.replace("&&", "")
    full = window._output_panel._generate_btn._labels[0]
    assert "&&" in full
    assert full.replace("&&", "&") == "Generate & Export MP3"


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


# ------------------------------------------------------------------ #
# Responsive density                                                   #
# ------------------------------------------------------------------ #

# The sizes a real user actually lands on: the window minimum, small laptops,
# common desktops, and the extremes in both axes.
WINDOW_SIZES = [
    (720, 480), (780, 520), (800, 560), (900, 620), (1000, 700),
    (1100, 660), (1280, 800), (1440, 900), (1920, 1080), (2560, 1440),
    (3840, 2160), (780, 1400), (2400, 600),
]


def _density_of(window, height):
    from app.ui.main_window import MainWindow
    return MainWindow._density_for_height(height)


def test_density_steps_down_as_the_window_shortens(window, styled_app):
    """
    Padding is given up before content.  A short window drops control padding
    first (compact) and only then the explanatory hints (minimal), so the user
    never loses the hints while there is still padding to reclaim.
    """
    from app.ui.panels.output_panel import OutputPanel

    panel = window._output_panel
    seen = {}
    for height in (1000, 800, 700, 600, 540):
        window.resize(1100, height)
        styled_app.processEvents()
        seen[height] = panel.density

    assert seen[1000] == OutputPanel.DENSITY_COMFORTABLE
    assert seen[800] == OutputPanel.DENSITY_COMFORTABLE
    assert seen[700] == OutputPanel.DENSITY_COMPACT
    assert seen[600] == OutputPanel.DENSITY_MINIMAL
    assert seen[540] == OutputPanel.DENSITY_MINIMAL


def test_density_is_a_pure_function_of_height(window, styled_app):
    """Growing back to a size must restore exactly the density it had there —
    otherwise the sidebar stays compacted after the window is re-maximised."""
    panel = window._output_panel
    window.resize(1100, 900)
    styled_app.processEvents()
    tall = panel.density

    window.resize(1100, 520)
    styled_app.processEvents()
    window.resize(1100, 900)
    styled_app.processEvents()

    assert panel.density == tall


def test_hints_collapse_only_at_minimal_density(window, styled_app):
    panel = window._output_panel
    window.resize(1100, 900)
    styled_app.processEvents()
    assert panel._generate_hint.isVisible()

    window.resize(1100, 560)
    styled_app.processEvents()
    assert not panel._generate_hint.isVisible()

    window.resize(1100, 900)
    styled_app.processEvents()
    assert panel._generate_hint.isVisible()


def test_collapsed_hint_text_survives_on_the_tooltip(window, styled_app):
    """Hiding a hint must not destroy the information it carried."""
    panel = window._output_panel
    window.resize(1100, 560)
    styled_app.processEvents()

    assert not panel._generate_hint.isVisible()
    assert "Type or paste text" in panel._generate_hint.toolTip()


def test_set_density_rejects_an_unknown_level(window):
    with pytest.raises(ValueError):
        window._output_panel.set_density("enormous")


# ------------------------------------------------------------------ #
# Sidebar scroll viewport                                              #
# ------------------------------------------------------------------ #

def _sidebar_viewport_height(window):
    return window._output_panel._scroll.viewport().height()


@pytest.mark.parametrize("width,height", WINDOW_SIZES)
def test_sidebar_viewport_is_never_a_sliver(window, styled_app, width, height):
    """
    The pinned footer is subtracted from the scrollable controls above it.
    When the footer was free to grow it left ~160 px of viewport, which is
    less than three controls; the voice card was cut in half by the footer
    edge.  Two full-density controls plus their card padding is the floor.
    """
    window.resize(width, height)
    styled_app.processEvents()
    assert _sidebar_viewport_height(window) >= 180


@pytest.mark.parametrize("width,height", WINDOW_SIZES)
def test_whole_voice_card_fits_above_the_fold(window, styled_app, width, height):
    """The voice picker is the first thing a user touches; it must never be
    sliced against the footer edge."""
    window.resize(width, height)
    styled_app.processEvents()

    panel = window._output_panel
    card = panel._voice_combo.parentWidget()
    assert card.height() <= _sidebar_viewport_height(window), (
        f"voice card ({card.height()} px) exceeds the sidebar viewport "
        f"({_sidebar_viewport_height(window)} px) at {width}x{height}"
    )


@pytest.mark.parametrize("width,height", WINDOW_SIZES)
def test_everything_in_the_sidebar_stays_reachable_by_scrolling(
    window, styled_app, width, height
):
    """Content may sit below the fold, but the scroll range must reach it."""
    window.resize(width, height)
    styled_app.processEvents()

    scroll = window._output_panel._scroll
    overflow = scroll.widget().sizeHint().height() - scroll.viewport().height()
    assert scroll.verticalScrollBar().maximum() >= min(0, overflow) or overflow <= 0
    if overflow > 0:
        assert scroll.verticalScrollBar().maximum() > 0


# ------------------------------------------------------------------ #
# Responsive history strip                                             #
# ------------------------------------------------------------------ #

def test_history_strip_scales_with_window_height(window, styled_app):
    """A fixed strip swallowed a quarter of a short window and looked
    stranded on a tall one."""
    heights = {}
    for height in (520, 660, 900, 1400):
        window.resize(1100, height)
        styled_app.processEvents()
        heights[height] = window._v_splitter.sizes()[-1]

    assert heights[520] < heights[900]
    assert heights[900] <= heights[1400]
    for height, strip in heights.items():
        assert strip >= 88, f"history strip unusable at window height {height}"
        assert strip <= height * 0.25, f"history strip dominates at {height}"


def test_dragging_the_history_split_pins_it(window, styled_app):
    window.resize(1100, 900)
    styled_app.processEvents()

    total = sum(window._v_splitter.sizes())
    window._v_splitter.setSizes([total - 200, 200])   # simulate the drag…
    window._on_history_resized()                      # …and the signal it emits
    styled_app.processEvents()

    window.resize(1100, 940)              # a resize must not undo the choice
    styled_app.processEvents()
    assert window._v_splitter.sizes()[-1] == pytest.approx(200, abs=8)


def test_pinned_history_is_still_clamped_on_a_short_window(window, styled_app):
    """A user-chosen height is honoured, but the editor and controls keep the
    majority of the window once it is made much shorter."""
    window.resize(1100, 1200)
    styled_app.processEvents()
    total = sum(window._v_splitter.sizes())
    window._v_splitter.setSizes([total - 300, 300])
    window._on_history_resized()
    styled_app.processEvents()
    assert window._v_splitter.sizes()[-1] == pytest.approx(300, abs=8)

    window.resize(1100, 520)
    styled_app.processEvents()
    main_row, strip = window._v_splitter.sizes()
    assert main_row > strip
    assert main_row >= (main_row + strip) * _MAIN_ROW_MIN_FRACTION - 1


# ------------------------------------------------------------------ #
# Active jobs visibility                                               #
# ------------------------------------------------------------------ #

def _submit_fake_job(panel, index=0):
    from app.workers.job_queue import JobItem

    item = JobItem(
        text="hello " * 50,
        voice="en-US-AvaNeural",
        voice_display="Ava · English (US)",
        rate="+5%",
        volume="+0%",
        output_path=f"/tmp/chapter-{index}.mp3",
        id=f"job{index}",
    )
    panel._on_job_submitted(item)
    return item


def test_first_job_scrolls_the_active_jobs_card_into_view(window, styled_app):
    """
    ACTIVE JOBS is the last card in the scrollable column, so on a short
    window it lands below the fold and the sidebar looks idle while the
    export runs.
    """
    window.resize(1100, 660)
    styled_app.processEvents()
    panel = window._output_panel

    _submit_fake_job(panel)
    panel._scroll_to_jobs()               # the deferred call, run synchronously
    styled_app.processEvents()

    visible = panel._jobs_card.visibleRegion().boundingRect()
    assert visible.height() > 20, "ACTIVE JOBS card is not on screen"


def test_a_long_job_filename_does_not_widen_the_sidebar(window, styled_app):
    window.resize(1100, 800)
    styled_app.processEvents()
    panel = window._output_panel
    before = panel.width()

    from app.workers.job_queue import JobItem
    panel._on_job_submitted(JobItem(
        text="x", voice="en-US-AvaNeural", voice_display="Ava · English (US)",
        rate="+5%", volume="+0%", id="longjob",
        output_path="/tmp/" + "an-extremely-long-audiobook-chapter-filename" * 4 + ".mp3",
    ))
    styled_app.processEvents()

    assert panel.width() == before


# ------------------------------------------------------------------ #
# Nothing is clipped, at any size                                      #
# ------------------------------------------------------------------ #

def _clipping_problems(root):
    """Widgets whose text or geometry does not fit the space they are given.

    Descendants of a scroll area are exempt: extending past the viewport is
    what a scroll area is for.
    """
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QAbstractScrollArea, QLabel, QPushButton

    problems = []
    bounds = root.rect()
    for w in root.findChildren(QWidget):
        if not w.isVisible():
            continue

        if isinstance(w, QPushButton) and w.text():
            hint = w.sizeHint()
            if w.width() < hint.width() or w.height() < hint.height():
                problems.append(
                    f"clipped button {w.text()!r}: "
                    f"{w.width()}x{w.height()} < {hint.width()}x{hint.height()}"
                )
        elif (
            isinstance(w, QLabel)
            and w.text()
            and not w.wordWrap()
            and type(w).__name__ != "_ElidingLabel"
            and w.textFormat() != Qt.RichText
        ):
            need = QFontMetrics(w.font()).horizontalAdvance(w.text())
            if w.width() < need:
                problems.append(
                    f"clipped label {w.text()[:30]!r}: {w.width()} < {need}"
                )

        rect = w.rect().translated(w.mapTo(root, QPoint(0, 0)))
        if rect.isEmpty():
            continue
        parent, in_scroll = w.parentWidget(), False
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                in_scroll = True
                break
            parent = parent.parentWidget()
        if in_scroll:
            continue
        if (
            rect.right() > bounds.right() + 1
            or rect.bottom() > bounds.bottom() + 1
            or rect.left() < -1
            or rect.top() < -1
        ):
            problems.append(
                f"{w.metaObject().className()}#{w.objectName()} escapes the "
                f"window: {rect.x()},{rect.y()} {rect.width()}x{rect.height()}"
            )
    return problems


@pytest.mark.parametrize("width,height", WINDOW_SIZES)
def test_nothing_is_clipped_at_any_window_size(window, styled_app, width, height):
    window.resize(width, height)
    styled_app.processEvents()
    assert _clipping_problems(window) == []


@pytest.mark.parametrize("width,height", WINDOW_SIZES)
def test_nothing_is_clipped_with_the_sidebar_at_its_fullest(
    window, styled_app, width, height
):
    """Warning banner, resume prompt and three running jobs all at once —
    every optional row in the sidebar visible simultaneously."""
    panel = window._output_panel
    panel._voice_warning_label.setText(
        "This voice is tuned for English but the text looks like Japanese. "
        "Generation may mispronounce large parts of the document."
    )
    panel._voice_warning.show()
    panel._resume_job_btn.show()
    panel._resume_job_hint.setText(
        "1 resumable job(s) saved locally. Latest: a-long-audiobook-name.mp3 "
        "— 412 chunk(s) preserved, resume at chunk 413."
    )
    panel._resume_job_hint.setVisible(
        panel.density != panel.DENSITY_MINIMAL
    )
    for i in range(3):
        _submit_fake_job(panel, i)

    window.set_input_text("Some text to convert. " * 200)
    window.resize(width, height)
    styled_app.processEvents()

    assert _clipping_problems(window) == []


@pytest.mark.parametrize("width,height", WINDOW_SIZES)
def test_the_editor_keeps_a_usable_width(window, styled_app, width, height):
    window.resize(width, height)
    styled_app.processEvents()
    assert window._input_panel._editor.width() >= 280


def test_repeated_resizes_do_not_drift(window, styled_app):
    """The same window size must always produce the same layout, however it
    was reached — otherwise the sidebar creeps on every resize."""
    def snapshot():
        return (
            window._output_panel.width(),
            window._input_panel.width(),
            window._v_splitter.sizes(),
            window._output_panel.density,
            _sidebar_viewport_height(window),
        )

    window.resize(1100, 660)
    styled_app.processEvents()
    first = snapshot()

    for size in [(1920, 1080), (780, 520), (2560, 1440), (900, 600)]:
        window.resize(*size)
        styled_app.processEvents()
    window.resize(1100, 660)
    styled_app.processEvents()

    assert snapshot() == first


def test_the_window_minimum_is_actually_renderable(window, styled_app):
    """setMinimumSize is a promise: the layout must hold at that size."""
    from app.ui.main_window import _MIN_WINDOW_SIZE

    assert window.minimumSize().width() == _MIN_WINDOW_SIZE[0]
    assert window.minimumSize().height() == _MIN_WINDOW_SIZE[1]

    window.resize(*_MIN_WINDOW_SIZE)
    styled_app.processEvents()
    assert _clipping_problems(window) == []
    assert window._output_panel._generate_btn.visibleRegion().boundingRect().height() > 20


# ------------------------------------------------------------------ #
# Wider fonts                                                          #
# ------------------------------------------------------------------ #

# Widget text width is driven by the stylesheet's px font-size, not by the
# application point size, so scaling every declared size is what reproduces a
# platform whose UI font is wider.  Windows' Segoe UI needed ~330 px for the
# CTA where macOS's SF Pro needs ~250 — a factor of about 1.3 — and Windows
# text scaling goes to 250 %, so the range below brackets both with room to
# spare.  Without this the macOS CI job passed while the Windows job failed.
FONT_SCALES = [1.0, 1.3, 1.6, 2.0, 2.5]


def _scaled_stylesheet(scale):
    import re

    qss = resource_path("app/assets/styles/app.qss").read_text(encoding="utf-8")
    return re.sub(
        r"font-size:\s*([\d.]+)px",
        lambda m: f"font-size: {max(1, round(float(m.group(1)) * scale))}px",
        qss,
    )


@pytest.fixture
def scaled_window(request, qapp, app_paths, qtbot):
    """A window rendered under an inflated stylesheet font size."""
    from app.ui.main_window import MainWindow

    qapp.setStyleSheet(_scaled_stylesheet(request.param))
    settings = AppSettings(app_paths)
    settings.window_x = settings.window_y = None
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
    qapp.processEvents()
    yield win
    win.ensure_workers_stopped()
    qapp.setStyleSheet("")


@pytest.mark.parametrize("scaled_window", FONT_SCALES, indirect=True)
@pytest.mark.parametrize("width,height", [(720, 480), (1100, 660), (1920, 1080)])
def test_nothing_is_clipped_under_a_wider_font(scaled_window, qapp, width, height):
    scaled_window.resize(width, height)
    qapp.processEvents()
    assert _clipping_problems(scaled_window) == []


@pytest.mark.parametrize("scaled_window", FONT_SCALES, indirect=True)
def test_the_cta_steps_down_to_a_shorter_label_instead_of_clipping(
    scaled_window, qapp
):
    """Qt neither elides nor wraps a button label — it just cuts it off."""
    button = scaled_window._output_panel._generate_btn
    scaled_window.resize(720, 480)
    qapp.processEvents()

    assert button.text() in button._labels
    assert button.width() >= button.sizeHint().width()


def test_the_cta_keeps_its_full_label_when_there_is_room(window, styled_app):
    button = window._output_panel._generate_btn
    window.resize(1600, 1000)
    styled_app.processEvents()
    assert button.text() == "Generate && Export MP3"


@pytest.mark.parametrize("scaled_window", FONT_SCALES, indirect=True)
def test_the_stats_bar_gives_up_the_drop_hint_before_the_word_count(
    scaled_window, qapp
):
    """Both labels cannot always fit; the count is the one that matters."""
    panel = scaled_window._input_panel
    scaled_window.set_input_text("word " * 900)
    scaled_window.resize(720, 480)
    qapp.processEvents()

    from PySide6.QtGui import QFontMetrics

    count = panel._count_label
    assert count.isVisible()
    assert QFontMetrics(count.font()).horizontalAdvance(count.text()) <= count.width()
    assert "900" in count.text()          # the number survives the abbreviation
    assert count.toolTip().endswith("characters")


def test_the_full_word_count_is_used_when_it_fits(window, styled_app):
    window.set_input_text("word " * 10)
    window.resize(1600, 1000)
    styled_app.processEvents()
    assert window._input_panel._count_label.text() == "10 words  ·  50 characters"
    assert window._input_panel._drop_hint.isVisible()


@pytest.mark.parametrize("scaled_window", FONT_SCALES, indirect=True)
def test_toolbars_grow_with_their_content_rather_than_clipping_it(
    scaled_window, qapp
):
    """These bars carried fixed or capped heights that a larger font overran."""
    scaled_window.resize(1100, 660)
    qapp.processEvents()

    for bar in (
        scaled_window.findChild(QWidget, "headerBar"),
        scaled_window.findChild(QWidget, "editorTopBar"),
        scaled_window.findChild(QWidget, "historyHeader"),
    ):
        assert bar is not None
        assert bar.height() >= bar.sizeHint().height()


@pytest.mark.parametrize("scaled_window", FONT_SCALES, indirect=True)
def test_the_window_minimum_grows_with_the_font(scaled_window):
    """An explicit minimum overrides the layout's own minimumSizeHint, so it
    must not promise a size the layout cannot actually render."""
    minimum = scaled_window.minimumSize()
    hint = scaled_window.minimumSizeHint()
    assert minimum.width() >= hint.width()
    assert minimum.height() >= hint.height()
