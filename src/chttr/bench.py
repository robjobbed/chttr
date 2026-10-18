"""`chttr bench`: where the time goes, on your machine, for your audio.

For each WAV it measures
- full:    one recognition pass over the whole recording (what chttr did before 0.4)
- release: the wait after you let go in the pause after your last word (PAUSE
           seconds after it) with streaming recognition on: the pass that started
           SPECULATE_PAUSE into that pause has had the rest of it to run, so only
           what it has not finished by then is left (the same as tail when there
           is no such pass: no pause before the end, or the open segment is too long)
- tail:    the same recording without that reuse: the audio after the last
           closed segment, recognized after a quick release 0.2 s after the
           last word (what chttr 1.0 did)
- cleanup: one cleanup round trip, when a backend is configured (--cleanup)
and, when a `<name>.txt` transcript sits next to `<name>.wav`, the word error rate.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import levels as L
from . import streaming as S
from .config import Config
from .transcriber import SAMPLE_RATE, load_wav


@dataclass
class Result:
    file: str
    seconds: float
    full_ms: list[float] = field(default_factory=list)
    release_ms: list[float] = field(default_factory=list)
    tail_ms: list[float] = field(default_factory=list)
    cleanup_ms: list[float] = field(default_factory=list)
    segments: int = 0
    text: str = ""
    wer: float | None = None

    @property
    def rtf(self) -> float:
        return statistics.median(self.full_ms) / 1000 / self.seconds if self.full_ms and self.seconds else 0.0


def chunk_levels(audio: np.ndarray) -> list[float]:
    n = len(audio) // S.CHUNK_FRAMES
    frames = audio[: n * S.CHUNK_FRAMES].reshape(n, S.CHUNK_FRAMES) if n else np.zeros((0, S.CHUNK_FRAMES))
    rms = np.sqrt(np.mean(frames * frames, axis=1)) if n else np.zeros(0)
    return [float(20 * np.log10(max(r, 1e-6))) for r in rms]


def release_point(levels: list[float], threshold_db: float, after: float = 0.2) -> int:
    """Chunk index of a key release `after` seconds past the last speech."""
    loud = [i for i, db in enumerate(levels) if db > threshold_db]
    if not loud:
        return len(levels)
    return min(len(levels), loud[-1] + 1 + int(after * S.CHUNKS_PER_SECOND))


def speculate_point(levels: list[float], threshold_db: float, end: int) -> int:
    """Chunk where streaming recognition starts its speculative pass in the pause after
    the last speech before chunk `end`: once SPECULATE_PAUSE of it has been quiet.
    Past the end of `levels` when the recording stops before the pause gets that long;
    `end` when nothing before it is speech."""
    loud = [i for i, db in enumerate(levels[:end]) if db > threshold_db]
    if not loud:
        return end
    return loud[-1] + 1 + int(S.SPECULATE_PAUSE * S.CHUNKS_PER_SECOND)


def stream_segments(levels: list[float], threshold_db: float, end: int) -> list[tuple[int, int]]:
    """The segments streaming recognition closes before chunk `end`."""
    out, done = [], 0
    while True:
        cut = S.find_cut(levels[:end], threshold_db, done)
        if cut is None or cut <= done or cut >= end:
            return out
        out.append((done, cut))
        done = cut


def normalize(text: str) -> list[str]:
    return re.sub(r"[^\w\s']", " ", text.lower()).split()


def wer(reference: str, hypothesis: str) -> float:
    r, h = normalize(reference), normalize(hypothesis)
    if not r:
        return 0.0 if not h else 1.0
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[len(h)] / len(r)


def run(files: list[Path], cfg: Config, runs: int = 3, cleanup: bool = False) -> tuple[float, list[Result]]:
    from .transcriber import Transcriber

    t0 = time.monotonic()
    transcriber = Transcriber(cfg)
    load_ms = (time.monotonic() - t0) * 1000
    results = []
    for path in files:
        audio = load_wav(path)
        res = Result(path.name, len(audio) / SAMPLE_RATE)
        transcriber.transcribe(audio)                     # warm-up pass (first-run allocations)
        for _ in range(runs):
            t = time.monotonic()
            res.text = transcriber.transcribe(audio)
            res.full_ms.append((time.monotonic() - t) * 1000)

        levels = chunk_levels(audio)
        threshold = cfg.audio.speech_threshold_db       # the daemon fits it to the recording, so this does too
        if cfg.audio.adaptive_threshold:
            threshold = L.effective_threshold(levels, threshold)
        end = release_point(levels, threshold)
        segments = stream_segments(levels, threshold, end)
        res.segments = len(segments)
        start = segments[-1][1] if segments else 0
        tail = audio[start * S.CHUNK_FRAMES: end * S.CHUNK_FRAMES]
        for _ in range(runs):
            t = time.monotonic()
            transcriber.transcribe(tail)
            res.tail_ms.append((time.monotonic() - t) * 1000)

        # letting go in the pause: the speculative pass got a head start of the quiet
        # between its start (noticed half a poll late, on average) and the release
        spec = speculate_point(levels, threshold, end)
        release = release_point(levels, threshold, after=S.PAUSE)
        if start < spec <= release and spec - start <= S.SPECULATE_MAX * S.CHUNKS_PER_SECOND:
            head_start = ((release - spec) / S.CHUNKS_PER_SECOND - S.POLL / 2) * 1000
            guess = audio[start * S.CHUNK_FRAMES: spec * S.CHUNK_FRAMES]
            for _ in range(runs):
                t = time.monotonic()
                transcriber.transcribe(guess)
                res.release_ms.append(max(0.0, (time.monotonic() - t) * 1000 - head_start))
        else:                                             # no pause to work in: nothing to reuse
            res.release_ms = list(res.tail_ms)

        if cleanup and res.text:
            from . import polish as P

            if P.backend(cfg.polish) != "none":
                for _ in range(runs):
                    t = time.monotonic()
                    P.polish(res.text, cfg.polish)
                    res.cleanup_ms.append((time.monotonic() - t) * 1000)

        ref = path.with_suffix(".txt")
        if ref.exists():
            res.wer = wer(ref.read_text(encoding="utf-8"), res.text)
        results.append(res)
    return load_ms, results


def _ms(values: list[float]) -> str:
    return f"{statistics.median(values):6.0f}" if values else "     -"


def report(engine: str, load_ms: float, results: list[Result]) -> str:
    lines = [f"engine: {engine}   model load: {load_ms / 1000:.1f}s   (times are medians, in ms)", "",
             f"{'file':28} {'audio':>6} {'full':>6} {'release':>7} {'tail':>6} {'cleanup':>7} {'RTF':>6} {'WER':>6}"]
    for r in results:
        wer_s = f"{r.wer:6.1%}" if r.wer is not None else "     -"
        lines.append(f"{r.file[:28]:28} {r.seconds:5.1f}s {_ms(r.full_ms)} {_ms(r.release_ms):>7} "
                     f"{_ms(r.tail_ms)} {_ms(r.cleanup_ms):>7} {r.rtf:6.3f} {wer_s}")
    lines += ["", "release = letting go in the pause after the last word (reuses the pass from that pause); "
                  "tail = letting go right after it (no reuse)"]
    if len(results) > 1:
        rel = [statistics.median(r.release_ms) for r in results if r.release_ms]
        tail = [statistics.median(r.tail_ms) for r in results if r.tail_ms]
        full = [statistics.median(r.full_ms) for r in results if r.full_ms]
        lines += [f"after key release: {statistics.median(rel):.0f} ms with streaming "
                  f"({statistics.median(tail):.0f} ms without reuse) vs "
                  f"{statistics.median(full):.0f} ms for a full pass (median over files)"]
        wers = [r.wer for r in results if r.wer is not None]
        if wers:
            lines.append(f"word error rate:   {statistics.mean(wers):.1%} (mean)")
    return "\n".join(lines)


def to_json(engine: str, load_ms: float, results: list[Result]) -> str:
    return json.dumps({"engine": engine, "load_ms": round(load_ms),
                       "results": [{**asdict(r), "rtf": round(r.rtf, 4)} for r in results]}, indent=1)


def markdown(engine: str, load_ms: float, results: list[Result]) -> str:
    rows = ["| Recording | Length | Full pass | After release (streaming) | Tail (no reuse) | RTF | WER |",
            "|---|---:|---:|---:|---:|---:|---:|"]
    for r in results:
        rows.append(f"| {r.file} | {r.seconds:.1f} s | {statistics.median(r.full_ms):.0f} ms | "
                    f"{statistics.median(r.release_ms):.0f} ms | {statistics.median(r.tail_ms):.0f} ms | {r.rtf:.3f} | "
                    + (f"{r.wer:.1%}" if r.wer is not None else "-") + " |")
    return f"**{engine}**, model load {load_ms / 1000:.1f} s\n\n" + "\n".join(rows)
