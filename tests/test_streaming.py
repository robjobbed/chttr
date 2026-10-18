from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from chttr import streaming as S

LOUD, QUIET = -20.0, -60.0
THRESHOLD = -38.0


@pytest.fixture(autouse=True)
def short_segments(monkeypatch, request):
    """The cutting logic is easier to read with short segments; the default (4 s of
    speech per segment, for accuracy) has its own test below."""
    if "real_minimum" not in request.keywords:
        monkeypatch.setattr(S, "MIN_SPEECH", 0.6)


@pytest.mark.real_minimum
def test_default_minimum_keeps_short_sentences_together():
    lv = levels(("speech", 2.0), ("pause", 0.7), ("speech", 1.5), ("pause", 0.7), ("speech", 2.0), ("pause", 0.7))
    cut = S.find_cut(lv, THRESHOLD)
    assert cut is not None and cut > 2 * 50 + 35 + 1.5 * 50     # not after the first 2-second sentence


def levels(*parts: tuple[str, float]) -> list[float]:
    """levels(("speech", 1.0), ("pause", 0.6), ...) in 20 ms chunks."""
    out: list[float] = []
    for kind, seconds in parts:
        out += [LOUD if kind == "speech" else QUIET] * int(seconds * S.CHUNKS_PER_SECOND)
    return out


def test_no_cut_while_still_talking():
    assert S.find_cut(levels(("speech", 3.0)), THRESHOLD) is None


def test_cut_in_the_middle_of_the_first_long_pause():
    lv = levels(("speech", 1.0), ("pause", 0.6), ("speech", 1.0))
    cut = S.find_cut(lv, THRESHOLD)
    assert cut is not None and 50 < cut < 80


def test_short_breaths_do_not_cut():
    lv = levels(("speech", 1.0), ("pause", 0.2), ("speech", 1.0), ("pause", 0.2), ("speech", 0.5))
    assert S.find_cut(lv, THRESHOLD) is None


def test_a_click_before_a_pause_is_not_a_segment():
    lv = levels(("speech", 0.1), ("pause", 1.0))
    assert S.find_cut(lv, THRESHOLD) is None


def test_trailing_pause_closes_the_segment():
    cut = S.find_cut(levels(("speech", 1.5), ("pause", 0.5)), THRESHOLD)
    assert cut is not None and cut >= 75


def test_cut_respects_the_start_offset():
    lv = levels(("speech", 1.0), ("pause", 0.6), ("speech", 1.0), ("pause", 0.6))
    first = S.find_cut(lv, THRESHOLD)
    second = S.find_cut(lv, THRESHOLD, first)
    assert second is not None and second > first


def test_long_unbroken_speech_is_cut_at_the_quietest_moment():
    lv = levels(("speech", S.MAX_SEGMENT + 0.5))
    lv[-30] = -35.0              # still "speech", but the quietest chunk of the last two seconds
    assert S.find_cut(lv, THRESHOLD) == len(lv) - 30


class GrowingSource:
    """Feeds pre-planned levels over (fast) time, like the recorder."""

    def __init__(self, plan: list[float]):
        self.plan = plan
        self.levels_db: list[float] = []
        self.audio = np.zeros(len(plan) * S.CHUNK_FRAMES, dtype=np.float32)

    @property
    def chunk_count(self) -> int:
        return len(self.levels_db)

    def feed(self, chunks: int) -> None:
        self.levels_db.extend(self.plan[len(self.levels_db):len(self.levels_db) + chunks])

    def audio_range(self, start: int, end: int) -> np.ndarray:
        return self.audio[start * S.CHUNK_FRAMES:end * S.CHUNK_FRAMES]


def test_streamer_recognizes_segments_during_recording_and_only_the_tail_after(monkeypatch):
    monkeypatch.setattr(S, "POLL", 0.005)
    plan = levels(("speech", 1.0), ("pause", 0.6), ("speech", 1.2), ("pause", 0.6), ("speech", 0.8))
    source = GrowingSource(plan)
    calls: list[float] = []
    words = iter(["first part.", "second part.", "the tail."])

    def transcribe(audio: np.ndarray) -> str:
        calls.append(len(audio) / 16000)
        return next(words)

    captions: list[str] = []
    streamer = S.Streamer(source, transcribe, THRESHOLD, on_caption=None).start()
    while source.chunk_count < len(plan):
        source.feed(10)
        time.sleep(0.01)
    time.sleep(0.05)
    text = streamer.finish(source.audio)
    assert text == "first part. second part. the tail."
    assert len(calls) == 3
    assert calls[-1] < 1.2           # after release only the last stretch was recognized
    assert captions == []


