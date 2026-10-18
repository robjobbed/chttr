"""`chttr doctor --mic` with a fake recorder and recognizer: no PipeWire, no model."""

from __future__ import annotations

import numpy as np
import pytest

from chttr import cli, recorder, transcriber
from chttr import config as C


def fake_mic(monkeypatch, levels_db, peak_db, heard="hello there"):
    class Rec:
        def __init__(self, device="", on_level=None):
            self.levels_db: list[float] = []
            self.peak_db = -90.0

        def start(self):
            pass

        def stop(self):
            self.levels_db, self.peak_db = list(levels_db), peak_db
            return np.zeros(16000, dtype=np.float32), 1.0

    class Tr:
        def __init__(self, cfg):
            pass

        def transcribe(self, audio):
            return heard

    monkeypatch.setattr(recorder, "Recorder", Rec)
    monkeypatch.setattr(transcriber, "Transcriber", Tr)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)


def mic_line(capsys) -> str:
    return next(line for line in capsys.readouterr().out.splitlines() if "peak" in line)


def test_quiet_mic_is_usable(monkeypatch, capsys):
    fake_mic(monkeypatch, [-68.0] * 40 + [-46.0] * 160, -33.0)
    assert cli._mic_check(C.Config()) is True
    assert mic_line(capsys).endswith("peak -33 dB, speech -46 dB, floor -68 dB: quiet but usable (threshold lowered to -58 dB)")


def test_good_mic(monkeypatch, capsys):
    fake_mic(monkeypatch, [-60.0] * 40 + [-25.0] * 160, -8.0)
    assert cli._mic_check(C.Config()) is True
    assert mic_line(capsys).endswith("speech -25 dB, floor -60 dB: good")


def test_silent_mic(monkeypatch, capsys):
    fake_mic(monkeypatch, [-80.0] * 200, -70.0, heard="")
    assert cli._mic_check(C.Config()) is False
    assert "PROBLEM: silent" in mic_line(capsys)


def test_speech_buried_in_a_noisy_room(monkeypatch, capsys):
    fake_mic(monkeypatch, [-45.0] * 100 + [-42.0] * 100, -30.0, heard="")
    assert cli._mic_check(C.Config()) is False
    assert mic_line(capsys).endswith("too quiet: speech is under the -38 dB threshold; raise the input level or move closer")


def test_clipping(monkeypatch, capsys):
    fake_mic(monkeypatch, [-60.0] * 40 + [-10.0] * 160, -0.2)
    cli._mic_check(C.Config())
    assert mic_line(capsys).endswith("clipping; lower the input volume")


@pytest.mark.parametrize("adaptive, verdict", [(True, "quiet but usable"), (False, "too quiet")])
def test_verdict_follows_the_adaptive_setting(monkeypatch, capsys, adaptive, verdict):
    fake_mic(monkeypatch, [-68.0] * 40 + [-46.0] * 160, -33.0)
    cfg = C.Config()
    cfg.audio.adaptive_threshold = adaptive
    cli._mic_check(cfg)
    assert verdict in mic_line(capsys)
