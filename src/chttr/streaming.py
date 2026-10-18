"""Recognize while you talk, so letting go of the key leaves almost nothing to do.

Every pause of PAUSE seconds or more closes a segment: the audio up to the middle
of the pause is transcribed in the background while you keep talking. When the
recording stops, only the audio after the last pause is left to recognize, and
the segments are joined. A dictation of any length therefore costs about one
second of recognition after release instead of a pass over the whole recording.

Most people let go of the key in the pause after their last word, so that pause
does the last bit of work too: once SPECULATE_PAUSE seconds have been quiet, the
open segment is recognized speculatively. If nothing louder than the speech
threshold comes after it, letting go reuses that pass and the text is ready at
once; if you keep talking it is simply recognized again. An open segment only
grows past SPECULATE_MAX when none of its pauses is long enough to close it; from
then on it gets no pause passes (letting go mid-word would wait for one that
long) and is recognized when it closes or at release, as before.

The same thread drives live captions: the closed segments plus a quick pass over
the last few seconds of the segment being spoken, so a long run of speech costs
the same per caption as a short one.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

import numpy as np

log = logging.getLogger("chttr.streaming")

CHUNKS_PER_SECOND = 50          # the recorder's 20 ms chunks
CHUNK_FRAMES = 320
PAUSE = 0.5                     # silence that ends a segment
MIN_SPEECH = 4.0                # a segment needs this much speech: shorter ones lose context and accuracy
MAX_SEGMENT = 20.0              # force a cut (at the quietest moment) in a long unbroken run
POLL = 0.1
CAPTION_EVERY = 0.7
CAPTION_MIN = 0.6               # show captions once this much audio is waiting
CAPTION_WINDOW_CHUNKS = 300     # a caption pass recognizes at most the last 6 s of the open segment
SPECULATE_PAUSE = 0.25          # quiet that triggers a speculative pass over the open segment
SPECULATE_MIN = 0.6             # new audio (with speech in it) needed since the last pass
SPECULATE_MAX = 6.0             # no pause pass over a longer open segment: letting go mid-word would wait for it


class Source(Protocol):
    levels_db: list[float]

    @property
    def chunk_count(self) -> int: ...

    def audio_range(self, start: int, end: int) -> np.ndarray: ...


def find_cut(levels: list[float], threshold_db: float, start: int = 0) -> int | None:
    """Where to close the segment that begins at chunk `start`: the middle of the first
    long-enough pause after enough speech, or the quietest chunk of the last two
    seconds when the segment has run too long. None while the segment is still open."""
    seg = levels[start:]
    loud = [i for i, db in enumerate(seg) if db > threshold_db]
    if not loud:
        return None
    pause = int(PAUSE * CHUNKS_PER_SECOND)
    min_speech = int(MIN_SPEECH * CHUNKS_PER_SECOND)
    first = loud[0]
    for a, b in zip(loud, [*loud[1:], len(seg)], strict=True):
        gap = b - a - 1                     # quiet chunks after chunk a (the last gap runs to "now")
        if gap >= pause and a - first + 1 >= min_speech:
            return start + a + 1 + gap // 2
    if len(seg) >= MAX_SEGMENT * CHUNKS_PER_SECOND:
        tail = 2 * CHUNKS_PER_SECOND
        window = seg[-tail:]
        return start + len(seg) - tail + int(np.argmin(window))
    return None


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def _loud(levels: list[float], threshold_db: float) -> bool:
    return any(db > threshold_db for db in levels)


class Streamer:
    def __init__(self, source: Source, transcribe: Callable[[np.ndarray], str], threshold_db: float,
                 on_caption: Callable[[str], None] | None = None) -> None:
        self.source = source
        self.transcribe = transcribe
        self.threshold_db = threshold_db    # read on every poll, so it can be adjusted while recording
        self.on_caption = on_caption
        self.done = 0                       # chunks already recognized
        self.segments: list[str] = []
        self.busy_seconds = 0.0             # recognition time spent while recording
        self.tail_reused = False            # finish() reused the pass from the last pause
        # (end chunk, text) of the open segment [done, end) as last recognized; after a
        # segment closes it is (done, ""): nothing has been said since
        self._partial: tuple[int, str] | None = None
        # this recording's levels: the recorder starts a fresh list for the next one
        self._levels = source.levels_db
        self._lock = threading.Lock()
        self._finished = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="streaming-asr", daemon=True)

    def start(self) -> Streamer:
        self._thread.start()
        return self

    @property
    def text(self) -> str:
        return _join(*self.segments)

    def _recognize(self, audio: np.ndarray) -> str:
        t0 = time.monotonic()
        try:
            return self.transcribe(audio)
        finally:
            self.busy_seconds += time.monotonic() - t0

    def _covered(self, levels: list[float], threshold_db: float, end: int) -> str | None:
        """The text of [done, end) if the last pass already has it: it ended within
        quiet audio of `end`, so the two differ only by silence."""
        if self._partial is None or self._partial[0] <= self.done:
            return None
        pass_end, text = self._partial
        if _loud(levels[min(pass_end, end):max(pass_end, end)], threshold_db):
            return None
        return text

    def _should_speculate(self, levels: list[float], threshold_db: float, n: int) -> bool:
        """The speaker has paused, and said something since the last pass."""
        quiet = int(SPECULATE_PAUSE * CHUNKS_PER_SECOND)
        since = self._partial[0] if self._partial else self.done
        return (n - since >= SPECULATE_MIN * CHUNKS_PER_SECOND
                and n - self.done <= SPECULATE_MAX * CHUNKS_PER_SECOND
                and not _loud(levels[n - quiet:n], threshold_db)
                and _loud(levels[since:n - quiet], threshold_db))

    def _audio(self, start: int, end: int) -> np.ndarray | None:
        """Chunks [start, end), or None when the recorder has already let go of them
        (it stopped, or started the next recording): this recording is over."""
        audio = self.source.audio_range(start, end)
        return audio if len(audio) >= (end - start) * CHUNK_FRAMES else None

    def _loop(self) -> None:
        last_caption = last_guess = float("-inf")
        captioned = -1                      # the end of the last pass of any kind
        while not self._stop.wait(POLL):
            try:
                if self.source.levels_db is not self._levels:
                    break                   # a new recording has started: this one is over
                n = self.source.chunk_count
                levels = self._levels[:n]
                threshold = self.threshold_db
                cut = find_cut(levels, threshold, self.done)
                if cut is not None and cut > self.done:
                    text = self._covered(levels, threshold, cut)
                    if text is None:
                        audio = self._audio(self.done, cut)
                        if audio is None:
                            break
                        text = self._recognize(audio)
                        if self._stop.is_set() and not text:
                            break
                    with self._lock:
                        if self._finished:
                            break
                        self.segments.append(text)
                        self.done = cut
                        self._partial = (cut, "")
                    self._caption(self.text)
                    captioned = n
                elif self._should_speculate(levels, threshold, n):
                    audio = self._audio(self.done, n)
                    if audio is None:
                        break               # never keep a pass over a cut-short recording as the text
                    last_guess = time.monotonic()
                    text = self._recognize(audio)
                    with self._lock:
                        if self._finished:
                            break
                        self._partial = (n, text)
                    if not self._stop.is_set():
                        self._caption(_join(self.text, text))
                    captioned = n
                elif (self.on_caption and n != captioned and n - self.done >= CAPTION_MIN * CHUNKS_PER_SECOND
                      and time.monotonic() - max(last_caption, last_guess) >= CAPTION_EVERY
                      and _loud(levels[max(self.done, captioned):n], threshold)):   # said something new
                    last_caption, captioned = time.monotonic(), n
                    start = max(self.done, n - CAPTION_WINDOW_CHUNKS)
                    partial = self._recognize(self.source.audio_range(start, n))
                    if not self._stop.is_set():
                        self._caption(_join(self.text, partial))
            except Exception as exc:  # noqa: BLE001 - the final pass covers whatever this missed
                log.debug("streaming pass failed: %s", exc)

    def _caption(self, text: str) -> None:
        if self.on_caption and text:
            try:
                self.on_caption(text)
            except Exception:  # noqa: BLE001 - captions are cosmetic
                pass

    def finish_parts(self, audio: np.ndarray, timeout: float = 15.0) -> tuple[list[str], str]:
        """Stop and return (the segments closed while recording, the text of the rest
        of `audio`, the whole recording). The rest is recognized now, unless the pass
        that ran in the last pause already covers it."""
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("streaming: a recognition pass is still running after %.0fs; finishing without it",
                            timeout)
        with self._lock:
            self._finished = True       # a pass still running can no longer change what we read here
            done, partial, segments = self.done, self._partial, [s for s in self.segments if s]
        n_total = len(audio) // CHUNK_FRAMES
        tail = audio[done * CHUNK_FRAMES:]
        self.tail_reused = False
        if partial is not None and partial[0] <= n_total <= len(self._levels):
            # nothing louder than the speech threshold since that pass: it has the rest
            self.tail_reused = not _loud(self._levels[partial[0]:n_total], self.threshold_db)
        if self.tail_reused and partial is not None:
            tail_text = partial[1]
            note = (" (reused the pass from your last pause)" if partial[0] > done
                    else " (nothing said after your last pause)")
        else:
            tail_text = self.transcribe(tail) if len(tail) else ""
            note = ""
        log.info("streaming: %d segment(s) during recording (%.2fs of recognition), tail %.1fs%s",
                 len(segments), self.busy_seconds, len(tail) / 16000, note)
        return segments, tail_text

    def finish(self, audio: np.ndarray, timeout: float = 15.0) -> str:
        """Stop, recognize what came after the last closed segment of `audio` (the
        whole recording) and return the full transcript."""
        segments, tail_text = self.finish_parts(audio, timeout)
        return _join(*segments, tail_text)

    def abort(self) -> None:
        self._stop.set()
