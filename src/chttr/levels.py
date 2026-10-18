"""Speech levels that fit the room: a fixed dBFS threshold drops everything a quiet
laptop mic hears, so the threshold follows the measured noise floor instead."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

import numpy as np

FLOOR_MIN = -62.0       # never treat anything quieter than this as speech
MARGIN_DB = 10.0        # speech is at least this far above the noise floor
WINDOW_CHUNKS = 500     # 10 s of 20 ms chunks
FLOOR_PERCENTILE = 15   # the quietest 15% of chunks are the room, not you
EARLY_CHUNKS = 15       # under a second in, only the first 0.3 s (before you speak) count
SETTLED_CHUNKS = 50
SILENT = -90.0          # the floor before anything was measured


def noise_floor(levels_db: Sequence[float]) -> float:
    """The room's level in dBFS: the 15th percentile of the chunk levels (of the
    first 15 chunks while fewer than 50 are in). SILENT when there is nothing yet."""
    if len(levels_db) == 0:
        return SILENT
    if len(levels_db) < SETTLED_CHUNKS:
        levels_db = levels_db[:EARLY_CHUNKS]
    return float(np.percentile(np.asarray(levels_db, dtype=np.float64), FLOOR_PERCENTILE))


def effective_threshold(levels_db: Sequence[float], configured: float, margin: float = MARGIN_DB,
                        floor_min: float = FLOOR_MIN) -> float:
    """The speech threshold for these levels: `margin` dB above the noise floor, but
    only ever lower than `configured` and never below `floor_min`."""
    if len(levels_db) == 0:
        return configured
    return max(floor_min, min(configured, noise_floor(levels_db) + margin))


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

    def threshold(self, configured: float) -> float:
        return effective_threshold(list(self._levels), configured)
