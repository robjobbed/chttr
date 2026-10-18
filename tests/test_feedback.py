"""Notifications you answer with a click: Omarchy's --exec toasts and libnotify actions
elsewhere. Fake runners only: nothing is shown and nothing is run."""

from __future__ import annotations

import subprocess

import pytest

from chttr import feedback as F
from chttr.config import FeedbackCfg

RECORD = ("Record", ["chttr", "meeting", "start"])


class Runner:
    """Stands in for subprocess.run: records argv, answers like notify-send would."""

    def __init__(self, stdout="", returncode=0, raises=None):
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.stdout, self.returncode, self.raises = stdout, returncode, raises

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        self.kwargs.append(kw)
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "boom" if self.returncode else "")


@pytest.fixture
def fb(tmp_path):
    return F.Feedback(FeedbackCfg(sounds=False), tmp_path)


@pytest.fixture
def popen(monkeypatch):
    ran: list[list[str]] = []

    def fake(argv, **kw):
        assert kw.get("start_new_session")
        ran.append(argv)

    monkeypatch.setattr(F.subprocess, "Popen", fake)
    return ran


def test_omarchy_click_runs_the_command_and_nothing_waits(fb, popen):
    fb.omarchy = True
    run = Runner()
    assert fb.offer("Record “Standup”?", "Dana, Lee", RECORD, glyph="󰑊", runner=run) is None
    [cmd] = run.calls
    assert cmd[0] == "omarchy-notification-send"
    assert cmd[cmd.index("--app-name") + 1] == "chttr"
    assert cmd[cmd.index("-g") + 1] == "󰑊"
    assert cmd[cmd.index("-t") + 1] == "120000"
    assert cmd[cmd.index("--exec") + 1:] == ["chttr", "meeting", "start"]
    assert cmd[cmd.index("--exec") - 2:cmd.index("--exec")] == ["Record “Standup”?",
                                                                "Dana, Lee\nClick to record."]
    assert "--wait" not in cmd
    assert not popen                           # the shell runs it on click, not us


def test_omarchy_without_a_glyph_or_body(fb):
    fb.omarchy = True
    run = Runner()
    fb.offer("Learn “Sohrab”?", "", ("Learn it", ["chttr", "learn", "Sohrab"]), runner=run)
    [cmd] = run.calls
    assert "-g" not in cmd
    assert "Click to learn it." in cmd


def test_omarchy_failure_falls_back_to_plain_actions(fb, popen):
    fb.omarchy = True
    run = Runner(returncode=1, stdout="default\n")
    assert fb.offer("Record?", "", RECORD, runner=run) == "Record"
    assert [c[0] for c in run.calls] == ["omarchy-notification-send", "notify-send"]
    assert popen == [["chttr", "meeting", "start"]]


def test_generic_click_runs_the_primary_action(fb, popen):
    fb.omarchy = False
    run = Runner(stdout="default\n")
    assert fb.offer("Record?", "Dana", RECORD, [("Not now", [])], runner=run) == "Record"
    [cmd] = run.calls
    assert cmd[0] == "notify-send"
    assert "default=Record" in cmd and "0=Not now" in cmd
    assert cmd[-3:] == ["--wait", "Record?", "Dana\nClick to record."]
    assert run.kwargs[0]["timeout"] == 120000 / 1000 + 60
    assert popen == [["chttr", "meeting", "start"]]


def test_generic_button_runs_its_own_action(fb, popen):
    fb.omarchy = False
    others = [("Open", ["xdg-open", "notes.md"]), ("Send to Slack", ["chttr", "send", "--to", "slack"])]
    assert fb.offer("Notes saved", "", RECORD, others, runner=Runner(stdout="1\n")) == "Send to Slack"
    assert popen == [["chttr", "send", "--to", "slack"]]


@pytest.mark.parametrize("stdout", ["", "\n", "7", "learn"])
def test_generic_dismissed_or_unknown_answer_runs_nothing(fb, popen, stdout):
    fb.omarchy = False
    assert fb.offer("Record?", "", RECORD, [("Not now", [])], runner=Runner(stdout=stdout)) is None
    assert not popen


def test_generic_not_now_button_runs_nothing(fb, popen):
    fb.omarchy = False
    assert fb.offer("Record?", "", RECORD, [("Not now", [])], runner=Runner(stdout="0")) is None
    assert not popen


@pytest.mark.parametrize("exc", [FileNotFoundError("notify-send"), subprocess.TimeoutExpired("notify-send", 1)])
def test_generic_without_notify_send_or_an_answer(fb, popen, exc):
    fb.omarchy = False
    assert fb.offer("Record?", "", RECORD, runner=Runner(raises=exc)) is None
    assert not popen


def test_generic_command_that_cannot_run(fb, monkeypatch):
    fb.omarchy = False

    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(F.subprocess, "Popen", missing)
    assert fb.offer("Record?", "", RECORD, runner=Runner(stdout="default")) is None


def test_notifications_off_asks_nothing(tmp_path, popen):
    fb = F.Feedback(FeedbackCfg(sounds=False, notifications=False), tmp_path)
    run = Runner(stdout="default")
    for fb.omarchy in (True, False):
        assert fb.offer("Record?", "", RECORD, runner=run) is None
    assert not run.calls and not popen


def test_omarchy_is_detected_once(fb, monkeypatch):
    looked = []
    monkeypatch.setattr(F.shutil, "which", lambda name: looked.append(name) or "/usr/bin/" + name)
    run = Runner()
    fb.offer("a", "", RECORD, runner=run)
    fb.offer("b", "", RECORD, runner=run)
    assert looked == ["omarchy-notification-send"]
    assert fb.omarchy is True


def notified(monkeypatch) -> list[list[str]]:
    sent: list[list[str]] = []
    monkeypatch.setattr(F.subprocess, "Popen", lambda cmd, **kw: sent.append(cmd))
    return sent


def ids(sent: list[list[str]]) -> list[str]:
    return [cmd[cmd.index("-r") + 1] for cmd in sent]


def test_errors_are_not_replaced_by_the_next_typed_toast(fb, monkeypatch):
    sent = notified(monkeypatch)
    fb.notify("Transcribing…", urgency="low", timeout_ms=0)       # never expires on its own
    fb.notify("chttr: can't record", "no mic", urgency="critical", timeout_ms=8000)
    fb.notify("Typed", urgency="low")
    fb.notify("Transcribing…", urgency="low", timeout_ms=0)
    fb.notify("No speech detected: mic is silent", urgency="normal")
    fb.notify("Typed", urgency="low")
    got = ids(sent)
    assert got[1] == got[0]              # the error replaces the sticky "Transcribing…"
    assert got[2] != got[1]              # "Typed" shows beside the error, not over it
    assert got[3] == got[2]
    assert got[4] == got[3]              # the warning replaces "Transcribing…" too
    assert got[5] not in (got[1], got[4])    # and neither error is covered


def test_an_expired_error_frees_its_id(fb, monkeypatch):
    sent = notified(monkeypatch)
    clock = {"now": 100.0}
    monkeypatch.setattr(F.time, "monotonic", lambda: clock["now"])
    fb.notify("chttr: error", urgency="critical", timeout_ms=8000)
    clock["now"] += 1
    fb.notify("Warning", urgency="normal", timeout_ms=3000)
    clock["now"] += 10                   # both have expired: the warning first (at 104)
    fb.notify("Another warning", urgency="normal", timeout_ms=3000)
    fb.notify("Typed", urgency="low")
    got = ids(sent)
    assert len(set(got[:3])) == 3
    assert got[3] == got[1]              # progress takes the id that has been free longest
