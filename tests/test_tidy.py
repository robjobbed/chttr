"""Local cleanup rules: what is typed when no model is available."""

from __future__ import annotations

import pytest

from chttr import tidy as T
from chttr.tidy import tidy

MEDIUM = [
    # the README example: fillers out, capital in, a closing period; the self-correction
    # ("Tuesday, no, Friday") is the model's job
    ("um so send the contract to Sorov by Tuesday, no, Friday",
     "So send the contract to Sorov by Tuesday, no, Friday."),
    ("um so send the contract comma then new line scratch that okay", "So send the contract, okay."),
    # spoken punctuation
    ("send it Friday period", "Send it Friday."),
    ("Send it Friday period.", "Send it Friday."),
    ("Send it Friday period Then call me.", "Send it Friday. Then call me."),
    ("Are you coming question mark", "Are you coming?"),
    ("Is it ready? Question mark", "Is it ready?"),
    ("That's great exclamation point", "That's great!"),
    ("Apples, comma, pears", "Apples, pears"),
    ("Dear John colon thanks for the notes", "Dear John: thanks for the notes."),
    ("Notes semicolon more notes", "Notes; more notes."),
    ("pros dash cons", "Pros - cons."),
    ("he said open quote hello close quote and left", 'He said "hello" and left.'),
    # the words themselves stay
    ("It took a period of time to load.", "It took a period of time to load."),
    ("During the trial period we pay nothing.", "During the trial period we pay nothing."),
    ("Send it Friday period then call me", "Send it Friday period then call me."),   # lowercase next: a noun
    ("Use the Oxford comma here.", "Use the Oxford comma here."),
    ("Add a comma after the name.", "Add a comma after the name."),
    ("We need a new line of products.", "We need a new line of products."),
    ("Use a dash here.", "Use a dash here."),
    # line breaks and lists
    ("first line new line second line new paragraph third", "First line\nSecond line\n\nThird"),
    ("Okay. New line. Next thing.", "Okay.\nNext thing."),
    ("bullet point apples bullet point pears", "- Apples\n- Pears"),
    ("Shopping list colon bullet point apples bullet point pears", "Shopping list:\n- Apples\n- Pears"),
    ("Line one new line line two scratch that line three", "Line one\nLine three"),
    # scratch that
    ("Send it Tuesday. Scratch that, send it Friday.", "Send it Friday."),
    ("Send it Tuesday. Delete that. Send it Friday.", "Send it Friday."),
    ("One, two, three, scratch that", "One, two"),
    ("scratch that", ""),
    ("Please delete that file.", "Please delete that file."),                       # a sentence, not a command
    # fillers and stutters
    ("Uh, the the report is done.", "The report is done."),
    ("I I I think so", "I think so."),
    ("I had had enough", "I had had enough."),
    ("It was, like, huge.", "It was huge."),
    ("I like it a lot", "I like it a lot."),
    ("You know, it works, you know.", "It works."),
    ("Do you know where it is?", "Do you know where it is?"),
    ("What I mean is simple.", "What I mean is simple."),
    ("Um.", ""),
    # capitals and spacing
    ("i think i'm right", "I think I'm right."),
    ("hello there. how are you", "Hello there. How are you."),
    ("I use an iPhone. iPhones are nice", "I use an iPhone. iPhones are nice."),
    ("check example.com now please", "Check example.com now please."),
    ("e.g. this works", "e.g. this works."),
    ("Meet at 5 p.m. tomorrow", "Meet at 5 p.m. tomorrow."),
    ("Wait , what ?", "Wait, what?"),
    ("Sounds good", "Sounds good"),                                                 # too short to need a period
]


@pytest.mark.parametrize(("raw", "clean"), MEDIUM)
def test_medium(raw, clean):
    assert tidy(raw) == clean


CODE = [
    ("git commit dash m fix the parser.", "git commit dash m fix the parser"),     # "dash m" is dev mode's
    ("Um, git status period.", "git status"),
    ("git status", "git status"),                                                   # your capitals, as spoken
    ("Git status.", "Git status"),
    ("Done. Ship it.", "Done. Ship it"),
    ("git add .", "git add ."),
    ("cd ..", "cd .."),
    ("Wait...", "Wait..."),
    ("open main.py", "open main.py"),
]


@pytest.mark.parametrize(("raw", "clean"), CODE)
def test_code_keeps_case_and_drops_the_trailing_period(raw, clean):
    assert tidy(raw, code=True) == clean


def test_chat_folds_line_breaks_into_sentences():
    assert tidy("first line new line second line new paragraph third", chat=True) == "First line. Second line. Third"
    assert tidy("Hi Dana, new line thanks for the notes.", chat=True) == "Hi Dana. Thanks for the notes."
    assert tidy("Hi Dana,\nthanks.", chat=True) == "Hi Dana, thanks."


def test_chat_keeps_a_dictated_list():
    assert tidy("Groceries: bullet point apples bullet point pears", chat=True) == "Groceries:\n- Apples\n- Pears"


def test_chat_adds_no_closing_period():
    assert tidy("sounds good to me", chat=True) == "Sounds good to me"


def test_light_keeps_spoken_commands():
    assert tidy("um the the plan comma okay", "light") == "The plan comma okay."
    assert tidy("send it Friday scratch that", "light") == "Send it Friday scratch that."


def test_none_is_unchanged():
    for raw, _ in MEDIUM:
        assert tidy(raw, "none") == raw
    assert tidy("um the plan.", "none", code=True) == "um the plan."


def test_empty_text():
    assert tidy("") == ""
    assert tidy("   ") == ""


@pytest.mark.parametrize("raw", [r for r, _ in MEDIUM + CODE] + ["a b c. d e f? g h i!", "x ... y", "\n\nnew line"])
@pytest.mark.parametrize("kw", [{}, {"code": True}, {"chat": True}, {"level": "light"}])
def test_idempotent(raw, kw):
    once = tidy(raw, **kw)
    assert tidy(once, **kw) == once


def test_for_app_is_the_safety_net_after_the_model():
    assert T.for_app("git status.", code=True) == "git status"
    assert T.for_app("Ready?", code=True) == "Ready?"
    assert T.for_app("send it. Thanks.", code=False) == "send it. Thanks."         # nothing else changes
    assert T.for_app("Hey\nAre you free", chat=True) == "Hey. Are you free"


@pytest.mark.parametrize(("text", "needs"), [
    ("git status", False), ("Sounds good.", False), ("um ship it", True), ("yes comma do it", True),
    ("the the plan", True), ("insert my email", True),
])
def test_needs_model(text, needs):
    assert T.needs_model(text) is needs
