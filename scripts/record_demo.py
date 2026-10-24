"""
Record docs/demo.gif: a scripted walk through the real app.

    python scripts/record_demo.py --data-dir /Users/Shared/SetupTTS-demo \
        --kokoro-from <folder with model.onnx and voices/> --out docs/demo.gif

The app runs against its own data folder (SETUPTTS_DATA_DIR), so the
recording shows a fresh install, not your history.  A virtual cursor moves
between real widgets and clicks them; every frame is the real window, with
any open popup or dialog painted on top where it sits on screen.  The voice
list loads from the live service and the conversion at the end is real
(Kokoro, on this machine).  Needs ffmpeg on PATH.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

KOKORO_VOICES = ("af_heart", "af_bella", "am_michael", "bf_emma", "bm_george",
                 "ff_siwis", "ef_dora")
SAMPLE = (
    "Chapter One\n\n"
    "The rain had stopped by the time the last train pulled into the station. "
    "Mara stepped onto the platform, pulled her coat tighter, and looked for "
    "the lantern her brother had promised to leave in the window.\n\n"
    "It was there, small and steady, at the top of the hill. She climbed slowly, "
    "counting the steps the way they had as children, and when the door opened "
    "the smell of bread and wood smoke rushed out to meet her.\n\n"
    "\"You're late,\" he said, grinning. \"I kept the soup warm anyway.\" "
    "They talked until the fire burned low, about the farm, the river and the "
    "long winter ahead, and for the first time in months she slept without dreaming."
)
TYPED = 125   # characters typed by hand; the rest is pasted


def _prepare(data_dir: Path, kokoro_from: Path, out_dir: Path) -> None:
    if data_dir.exists():
        shutil.rmtree(data_dir)
    voices = data_dir / "kokoro" / "voices"
    voices.mkdir(parents=True)
    os.link(kokoro_from / "model.onnx", data_dir / "kokoro" / "model.onnx")
    for v in KOKORO_VOICES:
        shutil.copy2(kokoro_from / "voices" / f"{v}.bin", voices / f"{v}.bin")
    out_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({
        "voice": "en-US-AvaMultilingualNeural",
        "output_dir": str(out_dir),
        "window_width": 1280, "window_height": 800,
        "show_history": True,
    }))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--kokoro-from", required=True)
    ap.add_argument("--out", default=str(ROOT / "docs" / "demo.gif"))
    ap.add_argument("--width", type=int, default=1000)
    ap.add_argument("--fps", type=int, default=15)
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    out_audio = data_dir / "Audio"
    _prepare(data_dir, Path(args.kokoro_from), out_audio)
    os.environ["SETUPTTS_DATA_DIR"] = str(data_dir)

    from PySide6.QtCore import QPoint, QPointF, QRect, Qt, QTimer
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication(sys.argv)

    from PySide6.QtCore import QEvent, QObject

    class _NoRealInput(QObject):
        """Ignore the real mouse and keyboard, so only the script drives the UI."""
        KINDS = {QEvent.MouseMove, QEvent.MouseButtonPress, QEvent.MouseButtonRelease,
                 QEvent.MouseButtonDblClick, QEvent.Wheel, QEvent.HoverEnter,
                 QEvent.HoverMove, QEvent.HoverLeave, QEvent.Enter, QEvent.Leave,
                 QEvent.KeyPress, QEvent.KeyRelease}

        def eventFilter(self, obj, ev):  # noqa: N802 - Qt override
            return ev.spontaneous() and ev.type() in self.KINDS

    no_input = _NoRealInput()
    app.installEventFilter(no_input)
    from app.main import _set_platform_font
    from app.ui.style import stylesheet_text
    _set_platform_font(app)
    app.setStyleSheet(stylesheet_text())

    from app.config.settings import AppSettings
    from app.ui.main_window import MainWindow
    from app.utils.paths import AppPaths

    paths = AppPaths()
    settings = AppSettings(paths)
    win = MainWindow(settings=settings, paths=paths)
    win.resize(1280, 800)
    win.move(80, 60)
    win.show()
    panel = win._output_panel
    editor = win._input_panel._editor

    frames_dir = Path(tempfile.mkdtemp(prefix="setuptts-demo-"))
    frames: list[tuple[Path, float]] = []
    cursor = {"pos": QPointF(640, 420), "click": -1.0}
    t0 = time.monotonic()

    def gpos(w: QWidget, dx: float = 0.5, dy: float = 0.5) -> QPointF:
        """A point inside widget *w*, in main-window coordinates."""
        p = w.mapToGlobal(QPoint(int(w.width() * dx), int(w.height() * dy)))
        return QPointF(win.mapFromGlobal(p))

    def draw_cursor(p: QPainter, at: QPointF) -> None:
        since = time.monotonic() - cursor["click"]
        if 0 <= since < 0.45:
            r = 6 + 22 * since / 0.45
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(10, 132, 255, int(110 * (1 - since / 0.45))))
            p.drawEllipse(at, r, r)
        arrow = QPainterPath(at)
        for dx, dy in ((0, 17), (4.2, 13), (7.2, 19.5), (9.8, 18.3), (6.9, 12), (12, 12)):
            arrow.lineTo(at + QPointF(dx, dy))
        arrow.closeSubpath()
        p.setPen(QPen(QColor("white"), 1.4))
        p.setBrush(QColor("black"))
        p.drawPath(arrow)

    def capture() -> None:
        dpr = win.devicePixelRatioF()
        img = QImage(int(win.width() * dpr), int(win.height() * dpr), QImage.Format_RGB32)
        img.setDevicePixelRatio(dpr)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap(0, 0, win.grab())
        for top in QApplication.topLevelWidgets():
            if top is win or not top.isVisible() or top.width() < 20:
                continue
            origin = win.mapFromGlobal(top.mapToGlobal(QPoint(0, 0)))
            if top.isWindow() and top.windowType() == Qt.Dialog:
                p.fillRect(QRect(0, 0, win.width(), win.height()), QColor(0, 0, 0, 70))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(0, 0, 0, 60))
                p.drawRoundedRect(QRect(origin + QPoint(0, 6), top.size()).adjusted(-4, -2, 4, 8), 10, 10)
            p.drawPixmap(origin, top.grab())
        draw_cursor(p, cursor["pos"])
        p.end()
        f = frames_dir / f"f{len(frames):05d}.png"
        img.save(str(f), "PNG", 30)
        frames.append((f, time.monotonic() - t0))

    # ── the walk-through: generators yield milliseconds to wait ─────── #

    def move(target: QPointF, ms: int = 650):
        start, t = QPointF(cursor["pos"]), time.monotonic()
        while (k := (time.monotonic() - t) * 1000 / ms) < 1:
            e = 0.5 - 0.5 * math.cos(math.pi * k)          # ease in-out
            cursor["pos"] = start + (target - start) * e
            yield 16
        cursor["pos"] = target

    def click(w: QWidget, dx: float = 0.5, dy: float = 0.5, action=None, ms: int = 650):
        yield from move(gpos(w, dx, dy), ms)
        yield 120
        cursor["click"] = time.monotonic()
        (action or w.click)()
        yield 350

    def pause(ms: int):
        yield ms

    def type_text(text: str, cps: float = 38):
        for ch in text:
            editor.insertPlainText(ch)
            yield int(1000 / cps)

    def slide(slider, to: int, ms: int = 900):
        handle = lambda: QPointF(win.mapFromGlobal(slider.mapToGlobal(QPoint(
            int((slider.value() - slider.minimum()) / (slider.maximum() - slider.minimum())
                * (slider.width() - 16) + 8), slider.height() // 2))))
        yield from move(handle(), 600)
        cursor["click"] = time.monotonic()
        start, t = slider.value(), time.monotonic()
        while (k := (time.monotonic() - t) * 1000 / ms) < 1:
            slider.setValue(round(start + (to - start) * (0.5 - 0.5 * math.cos(math.pi * k))))
            cursor["pos"] = handle()
            yield 16
        slider.setValue(to)
        cursor["pos"] = handle()
        yield 400

    def scroll(bar, to: int, ms: int = 1200):
        start, t = bar.value(), time.monotonic()
        while (k := (time.monotonic() - t) * 1000 / ms) < 1:
            bar.setValue(round(start + (to - start) * (0.5 - 0.5 * math.cos(math.pi * k))))
            yield 16
        bar.setValue(to)

    def wait_for(cond, timeout_ms: int):
        t = time.monotonic()
        while not cond() and (time.monotonic() - t) * 1000 < timeout_ms:
            yield 100

    def button(parent: QWidget, text: str):
        from PySide6.QtWidgets import QAbstractButton
        return next(b for b in parent.findChildren(QAbstractButton) if b.text() == text)

    def voice_index(prefix: str) -> int:
        from app.ui.panels.output_panel import _ROLE_SHORT_NAME
        c = panel._voice_combo
        for i in range(c.count()):
            if str(c.itemData(i, _ROLE_SHORT_NAME) or "").startswith(prefix):
                return i
        return -1

    def story():
        tabs = panel._source_buttons
        yield from wait_for(lambda: panel._voice_combo.isEnabled(), 30_000)
        yield 900
        # 1. Text
        yield from click(editor, 0.3, 0.12, action=editor.setFocus)
        yield from type_text(SAMPLE[:TYPED])
        yield 300
        editor.insertPlainText(SAMPLE[TYPED:])
        yield 700
        # 2. Voice sources
        for key in ("online", "piper", "system", ""):
            yield from click(tabs[key], ms=500)
            yield 650
        yield from click(panel._search_edit, ms=500, action=panel._search_edit.setFocus)
        for ch in "aria":
            panel._search_edit.insert(ch)
            yield 140
        yield 1300
        panel._search_edit.clear()
        yield 300
        # 3. Pick Kokoro Heart from the Offline list
        yield from click(tabs["piper"], ms=550)
        yield 500
        combo = panel._voice_combo
        yield from click(combo, 0.45, action=combo.showPopup)
        view = combo.view()
        target = max(voice_index("kokoro:af_heart"), 0)
        for i in range(min(4, combo.count())):
            view.setCurrentIndex(combo.model().index(i, 0))
            yield 260
        view.setCurrentIndex(combo.model().index(target, 0))
        yield 600
        cursor["click"] = time.monotonic()
        combo.setCurrentIndex(target)
        combo.hidePopup()
        yield 900
        # 4. Speed and file name
        yield from slide(panel._rate_slider, 10)
        yield from click(panel._filename_edit, 0.6, action=panel._filename_edit.selectAll)
        panel._filename_edit.setText("")
        for ch in "the-lantern":
            panel._filename_edit.insert(ch)
            yield 70
        yield 600
        # 5. Get voices
        from app.ui.dialogs.voice_manager_dialog import VoiceManagerDialog
        dialog = None

        def open_manager():
            nonlocal dialog
            dialog = VoiceManagerDialog(win)
            dialog.show()
        yield from click(panel._get_voices_btn, action=open_manager)
        yield from wait_for(lambda: dialog._table.rowCount() > 0, 20_000)
        yield 1200
        yield from scroll(dialog._table.verticalScrollBar(), 900, 1800)
        yield 500
        yield from scroll(dialog._table.verticalScrollBar(), 0, 900)
        ec = dialog._engine_combo
        kokoro_i = next(i for i in range(ec.count()) if ec.itemData(i) == "kokoro")
        yield from click(ec, action=lambda: ec.setCurrentIndex(kokoro_i))
        yield 1800
        yield from click(dialog._search, action=dialog._search.setFocus)
        for ch in "french":
            dialog._search.insert(ch)
            yield 110
        yield 1600
        dialog._search.clear()
        yield 400
        yield from click(button(dialog, "Done"), action=dialog.reject)
        yield 700
        # 6. Generate
        yield from click(panel._generate_btn)
        yield from wait_for(lambda: bool(win._history_panel._jobs), 120_000)
        yield 1600
        yield from move(gpos(win._history_panel._table, 0.3, 0.35), 700)
        yield 1200
        # 7. Settings
        from app.ui.dialogs.settings_dialog import SettingsDialog
        sd = None

        def open_settings():
            nonlocal sd
            sd = SettingsDialog(settings, paths, win)
            sd.show()
        yield from click(win._header_bar, 0.955, action=open_settings)
        yield 2200
        yield from move(gpos(sd, 0.5, 0.6), 900)
        yield 1500
        yield from click(button(sd, "Cancel"), action=sd.reject)
        yield 1200
        done[0] = True

    done = [False]
    steps = story()

    def tick():
        capture()

    def advance():
        if done[0]:
            return
        try:
            delay = next(steps)
        except StopIteration:
            done[0] = True
            return
        QTimer.singleShot(delay, advance)

    shot = QTimer()
    shot.timeout.connect(tick)
    shot.start(1000 // args.fps)
    QTimer.singleShot(400, advance)
    while not done[0]:
        app.processEvents()
        time.sleep(0.002)
    shot.stop()
    win.ensure_workers_stopped()

    # Constant-rate video from the timestamped frames, then a GIF.
    concat = frames_dir / "frames.txt"
    with concat.open("w") as fh:
        for (f, t), (_, t_next) in zip(frames, frames[1:] + [(None, frames[-1][1] + 1)]):
            fh.write(f"file '{f}'\nduration {max(t_next - t, 0.001):.3f}\n")
        fh.write(f"file '{frames[-1][0]}'\n")
    vf = (f"fps={args.fps},scale={args.width}:-1:flags=lanczos,split[a][b];"
          "[a]palettegen=max_colors=200:stats_mode=diff[p];"
          "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(concat), "-vf", vf, "-loop", "0", args.out], check=True)
    shutil.rmtree(frames_dir, ignore_errors=True)
    print(f"{args.out}: {len(frames)} frames, {Path(args.out).stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
