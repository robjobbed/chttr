from __future__ import annotations

import json
from datetime import datetime

import pytest

from chttr import config as C
from chttr import library as L


def make_meeting(root, mid, started, title, lines, notes=None):
    d = root / mid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"id": mid, "started": started}), encoding="utf-8")
    result = {
        "meta": {"id": mid, "started": started, "title": title}, "duration": 1800.0, "event": {"title": title},
        "speakers": {s: {"seconds": 60, "source": "captions"} for s in {sp for sp, _, _ in lines}},
        "utterances": [{"start": t, "end": t + 3, "channel": "them", "speaker": sp, "text": text}
                       for sp, t, text in lines],
        "notes": notes or {}, "markdown": f"/notes/{mid}.md",
    }
    (d / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return d


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MEETINGS_DATA_DIR", tmp_path / "meetings")
    monkeypatch.setattr(C, "HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr(L, "DB_FILE", tmp_path / "library.sqlite")
    oct1 = datetime(2026, 10, 1, 14, 0).astimezone().timestamp()
    oct8 = datetime(2026, 10, 8, 14, 0).astimezone().timestamp()
    make_meeting(tmp_path / "meetings", "2026-10-01-140000", oct1, "Pricing review",
                 [("Dana", 10, "We agreed the budget is fifty thousand dollars."),
                  ("Ali", 40, "I will send the revised contract by Friday.")],
                 {"sections": [{"title": "Summary", "items": ["Budget set at $50k."]}],
                  "action_items": [{"owner": "Ali", "task": "Send the revised contract", "due": "Friday"}]})
    make_meeting(tmp_path / "meetings", "2026-10-08-140000", oct8, "Launch sync",
                 [("Dana", 5, "The launch moves to November."), ("Rob", 20, "Then the budget needs a second look.")])
    (tmp_path / "history.jsonl").write_text(json.dumps({"ts": "2026-10-09T10:00:00+00:00", "app": "Slack",
                                                        "text": "ping the vendor about the contract"}) + "\n",
                                            encoding="utf-8")
    return tmp_path


def test_fts_query_quotes_every_word():
    assert L.fts_query('budget AND "drop table" NEAR(x)') == '"budget" "and" "drop" "table" "near"'
    assert L.fts_query("a b", any_word=True) == ""
    assert L.fts_query("budget contract", any_word=True) == '"budget" OR "contract"'


def test_search_ranks_lines_and_notes(lib):
    hits = L.search("budget")
    kinds = {(h.kind, h.meeting_id) for h in hits}
    assert ("line", "2026-10-01-140000") in kinds and ("line", "2026-10-08-140000") in kinds
    line = next(h for h in hits if h.kind == "line" and h.speaker == "Dana")
    assert "**budget**" in line.snippet and line.title == "Pricing review" and line.start == 10


def test_search_filters_by_speaker_and_date(lib):
    assert {h.speaker for h in L.search("budget", speaker="rob")} == {"Rob"}
    assert {h.meeting_id for h in L.search("budget", after="2026-10-05")} == {"2026-10-08-140000"}
    assert {h.meeting_id for h in L.search("budget", before="2026-10-02")} == {"2026-10-01-140000"}


def test_search_falls_back_to_any_word_and_includes_dictations(lib):
    hits = L.search("contract unicorn", include_dictations=True)
    assert any(h.kind == "line" for h in hits)
    assert any(h.kind == "dictation" and h.title == "Slack" for h in hits)


def test_stemming_matches_word_forms(lib):
    assert L.search("agreeing")                          # porter stemmer: agreed / agreeing


def test_meetings_actions_and_context(lib):
    ms = L.meetings()
    assert [m["title"] for m in ms] == ["Launch sync", "Pricing review"]
    assert L.action_items(owner="ali")[0]["task"] == "Send the revised contract"
    ctx = L.context_around("2026-10-01-140000", 30, seconds=20)
    assert [c["speaker"] for c in ctx] == ["Dana", "Ali"]
    m = L.meeting("2026-10-01")
    assert m and m["title"] == "Pricing review" and len(m["transcript"]) == 2


def test_sync_is_incremental_and_forgets_deleted_meetings(lib):
    import shutil

    with L.connect() as conn:
        assert L.sync(conn) == 2
        assert L.sync(conn) == 0
        shutil.rmtree(lib / "meetings" / "2026-10-08-140000")
        L.sync(conn)
        assert [r["id"] for r in conn.execute("SELECT id FROM meetings")] == ["2026-10-01-140000"]
        assert not conn.execute("SELECT 1 FROM lines_fts WHERE meeting_id = '2026-10-08-140000'").fetchall()


def test_fts_query_prefix_matches_the_last_word_only_from_three_characters():
    assert L.fts_query("revised contr", prefix_last=True) == '"revised" "contr"*'
    assert L.fts_query("budget co", prefix_last=True) == '"budget" "co"'
    assert L.fts_query("budget contr", any_word=True, prefix_last=True) == '"budget" OR "contr"*'
    assert L.fts_query("", prefix_last=True) == ""


def test_prefix_search_finds_words_as_you_type(lib):
    assert {h.speaker for h in L.search("contr", prefix_last=True) if h.kind == "line"} == {"Ali"}
    assert not [h for h in L.search("conta", prefix_last=True) if h.kind == "line"]
    assert not L.search("contr")                              # without prefix_last a fragment is just a word


def test_parse_query_reads_filters_out_of_the_search_box():
    assert L.parse_query("budget speaker:dana after:2026-10-01") == ("budget", "dana", "2026-10-01", "")
    assert L.parse_query('from:"Dana Lee" before:2026-10-31 the plan') == ("the plan", "Dana Lee", "", "2026-10-31")
    assert L.parse_query("on:2026-10-08 launch") == ("launch", "", "2026-10-08", "2026-10-08")
    assert L.parse_query("after:2026-1 budget") == ("budget", "", "", "")   # half-typed dates are ignored
    assert L.parse_query("after:2026-02-30") == ("", "", "", "")
    assert L.parse_query("email:dana@x.com") == ("email:dana@x.com", "", "", "")
    words, _, after, before = L.parse_query("on:today")
    assert words == "" and after == before == datetime.now().astimezone().date().isoformat()


def test_search_parses_filters_when_none_are_given(lib):
    assert {h.speaker for h in L.search("budget speaker:rob")} == {"Rob"}
    assert {h.meeting_id for h in L.search("budget after:2026-10-05")} == {"2026-10-08-140000"}
    # explicit filters win; the text is then searched as it is
    assert {h.speaker for h in L.search("budget", speaker="dana")} == {"Dana"}


def test_filters_without_words_browse(lib):
    hits = L.search("speaker:dana")
    assert [(h.meeting_id, h.start) for h in hits] == [("2026-10-08-140000", 5.0), ("2026-10-01-140000", 10.0)]
    assert [h.title for h in L.search("on:2026-10-01")] == ["Pricing review"]
    assert L.search("") == []


def test_search_with_facets_counts_lines_per_meeting(lib):
    hits, facets = L.search_with_facets("budget")
    assert {f["meeting_id"]: f["count"] for f in facets} == {"2026-10-01-140000": 1, "2026-10-08-140000": 1}
    assert facets[0]["title"] in ("Pricing review", "Launch sync")
    hits, facets = L.search_with_facets("budget", meeting_id="2026-10-08-140000")
    assert {h.meeting_id for h in hits} == {"2026-10-08-140000"} and len(facets) == 2   # facets stay, to switch


def test_turns_join_one_speakers_lines_within_three_seconds():
    lines = [{"start": 0, "end": 2, "speaker": "Dana", "text": "Hi."},
             {"start": 3.5, "end": 5, "speaker": "Dana", "text": "Shall we start?"},     # 1.5 s gap: same turn
             {"start": 9, "end": 10, "speaker": "Dana", "text": "Hello?"},               # 4 s gap: new turn
             {"start": 10.5, "end": 12, "speaker": "Ali", "text": "Yes."},
             {"start": 12.2, "speaker": "Dana", "text": "Good."}]                        # no end: a moment
    turns = L.turns(lines)
    assert [(t["speaker"], t["start"], t["end"], t["text"]) for t in turns] == [
        ("Dana", 0, 5, "Hi. Shall we start?"), ("Dana", 9, 10, "Hello?"), ("Ali", 10.5, 12, "Yes."),
        ("Dana", 12.2, 12.2, "Good.")]
    assert [x["text"] for x in turns[0]["lines"]] == ["Hi.", "Shall we start?"]
    assert len(L.turns(lines, gap=10)) == 3 and L.turns([]) == []


def test_meeting_has_turns_and_talk_seconds(lib):
    m = L.meeting("2026-10-01-140000")
    assert [t["speaker"] for t in m["turns"]] == ["Dana", "Ali"]
    assert m["talk_seconds"] == {"Dana": 60.0, "Ali": 60.0}
    assert m["transcript"][0]["end"] == 13
