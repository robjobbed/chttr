from __future__ import annotations

import json
from dataclasses import asdict

import numpy as np
import pytest

from chttr import config as C
from chttr import diarize as D
from chttr import notes as N
from chttr.meeting import lite, pipeline, store
from chttr.meeting.store import Speaker, Utterance

STARTED = 1_760_000_000.0


def meeting(lines: list[tuple[str, str, float, str]], seconds: dict[str, float] | None = None,
            title: str = "Pricing call") -> dict:
    """A processed result from (speaker, channel, start, text) lines."""
    utts = [Utterance(t, t + 5, ch, text, speaker=who) for who, ch, t, text in lines]
    talk = seconds or {}
    sources = {"Rob": "me"}
    speakers = {who: asdict(Speaker(who, who, "", sources.get(who, "captions"), seconds=talk.get(who, 0.0)))
                for who in dict.fromkeys(w for w, _, _, _ in lines)}
    return {"meta": {"id": "m1", "started": STARTED, "title": ""}, "duration": 610.0,
            "event": {"title": title} if title else None, "speakers": speakers,
            "utterances": [asdict(u) for u in utts], "notes": {}, "notes_by_style": {}}


CALL = [
    ("Rob", "mic", 0, "Thanks for joining. Can you hear me?"),
    ("Dana", "them", 10, "Happy to. The budget is $50k for the pilot, and we need 20% of it by March 3rd."),
    ("Rob", "mic", 20, "Ship Friday?"),
    ("Dana", "them", 30, "Yes. I'll send the contract by Friday. Let me share my screen."),
    ("Ali", "them", 45, "what about onboarding for 200 users"),
    ("Rob", "mic", 60, "We'll have it done by end of week. I can't promise more than that."),
    ("Dana", "them", 75, "Great, talk tomorrow at 3pm."),
]


@pytest.fixture
def notes() -> dict:
    return lite.notes_lite(meeting(CALL, {"Rob": 30, "Dana": 45, "Ali": 10}))


def section(notes: dict, title: str) -> list[str]:
    return next(s["items"] for s in notes["sections"] if s["title"] == title)


def test_lite_notes_have_the_shape_of_claude_notes(notes):
    assert set(notes) == {"style", "style_name", "title", "sections", "actions_heading", "action_items",
                          "speakers", "extra"}
    assert notes["style"] == "lite" and notes["style_name"] == "On-device notes"
    assert notes["title"] == "Pricing call" and notes["speakers"] == [] and notes["extra"] == ""
    assert [s["title"] for s in notes["sections"]] == ["Summary", "Questions asked", "Numbers and dates", "Commitments"]
    assert all(set(s) == {"title", "kind", "items"} for s in notes["sections"])
    assert notes["actions_heading"] == "Action items"


def test_summary_says_who_spoke_most_and_how_it_started_and_ended(notes):
    (summary,) = section(notes, "Summary")
    assert summary.startswith("10 minutes with Dana and Ali; Dana spoke most (53%).")
    assert "It started with Rob: “Thanks for joining. Can you hear me?”" in summary
    assert summary.endswith("ended with Dana: “Great, talk tomorrow at 3pm.”")


def test_questions_are_found_by_question_mark_or_first_word(notes):
    assert section(notes, "Questions asked") == [
        "Rob (00:00:20): Ship Friday?",
        "Ali (00:00:45): what about onboarding for 200 users",       # no question mark from the recognizer
    ]                                                                # "Can you hear me?" is small talk


def test_numbers_and_dates_are_grouped_per_sentence_and_deduplicated(notes):
    items = section(notes, "Numbers and dates")
    assert items[0] == ("$50k, 20%, March 3rd · Dana (00:00:10): The budget is $50k for the pilot, "
                        "and we need 20% of it by March 3rd.")
    assert "Friday · Rob (00:00:20): Ship Friday?" in items
    assert not any(x.startswith("Friday · Dana") for x in items)    # the same day again
    assert any(x.startswith("200 users · Ali") for x in items)
    assert any(x.startswith("tomorrow, 3pm · Dana") for x in items)


def test_commitments_become_action_items_with_owner_and_due(notes):
    assert section(notes, "Commitments") == [
        "Dana (00:00:30): I'll send the contract by Friday.",
        "Rob (00:01:00): We'll have it done by end of week.",
    ]                                       # not "Let me share my screen", not "I can't promise ..."
    assert notes["action_items"] == [
        {"owner": "Dana", "task": "I'll send the contract by Friday.", "due": "Friday"},
        {"owner": "Rob", "task": "We'll have it done by end of week.", "due": "end of week"},
    ]


def test_lists_are_capped_keeping_the_strongest_items_in_time_order():
    lines = [("Dana", "them", float(i * 10), f"I will send report {i} by {'Friday' if i % 2 else 'later on'}.")
             for i in range(30)]
    out = lite.notes_lite(meeting(lines, {"Dana": 300}))
    commitments = section(out, "Commitments")
    assert len(commitments) == lite.MAX_COMMITMENTS == len(out["action_items"])
    assert all(a["due"] == "Friday" for a in out["action_items"])       # dated ones first
    starts = [int(c.split("(")[1][6:8]) + 60 * int(c.split("(")[1][3:5]) for c in commitments]
    assert starts == sorted(starts)


def test_sentences_of_one_turn_stay_in_the_order_they_were_said():
    out = lite.notes_lite(meeting([("Dana", "them", 0, "Let me check the numbers. I will send the long report tomorrow.")]))
    assert section(out, "Commitments") == ["Dana (00:00:00): Let me check the numbers.",
                                           "Dana (00:00:00): I will send the long report tomorrow."]
    assert [a["task"] for a in out["action_items"]] == ["Let me check the numbers.",
                                                         "I will send the long report tomorrow."]