def test_streamer_captions_show_closed_segments_plus_the_open_one(monkeypatch):
    monkeypatch.setattr(S, "POLL", 0.005)
    monkeypatch.setattr(S, "CAPTION_EVERY", 0.0)
    plan = levels(("speech", 1.0), ("pause", 0.6), ("speech", 1.5))
    source = GrowingSource(plan)
    lock = threading.Lock()
    captions: list[str] = []

    def transcribe(audio: np.ndarray) -> str:
        return f"w{len(audio) // S.CHUNK_FRAMES}"      # names each pass by its length

    def on_caption(text: str) -> None:
        with lock:
            captions.append(text)

    streamer = S.Streamer(source, transcribe, THRESHOLD, on_caption=on_caption).start()
    while source.chunk_count < len(plan):
        source.feed(5)
        time.sleep(0.01)
    time.sleep(0.05)
    streamer.abort()
    closed = streamer.segments[0]
    assert closed in captions                                         # the segment, once closed
    assert any(c.startswith(closed + " w") for c in captions)        # then closed + the open one


def test_finish_without_any_pause_is_one_pass_over_everything():
    plan = levels(("speech", 2.0))
    source = GrowingSource(plan)
    source.feed(len(plan))
    lengths = []
    streamer = S.Streamer(source, lambda a: lengths.append(len(a)) or "all of it", THRESHOLD)
    assert streamer.finish(source.audio) == "all of it"
    assert lengths == [len(source.audio)]


