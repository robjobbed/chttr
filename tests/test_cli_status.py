"""`chttr status`: what the bar is told, with no daemon, a dead one and a live one."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from argparse import Namespace
from pathlib import Path

import pytest

from chttr import cli
from chttr import client as K
from chttr import config as C

ALIVE_PID = 2**22 + 1000        # above the largest pid_max: never a real process, so os.kill is faked


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    """A runtime dir of our own for every test (never the real one)."""
    rt = tmp_path / "chttr"
    rt.mkdir()
    for name, path in (("RUNTIME_DIR", rt), ("SOCKET_PATH", rt / "chttr.sock"), ("STATE_FILE", rt / "state"),
                       ("STATE_JSON", rt / "state.json"), ("MEETING_STATE_FILE", rt / "meeting")):
        monkeypatch.setattr(C, name, path)
    return rt


@pytest.fixture
def alive(monkeypatch):
    """Pretend ALIVE_PID is a running chttr daemon."""
    real_kill = os.kill

    def kill(pid, sig):
        if pid == ALIVE_PID and sig == 0:
            return
        return real_kill(pid, sig)

    monkeypatch.setattr(K.os, "kill", kill)


def write_state(state="idle", pid=ALIVE_PID, **extra):
    info = {"state": state, "kind": "", "since": time.time(), "pid": pid, "meeting": None, "cleanup": "claude",
            "hotkeys": {"dictate": "Insert", "command": "Alt+Insert"}, "model": "ready", **extra}
    C.STATE_JSON.write_text(json.dumps(info), encoding="utf-8")
    C.STATE_FILE.write_text(state, encoding="utf-8")
    return info


def status(capsys, *, json_out=False) -> str:
    assert cli.cmd_status(Namespace(follow=False, json=json_out)) == 0
    return capsys.readouterr().out.strip()


def stale_socket():
    """What a SIGKILLed daemon leaves behind: a socket file nobody listens on."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(C.SOCKET_PATH))
    s.close()


def test_stale_socket_and_no_state_json_is_off(capsys):
    stale_socket()
    C.STATE_FILE.write_text("idle", encoding="utf-8")
    assert status(capsys) == "off"
    data = json.loads(status(capsys, json_out=True))
    assert data["alt"] == data["class"] == "off"
    assert data["tooltip"] == "chttr is off · click to start"
    assert data["text"] == cli.ICONS["off"]


def test_a_dead_pid_is_off_even_with_a_meeting_file_left_behind(capsys):
    write_state("recording", pid=ALIVE_PID)          # not faked alive here: nothing runs as this pid
    C.MEETING_STATE_FILE.write_text(json.dumps({"title": "Sync", "code": "", "started": time.time() - 5}))
    data = json.loads(status(capsys, json_out=True))
    assert data["alt"] == "off"
    assert data["meeting_seconds"] is None


def test_a_live_daemon_reports_the_state_from_the_json(capsys, alive):
    write_state("recording", kind="dictate")
    C.STATE_FILE.write_text("idle", encoding="utf-8")   # state.json wins over the plain file
    assert status(capsys) == "recording"
    data = json.loads(status(capsys, json_out=True))
    assert data["alt"] == "recording"
    assert data["tooltip"] == "chttr: listening · let go of Insert to type"


def test_loading(capsys, alive):
    write_state("loading", model="loading")
    data = json.loads(status(capsys, json_out=True))
    assert (data["alt"], data["tooltip"]) == ("loading", "chttr: loading the speech model")


def test_an_unknown_state_shows_as_idle(capsys, alive):
    write_state("dreaming")
    assert status(capsys) == "idle"


