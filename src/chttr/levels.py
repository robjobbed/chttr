"""Speech levels that fit the room: a fixed dBFS threshold drops everything a quiet
laptop mic hears, so the threshold follows the measured noise floor instead."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

import numpy as np

Levels = Sequence[float] | np.ndarray     # chunk levels in dBFS: a list, a deque or an array

FLOOR_MIN = -62.0       # never treat anything quieter than this as speech
MARGIN_DB = 10.0        # speech is at least this far above the noise floor
WINDOW_CHUNKS = 500     # 10 s of 20 ms chunks
FLOOR_PERCENTILE = 15   # the quietest 15% of chunks are the room, not you
EARLY_CHUNKS = 15       # under a second in, only the first 0.3 s (before you speak) count
SETTLED_CHUNKS = 50
SPEECH_GAP_DB = 20.0    # anything this far under your speech is not speech either
SILENT = -90.0          # the floor before anything was measured


def noise_floor(levels_db: Levels) -> float:
    """The room's level in dBFS: the 15th percentile of the chunk levels (of the
    first 15 chunks while fewer than 50 are in). SILENT when there is nothing yet."""
    if len(levels_db) == 0:
        return SILENT
    if len(levels_db) < SETTLED_CHUNKS:
        levels_db = levels_db[:EARLY_CHUNKS]
    return float(np.percentile(np.asarray(levels_db, dtype=np.float64), FLOOR_PERCENTILE))


def speech_level(levels_db: Levels, floor: float | None = None) -> float:
    """How loud you speak, in dBFS: the median of the chunks clearly above the noise
    floor, so a short word in a long quiet recording still counts and a single cough
    does not. The 90th percentile of everything when no chunk stands out."""
    if len(levels_db) == 0:
        return SILENT
    db = np.asarray(levels_db, dtype=np.float64)
    loud = db[db > (noise_floor(levels_db) if floor is None else floor) + MARGIN_DB]
    return float(np.median(loud)) if len(loud) else float(np.percentile(db, 90))


def effective_threshold(levels_db: Levels, configured: float, margin: float = MARGIN_DB,
                        floor_min: float = FLOOR_MIN) -> float:
    """The speech threshold for these levels: `margin` dB above the noise floor, but
    only as low as your speech needs (SPEECH_GAP_DB under it), only ever lower than
    `configured` and never below `floor_min`. A loud mic in a quiet room keeps about
    the configured threshold, so a TV or a keyboard still counts as a pause."""
    if len(levels_db) == 0:
        return configured
    floor = noise_floor(levels_db)
    needed = max(floor + margin, speech_level(levels_db, floor) - SPEECH_GAP_DB)
    return max(floor_min, min(configured, needed))


class Tracker:
    """The levels of the recording in progress (the last 10 s), fed one 20 ms chunk
    at a time from the recorder thread."""

    def __init__(self) -> None:
        self._levels: deque[float] = deque(maxlen=WINDOW_CHUNKS)

    def update(self, db: float) -> None:
        self._levels.append(float(db))

    @property
    def count(self) -> int:
        return len(self._levels)

    @property
    def floor(self) -> float:
        return noise_floor(list(self._levels))

    @property
    def speech(self) -> float:
        return speech_level(list(self._levels))

    def threshold(self, configured: float) -> float:
        return effective_threshold(list(self._levels), configured)
