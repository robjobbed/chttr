from __future__ import annotations

import pytest

from chttr import levels as L


def room(floor: float, speech: float, quiet: int = 40, loud: int = 60) -> list[float]:
    """A recording: `quiet` chunks of room noise, then `loud` chunks of speech."""
    return [floor] * quiet + [speech] * loud


def test_quiet_speech_in_a_quiet_room_lowers_the_threshold():
    assert L.effective_threshold(room(-70, -44), -38) == pytest.approx(-60)


def test_a_noisy_room_keeps_the_configured_threshold():
    assert L.effective_threshold(room(-45, -30), -38) == -38      # -45 + 10 is above -38: only ever lowers


def test_a_loud_mic_in_a_quiet_room_keeps_its_pauses():
    # Lowering only goes as far as the speech needs: a TV or a keyboard at -50 dB is
    # still a pause for someone talking at -20 dB.
    assert L.effective_threshold([-70.0] * 40 + [-20.0] * 160, -38) == pytest.approx(-40)
    assert L.effective_threshold(room(-70, -30), -38) == pytest.approx(-50)
    assert L.effective_threshold(room(-70, -10), -38) == -38


def test_a_cough_does_not_undo_the_lowering():
    quiet = [-70.0] * 60 + [-46.0] * 150 + [-15.0] * 15 + [-46.0] * 50
    assert L.effective_threshold(quiet, -38) == pytest.approx(-60)


def test_speech_level_is_the_loud_part_even_when_it_is_short():
    word = [-65.0] * 90 + [-25.0] * 15 + [-65.0] * 95     # "hello" in 4 s of room
    assert L.speech_level(word) == pytest.approx(-25)
    assert L.speech_level([-45.0] * 100 + [-42.0] * 100) == pytest.approx(-42)   # nothing stands out
    assert L.speech_level([]) == L.SILENT


def test_no_levels_means_the_configured_threshold():
    assert L.effective_threshold([], -38) == -38


def test_never_below_floor_min():
    assert L.effective_threshold(room(-90, -50), -38) == L.FLOOR_MIN == -62
    assert L.effective_threshold(room(-90, -50), -38, floor_min=-70) == -70


def test_margin_is_configurable():
    assert L.effective_threshold(room(-55, -40), -38, margin=0) == pytest.approx(-55)


def test_early_on_only_the_first_chunks_count():
    # 0.6 s in: the first 0.3 s (before you speak) set the floor, not the speech since.
    early = [-70.0] * 15 + [-44.0] * 15
    assert L.noise_floor(early) == pytest.approx(-70)
    assert L.effective_threshold(early, -38) == pytest.approx(-60)


def test_noise_floor_is_a_low_percentile_not_the_minimum():
    levels = [-90.0] + [-60.0] * 99      # one dropout doesn't drag the floor down
    assert L.noise_floor(levels) == pytest.approx(-60)
    assert L.noise_floor([]) == L.SILENT


def test_tracker_follows_the_room():
    t = L.Tracker()
    assert t.count == 0 and t.floor == L.SILENT
    assert t.threshold(-38) == -38
    for _ in range(20):
        t.update(-70)
    for _ in range(40):
        t.update(-44)
    assert t.count == 60
    assert t.floor == pytest.approx(-70)
    assert t.speech == pytest.approx(-44)
    assert t.threshold(-38) == pytest.approx(-60)


def test_tracker_forgets_after_its_window():
    t = L.Tracker()
    for _ in range(L.WINDOW_CHUNKS):
        t.update(-70)
    for _ in range(L.WINDOW_CHUNKS):
        t.update(-45)                    # the room got louder (a fan came on)
    assert t.count == L.WINDOW_CHUNKS
    assert t.floor == pytest.approx(-45)
    assert t.threshold(-38) == -38
