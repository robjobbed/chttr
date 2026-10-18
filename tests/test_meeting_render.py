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


def test_a_failed_custom_style_is_retried_with_its_prompt(tmp_path, cfg, result):
    from chttr.meeting import lite

    result["meta"].update(style="custom", style_extra='every "number" and who said it')
    result["notes"] = {**lite.notes_lite(result), "error": "RuntimeError: overloaded", "wanted": "custom"}
    text = render.render(tmp_path, result, cfg).read_text(encoding="utf-8")
    assert f"Try again with `chttr meeting style --prompt \"every 'number' and who said it\" --meeting {tmp_path.name}`." \
        in text
    assert "add a Claude key" not in text


def test_notes_prompt_lists_each_speakers_share_of_the_talk(result):
    from chttr.meeting.styles import _notes_inputs

    _, _, speakers, _, _ = _notes_inputs(result)
    assert "- Dana: named by Meet's captions (49% of talk time)" in speakers.splitlines()


def test_done_title_says_what_was_saved(tmp_path, result):
    directory = tmp_path / "m"
    directory.mkdir()
    store.write_json(directory / "result.json", result)
    assert edit.done_title(directory) == "Notes saved"
    result["notes"] = {**result["notes"], "style": "lite"}
    store.write_json(directory / "result.json", result)
    assert edit.done_title(directory) == "On-device notes saved"
    result["notes"] = {}
    store.write_json(directory / "result.json", result)
    assert edit.done_title(directory) == "Transcript saved"
    result["notes"] = {"error": "RuntimeError: overloaded"}
    store.write_json(directory / "result.json", result)
    assert edit.done_title(directory) == "Transcript saved"
    assert edit.done_title(None) == "Transcript saved"


def test_summary_line_counts_people_and_action_items(tmp_path, result):
    directory = tmp_path / "m"
    directory.mkdir()
    store.write_json(directory / "result.json", result)
    assert edit.summary_line(tmp_path / "x.md", directory) == "10 min · Rob, Dana, Speaker 1 · 1 action item"
    result["speakers"].update({n: {**result["speakers"]["Dana"], "label": n} for n in ("Ali", "Sam")})
    result["notes"]["action_items"] *= 3
    store.write_json(directory / "result.json", result)
    assert edit.summary_line(tmp_path / "x.md", directory) == "10 min · Rob, Dana, Speaker 1 (+2) · 3 action items"
    result["notes"] = {}
    store.write_json(directory / "result.json", result)
    assert edit.summary_line(tmp_path / "x.md", directory).endswith("(+2)")


def test_rename_forgets_the_voice_stored_under_the_wrong_name(tmp_path, cfg, result, monkeypatch):
    voices_file = tmp_path / "voices.json"
    monkeypatch.setattr(D.load_voices, "__defaults__", (voices_file,))
    monkeypatch.setattr(D.save_voices, "__defaults__", (voices_file,))
    voice = np.ones(8, dtype=np.float32) / np.sqrt(8)
    other = np.array([1, -1] * 4, dtype=np.float32) / np.sqrt(8)
    D.save_voices({"dana@x.com": D.Voice("dana@x.com", "Dana", "dana@x.com", voice, 3, "2026-10-01T10:00:00+00:00"),
                   "ali@x.com": D.Voice("ali@x.com", "Ali", "ali@x.com", other, 2, "2026-10-01T10:00:00+00:00")})
    directory = tmp_path / "meeting"
    directory.mkdir()
    store.write_json(directory / "result.json", result)
    np.savez(directory / "speakers.npz", **{"0": voice})

    edit.rename(directory, "Dana", "Sam", "", cfg)               # the captions named the wrong person

    voices = D.load_voices()
    assert "dana@x.com" not in voices and "sam" in voices and "ali@x.com" in voices
    assert float(voices["sam"].embedding @ voice) > 0.99


def test_rename_keeps_a_stored_voice_that_does_not_match(tmp_path, cfg, result, monkeypatch):
    voices_file = tmp_path / "voices.json"
    monkeypatch.setattr(D.load_voices, "__defaults__", (voices_file,))
    monkeypatch.setattr(D.save_voices, "__defaults__", (voices_file,))
    voice = np.ones(8, dtype=np.float32) / np.sqrt(8)
    other = np.array([1, -1] * 4, dtype=np.float32) / np.sqrt(8)
    D.save_voices({"dana@x.com": D.Voice("dana@x.com", "Dana", "dana@x.com", other, 9, "2026-10-01T10:00:00+00:00")})
    directory = tmp_path / "meeting"
    directory.mkdir()
    store.write_json(directory / "result.json", result)
    np.savez(directory / "speakers.npz", **{"0": voice})

    edit.rename(directory, "Dana", "Sam", "sam@x.com", cfg)

    assert set(D.load_voices()) == {"dana@x.com", "sam@x.com"}   # the real Dana sounds different: keep her


