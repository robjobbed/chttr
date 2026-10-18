from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from chttr import brief as B
from chttr import cli, gcal
from chttr import config as C


def make_meeting(root, mid, started, title, *, attendees=(), speakers=None, sections=(), actions=(), me="Rob"):
    d = root / mid
    d.mkdir(parents=True)
    ts = started.timestamp()
    (d / "meta.json").write_text(json.dumps({"id": mid, "started": ts}), encoding="utf-8")
    result = {
        "meta": {"id": mid, "started": ts, "me": me}, "duration": 1800.0,
        "event": {"title": title, "attendees": [{"name": n, "email": e, "is_me": e == "rob@example.com"}
                                                for n, e in attendees]} if attendees else None,
        "speakers": speakers or {me: {"name": me, "email": "", "source": "me", "seconds": 300}},
        "utterances": [],
        "notes": {"title": title, "sections": [{"title": t, "kind": "bullets", "items": list(items)}
                                               for t, items in sections],
                  "action_items": [{"owner": o, "task": t, "due": due} for o, t, due in actions]},
        "markdown": f"/notes/{mid}.md",
    }
    (d / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return d


@pytest.fixture
def weekly(fixtures):
    """'Weekly sync' from the fixture calendar: Dana (organizer), you, Ali (declined)."""
    events, _ = gcal._events((fixtures / "calendar.ics").read_bytes(),
                             datetime(2026, 10, 11, 21, 0, tzinfo=UTC), timedelta(hours=1))
    return next(e for e in events if e.title == "Weekly sync")


@pytest.fixture
def meetings(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MEETINGS_DATA_DIR", tmp_path / "meetings")
    root = tmp_path / "meetings"
    make_meeting(root, "2026-10-04-210000", datetime(2026, 10, 4, 21, 0, tzinfo=UTC), "Pricing review",
                 attendees=[("Dana Lee", "dana@example.com"), ("", "rob@example.com")],
                 speakers={"Rob": {"name": "Rob", "email": "rob@example.com", "source": "me"},
                           "Dana Lee": {"name": "Dana Lee", "email": "dana@example.com", "source": "calendar"}},
                 sections=[("Summary", ["Not discussed."]),
                           ("Decisions", ["Ship the pricing page before the roadmap review."])],
                 actions=[("Rob", "Send Dana the pricing sheet", "Friday"),
                          ("Dana Lee", "Share the Q4 roadmap draft", ""),
                          ("Sam Ortiz", "Book the venue", "")])
    make_meeting(root, "2026-10-09-150000", datetime(2026, 10, 9, 15, 0, tzinfo=UTC), "Coffee with Sam",
                 attendees=[("Sam Ortiz", "sam@example.com")],
                 sections=[("Summary", ["Sam wants an intro to the design team."])],
                 actions=[("Rob", "Intro Sam to the design team", "")])
    (root / "2026-10-10-090000").mkdir()      # still recording: no result.json yet
    (root / "2026-10-10-090000" / "meta.json").write_text("{}", encoding="utf-8")
    return root


NOW = datetime(2026, 10, 11, 20, 55, tzinfo=UTC)     # five minutes before the weekly sync


def test_past_meetings_newest_first_with_what_the_brief_needs(meetings):
    got = B.past_meetings()
    assert [m["id"] for m in got] == ["2026-10-09-150000", "2026-10-04-210000"]
    pricing = got[1]
    assert pricing["title"] == "Pricing review"
    assert pricing["attendee_emails"] == ["dana@example.com"]        # not you
    assert pricing["speaker_emails"] == ["dana@example.com"]
    assert pricing["summary"] == "Ship the pricing page before the roadmap review."   # skips "Not discussed."
    assert pricing["markdown"] == "/notes/2026-10-04-210000.md"
    assert pricing["me"] == ["Rob"]


def test_summary_is_cut_at_a_word():
    long = "word " * 60
    s = B._summary({"sections": [{"items": []}, {"items": [long]}]})
    assert len(s) <= B.SUMMARY_CHARS and s.endswith("word…")


def test_previous_meeting_by_attendee_overlap(meetings, weekly):
    # Dana is half of the other invitees (Ali declined but was invited): enough
    last = B.previous_meeting(weekly, B.past_meetings())
    assert last is not None and last["id"] == "2026-10-04-210000"


def test_previous_meeting_by_title_beats_overlap(meetings, weekly):
    make_meeting(meetings, "2026-09-27-210000", datetime(2026, 9, 27, 21, 0, tzinfo=UTC), "weekly  SYNC",
                 sections=[("Summary", ["Roadmap first."])])
    last = B.previous_meeting(weekly, B.past_meetings())
    assert last is not None and last["id"] == "2026-09-27-210000"


def test_no_previous_meeting_the_first_time(meetings, weekly):
    assert B.previous_meeting(weekly, []) is None
    only_sam = [m for m in B.past_meetings() if m["title"] == "Coffee with Sam"]
    assert B.previous_meeting(weekly, only_sam) is None


def test_your_own_email_is_not_overlap_when_the_feed_does_not_know_you(weekly):
    def meeting(mid, day, emails, me_emails=()):
        return {"id": mid, "started": datetime(2026, 10, day, tzinfo=UTC).timestamp(), "title": mid,
                "attendee_emails": emails, "speaker_emails": [], "me_emails": list(me_emails)}

    weekly.title = "Planning"
    weekly.attendees = [a for a in weekly.attendees if a.email != "ali@example.com"]
    weekly.attendees[1].is_me = False                     # a calendar named "Work", not your address
    lunch = meeting("lunch", 9, ["rob@example.com", "zed@example.com"])
    pricing = meeting("pricing", 4, ["dana@example.com"], me_emails=["rob@example.com"])
    assert B.previous_meeting(weekly, [lunch, pricing])["id"] == "pricing"


def test_a_recording_of_this_very_meeting_is_not_last_time(meetings, weekly):
    make_meeting(meetings, "2026-10-11-205800", datetime(2026, 10, 11, 20, 58, tzinfo=UTC), "Weekly sync")
    last = B.previous_meeting(weekly, B.past_meetings())
    assert last is not None and last["id"] == "2026-10-04-210000"


def test_open_items_tags_yours_and_theirs(meetings):
    items = B.open_items(B.past_meetings(), {"dana@example.com"}, {"dana"}, me={"rob"}, now=NOW)
    assert [(a["task"], a["yours"]) for a in items] == [
        ("Send Dana the pricing sheet", True),
        ("Share the Q4 roadmap draft", False),
    ]   # not Sam's venue, not your intro from the coffee with Sam


def test_open_items_window_and_carried_over_tasks(meetings):
    make_meeting(meetings, "2026-10-10-100000", datetime(2026, 10, 10, 10, 0, tzinfo=UTC), "Standup",
                 actions=[("Dana", "Share the Q4 roadmap draft!", ""), ("", "Someone should book a room", "")])
    make_meeting(meetings, "2026-07-01-100000", datetime(2026, 7, 1, 10, 0, tzinfo=UTC), "Old",
                 actions=[("Dana", "Something from July", "")])
    items = B.open_items(B.past_meetings(), set(), {"dana"}, now=NOW)
    assert [(a["task"], a["meeting"]) for a in items] == [("Share the Q4 roadmap draft!", "2026-10-10-100000")]
    longer = B.open_items(B.past_meetings(), set(), {"dana"}, since_days=120, now=NOW)
    assert [a["task"] for a in longer][-1] == "Something from July"


def test_open_items_cap_at_six(meetings):
    make_meeting(meetings, "2026-10-10-100000", datetime(2026, 10, 10, 10, 0, tzinfo=UTC), "Standup",
                 actions=[("Dana", f"Task number {i}", "") for i in range(9)])
    items = B.open_items(B.past_meetings(), set(), {"dana"}, now=NOW)
    assert len(items) == B.MAX_ITEMS == 6
    assert [a["task"] for a in items] == [f"Task number {i}" for i in range(6)]


def test_render_the_brief(meetings, weekly):
    brief = B.brief_for(weekly, B.past_meetings(), B.me_names("", weekly), now=NOW)
    assert brief.starts_in_min == 5 and brief.attendees == ["Dana Lee"]    # Ali declined
    title, body = B.render(brief, now=NOW)
    assert title == "Weekly sync in 5 min"
    assert body.splitlines() == [
        "Last met Oct 4: Ship the pricing page before the roadmap review.",
        "2 open items:",
        "• You: Send Dana the pricing sheet (due Friday)",
        "• Dana: Share the Q4 roadmap draft",
    ]
    _, cli_body = B.render(brief, cli=True, now=NOW)
    assert cli_body.splitlines()[-1] == "Open last notes: /notes/2026-10-04-210000.md"
    assert len(cli_body.splitlines()) <= 5


def test_render_first_time_and_times():
    b = B.Brief("Intro call", 0, ["Ana Diaz", "Bo Chen", "Cy Park"], None, [])
    assert B.render(b) == ("Intro call starting now", "First time with Ana, Bo and 1 other")
    assert B.render(B.Brief("Lunch", 75, [], None, []))[0] == "Lunch in 1 h 15 min"
    yesterday = datetime(2026, 10, 10, 15, 0, tzinfo=UTC).timestamp()
    last = {"started": yesterday, "summary": "", "markdown": ""}
    assert B.render(B.Brief("Sync", 3, ["Dana"], last, []), now=NOW)[1] == "Last met yesterday"


def test_me_names_and_worth_a_brief(weekly):
    assert B.me_names("Rob Behbahani", weekly) == {"rob"}
    assert B.worth_a_brief(weekly)
    weekly.attendees[1].status = "DECLINED"           # you declined
    assert not B.worth_a_brief(weekly)
    allday = gcal.Event("Holiday", weekly.start, weekly.start + timedelta(days=1))
    assert not B.worth_a_brief(allday)


# ---- chttr brief -------------------------------------------------------------
def test_cli_brief_needs_a_calendar(capsys, monkeypatch):
    monkeypatch.setattr(gcal, "read_urls", lambda path: [])
    assert cli.cmd_brief(cli.build_parser().parse_args(["brief"])) == 1
    assert "no calendar yet (chttr meeting calendar URL)" in capsys.readouterr().err


def test_cli_brief_prints_the_next_meeting_with_people(capsys, monkeypatch, meetings, weekly):
    soon = datetime.now(UTC) + timedelta(minutes=30)
    weekly.start, weekly.end = soon, soon + timedelta(minutes=30)
    solo = gcal.Event("Focus time", soon - timedelta(minutes=20), soon)
    monkeypatch.setattr(gcal, "read_urls", lambda path: ["https://calendar.google.com/x/basic.ics"])
    monkeypatch.setattr(gcal, "upcoming", lambda urls, hours=24: [solo, weekly])
    assert cli.cmd_brief(cli.build_parser().parse_args(["brief", "--next"])) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] in ("Weekly sync in 30 min", "Weekly sync in 29 min")
    assert out[1].startswith("Last met Oct 4: Ship the pricing page")
    assert out[-1] == "Open last notes: /notes/2026-10-04-210000.md"


def test_cli_brief_by_title(capsys, monkeypatch, meetings, weekly):
    monkeypatch.setattr(gcal, "read_urls", lambda path: ["https://calendar.google.com/x/basic.ics"])
    monkeypatch.setattr(gcal, "upcoming", lambda urls, hours=24: [weekly])
    assert cli.cmd_brief(cli.build_parser().parse_args(["brief", "standup"])) == 1
    assert "no meeting matching 'standup'" in capsys.readouterr().err
    assert cli.cmd_brief(cli.build_parser().parse_args(["brief", "WEEKLY"])) == 0
    assert capsys.readouterr().out.startswith("Weekly sync ")
