#!/usr/bin/python3
"""Floating dictation pill (GTK4 + wlr-layer-shell).

Runs under the *system* python (needs PyGObject and gtk4-layer-shell) and stays
resident: the daemon starts it hidden when it starts and drives it over stdin,
one line per message (see overlay_proto.py). The pill shows the whole dictation:
the waveform while you speak, a shimmer while it's recognized, the first words
with a check once they're typed, or "nothing heard" / "error". Exits when stdin
closes.

    overlay.py [--label TEXT] [--captions] [--palette JSON]

To try it by hand (the sleep keeps stdin open; it quits when stdin closes):

    (printf 'show\\n0.05 -20\\nstate transcribing\\n'; sleep 5) | /usr/bin/python3 overlay.py
"""

import argparse
import importlib.util
import json
import math
import os
import signal
import sys
import time
from collections import deque


def _load_lines():
    """overlay_lines.py from this directory: the protocol parser, shared with the
    daemon. Loaded by path so chttr's other modules never shadow anything here."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay_lines.py")
    spec = importlib.util.spec_from_file_location("chttr_overlay_lines", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lines = _load_lines()

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
import cairo
from gi.repository import Gio, GLib, Gtk
from gi.repository import Gtk4LayerShell as LayerShell

BARS = 28
W, H = 320, 72
W_CAP, H_CAP = 460, 112     # with live captions
TICK_MS = 33
SHIMMER_PERIOD = 1.1        # seconds per sweep across the bars
LISTENING = ("listening", "command", "handsfree")
# Used when the daemon sends no theme: lavender on near-black.
DEFAULT_PALETTE = {"bg": "#141419", "fg": "#ebebf2", "accent": "#b099ed", "hot": "#f24d4d",
                   "dim": "#737380", "dark": True}


class Overlay:
    def __init__(self, label: str = "", captions: bool = False, palette: dict | None = None) -> None:
        self.label = label
        self.captions = captions
        self.w, self.h = (W_CAP, H_CAP) if captions else (W, H)
        self.set_palette(palette or {})
        self.reset()
        self.mode, self.mode_t0 = "listening", time.monotonic()
        self.visible = self.ticking = False
        self.buf = b""
        self.phase = 0.0

        self.app = Gtk.Application(application_id="dev.rab.chttr.overlay", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.app.connect("activate", self.on_activate)

    def reset(self) -> None:
        self.levels = [-90.0] * BARS    # recent levels in dB (falling off smoothly), newest last
        self.hot = [False] * BARS       # that reading was close to clipping
        self.shown = [0.0] * BARS       # what each bar shows, 0..1
        self.recent: deque[tuple[float, float]] = deque()   # (time, dB) for the display gain
        self.peak_db = -90.0
        self.caption = self.text = ""
        self.alpha = 1.0

    def set_palette(self, palette: dict) -> None:
        merged = {**DEFAULT_PALETTE, **palette}
        colours = {}
        for key in ("bg", "fg", "accent", "hot", "dim"):
            try:
                colours[key] = lines.hex_to_rgb(str(merged[key]))
            except ValueError:
                colours[key] = lines.hex_to_rgb(DEFAULT_PALETTE[key])
        self.bg, self.fg, self.accent = colours["bg"], colours["fg"], colours["accent"]
        self.hot_c, self.dim = colours["hot"], colours["dim"]
        self.dark = bool(merged.get("dark", True))

    def on_activate(self, app: Gtk.Application) -> None:
        app.hold()                      # stay alive while hidden
        win = self.win = Gtk.ApplicationWindow(application=app)
        win.set_default_size(self.w, self.h)
        win.set_decorated(False)
        LayerShell.init_for_window(win)
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(win, "chttr-overlay")
        LayerShell.set_anchor(win, LayerShell.Edge.BOTTOM, True)
        LayerShell.set_margin(win, LayerShell.Edge.BOTTOM, 96)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_exclusive_zone(win, -1)

        self.area = Gtk.DrawingArea()
        self.area.set_content_width(self.w)
        self.area.set_content_height(self.h)
        self.area.set_draw_func(self.draw)
        win.set_child(self.area)

        css = Gtk.CssProvider()
        css.load_from_string("window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(win.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # Not presented: the window is mapped on "show" and unmapped on "hide".

        self.fd = sys.stdin.fileno()
        GLib.io_add_watch(self.fd, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self.on_stdin)

    # ---- input ----------------------------------------------------------
    def on_stdin(self, *_args) -> bool:
        # Raw reads, not readline(): a buffered reader would sit on the last lines of
        # a burst ("text ...", "state done") until the next write arrived.
        try:
            chunk = os.read(self.fd, 65536)
        except BlockingIOError:
            return True
        except OSError:
            chunk = b""
        if not chunk:                   # EOF: the daemon is gone
            self.app.quit()
            return False
        *complete, self.buf = (self.buf + chunk).split(b"\n")
        for raw in complete:
            self.handle(raw.decode(errors="replace"))
        return True

    def handle(self, line: str) -> None:
        msg = lines.parse(line)
        if msg is None:
            return
        if msg[0] == "level":
            self.on_level(msg[1], msg[2])
            return
        cmd, arg = msg
        if cmd == "show":
            self.show(arg)
        elif cmd == "hide":
            self.hide()
        elif cmd == "state" and arg in lines.STATES:
            self.mode, self.mode_t0, self.alpha = arg, time.monotonic(), 1.0
            if not self.visible:
                self.map()
        elif cmd == "text":
            self.text = arg
        elif cmd == "caption":
            self.caption = arg
        elif cmd == "palette":
            try:
                palette = json.loads(arg) if arg else {}
            except ValueError:
                return
            if isinstance(palette, dict):
                self.set_palette(palette)
                self.queue_draw()

    def on_level(self, rms: float, peak_db: float) -> None:
        if self.mode not in LISTENING:  # bars stay frozen once you let go
            return
        db = 20 * math.log10(min(max(rms, 1e-6), 1.0))
        now = time.monotonic()
        since = now - self.recent[-1][0] if self.recent else 0.0
        self.recent.append((now, db))
        level = lines.envelope(db, self.levels[-1], since)
        self.levels.append(level)
        self.hot.append(peak_db > -1.0)
        self.shown.append(lines.gain(level, *lines.gain_range(d for _, d in self.recent)))
        del self.levels[:-BARS], self.hot[:-BARS], self.shown[:-BARS]
        self.peak_db = peak_db

    def show(self, label: str) -> None:
        self.label = label
        self.reset()
        self.mode, self.mode_t0 = "listening", time.monotonic()
        self.map()

    def map(self) -> None:
        self.visible = True
        self.win.present()
        if not self.ticking:
            self.ticking = True
            GLib.timeout_add(TICK_MS, self.tick)

    def hide(self) -> None:
        self.visible = False
        self.win.set_visible(False)

    def queue_draw(self) -> None:
        if self.visible:
            self.area.queue_draw()

    def tick(self) -> bool:
        if not self.visible:
            self.ticking = False
            return False
        now = time.monotonic()
        self.phase += 0.15
        since = now - self.mode_t0
        if self.mode in LISTENING:
            while self.recent and now - self.recent[0][0] > lines.GAIN_WINDOW:
                self.recent.popleft()
            # The gain follows the last few seconds, so bars rescale as you get louder or quieter.
            floor, top = lines.gain_range(d for _, d in self.recent)
            self.shown = [lines.gain(db, floor, top) for db in self.levels]
        elif self.mode == "transcribing":
            # The waveform you just spoke settles into a low ridge for the shimmer to run along.
            self.shown = [s + (0.22 - s) * 0.08 for s in self.shown]
        alpha = lines.fade(self.mode, since)
        if alpha is None:
            self.hide()
            return self.tick()
        self.alpha = alpha
        self.area.queue_draw()
        return True

    # ---- drawing --------------------------------------------------------
    def draw(self, area, cr, width, height) -> None:
        cr.push_group()
        notice = self.mode in ("error", "nospeech")
        r = 18
        cr.new_sub_path()
        cr.arc(width - r, r, r, -math.pi / 2, 0)
        cr.arc(width - r, height - r, r, 0, math.pi / 2)
        cr.arc(r, height - r, r, math.pi / 2, math.pi)
        cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
        if notice:
            cr.set_source_rgba(*self.hot_c, 0.96)
        else:
            cr.set_source_rgba(*self.bg, 0.92 if self.dark else 0.96)
        cr.fill()

        band = H                    # the meter row; captions go below it
        mid = band / 2
        if notice:
            self.draw_notice(cr, width, height)
        elif self.mode == "done":
            self.draw_done(cr, width, mid)
        else:
            self.draw_meter(cr, width, band, mid)
            if self.captions:
                self.draw_caption(cr, width, band, height)
        cr.pop_group_to_source()
        cr.paint_with_alpha(max(0.0, min(1.0, self.alpha)))

    def draw_meter(self, cr, width, band, mid) -> None:
        thinking = self.mode == "transcribing"
        # Pulsing dot = recording: red when dictating, the accent in command and hands-free mode.
        # While transcribing it breathes slowly in the accent.
        if thinking:
            cr.set_source_rgba(*self.accent, 0.55 + 0.25 * math.sin(self.phase * 0.5))
        else:
            dot = self.hot_c if self.mode == "listening" else self.accent
            cr.set_source_rgba(*dot, 0.65 + 0.35 * math.sin(self.phase))
        cr.arc(24, mid, 6, 0, 2 * math.pi)
        cr.fill()

        # Mode label above the dot
        label = "transcribing…" if thinking else self.label
        if label:
            cr.set_source_rgb(*self.dim)
            cr.select_font_face("sans-serif")
            cr.set_font_size(9)
            cr.move_to(12, 15)
            cr.show_text(label)

        # Waveform bars (newest on the right); while transcribing, a shimmer sweeps left to right.
        x0, x1 = 44, width - (24 if thinking else 70)
        gap = (x1 - x0) / BARS
        bw = gap * 0.6
        sweep = ((time.monotonic() - self.mode_t0) % SHIMMER_PERIOD) / SHIMMER_PERIOD * (BARS + 8) - 4
        for i, v in enumerate(self.shown):
            c = self.hot_c if self.hot[i] and not thinking else self.accent
            alpha = 0.35 + 0.65 * (i + 1) / BARS
            if thinking:
                glow = math.exp(-(((i - sweep) / 2.5) ** 2))
                v = v + 0.35 * glow
                c = tuple(a + (b - a) * glow * 0.6 for a, b in zip(self.accent, self.fg, strict=True))
                alpha = 0.45 + 0.55 * glow
            h = max(3.0, min(1.0, v) * (band - 22))
            cr.set_source_rgba(c[0], c[1], c[2], alpha)
            x = x0 + i * gap + (gap - bw) / 2
            cr.rectangle(x, mid - h / 2, bw, h)
            cr.fill()

        # Peak dB readout
        if not thinking:
            cr.set_source_rgb(*self.dim)
            cr.select_font_face("monospace")
            cr.set_font_size(12)
            text = f"{self.peak_db:5.0f} dB" if self.peak_db > -89 else "  -- dB"
            cr.move_to(width - 66, mid + 4)
            cr.show_text(text)

    def draw_done(self, cr, width, mid) -> None:
        """A check and the first words that were typed."""
        cr.set_source_rgb(*self.accent)
        cr.set_line_width(2.6)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.move_to(17, mid + 0.5)
        cr.line_to(22, mid + 5.5)
        cr.line_to(31, mid - 5.5)
        cr.stroke()
        cr.set_source_rgb(*self.fg)
        cr.select_font_face("sans-serif")
        cr.set_font_size(14)
        text = lines.fit(" ".join(self.text.split()), width - 44 - 20,
                         lambda t: cr.text_extents(t).x_advance) or "typed"
        cr.move_to(44, mid + 5)
        cr.show_text(text)

    def draw_notice(self, cr, width, height) -> None:
        """'nothing heard' or 'error', centred on the red pill."""
        r, g, b = self.hot_c
        light = 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.6
        cr.set_source_rgb(*((0.1, 0.1, 0.12) if light else (1.0, 1.0, 1.0)))
        cr.select_font_face("sans-serif")
        cr.set_font_size(14)
        text = "nothing heard" if self.mode == "nospeech" else "error"
        ext = cr.text_extents(text)
        mid = (H if not self.captions else height) / 2
        cr.move_to((width - ext.x_advance) / 2, mid + ext.height / 2 - 1)
        cr.show_text(text)

    def draw_caption(self, cr, width, top, height) -> None:
        """The tail of the live transcript, two lines at most, newest words kept."""
        cr.select_font_face("sans-serif")
        cr.set_font_size(13)
        maxw = width - 36
        words = self.caption.split() if self.caption else []
        rows: list[str] = []
        line = ""
        for word in reversed(words):           # fill from the end
            trial = f"{word} {line}".strip()
            if cr.text_extents(trial).x_advance > maxw and line:
                rows.insert(0, line)
                if len(rows) == 2:
                    break
                line = word
            else:
                line = trial
        else:
            if line:
                rows.insert(0, line)
        rows = rows[-2:]
        if not rows:
            cr.set_source_rgb(*self.dim)
            rows = ["transcribing…" if self.mode == "transcribing" else "listening…"]
        else:
            cr.set_source_rgb(*self.fg)
        y = top + 6
        for text in rows:
            cr.move_to(18, y)
            cr.show_text(text)
            y += 18

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.app.quit())
        self.app.run([])


if __name__ == "__main__":
    os.environ.setdefault("GDK_BACKEND", "wayland")
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="")
    ap.add_argument("--captions", action="store_true")
    ap.add_argument("--palette", default="", help="theme colours as JSON (see overlay_proto.py)")
    a = ap.parse_args()
    try:
        theme = json.loads(a.palette) if a.palette else {}
    except ValueError:
        theme = {}
    Overlay(a.label, a.captions, theme if isinstance(theme, dict) else {}).run()
