from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from chttr import history


def test_stats_counts_words_speed_and_apps(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    now = datetime.now(UTC)
    rows = [
        {"ts": now.isoformat(), "seconds": 6, "app": "Slack", "text": "one two three four five six seven eight nine ten"},
        {"ts": (now - timedelta(days=1)).isoformat(), "seconds": 3, "app": "kitty", "raw": "git status"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    monkeypatch.setattr(history.C, "HISTORY_FILE", path)
    entries = history.load()
    assert len(entries) == 2
    out = history.stats(entries, days=3)
    assert "dictations   2" in out
    assert "words        12" in out
    assert "80 wpm" in out
    assert "Slack" in out and "kitty" in out


def test_stats_with_no_history():
    assert history.stats([]) == "no dictations in history yet"


def test_pick_gives_the_menu_an_icon_label_and_subtext(monkeypatch):
    from chttr import desktop

    shown = []

    def fake_menu(title, options):
        shown.extend(options)
        return desktop.match_option("\t".join(options[1].split("\t")[1:]), options)   # Omarchy drops the icon

    monkeypatch.setattr(desktop, "menu", fake_menu)
    entries = [{"ts": "2026-10-13T13:52:00+00:00", "app": "foot", "text": "first one", "output": "type"},
               {"ts": "2026-10-13T13:53:00+00:00", "app": "chromium", "text": "second one", "output": "type"}]
    assert history.pick(entries) == "first one"
    icon, label, sub = shown[0].split("\t")
    assert icon and label == "second one" and sub.startswith("#1 · ") and sub.endswith("chromium")


def _at(day: date, hour: int = 12) -> str:
    return datetime(day.year, day.month, day.day, hour).astimezone().isoformat()


def test_summary_numbers_per_day_and_top_apps():
    today = date(2026, 10, 18)
    entries = [
        {"ts": _at(today - timedelta(2)), "seconds": 6, "app": "Slack", "text": "one two three four five six"},
        {"ts": _at(today), "seconds": 3, "app": "kitty", "raw": "git status"},
        {"ts": _at(today), "seconds": 12, "app": "Slack", "text": "a much longer one with eight words here"},
        {"ts": "not a date", "seconds": 0, "app": "", "text": "undated"},
    ]
    s = history.summary(entries, days=3, today=today)
    assert (s["dictations"], s["words"], s["seconds"]) == (4, 17, 21.0)
    assert s["wpm"] == round(17 / (21 / 60))
    assert s["per_day"] == [{"date": "2026-10-16", "words": 6, "count": 1},
                            {"date": "2026-10-17", "words": 0, "count": 0},
                            {"date": "2026-10-18", "words": 10, "count": 2}]
    assert s["top_apps"] == [{"app": "Slack", "words": 14}, {"app": "kitty", "words": 2}, {"app": "unknown", "words": 1}]
    assert s["best_day"] == {"date": "2026-10-18", "words": 10}
    assert s["longest_dictation"] == {"seconds": 12.0, "words": 8, "text": "a much longer one with eight words here"}
    assert s["streak_days"] == 1                        # the gap on the 17th breaks it


def test_summary_streak_counts_back_from_today_or_yesterday():
    today = date(2026, 10, 18)
    days = [today - timedelta(n) for n in (1, 2, 3, 5, 6)]
    entries = [{"ts": _at(d), "seconds": 2, "text": "hello there"} for d in days]
    assert history.summary(entries, today=today)["streak_days"] == 3        # 15..17: today isn't over yet
    entries.append({"ts": _at(today), "seconds": 2, "text": "and today"})
    assert history.summary(entries, today=today)["streak_days"] == 4
    assert history.summary(entries, today=today + timedelta(2))["streak_days"] == 0


def test_summary_of_nothing():
    s = history.summary([], days=2, today=date(2026, 10, 18))
    assert s["dictations"] == s["words"] == s["wpm"] == s["streak_days"] == 0
    assert s["saved_minutes"] == 0 and s["best_day"] is None and s["longest_dictation"] is None
    assert [d["date"] for d in s["per_day"]] == ["2026-10-17", "2026-10-18"]


def test_saved_minutes_never_goes_negative():
    slow = [{"ts": datetime.now(UTC).isoformat(), "seconds": 30, "text": "um so yeah"}]   # 6 wpm
    assert history.summary(slow)["saved_minutes"] == 0
    fast = [{"ts": datetime.now(UTC).isoformat(), "seconds": 30, "text": " ".join(["word"] * 80)}]
    assert history.summary(fast)["saved_minutes"] == 1.5                                # 2 min typing - 0.5 min talking


def test_stats_says_no_time_saved_instead_of_a_negative_number():
    slow = [{"ts": datetime.now(UTC).isoformat(), "seconds": 30, "text": "um so yeah"}]
    out = history.stats(slow)
    assert "time saved   none yet (speaking was slower than typing)" in out
    assert "-" not in out.split("time saved", 1)[1].splitlines()[0]
    quick = [{"ts": datetime.now(UTC).isoformat(), "seconds": 3, "text": " ".join(["word"] * 10)}]
    assert "time saved   12 s" in history.stats(quick)                             # 15 s typing - 3 s talking


def test_entries_with_ids_number_the_lines():
    assert [e["id"] for e in history.entries_with_ids([{"text": "a"}, {"text": "b"}])] == ["0", "1"]


def test_word_diff_shows_cleanup_removing_fillers():
    assert history.word_diff("um so I think uh we ship it friday", "So I think we ship it Friday.") == [
        ("del", "um"), ("equal", "So I think"), ("del", "uh"), ("equal", "we ship it Friday.")]


def test_word_diff_substitution_and_identical_text():
    assert history.word_diff("send it to sora beth", "send it to Sohrab.") == [
        ("equal", "send it to"), ("del", "sora beth"), ("ins", "Sohrab.")]
    assert history.word_diff("same words here", "same words here") == [("equal", "same words here")]
    assert history.word_diff("new line please", "new line —") == [("equal", "new line"), ("del", "please"), ("ins", "—")]
    assert history.word_diff("", "typed only") == [("ins", "typed only")]
    assert history.word_diff("", "") == []
