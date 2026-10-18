"""The active Omarchy theme's colours, for `chttr ui`.

Omarchy writes the current theme's palette to colors.toml (`accent`, `background`,
`foreground`, the named colours and/or `color0`..`color15`). This turns it into the
CSS variables app.css uses; anything the theme doesn't define is left out, so the
page keeps its own default for it. No theme (not Omarchy, or an unreadable file):
None, and the page follows the browser's light/dark preference.
"""

from __future__ import annotations

import logging
import re
import tomllib
from pathlib import Path

log = logging.getLogger("chttr.theme")
OMARCHY_COLORS = Path.home() / ".local/state/omarchy/current/theme/colors.toml"
_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?$")
# named key first, then the terminal colour Omarchy themes always define
_FALLBACK = {"accent": "color4", "background": "color0", "foreground": "color7", "red": "color1",
             "muted": "color8", "green": "color2", "yellow": "color3", "blue": "color4", "magenta": "color5",
             "cyan": "color6", "orange": "color11"}
SOFT = 0.18                     # --accent-soft is the accent at this alpha
SPEAKERS = ("blue", "magenta", "cyan", "green", "orange", "red", "yellow", "accent")   # --c1 .. --c8


def _hex(value: object) -> str | None:
    """'#5b2bb5', '5B2BB5', '#abc' or '#5b2bb5ff' -> '#5b2bb5'; None for anything else."""
    m = _HEX.match(str(value or "").strip())
    if not m:
        return None
    digits = m.group(1).lower()
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return "#" + digits


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _rgba(color: str, alpha: float) -> str:
    r, g, b = _rgb(color)
    return f"rgba({r}, {g}, {b}, {alpha:g})"


def _mix(a: str, b: str, amount: float) -> str:
    """`amount` of colour b over colour a."""
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(_rgb(a), _rgb(b), strict=True))


def luminance(color: str) -> float:
    """WCAG relative luminance, 0 (black) .. 1 (white)."""
    def channel(c: int) -> float:
        x = c / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two colours, 1 .. 21."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _readable(color: str, backgrounds: list[str], text: str, ratio: float = 4.5) -> str:
    """`color`, nudged towards the text colour until small text in it reads on every one of `backgrounds`."""
    for step in range(11):
        mixed = _mix(color, text, step / 10)
        if all(contrast(mixed, b) >= ratio for b in backgrounds):
            return mixed
    return text


def load_theme(path: Path | None = None) -> dict | None:
    """{"mode": "light" | "dark", "vars": {"--bg": "#…", …}} from Omarchy's colors.toml, or None."""
    path = path or OMARCHY_COLORS
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        if not isinstance(exc, FileNotFoundError):
            log.debug("theme: can't read %s: %s", path, exc)
        return None

    def color(name: str) -> str | None:
        return _hex(data.get(name)) or (_hex(data.get(_FALLBACK[name])) if name in _FALLBACK else None)

    bg, fg, accent = color("background"), color("foreground"), color("accent")
    if not bg and not fg and not accent:
        return None
    mode = str(data.get("mode") or data.get("theme_type") or "").strip().lower()
    if mode not in ("light", "dark"):
        mode = "light" if bg and luminance(bg) >= 0.5 else "dark"

    out: dict[str, str] = {}
    if bg:
        out["--bg"] = bg
    # the list pane and cards sit a shade off the page: a little lighter in light mode,
    # a little towards the text colour in dark mode (where "lighter" would glare)
    panel = color("lighter_background") or (bg and _mix(bg, "#ffffff" if mode == "light" else (fg or bg),
                                                        0.55 if mode == "light" else 0.05))
    if panel:
        out["--panel"] = panel
    if fg:
        out["--text"] = fg
    muted = color("muted") or color("dark_foreground")
    if muted:
        # meta text stays >= 4.5:1 wherever it sits: the list, the page, and the active row or
        # selected tab (--accent-soft composited over either)
        backs = [b for b in (panel, bg) if b]
        if accent:
            backs += [_mix(b, accent, SOFT) for b in backs]
        out["--muted"] = _readable(muted, backs, fg) if backs and fg else muted
    line = color("dark_background") or (_mix(bg, fg, 0.12) if bg and fg else None)
    if line:
        out["--line"] = line
    if accent:
        out["--accent"] = accent
        out["--accent-soft"] = _rgba(accent, SOFT)
    if yellow := color("yellow"):
        out["--mark"] = _rgba(yellow, 0.35)
    if red := color("red"):
        out["--danger"] = red
    for i, name in enumerate(SPEAKERS, 1):
        if c := color(name):
            out[f"--c{i}"] = _readable(c, [bg], fg) if bg and fg else c       # speaker names are text too
    return {"mode": mode, "vars": out}
