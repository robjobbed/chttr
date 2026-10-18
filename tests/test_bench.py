from __future__ import annotations

import json
import time
import wave

import numpy as np
import pytest

from chttr import bench
from chttr import streaming as S


def test_wer():
    assert bench.wer("Send it on Wednesday.", "send it on wednesday") == 0.0
    assert bench.wer("send it on wednesday", "send it on tuesday") == 0.25
    assert bench.wer("a b c", "") == 1.0
    assert bench.wer("", "") == 0.0


def tone_with_pauses() -> np.ndarray:
    t = np.arange(16000) / 16000
    speech = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    quiet = np.zeros(int(0.7 * 16000), dtype=np.float32)
    return np.concatenate([speech, quiet, speech, quiet, speech[:8000], quiet])


def test_release_point_is_just_after_the_last_word():
    levels = bench.chunk_levels(tone_with_pauses())
    end = bench.release_point(levels, -38)
    loud = [i for i, db in enumerate(levels) if db > -38]
    assert end == loud[-1] + 1 + int(0.2 * S.CHUNKS_PER_SECOND)


def test_stream_segments_close_at_the_pauses_before_release(monkeypatch):
    monkeypatch.setattr(S, "MIN_SPEECH", 0.6)
    levels = bench.chunk_levels(tone_with_pauses())
    end = bench.release_point(levels, -38)
    segments = bench.stream_segments(levels, -38, end)
    assert len(segments) == 2
    assert segments[0][0] == 0 and segments[1][0] == segments[0][1]


def test_speculate_point_is_a_quarter_second_into_the_last_pause():
    levels = bench.chunk_levels(tone_with_pauses())
    end = bench.release_point(levels, -38)
    loud = [i for i, db in enumerate(levels) if db > -38]
    assert bench.speculate_point(levels, -38, end) == loud[-1] + 1 + int(S.SPECULATE_PAUSE * S.CHUNKS_PER_SECOND)


def test_speculate_point_only_looks_at_speech_before_the_release():
    levels = [-20.0] * 50 + [-60.0] * 50 + [-20.0] * 50
    assert bench.speculate_point(levels, -38, 100) == 50 + int(S.SPECULATE_PAUSE * S.CHUNKS_PER_SECOND)


def test_speculate_point_without_a_pause_or_without_speech():
    speech = [-20.0] * 50
    assert bench.speculate_point(speech, -38, 50) > len(speech)    # the pause never got long enough
    assert bench.speculate_point([-60.0] * 50, -38, 50) == 50      # nothing to recognize ahead


class FakeTranscriber:
    delay = 0.0

    def __init__(self, cfg):
        self.engine = type("E", (), {"name": "fake"})()

    def transcribe(self, audio, prompt=None, trim=True):
        time.sleep(self.delay)
        return "hello world"


def test_run_and_report(tmp_path, monkeypatch):
    import chttr.transcriber

    monkeypatch.setattr(S, "MIN_SPEECH", 0.6)

    monkeypatch.setattr(chttr.transcriber, "Transcriber", FakeTranscriber)
    wav = tmp_path / "hello.wav"
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes((tone_with_pauses() * 32767).astype(np.int16).tobytes())
    (tmp_path / "hello.txt").write_text("Hello world")
    from chttr.config import Config

    load_ms, results = bench.run([wav], Config(), runs=2)
    assert results[0].wer == 0.0
    assert results[0].segments == 2
    assert len(results[0].full_ms) == 2
    text = bench.report("fake", load_ms, results)
    assert "hello.wav" in text and "0.0%" in text
    assert " release " in text and " tail " in text
    data = json.loads(bench.to_json("fake", load_ms, results))
    assert "rtf" in data["results"][0] and len(data["results"][0]["tail_ms"]) == 2
    md = bench.markdown("fake", load_ms, results)
    assert "| hello.wav |" in md and "Tail (no reuse)" in md


def test_run_counts_the_pause_as_a_head_start(tmp_path, monkeypatch):
    import chttr.transcriber

    monkeypatch.setattr(chttr.transcriber, "Transcriber", FakeTranscriber)
    monkeypatch.setattr(FakeTranscriber, "delay", 0.05)
    wav = tmp_path / "short.wav"
    t = np.arange(int(1.5 * 16000)) / 16000
    audio = np.concatenate([(0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), np.zeros(12000, np.float32)])
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes((audio * 32767).astype(np.int16).tobytes())
    from chttr.config import Config

    _, [res] = bench.run([wav], Config(), runs=2)
    assert res.segments == 0
    assert all(ms >= 45 for ms in res.tail_ms)          # recognized after the release
    assert res.release_ms == [0.0, 0.0]                  # done during the pause (a 50 ms pass, 210 ms head start)


def test_run_without_a_pause_before_the_end_has_nothing_to_reuse(tmp_path, monkeypatch):
    import chttr.transcriber

    monkeypatch.setattr(chttr.transcriber, "Transcriber", FakeTranscriber)
    wav = tmp_path / "abrupt.wav"
    t = np.arange(16000) / 16000
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(((0.3 * np.sin(2 * np.pi * 220 * t)) * 32767).astype(np.int16).tobytes())
    from chttr.config import Config

    _, [res] = bench.run([wav], Config(), runs=2)
    assert res.release_ms == res.tail_ms


@pytest.mark.model
def test_parakeet_accuracy_on_the_bench_recordings(fixtures):
    """Run with: uv run pytest -m model (needs the Parakeet model and tests/bench/*.wav)."""
    from chttr.config import Config

    files = sorted((fixtures.parent / "bench").glob("*.wav"))
    if not files:
        pytest.skip("no bench recordings (CI generates them with espeak-ng)")
    _, results = bench.run(files, Config(), runs=1)
    for r in results:
        assert r.wer is not None and r.wer < 0.25, (r.file, r.text, r.wer)
