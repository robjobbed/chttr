from __future__ import annotations

import threading
import wave

import numpy as np
import pytest

from chttr import transcriber as T
from chttr.transcriber import SAMPLE_RATE, Transcriber, _clean, frame_levels, load_wav, trim_silence


def tone(seconds: float, amplitude: float = 0.3, hz: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


def test_trim_silence_cuts_both_ends_but_keeps_padding():
    audio = np.concatenate([silence(2.0), tone(1.0), silence(2.0)])
    out = trim_silence(audio, threshold_db=-40, pad=0.3)
    assert 1.5 <= len(out) / SAMPLE_RATE <= 1.7


def test_trim_silence_returns_empty_for_pure_silence():
    assert len(trim_silence(silence(1.0), threshold_db=-40)) == 0


def test_trim_silence_handles_tiny_input():
    tiny = tone(0.005)
    assert np.array_equal(trim_silence(tiny, -40), tiny)


@pytest.mark.parametrize("text", ["", "   ", "[BLANK_AUDIO]", "(music)", "*coughs*", "Thank you.", "you"])
def test_clean_drops_non_speech_and_whisper_hallucinations(text):
    assert _clean(text) == ""


def test_clean_normalizes_whitespace():
    assert _clean("  hello \n  world ") == "hello world"


def test_load_wav_downmixes_and_resamples(tmp_path):
    path = tmp_path / "stereo48k.wav"
    rate = 48000
    t = np.arange(rate) / rate
    left = (0.5 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    stereo = np.stack([left, left], axis=1).reshape(-1)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(stereo.tobytes())
    audio = load_wav(path)
    assert audio.dtype == np.float32
    assert len(audio) == SAMPLE_RATE
    assert 0.45 < float(np.abs(audio).max()) < 0.55


def test_frame_levels_are_dbfs_per_20ms_frame():
    db = frame_levels(np.concatenate([tone(0.1, amplitude=0.1 * np.sqrt(2)), silence(0.1)]))
    assert len(db) == 10
    assert db[:5] == pytest.approx([-20.0] * 5, abs=0.1)
    assert db[5:] == pytest.approx([-120.0] * 5)


# ---- quiet mics --------------------------------------------------------------
def noise(seconds: float, db: float = -70.0, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, 10 ** (db / 20), int(seconds * SAMPLE_RATE)).astype(np.float32)


def quiet_speech(rms_db: float, seconds: float = 2.0, pad: float = 0.5) -> np.ndarray:
    """A tone at `rms_db` RMS inside -70 dB room noise, with `pad` s of room either side."""
    voice = tone(seconds, amplitude=10 ** (rms_db / 20) * np.sqrt(2))
    return noise(pad + seconds + pad) + np.concatenate([silence(pad), voice, silence(pad)])


class FakeEngine:
    name = "fake"
    key = ("fake",)

    def __init__(self):
        self.heard: list[np.ndarray] = []

    def recognize(self, audio, vocabulary_prompt):
        self.heard.append(audio)
        return "something quiet"


def fake_transcriber(adaptive: bool = True, boost: bool = False, threshold_db: float = -38) -> Transcriber:
    t = Transcriber.__new__(Transcriber)
    t.lock = threading.Lock()
    t.engine = FakeEngine()
    t.threshold_db, t.boost, t.adaptive = threshold_db, boost, adaptive
    return t


@pytest.mark.parametrize("rms_db", [-40, -44, -50])
def test_quiet_speech_is_kept(rms_db):
    t = fake_transcriber()
    assert t.transcribe(quiet_speech(rms_db)) == "something quiet"
    assert 2.0 <= len(t.engine.heard[0]) / SAMPLE_RATE <= 2.7      # the speech, plus a little padding


@pytest.mark.parametrize("rms_db", [-40, -44, -50])
def test_without_adaptive_levels_quiet_speech_is_dropped_as_before(rms_db):
    t = fake_transcriber(adaptive=False)
    assert t.transcribe(quiet_speech(rms_db)) == ""
    assert t.engine.heard == []


def test_quiet_audio_is_boosted_before_it_is_trimmed(monkeypatch):
    seen = []
    real = T.trim_silence
    monkeypatch.setattr(T, "trim_silence", lambda audio, *a, **k: seen.append(float(np.abs(audio).max())) or real(audio, *a, **k))
    audio = quiet_speech(-44)
    raw_peak = float(np.abs(audio).max())
    t = fake_transcriber(boost=True)                  # whisper mode: up to 30x
    t.transcribe(audio)
    assert seen and seen[0] == pytest.approx(30 * raw_peak, rel=1e-4)
    assert float(np.abs(t.engine.heard[0]).max()) == pytest.approx(30 * raw_peak, rel=1e-4)


def test_boost_does_not_move_the_threshold():
    # 6x the gain is +15.6 dB; -45 dB fan noise must not become "speech" at -29 dB.
    t = fake_transcriber(adaptive=False)
    assert t.transcribe(noise(2.0, db=-45)) == ""
    assert t.transcribe(quiet_speech(-30)) == "something quiet"


def test_the_threshold_follows_the_floor_even_when_a_click_stops_the_boost():
    # A loud click keeps boost_quiet from raising anything, so only the lower
    # threshold saves the -50 dB speech that follows it.
    audio = quiet_speech(-50, pad=1.0)
    audio[100:200] = 0.5
    t = fake_transcriber()
    assert t.transcribe(audio) == "something quiet"
    kept = t.engine.heard[0]
    assert len(kept) / SAMPLE_RATE >= 3.0             # from the click through the end of the speech
    assert float(np.abs(kept).max()) == pytest.approx(0.5)


def test_room_noise_alone_is_still_no_speech():
    t = fake_transcriber()
    assert t.transcribe(noise(2.0)) == ""
    assert t.engine.heard == []


def test_a_noisy_room_keeps_the_configured_threshold():
    t = fake_transcriber()
    assert t.transcribe(noise(2.0, db=-45)) == ""     # -45 dB of fan noise is not speech


def test_trim_false_skips_trimming_but_still_boosts():
    t = fake_transcriber()
    audio = quiet_speech(-44)
    t.transcribe(audio, trim=False)
    assert len(t.engine.heard[0]) == len(audio)
    assert float(np.abs(t.engine.heard[0]).max()) > float(np.abs(audio).max()) * 5