@pytest.mark.parametrize("filler", ["I will be honest, it's fine.", "I can tell you it's a mess.",
                                    "I'm going to be out on Monday.", "Let me go back to the slide.",
                                    "I can imagine that was hard.", "I will say the demo went well.",
                                    "Let me start with the agenda."])
def test_filler_is_not_a_commitment(filler):
    out = lite.notes_lite(meeting([("Dana", "them", 0, filler)]))
    assert section(out, "Commitments") == [] and out["action_items"] == []


def test_talk_time_falls_back_to_utterance_lengths_and_handles_one_speaker():
    out = lite.notes_lite(meeting([("Dana", "them", 0, "Hello there everyone."), ("Dana", "them", 2, "Bye.")],
                                  title=""))
    assert out["title"] == "Meeting"
    assert section(out, "Summary") == ["10 minutes; only Dana spoke. It started with Dana: “Hello there everyone. Bye.”"]


def test_no_transcript_gives_a_plain_summary():
    out = lite.notes_lite(meeting([]))
    assert section(out, "Summary") == ["No speech was transcribed."]
    assert out["action_items"] == []


def test_long_turns_are_clipped_in_the_summary():
    out = lite.notes_lite(meeting([("Dana", "them", 0, "word " * 100), ("Rob", "mic", 600, "ok then")]))
    quoted = section(out, "Summary")[0].split("“")[1].split("”")[0]
    assert len(quoted) == 140 and quoted.endswith("…")


# ---- the pipeline without a key ----------------------------------------------

@pytest.fixture
def recorded(tmp_path, monkeypatch):
    """A recorded meeting directory, with the models and the recognizer faked."""
    monkeypatch.setattr(C, "MEETINGS_DATA_DIR", tmp_path / "meetings")
    directory = store.new_meeting_dir(STARTED, "")
    store.update_meta(directory, id=directory.name, started=STARTED, code="", title="Pricing call", source="manual")
    for name in ("mic", "them"):
        np.zeros(16000 * 90, dtype=np.int16).tofile(directory / f"{name}.raw")
    segments = iter([[(0.0, 4.0), (40.0, 44.0)], [(10.0, 30.0)]])            # mic, then them
    monkeypatch.setattr(D, "ensure_models", lambda: None)
    monkeypatch.setattr(D, "speech_segments", lambda data, threads=0: next(segments))
    monkeypatch.setattr(D, "load_voices", lambda: {})
    monkeypatch.setattr(D, "save_voices", lambda voices: None)
    texts = {0.0: "Thanks for joining.", 40.0: "I'll send the contract by Friday.",
             10.0: "The budget is $50k. Can we ship by March 3rd?"}

    def transcribe(utts, audio, cfg, names):
        for u in utts:
            u.text = texts[u.start]

    def diarize(them, audio, threads):
        for u in them:
            u.cluster = 0
        return {}

    monkeypatch.setattr(pipeline, "_transcribe", transcribe)
    monkeypatch.setattr(pipeline, "_diarize", diarize)
    cfg = C.Config()
    cfg.meeting.dir = str(tmp_path / "notes")
    cfg.meeting.calendar_file = str(tmp_path / "no-calendar")
    cfg.meeting.me = "Rob"
    return directory, cfg


def test_pipeline_without_a_key_writes_on_device_notes(recorded, monkeypatch):
    directory, cfg = recorded
    monkeypatch.setattr(N, "available", lambda cfg: False)
    md = pipeline.process(directory, cfg)
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    assert result["notes"]["style"] == "lite" and "error" not in result["notes"]
    assert section(result["notes"], "Questions asked") == ["Speaker 1 (00:00:10): Can we ship by March 3rd?"]
    assert result["notes"]["action_items"][0] == {"owner": "Rob", "task": "I'll send the contract by Friday.",
                                                  "due": "Friday"}
    text = md.read_text(encoding="utf-8")
    assert "*On-device notes* · add a Claude key (chttr connect claude) or run Ollama for full notes" in text
    assert "## Questions asked" in text and "- [ ] **Rob**: I'll send the contract by Friday. *(due Friday)*" in text
    assert "talk_time:\n" in text and "▇" in text
    assert not (directory / "mic.raw").exists()


def test_pipeline_falls_back_to_on_device_notes_when_the_model_fails(recorded, monkeypatch):
    directory, cfg = recorded
    monkeypatch.setattr(N, "available", lambda cfg: True)
    monkeypatch.setattr(pipeline, "choose_style", lambda result, cfg: "standup")   # e.g. from a style rule

    def fail(*a, **k):
        raise RuntimeError("Ollama is not answering at http://127.0.0.1:11434")

    monkeypatch.setattr(pipeline, "write_notes", fail)
    md = pipeline.process(directory, cfg)
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    assert result["notes"]["style"] == "lite" and result["notes"]["wanted"] == "standup"
    assert result["notes"]["error"] == "RuntimeError: Ollama is not answering at http://127.0.0.1:11434"
    text = md.read_text(encoding="utf-8")
    assert "> Notes could not be written: RuntimeError: Ollama is not answering at http://127.0.0.1:11434. " \
           f"Try again with `chttr meeting style standup --meeting {directory.name}`." in text
    assert "## Commitments" in text
    assert "*On-device notes*\n" in text and "add a Claude key" not in text   # they already have a model


def test_pipeline_without_summary_writes_no_notes(recorded, monkeypatch):
    directory, cfg = recorded
    monkeypatch.setattr(N, "available", lambda cfg: False)
    pipeline.process(directory, cfg, summary=False)
    assert json.loads((directory / "result.json").read_text(encoding="utf-8"))["notes"] == {}