def test_rename_that_fixes_the_spelling_keeps_the_learned_voice(tmp_path, cfg, result, monkeypatch):
    voices_file = tmp_path / "voices.json"
    monkeypatch.setattr(D.load_voices, "__defaults__", (voices_file,))
    monkeypatch.setattr(D.save_voices, "__defaults__", (voices_file,))
    voice = np.ones(8, dtype=np.float32) / np.sqrt(8)
    D.save_voices({"dana@x.com": D.Voice("dana@x.com", "Dana", "dana@x.com", voice, 20, "2026-10-01T10:00:00+00:00")})
    directory = tmp_path / "meeting"
    directory.mkdir()
    store.write_json(directory / "result.json", result)
    np.savez(directory / "speakers.npz", **{"0": voice})

    edit.rename(directory, "Dana", "Dana Smith", "", cfg)        # only adds her surname

    voices = D.load_voices()
    assert set(voices) == {"dana@x.com"}
    assert voices["dana@x.com"].count == 21 and voices["dana@x.com"].name == "Dana Smith"
    assert store.read_json(directory / "result.json")["speakers"]["Dana Smith"]["email"] == "dana@x.com"


def test_rename_moves_a_voice_without_an_email_to_the_new_spelling(tmp_path, cfg, result, monkeypatch):
    voices_file = tmp_path / "voices.json"
    monkeypatch.setattr(D.load_voices, "__defaults__", (voices_file,))
    monkeypatch.setattr(D.save_voices, "__defaults__", (voices_file,))
    voice = np.ones(8, dtype=np.float32) / np.sqrt(8)
    result["speakers"]["Dana"]["email"] = ""
    D.save_voices({"dana": D.Voice("dana", "Dana", "", voice, 7, "2026-10-01T10:00:00+00:00")})
    directory = tmp_path / "meeting"
    directory.mkdir()
    store.write_json(directory / "result.json", result)
    np.savez(directory / "speakers.npz", **{"0": voice})

    edit.rename(directory, "Dana", "Danna", "", cfg)

    voices = D.load_voices()
    assert set(voices) == {"danna"} and voices["danna"].count == 8


def test_rename_to_someone_else_drops_the_wrong_address(tmp_path, cfg, result):
    directory = tmp_path / "meeting"
    directory.mkdir()
    store.write_json(directory / "result.json", result)
    edit.rename(directory, "Dana", "Sam", "", cfg)
    assert store.read_json(directory / "result.json")["speakers"]["Sam"]["email"] == ""


@pytest.mark.parametrize("old,new,same", [("Dana", "Dana Smith", True), ("Jon", "John", True),
                                          ("Chris", "Christopher", True), ("Dana", "Sam", False),
                                          ("Mark", "Mary", False), ("Steven", "Stephen", True), ("Ali", "Alex", False), ("Dana", "", False)])
def test_same_person(old, new, same):
    assert edit._same_person(old, new) is same


class Run:
    def __init__(self, stdout=""):
        self.calls: list[list[str]] = []
        self.stdout = stdout

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return type("Out", (), {"stdout": self.stdout, "returncode": 0})()


def test_notify_done_on_omarchy_opens_the_notes_on_click(tmp_path, result, monkeypatch):
    monkeypatch.setattr(C, "MEETINGS_DATA_DIR", tmp_path / "meetings")
    directory = tmp_path / "meetings" / "2026-10-11-140000"
    directory.mkdir(parents=True)
    md = tmp_path / "notes" / "2026-10-11 1400 Pricing call.md"
    store.update_meta(directory, id=directory.name, markdown=str(md))
    result["notes"]["style"] = "lite"
    store.write_json(directory / "result.json", result)
    run = Run()
    monkeypatch.setattr(edit.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(edit.subprocess, "run", run)

    edit.notify_done(md, "10 min · Rob, Dana", ["slack"], on_send=lambda d: None)

    assert run.calls == [["omarchy-notification-send", "--app-name", "chttr", "-g", edit.GLYPH, "-u", "normal",
                          "-t", "120000", "On-device notes saved: 2026-10-11 1400 Pricing call",
                          "10 min · Rob, Dana\nClick to open. Send it to Slack with: chttr send",
                          "--exec", "xdg-open", str(md)]]
    run.calls.clear()
    edit.notify_done(md, "10 min · Rob, Dana")
    assert run.calls[0][10] == "10 min · Rob, Dana\nClick to open."


def test_notify_done_elsewhere_waits_for_open_or_send(tmp_path, result, monkeypatch):
    directory = tmp_path / "m"
    directory.mkdir()
    store.write_json(directory / "result.json", result)
    run = Run(stdout="send:slack\n")
    sent = []
    monkeypatch.setattr(edit.shutil, "which", lambda name: None)
    monkeypatch.setattr(edit.subprocess, "run", run)

    edit.notify_done(tmp_path / "Pricing call.md", "10 min", ["slack"], on_send=sent.append, directory=directory)

    (cmd,) = run.calls
    assert cmd[0] == "notify-send" and "--wait" in cmd and "send:slack=Send to Slack" in cmd
    assert cmd[-2:] == ["Notes saved: Pricing call", "10 min"]
    assert sent == ["slack"]