def wait_for(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


class Recognizer:
    """A fake transcribe() that records the length (in chunks) of every pass, and
    whether it ran inside finish()."""

    def __init__(self, *texts: str):
        self.texts = list(texts)
        self.calls: list[int] = []
        self.finishing = False
        self.during_finish: list[int] = []
        self.lock = threading.Lock()

    def __call__(self, audio: np.ndarray) -> str:
        with self.lock:
            chunks = len(audio) // S.CHUNK_FRAMES
            self.calls.append(chunks)
            if self.finishing:
                self.during_finish.append(chunks)
            return self.texts.pop(0) if self.texts else f"w{chunks}"

    def finish(self, streamer: S.Streamer, audio: np.ndarray) -> str:
        self.finishing = True
        return streamer.finish(audio)


@pytest.mark.real_minimum
def test_letting_go_in_a_pause_reuses_the_pass_from_that_pause(monkeypatch):
    monkeypatch.setattr(S, "POLL", 0.005)
    plan = levels(("speech", 1.5), ("pause", 0.5))
    source = GrowingSource(plan)
    source.feed(len(plan))
    rec = Recognizer("send it on wednesday")
    streamer = S.Streamer(source, rec, THRESHOLD).start()
    wait_for(lambda: rec.calls)
    time.sleep(0.05)
    assert rec.finish(streamer, source.audio) == "send it on wednesday"
    assert rec.calls == [len(plan)]          # one pass in total: the one in the pause
    assert rec.during_finish == []           # nothing left to do after the release
    assert streamer.tail_reused is True
    assert streamer.segments == []


@pytest.mark.real_minimum
def test_speech_after_the_pause_pass_is_recognized_again_once(monkeypatch):
    monkeypatch.setattr(S, "POLL", 0.005)
    plan = levels(("speech", 1.0), ("pause", 0.4), ("speech", 0.8))
    source = GrowingSource(plan)
    rec = Recognizer("send it", "send it on wednesday")
    streamer = S.Streamer(source, rec, THRESHOLD).start()
    source.feed(70)                          # up to the end of the pause
    wait_for(lambda: rec.calls)
    source.feed(len(plan))
    time.sleep(0.05)
    assert rec.finish(streamer, source.audio) == "send it on wednesday"
    assert rec.during_finish == [len(plan) - streamer.done]     # only audio[done:], once
    assert streamer.tail_reused is False


@pytest.mark.real_minimum
def test_the_pause_pass_runs_with_captions_off_and_shows_with_captions_on(monkeypatch):
    monkeypatch.setattr(S, "POLL", 0.005)
    plan = levels(("speech", 1.0), ("pause", 0.4))
    for captions in (None, []):
        source = GrowingSource(plan)
        source.feed(len(plan))
        rec = Recognizer("hello there")
        streamer = S.Streamer(source, rec, THRESHOLD,
                              on_caption=captions.append if captions is not None else None).start()
        wait_for(lambda s=streamer: s._partial is not None)
        streamer.abort()
        assert streamer._partial == (len(plan), "hello there")
        if captions is not None:
            assert captions == ["hello there"]


def test_a_long_pause_runs_one_pause_pass_not_one_per_poll(monkeypatch):
    monkeypatch.setattr(S, "POLL", 0.005)
    monkeypatch.setattr(S, "MIN_SPEECH", 4.0)
    plan = levels(("speech", 1.0), ("pause", 2.0))
    source = GrowingSource(plan)
    rec = Recognizer()
    streamer = S.Streamer(source, rec, THRESHOLD).start()
    while source.chunk_count < len(plan):
        source.feed(5)
        time.sleep(0.005)
    time.sleep(0.05)
    streamer.abort()
    assert len(rec.calls) == 1


def test_a_closed_segment_reuses_the_pause_pass_that_covers_it(monkeypatch):
    monkeypatch.setattr(S, "POLL", 0.005)
    plan = levels(("speech", 1.0), ("pause", 0.8))
    source = GrowingSource(plan)
    rec = Recognizer("first part.")
    streamer = S.Streamer(source, rec, THRESHOLD).start()
    source.feed(64)                          # 0.28 s into the pause: the pass runs, no cut yet
    wait_for(lambda: rec.calls)
    source.feed(len(plan))                   # now the pause is long enough to close the segment
    wait_for(lambda: streamer.segments)
    assert streamer.segments == ["first part."]
    assert rec.calls == [64]
    assert rec.finish(streamer, source.audio) == "first part."
    assert rec.during_finish == []           # nothing was said after the segment closed
    assert streamer.tail_reused is True


def test_caption_passes_recognize_at_most_the_last_six_seconds(monkeypatch):
    monkeypatch.setattr(S, "POLL", 0.005)
    monkeypatch.setattr(S, "CAPTION_EVERY", 0.0)
    plan = levels(("speech", 8.0))
    source = GrowingSource(plan)
    rec = Recognizer()
    captions: list[str] = []
    streamer = S.Streamer(source, rec, THRESHOLD, on_caption=captions.append).start()
    while source.chunk_count < len(plan):
        source.feed(10)
        n = source.chunk_count
        if n >= S.CAPTION_MIN * S.CHUNKS_PER_SECOND:          # let a caption pass see this much
            wait_for(lambda n=n: rec.calls and rec.calls[-1] >= min(n, S.CAPTION_WINDOW_CHUNKS) - 10)
    time.sleep(0.05)
    streamer.abort()
    assert streamer.segments == []
    assert max(rec.calls) == S.CAPTION_WINDOW_CHUNKS     # it got that far, and no further
    assert captions[-1] == f"w{S.CAPTION_WINDOW_CHUNKS}"


def test_a_new_threshold_applies_on_the_next_poll(monkeypatch):
    monkeypatch.setattr(S, "POLL", 0.005)
    plan = levels(("speech", 1.0), ("pause", 0.6), ("speech", 1.0))
    source = GrowingSource(plan)
    source.feed(len(plan))
    rec = Recognizer("first part.")
    streamer = S.Streamer(source, rec, QUIET - 10).start()     # everything sounds like speech
    time.sleep(0.05)
    assert rec.calls == [] and streamer.segments == []
    streamer.threshold_db = THRESHOLD
    wait_for(lambda: streamer.segments)
    streamer.abort()
    assert streamer.segments == ["first part."]


@pytest.mark.real_minimum
def test_a_new_recording_does_not_leak_into_the_last_one(monkeypatch):
    monkeypatch.setattr(S, "POLL", 0.005)
    plan = levels(("speech", 1.0), ("pause", 0.4))
    source = GrowingSource(plan)
    source.feed(len(plan))
    rec = Recognizer("the first one")
    streamer = S.Streamer(source, rec, THRESHOLD).start()
    wait_for(lambda: rec.calls)
    audio = source.audio.copy()
    source.levels_db = [LOUD] * 100          # the recorder starts the next recording
    time.sleep(0.05)
    assert rec.calls == [len(plan)]
    assert rec.finish(streamer, audio) == "the first one"
    assert streamer.tail_reused is True


def test_finish_parts_returns_the_segments_and_the_tail(monkeypatch):
    monkeypatch.setattr(S, "POLL", 0.005)
    plan = levels(("speech", 1.0), ("pause", 0.6), ("speech", 0.8))
    source = GrowingSource(plan)
    source.feed(len(plan))
    rec = Recognizer("first part.", "the tail.")
    streamer = S.Streamer(source, rec, THRESHOLD).start()
    wait_for(lambda: streamer.segments)
    assert streamer.finish_parts(source.audio) == (["first part."], "the tail.")


def test_finish_does_not_wait_forever_for_a_stuck_pass(monkeypatch):
    monkeypatch.setattr(S, "POLL", 0.005)
    plan = levels(("speech", 1.0), ("pause", 0.6), ("speech", 0.8))
    source = GrowingSource(plan)
    source.feed(len(plan))
    stuck, release = threading.Event(), threading.Event()

    def transcribe(audio: np.ndarray) -> str:
        if not stuck.is_set():
            stuck.set()
            release.wait(2)
            return "too late."
        return "all of it"

    streamer = S.Streamer(source, transcribe, THRESHOLD).start()
    stuck.wait(1)
    assert streamer.finish(source.audio, timeout=0.05) == "all of it"
    release.set()
    time.sleep(0.05)
    assert streamer.segments == []           # the late pass did not sneak in afterwards
