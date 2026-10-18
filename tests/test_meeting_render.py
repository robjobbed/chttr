from __future__ import annotations

import importlib
import json
from dataclasses import asdict

import numpy as np
import pytest

from chttr import config as C
from chttr import diarize as D
from chttr.meeting import edit, store
from chttr.meeting.store import Speaker, Utterance
from chttr.meeting.styles import _relabel

render = importlib.import_module("chttr.meeting.render")   # the package re-exports a function of that name
STARTED = 1_760_000_000.0


@pytest.fixture
def cfg(tmp_path):
    c = C.Config()
    c.meeting.dir = str(tmp_path / "notes")
    return c


@pytest.fixture
def result() -> dict:
    speakers = {
        "Rob": Speaker("Rob", "Rob", "rob@x.com", "me", seconds=30),
        "Dana": Speaker("Dana", "Dana", "dana@x.com", "captions", clusters=[0], seconds=40),
        "Speaker 1": Speaker("Speaker 1", "Speaker 1", "", "unknown", clusters=[1], seconds=12),
    }
    utts = [
        Utterance(0, 3, "mic", "Thanks for joining.", speaker="Rob"),
        Utterance(4, 9, "them", "Happy to. Budget is fifty thousand.", 0, "Dana"),
        Utterance(10, 12, "them", "I can send the contract.", 1, "Speaker 1"),
    ]
    notes = {"style": "general", "style_name": "General", "title": "Pricing call",
             "sections": [{"title": "Summary", "kind": "bullets", "items": ["Speaker 1 will send the contract"]}],
             "actions_heading": "Action items",
             "action_items": [{"owner": "Speaker 1", "task": "Send the contract", "due": "Friday"}]}
    return {
        "meta": {"id": "m1", "started": STARTED, "code": "abc-defg-hij", "title": ""},
        "duration": 600.0,
        "event": {"title": "Pricing call", "start": "", "end": "", "meet_code": "abc-defg-hij", "description": "",
                  "attendees": [{"name": "Dana", "email": "dana@x.com", "organizer": True, "status": "ACCEPTED",
                                 "is_me": False}]},
        "speakers": {k: asdict(v) for k, v in speakers.items()},
        "utterances": [asdict(u) for u in utts],
        "notes": notes,
        "notes_by_style": {"general": notes},
    }


def test_render_writes_frontmatter_notes_transcript_and_speakers(tmp_path, cfg, result):
    md = render.render(tmp_path, result, cfg)
    text = md.read_text(encoding="utf-8")
    assert md.name.endswith("Pricing call.md")
    assert text.startswith("---\ntitle: \"Pricing call\"")
    assert 'meet: "https://meet.google.com/abc-defg-hij"' in text
    assert "## Summary\n\n- Speaker 1 will send the contract" in text
    assert "- [ ] **Speaker 1**: Send the contract *(due Friday)*" in text
    assert "**Dana** · 00:00:04" in text
    assert "Wrong or missing name?" in text


def test_render_reuses_the_same_file(tmp_path, cfg, result):
    first = render.render(tmp_path, result, cfg)
    result["markdown"] = str(first)
    again = render.render(tmp_path, result, cfg)
    assert again == first
    assert len(list(first.parent.iterdir())) == 1


def test_filename_strips_unsafe_characters():
    from datetime import datetime

    name = render._filename('Q3: plan / "budget"?', datetime(2026, 10, 11, 14, 0))
    assert name == "2026-10-11 1400 Q3 plan budget"


def test_relabel_renames_everywhere_including_notes(result):
    _relabel(result, "Speaker 1", "Ali Khan", "ali@x.com", "corrected", "by hand")
    assert "Ali Khan" in result["speakers"] and "Speaker 1" not in result["speakers"]
    assert result["utterances"][2]["speaker"] == "Ali Khan"
    assert result["notes"]["sections"][0]["items"] == ["Ali Khan will send the contract"]
    assert result["notes"]["action_items"][0]["owner"] == "Ali Khan"


def test_relabel_merges_into_an_existing_speaker(result):
    _relabel(result, "Speaker 1", "Dana", "", "corrected", "")
    assert set(result["speakers"]) == {"Rob", "Dana"}
    assert result["speakers"]["Dana"]["clusters"] == [0, 1]


def test_rename_updates_the_notes_and_remembers_the_voice(tmp_path, cfg, result, monkeypatch):
    directory = tmp_path / "meeting"
    directory.mkdir()
    store.write_json(directory / "result.json", result)
    np.savez(directory / "speakers.npz", **{"1": np.ones(8, dtype=np.float32) / np.sqrt(8)})
    remembered = {}
    monkeypatch.setattr(D, "load_voices", lambda: remembered)
    monkeypatch.setattr(D, "save_voices", lambda v: None)

    md = edit.rename(directory, "Speaker 1", "Ali", "ali@x.com", cfg)

    saved = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    assert "Ali" in saved["speakers"]
    assert "**Ali** · 00:00:10" in md.read_text(encoding="utf-8")
    assert "ali@x.com" in remembered


def test_rename_refuses_unknown_and_self(tmp_path, cfg, result):
    directory = tmp_path / "m"
    directory.mkdir()
    store.write_json(directory / "result.json", result)
    with pytest.raises(SystemExit):
        edit.rename(directory, "Nobody", "X", "", cfg)
    with pytest.raises(SystemExit):
        edit.rename(directory, "Rob", "X", "", cfg)


def test_resolve_finds_meetings_by_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "MEETINGS_DATA_DIR", tmp_path)
    for name in ("2026-10-11-140000-abc", "2026-10-12-090000"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "meta.json").write_text("{}")
    assert store.resolve(None).name == "2026-10-12-090000"
    assert store.resolve("2026-10-11").name == "2026-10-11-140000-abc"
    with pytest.raises(SystemExit):
        store.resolve("2026")


def test_render_shows_talk_time_as_bars_and_in_the_frontmatter(tmp_path, cfg, result):
    text = render.render(tmp_path, result, cfg).read_text(encoding="utf-8")
    assert 'talk_time:\n  "Rob": 37\n  "Dana": 49\n  "Speaker 1": 15\n' in text
    assert "- Dana <dana@x.com>: Meet captions, 0.7 min · 49% ▇▇▇▇▇░░░░░" in text
    assert "- Speaker 1: not identified, 0.2 min · 15% ▇░░░░░░░░░" in text


def test_render_marks_on_device_notes(tmp_path, cfg, result):
    from chttr.meeting import lite

    result["notes"] = lite.notes_lite(result)
    text = render.render(tmp_path, result, cfg).read_text(encoding="utf-8")
    assert "*On-device notes* · add a Claude key (chttr connect claude) or run Ollama for full notes" in text
    assert "switch with `chttr meeting style" not in text
    assert 'notes_style: "lite"' in text
    assert "## Numbers and dates" in text and "- [ ] **Speaker 1**: I can send the contract." in text


def test_notes_prompt_lists_each_speakers_share_of_the_talk(result):
    from chttr.meeting.styles import _notes_inputs

    _, _, speakers, _, _ = _notes_inputs(result)
    assert "- Dana: named by Meet's captions (49% of talk time)" in speakers.splitlines()
