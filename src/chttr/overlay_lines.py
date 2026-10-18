"""The overlay's stdin protocol, as the overlay reads it.

Shared by the daemon (through overlay_proto.py) and overlay.py, which runs under
the system Python and loads this file by path, so it must import nothing but the
standard library and nothing from chttr.
"""

from __future__ import annotations

import math

COMMANDS = ("show", "hide", "state", "text", "caption", "palette")
STATES = ("listening", "command", "handsfree", "transcribing", "done", "error", "nospeech")


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
