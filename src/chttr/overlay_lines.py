"""The overlay's stdin protocol as the overlay reads it, and the parts of its
drawing that need no GTK (bar heights, timing, fitting text), so they can be tested.

Shared by the daemon (through overlay_proto.py) and overlay.py, which runs under
the system Python and loads this file by path, so it must import nothing but the
standard library and nothing from chttr.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable

COMMANDS = ("show", "hide", "state", "text", "caption", "palette")
STATES = ("listening", "command", "handsfree", "transcribing", "done", "error", "nospeech")

GAIN_WINDOW = 3.0           # seconds of levels the display gain follows
GAIN_MIN_DB = -45.0         # a mic quieter than this isn't amplified further: silence stays flat
FALL_DB_PER_S = 200.0       # how fast a bar falls back after a loud moment (rising is instant)
DONE_HOLD, FADE = 0.9, 0.2  # "done": seconds shown, then seconds fading out
NOTICE_HOLD = 1.5           # "nothing heard" / "error", fade included


def parse(line: str) -> tuple[str, str] | tuple[str, float, float] | None:
    """("level", rms, peak_db) for a level line, (command, arg) for a command,
    None for anything else."""
    head, _, arg = line.strip().partition(" ")
    if head in COMMANDS:
        return head, arg.strip()
    try:
        rms, peak_db = (float(x) for x in line.split())
    except ValueError:
        return None
    if not (math.isfinite(rms) and math.isfinite(peak_db)):
        return None
    return "level", rms, peak_db


def hex_to_rgb(color: str) -> tuple[float, float, float]:
    """A "#rrggbb" or "#rgb" colour as (r, g, b) in 0..1; ValueError for anything else."""
    h = color.strip().removeprefix("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        raise ValueError(f"not a colour: {color!r}")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def envelope(db: float, previous: float, dt: float) -> float:
    """The level the newest bar shows: the reading, or the previous bar falling off
    at FALL_DB_PER_S if that's higher, so a loud syllable trails away instead of
    dropping to nothing between readings."""
    return max(db, previous - FALL_DB_PER_S * min(max(dt, 0.0), 0.1))


def gain_range(dbs: Iterable[float]) -> tuple[float, float]:
    """(floor, top) in dB for the readings of the last few seconds. The top never
    drops below GAIN_MIN_DB, so a quiet mic still fills the bars while plain silence
    stays flat."""
    dbs = list(dbs) or [-90.0]
    return min(max(min(dbs), -70.0), -55.0), max(max(dbs), GAIN_MIN_DB) + 1.5


def gain(db: float, floor: float, top: float) -> float:
    """A reading as a bar height, 0..1."""
    return min(1.0, max(0.0, (db - floor) / (top - floor)))


def fade(mode: str, since: float) -> float | None:
    """The pill's opacity `since` seconds into `mode`; None once it should hide."""
    if mode == "done":
        hold, total = DONE_HOLD, DONE_HOLD + FADE
    elif mode in ("error", "nospeech"):
        hold, total = NOTICE_HOLD - FADE, NOTICE_HOLD
    else:
        return 1.0
    if since >= total:
        return None
    return 1.0 if since < hold else (total - since) / FADE


def fit(text: str, maxw: float, width: Callable[[str], float]) -> str:
    """`text`, cut at a word with an ellipsis if `width(text)` is over `maxw`."""
    if width(text) <= maxw:
        return text
    words = text.split()
    while words:
        words.pop()
        trial = " ".join(words) + "…"
        if words and width(trial) <= maxw:
            return trial
    while text and width(text + "…") > maxw:
        text = text[:-1]
    return text + "…" if text else ""
