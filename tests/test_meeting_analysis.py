from __future__ import annotations

from chttr.meeting.analysis import (
    _caption_votes,
    _fmt_ts,
    _intervals,
    _is_echo,
    _match_attendee,
    _norm,
    _turns,
)
from chttr.meeting.store import Utterance

T0 = 1_700_000_000.0


def test_intervals_cover_mutes_and_dictations():
    events = [
        {"type": "mute", "muted": True, "t": T0 + 10},
        {"type": "mute", "muted": False, "t": T0 + 25},
        {"type": "dictation", "start": T0 + 40, "end": T0 + 44},
        {"type": "mute", "muted": True, "t": T0 + 90},
    ]
    assert _intervals(events, T0) == [(10, 25), (40, 44), (90, float("inf"))]


def test_repeated_mute_events_do_not_restart_the_interval():
    events = [{"type": "mute", "muted": True, "t": T0 + 5}, {"type": "mute", "muted": True, "t": T0 + 8},
              {"type": "mute", "muted": False, "t": T0 + 9}]
    assert _intervals(events, T0) == [(5, 9)]


def test_echo_is_detected_by_word_overlap():
    them = [Utterance(10.0, 14.0, "them", "we should ship the release on friday")]
    echo = Utterance(10.2, 14.1, "mic", "should ship the release on friday")
    reply = Utterance(15.0, 17.0, "mic", "friday works for me let's do it")
    assert _is_echo(echo, them)
    assert not _is_echo(reply, them)


def test_a_lone_word_is_echo_only_when_they_said_it_at_that_moment():
    them = [Utterance(10.0, 14.0, "them", "yeah exactly and the budget is fine")]
    assert _is_echo(Utterance(10.1, 10.5, "mic", "yeah"), them)          # their "yeah" leaking back
    assert not _is_echo(Utterance(11.0, 11.4, "mic", "right"), them)     # you, agreeing while they talk
    assert not _is_echo(Utterance(30.0, 30.4, "mic", "yeah"), them)      # nobody else talking: a real turn
    assert _is_echo(Utterance(12.0, 12.3, "mic", "…"), them)             # no words while they talk: noise


def test_a_short_answer_right_after_a_question_is_kept():
    them = [Utterance(10.0, 11.2, "them", "Ship Friday?")]
    assert not _is_echo(Utterance(11.5, 11.9, "mic", "Yes."), them)
    assert not _is_echo(Utterance(11.4, 12.0, "mic", "Agreed."), them)
    assert not _is_echo(Utterance(11.6, 12.0, "mic", "Friday."), them)   # their word, but after they stopped


def test_caption_votes_respect_the_lag_window():
    them = [Utterance(10.0, 12.0, "them", "hi", cluster=0), Utterance(20.0, 22.0, "them", "yo", cluster=1)]
    events = [
        {"type": "speaking", "name": "Dana Lee", "t": T0 + 10.5},
        {"type": "speaking", "name": "Dana Lee", "t": T0 + 13.5},     # within the 1.8 s lag after the end
        {"type": "speaking", "name": "You", "t": T0 + 11.0},          # your own captions never vote
        {"type": "speaking", "name": "Ali", "t": T0 + 21.0},
        {"type": "speaking", "name": "Ali", "t": T0 + 30.0},          # too late for anyone
    ]
    per_cluster, per_utt = _caption_votes(them, events, T0, {"you"})
    assert per_utt[0] == {"Dana Lee": 2}
    assert per_cluster[1] == {"Ali": 1}


def test_match_attendee_by_full_name_first_name_or_email():
    attendees = [
        {"name": "Dana Lee", "email": "dana@acme.com"},
        {"name": "", "email": "ali.khan@acme.com"},
        {"name": "Sam Ortiz", "email": "sam@acme.com"},
        {"name": "Sam Park", "email": "spark@acme.com"},
    ]
    assert _match_attendee("Dana Lee", attendees)["email"] == "dana@acme.com"
    assert _match_attendee("dana", attendees)["email"] == "dana@acme.com"
    assert _match_attendee("Ali", attendees)["email"] == "ali.khan@acme.com"
    assert _match_attendee("Sam", attendees) is None              # two Sams: don't guess
    assert _match_attendee("", attendees) is None


def test_norm_strips_punctuation_and_case():
    assert _norm("  O'Brien,  JR. ") == "o brien jr"


def test_fmt_ts():
    assert _fmt_ts(0) == "00:00:00"
    assert _fmt_ts(3725.9) == "01:02:05"


def test_turns_join_consecutive_lines_of_one_speaker():
    utts = [
        Utterance(0, 2, "mic", "hello", speaker="Rob"),
        Utterance(2.5, 4, "mic", "how are you", speaker="Rob"),
        Utterance(5, 6, "them", "good", speaker="Dana"),
        Utterance(20, 21, "mic", "anyway", speaker="Rob"),
        Utterance(21.5, 22, "mic", "", speaker="Rob"),
    ]
    assert _turns(utts) == [("Rob", 0, "hello how are you"), ("Dana", 5, "good"), ("Rob", 20, "anyway")]


def test_pipeline_transcribe_step_runs(monkeypatch):
    """The per-utterance transcription step, with the recognizer faked (catches broken
    imports inside the function, which only run during a real meeting)."""
    import numpy as np

    import chttr.transcriber
    from chttr.meeting import pipeline

    class Engine:
        name = "parakeet"

        def recognize(self, audio, prompt):
            return f"words  {len(audio)}"

    monkeypatch.setattr(chttr.transcriber, "Transcriber", lambda cfg: type("T", (), {"engine": Engine()})())
    utts = [Utterance(0.0, 1.0, "mic"), Utterance(1.0, 1.5, "them")]
    audio = {"mic": np.zeros(32000, dtype=np.int16), "them": np.zeros(32000, dtype=np.int16)}
    from chttr.config import Config

    pipeline._transcribe(utts, audio, Config(), ["Dana"])
    assert [u.text for u in utts] == ["words 16000", "words 8000"]
