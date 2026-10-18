from __future__ import annotations

import pytest

from chttr import devmode as DM


@pytest.mark.parametrize(("spoken", "typed"), [
    ("rename it to camel case user id", "rename it to userId"),
    ("set snake case max retries to five", "set max_retries to five"),
    ("the kebab case main nav bar component", "the main-nav-bar-component"),
    ("class pascal case http client", "class HttpClient"),
    ("constant case api base url", "API_BASE_URL"),
    ("use camel case user id and snake case user name", "use userId and user_name"),
    ("camel case one two three four five", "oneTwoThreeFour five"),
    ("camel case user id, then save", "userId, then save"),
    ("nothing to change here", "nothing to change here"),
    ("say camel case.", "say camel case."),
    ("Rename it to camel-case user ID.", "Rename it to userId."),
])
def test_casing(spoken, typed):
    assert DM.apply_casing(spoken) == typed


@pytest.mark.parametrize(("spoken", "typed"), [
    ("look at main dot py", "look @main.py"),
    ("open at src slash app dot ts and fix it", "open @src/app.ts and fix it"),
    ("edit config dot toml", "edit config.toml"),
    ("the meeting is at noon dot com", "the meeting is at noon dot com"),     # not a code extension
    ("run it at five", "run it at five"),
    ("Look at main.py and at README.md.", "Look @main.py and @README.md."),
    ("meet at example.com", "meet at example.com"),
])
def test_files(spoken, typed):
    assert DM.apply_files(spoken) == typed


def test_file_references_only_in_code_apps():
    assert DM.transform("check at main dot py", code=True) == "check @main.py"
    assert DM.transform("check at main dot py", code=False) == "check at main dot py"
    assert DM.transform("camel case user id", code=False) == "userId"


def test_keep_list_collects_identifiers_for_the_model():
    original = "rename camel case user id in at main dot py"
    out = DM.transform(original, code=True)
    assert DM.keep_list(original, out) == ["userId", "@main.py"]


@pytest.mark.parametrize(("spoken", "typed"), [
    ("ls dash l", "ls -l"),
    ("git commit dash m fix the tests", "git commit -m fix the tests"),
    ("pytest dash dash verbose", "pytest --verbose"),
    ("grep dash dash ignore dash case todo", "grep --ignore-case todo"),     # a flag, not kebab case
    ("npm install dash-dash save-dev", "npm install --save-dev"),
    ("cat log pipe grep error", "cat log | grep error"),
    ("pipe the output to jq", "pipe the output to jq"),                        # the verb stays a word
    ("cd tilde slash work slash chttr", "cd ~/work/chttr"),
    ("run dot slash setup dot sh", "run ./setup.sh"),
    ("std double colon vector", "std::vector"),
    ("fn main open paren close paren arrow i32", "fn main() -> i32"),
    ("use the arrow keys", "use the arrow keys"),
    ("x fat arrow x", "x => x"),
    ("if open paren ready close paren open brace", "if (ready) {"),
    ("items open bracket zero close bracket", "items[zero]"),
    ("make ampersand ampersand make install", "make && make install"),
    ("run backtick npm test backtick first", "run `npm test` first"),
    ("a dash of salt", "a dash of salt"),
])
def test_symbols(spoken, typed):
    assert DM.transform(spoken, code=True) == typed


def test_symbols_only_in_code_apps():
    assert DM.transform("ls dash l pipe less", code=False) == "ls dash l pipe less"


@pytest.mark.parametrize(("spoken", "typed", "enter"), [
    ("git status, run it", "git status", True),
    ("git status run it", "git status", True),
    ("Git status, run it.", "Git status", True),
    ("make test - press enter", "make test", True),
    ("ls. Hit enter!", "ls", True),
    ("npm test and go", "npm test", True),
    ("run it", "", True),                                    # just Enter
    ("please run it again later", "please run it again later", False),   # mid-sentence: left alone
    ("fix the failing test and run it", "fix the failing test and run it", False),
    ("Priyanka will send it", "Priyanka will send it", False),
    ("git status", "git status", False),
])
def test_strip_enter(spoken, typed, enter):
    assert DM.strip_enter(spoken) == (typed, enter)
