"""Talking to the waveform overlay: its stdin protocol and the Omarchy theme's colours.

One line per message:

    <rms> <peak_db>          a level reading (the waveform)
    show <label>             map the pill; "", "command" or "hands-free" above the dot
    hide                     unmap it (the process stays, ready for the next press)
    state <name>             listening | command | handsfree | transcribing | done | error | nospeech
    text <preview>           the words "done" shows next to its check
    caption <text>           the live transcript
    palette <json>           {"bg", "fg", "accent", "hot", "dim", "dark"}; {} for the default

The parser itself lives in overlay_lines.py, which overlay.py loads under the
system Python.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from .overlay_lines import COMMANDS, STATES, hex_to_rgb, parse

__all__ = ["COMMANDS", "STATES", "format", "hex_to_rgb", "omarchy_palette", "parse", "theme_colors_file"]


def format(cmd: str, arg: str = "") -> str:
    """One protocol line (without the newline)."""
    arg = arg.replace("\r", " ").replace("\n", " ")
    return f"{cmd} {arg}" if arg or cmd == "show" else cmd


def theme_colors_file() -> Path:
    """Where Omarchy keeps the active theme's colours."""
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "omarchy" / "current" / "theme" / "colors.toml"


def _hex(value: object) -> str:
    r, g, b = hex_to_rgb(str(value))
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _luminance(color: str) -> float:
    r, g, b = hex_to_rgb(color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _mix(a: str, b: str, t: float) -> str:
    ca, cb = hex_to_rgb(a), hex_to_rgb(b)
    return "#" + "".join(f"{round((x + (y - x) * t) * 255):02x}" for x, y in zip(ca, cb, strict=True))


def omarchy_palette(path: Path | None = None) -> dict | None:
    """The active Omarchy theme as overlay colours, or None (no theme, unreadable file,
    a colour that isn't one). Keys are resolved the way Omarchy's shell does: `accent`
    before `color4`, `background` before `color0` and so on."""
    try:
        data = tomllib.loads((path or theme_colors_file()).read_text(encoding="utf-8"))

        def pick(*keys: str) -> str | None:
            for key in keys:
                if data.get(key):
                    return _hex(data[key])
            return None

        bg, fg, accent = pick("background", "color0"), pick("foreground", "color7"), pick("accent", "color4")
        if not (bg and fg and accent):
            return None
        mode = str(data.get("mode") or data.get("theme_type") or "").strip().lower()
        dark = mode == "dark" if mode in ("dark", "light") else _luminance(bg) < 0.5
        return {"bg": bg, "fg": fg, "accent": accent,
                "hot": pick("red", "color1") or "#f24d4d",
                "dim": pick("muted", "color8") or _mix(fg, bg, 0.5),
                "dark": dark}
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None