def test_a_daemon_from_before_state_json_still_shows(capsys, tmp_path):
    """No state.json but something answers on the socket: trust the plain state file."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(C.SOCKET_PATH))
    server.listen(1)
    try:
        C.STATE_FILE.write_text("transcribing", encoding="utf-8")
        assert status(capsys) == "transcribing"
    finally:
        server.close()


def test_daemon_alive():
    assert not K.daemon_alive({})
    assert not K.daemon_alive({"pid": "nonsense"})
    assert not K.daemon_alive({"pid": ALIVE_PID})


def test_daemon_alive_counts_someone_elses_process(monkeypatch):
    def kill(pid, sig):
        raise PermissionError

    monkeypatch.setattr(K.os, "kill", kill)
    assert K.daemon_alive({"pid": ALIVE_PID})


def test_a_reused_pid_is_not_chttr(monkeypatch, tmp_path):
    monkeypatch.setattr(K.os, "kill", lambda pid, sig: None)
    proc = tmp_path / "proc" / "1234"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"/usr/bin/firefox\0")
    monkeypatch.setattr(K, "Path", lambda p: tmp_path / "proc" if p == "/proc" else os.fspath(p))
    assert not K.daemon_alive({"pid": 1234})
    (proc / "cmdline").write_bytes(b"/usr/bin/python3\0/home/me/.local/bin/chttr\0daemon\0")
    assert K.daemon_alive({"pid": 1234})


def test_read_state_json_survives_garbage():
    assert K.read_state_json() == {}
    C.STATE_JSON.write_text("{not json", encoding="utf-8")
    assert K.read_state_json() == {}
    C.STATE_JSON.write_text("[1, 2]", encoding="utf-8")
    assert K.read_state_json() == {}


# ---- the bar line -------------------------------------------------------------


def test_idle_tooltip_names_the_configured_keys():
    data = json.loads(cli._status_json("idle", {"hotkeys": {"dictate": "F9", "command": "Alt+F9"}, "cleanup": "claude"}))
    assert data["tooltip"] == "chttr: hold F9 to dictate · Alt+F9: command mode · right click: record a meeting"
    assert data["hotkey"] == "F9"
    assert data["cleanup"] == "claude"


def test_idle_without_info_uses_the_default_keys():
    data = json.loads(cli._status_json("idle"))
    assert data["tooltip"].startswith("chttr: hold Insert to dictate · Alt+Insert: command mode")


def test_a_key_turned_off_is_not_mentioned():
    data = json.loads(cli._status_json("idle", {"hotkeys": {"dictate": "", "command": ""}}))
    assert data["tooltip"] == "chttr: ready · right click: record a meeting"


def test_cleanup_off_says_so():
    data = json.loads(cli._status_json("idle", {"hotkeys": {"dictate": "F9"}, "cleanup": "none"}))
    assert data["tooltip"] == "chttr: cleanup is off (local rules) · hold F9 to dictate"
    assert data["cleanup"] == "none"


def test_recording_tooltips_follow_the_kind():
    keys = {"dictate": "Insert", "command": "Alt+Insert"}
    hands_free = json.loads(cli._status_json("recording", {"kind": "hands-free", "hotkeys": keys}))
    assert hands_free["tooltip"] == "chttr: listening hands-free · stops when you pause"
    command = json.loads(cli._status_json("recording", {"kind": "command", "hotkeys": keys}))
    assert command["tooltip"] == "chttr: listening for an instruction · let go of Alt+Insert to apply it"


def test_meeting_shows_a_running_timer():
    C.MEETING_STATE_FILE.write_text(json.dumps({"title": "", "code": "", "started": time.time() - 754}))
    data = json.loads(cli._status_json("idle", {"hotkeys": {"dictate": "Insert"}}))
    assert data["alt"] == "meeting"
    assert data["meeting_seconds"] in (754, 755)
    assert data["tooltip"] in ("chttr: recording a meeting (12:34) · right click to stop",
                               "chttr: recording a meeting (12:35) · right click to stop")
    assert data["text"].startswith(cli.ICONS["meeting"] + " 12:3")


def test_meeting_title_and_hours():
    C.MEETING_STATE_FILE.write_text(json.dumps({"title": "Weekly sync", "code": "abc", "started": time.time() - 3725}))
    data = json.loads(cli._status_json("meeting"))
    assert data["tooltip"].startswith("chttr: recording “Weekly sync” (1:02:0")


def test_dictating_during_a_meeting_keeps_the_timer():
    C.MEETING_STATE_FILE.write_text(json.dumps({"title": "", "code": "", "started": time.time() - 65}))
    data = json.loads(cli._status_json("recording", {"kind": "dictate", "hotkeys": {"dictate": "Insert"}}))
    assert data["alt"] == "recording"
    assert data["meeting_seconds"] in (65, 66)
    assert data["tooltip"].endswith("· meeting 1:05") or data["tooltip"].endswith("· meeting 1:06")


def test_a_discarded_meeting_is_gone_even_if_state_json_still_has_it():
    info = {"meeting": {"title": "Old", "code": "", "started": time.time() - 30}}
    data = json.loads(cli._status_json("idle", info))
    assert data["alt"] == "idle"
    assert data["meeting_seconds"] is None


def test_an_unreadable_meeting_file_falls_back_to_state_json():
    C.MEETING_STATE_FILE.write_text("{half written")
    data = json.loads(cli._status_json("idle", {"meeting": {"title": "", "code": "", "started": time.time() - 10}}))
    assert data["alt"] == "meeting"
    assert data["meeting_seconds"] in (10, 11)


# ---- --follow -----------------------------------------------------------------


def follow(monkeypatch, capsys, ticks: list, json_out=True) -> list[str]:
    """Run `chttr status --follow` over `ticks`: each sleep first runs the next tick
    (a callable that changes the world), and the last one ends the loop."""
    queue = list(ticks)

    def fake_sleep(_secs):
        if not queue:
            raise KeyboardInterrupt
        queue.pop(0)()

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("rc", cli.cmd_status(Namespace(follow=True, json=json_out))))
    t.start()
    t.join(0.5)
    assert not t.is_alive()
    assert result["rc"] == 0
    return [line for line in capsys.readouterr().out.splitlines() if line]


def test_follow_emits_on_changes_only(monkeypatch, capsys, alive):
    write_state("idle")
    clock = {"now": 1000.0}
    monkeypatch.setattr(cli.time, "time", lambda: clock["now"])

    def to(state, **extra):
        return lambda: write_state(state, **extra)

    def meeting_starts():
        C.MEETING_STATE_FILE.write_text(json.dumps({"title": "", "code": "", "started": 1000.0}))

    def tick(secs):
        def go():
            clock["now"] += secs
        return go

    lines = follow(monkeypatch, capsys, [
        lambda: None,                       # nothing changed: no new line
        to("recording", kind="dictate"),
        lambda: None,
        to("transcribing"),
        to("idle"),
        meeting_starts,                     # 0:00
        tick(0.4), tick(0.4),               # still 0:00
        tick(0.4),                          # 0:01
        to("idle", cleanup="none"),         # the probe found no backend: same second, new line
        lambda: C.STATE_JSON.unlink(),      # daemon gone
    ])
    alts = [json.loads(line)["alt"] for line in lines]
    assert alts == ["idle", "recording", "transcribing", "idle", "meeting", "meeting", "meeting", "off"]
    seconds = [json.loads(line)["meeting_seconds"] for line in lines[4:7]]
    assert seconds == [0, 1, 1]
    assert json.loads(lines[6])["cleanup"] == "none"


def test_follow_plain_text(monkeypatch, capsys, alive):
    write_state("idle")
    lines = follow(monkeypatch, capsys, [lambda: None, lambda: write_state("recording"), lambda: None],
                   json_out=False)
    assert lines == ["idle", "recording"]


def test_status_never_raises_daemon_not_running(monkeypatch, capsys):
    def boom():
        raise K.DaemonNotRunning("gone")

    monkeypatch.setattr(cli, "_current_state", boom)
    assert json.loads(status(capsys, json_out=True))["alt"] == "off"


# ---- the Omarchy widget -----------------------------------------------------


ROOT = Path(__file__).resolve().parents[1]


def test_manifest_declares_the_widget_settings():
    widget = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["barWidget"]
    schema = {entry["key"]: entry for entry in widget["schema"]}
    assert schema["showMeetingTimer"]["type"] == "boolean" and schema["showMeetingTimer"]["defaultValue"] is True
    assert schema["rightClick"]["options"] == ["meeting", "handsfree"]
    assert widget["defaults"] == {"showMeetingTimer": True, "rightClick": "meeting"}


def test_widget_reads_the_settings_and_uses_the_bars_font():
    qml = (ROOT / "BarWidget.qml").read_text(encoding="utf-8")
    assert 'setting("showMeetingTimer"' in qml and 'setting("rightClick"' in qml
    assert "JetBrainsMono" not in qml                   # the bar's own font, like first-party widgets
    assert "property string state" not in qml           # Item.state drives QML States; never shadow it
