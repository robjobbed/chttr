"""Delivering text: how it joins the text around the cursor, and how line breaks
reach chat apps. Every desktop tool is faked."""

from __future__ import annotations

import pytest

from chttr import output as O
from chttr.config import OutputCfg


@pytest.mark.parametrize(("before", "after", "text", "joined", "trailing"), [
    ("Hello", "", "world", " world", None),                    # right after a word: one space
    ("Done. ", "", "next", "Next", None),                      # a new sentence gets its capital
    ("Done.", "", "next", " Next", None),
    ("", "", "hi", "Hi", None),                                # an empty field
    ("Notes:\n", "", "buy milk", "Buy milk", None),            # a new line
    ("as we said", "", "Tomorrow works", " tomorrow works", None),   # mid-sentence: lower case
    ("Thanks, ", "", "We can meet", "we can meet", None),
    ("see (", "", "below", "below", None),                     # no space after an opening bracket
    ("say “", "", "hello", "hello", None),
    ("path/", "", "to", "to", None),
    ("Hello ", ", how are you", "Dear", "dear", False),        # punctuation after the cursor: no space
    ("Hello ", " there", "dear", "dear", False),               # a space after the cursor already
    ("I think ", " is great", "The new design.", "the new design", False),   # no full stop mid-sentence
    ("as we said, ", "", "Sohrab will call", "Sohrab will call", None),      # your words keep their capital
    ("as we said, ", "", "I can call", "I can call", None),
    ("as we said, ", "", "I'm in", "I'm in", None),
    ("as we said, ", "", "NASA called", "NASA called", None),
    ("as we said, ", "", "\"Soon\" works", "\"Soon\" works", None),
    ("", "", "iPhone notes", "iPhone notes", None),            # spelled lower case on purpose
    ("Hello", "", ", world", ", world", None),
])
def test_smart_join(before, after, text, joined, trailing):
    assert O.smart_join(before, after, text, ["Sohrab"]) == (joined, trailing)


def test_names_days_and_months_keep_their_capital():
    # only a common sentence opener ("The", "Tomorrow", "We") is lower-cased mid-sentence
    assert O.smart_join("as we said, ", "", "Priyanka will call") == ("Priyanka will call", None)
    assert O.smart_join("the meeting with", "", "Priyanka went well") == (" Priyanka went well", None)
    assert O.smart_join("let's meet on", "", "Monday at noon") == (" Monday at noon", None)
    assert O.smart_join("we ship in", "", "May.") == (" May.", None)
    assert O.smart_join("thanks to", "", "Will for the notes") == (" Will for the notes", None)
    assert O.smart_join("as we said, ", "", "Claude Code works", ["claude code"]) == ("Claude Code works", None)
    assert O.smart_join("as we said,", "", "The plan works") == (" the plan works", None)
    assert O.smart_join("I think", "", "We're fine") == (" we're fine", None)


def test_smart_join_in_code_only_fixes_spacing():
    assert O.smart_join("git commit -m", "", "Fix", code=True) == (" Fix", None)
    assert O.smart_join("self.", "", "name", code=True) == ("name", None)
    assert O.smart_join("Done. ", "", "next", code=True) == ("next", None)
    assert O.smart_join("$ ", "", "ls", code=True) == ("ls", None)


def test_smart_join_leaves_empty_text_alone():
    assert O.smart_join("Hello", "world", "") == ("", None)


class FakeDesktop:
    """Records what deliver() asked the desktop to do."""

    def __init__(self, monkeypatch, typing_works=True):
        self.typed: list[tuple[str, str]] = []
        self.pasted: list[str] = []
        self.clipboard: list[str] = []
        self.keys: list[str] = []
        self.typing_works = typing_works
        monkeypatch.setattr(O, "current_desktop", lambda: O.DT.Desktop("wayland", "hyprland"))
        monkeypatch.setattr(O.DT, "type_text", self.type_text)
        monkeypatch.setattr(O.DT, "send_shortcut", lambda combo, desk=None: self.keys.append(combo))
        monkeypatch.setattr(O, "paste_preserving_clipboard", lambda text, restore_after=0.6: self.pasted.append(text))
        monkeypatch.setattr(O, "copy_to_clipboard", self.clipboard.append)

    def type_text(self, text, desk=None, preferred="auto", newline="enter"):
        if not self.typing_works:
            raise FileNotFoundError("no typing tool")
        self.typed.append((text, newline))
        return "wtype"


def test_line_breaks_go_through_as_shift_enter(monkeypatch):
    fake = FakeDesktop(monkeypatch)
    assert O.deliver("first\nsecond", OutputCfg(), newline="shift+enter") == "type"
    assert fake.typed == [("first\nsecond ", "shift+enter")]


def test_multi_line_chat_text_is_never_pasted(monkeypatch):
    fake = FakeDesktop(monkeypatch)
    long = "a long paragraph\n" * 40
    assert O.deliver(long, OutputCfg(paste_over=100), newline="shift+enter") == "type"
    assert fake.pasted == [] and fake.typed
    fake = FakeDesktop(monkeypatch, typing_works=False)
    assert O.deliver("one\ntwo", OutputCfg(), newline="shift+enter") == "clipboard"   # not pasted either
    assert fake.pasted == [] and fake.clipboard == ["one\ntwo "]


def test_long_text_is_still_pasted_elsewhere(monkeypatch):
    fake = FakeDesktop(monkeypatch)
    assert O.deliver("x" * 500, OutputCfg()) == "paste"
    assert O.deliver("one line " * 60, OutputCfg(), newline="shift+enter") == "paste"   # no line break in it
    assert len(fake.pasted) == 2


def test_nothing_to_type(monkeypatch):
    fake = FakeDesktop(monkeypatch)
    assert O.deliver("", OutputCfg()) == "type"
    assert O.deliver("", OutputCfg(mode="clipboard")) == "clipboard"
    assert fake.typed == [] and fake.clipboard == []


def test_press_enter_and_delete_selection(monkeypatch):
    fake = FakeDesktop(monkeypatch)
    O.press_enter()
    O.delete_selection()
    assert fake.keys == ["return", "backspace"]
