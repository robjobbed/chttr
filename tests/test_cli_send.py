from __future__ import annotations

import argparse
import subprocess

import pytest

from chttr import cli, install
from chttr.client import DaemonNotRunning


@pytest.fixture
def daemon_down(tmp_path, monkeypatch):
    """No daemon; record systemctl calls and notifications instead of making them."""
    def send(command, timeout=5.0):
        raise DaemonNotRunning("chttr daemon is not running (systemctl --user start chttr, or: chttr daemon)")

    monkeypatch.setattr(cli, "send", send)
    unit = tmp_path / "chttr.service"
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(install, "UNIT", unit)
    record = argparse.Namespace(runs=[], notes=[], code=0)

    def run(argv, **kw):
        record.runs.append(list(argv))
        return subprocess.CompletedProcess(argv, record.code, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(install, "notify", lambda title, body="", **kw: record.notes.append((title, body, kw)))
    return record


def _send(command):
    return cli.cmd_send(argparse.Namespace(command=command, json=False))


@pytest.mark.parametrize("command", ["start", "command", "toggle"])
def test_a_key_press_with_the_daemon_down_starts_it_and_says_so(daemon_down, command, capsys):
    assert _send(command) == 2
    assert daemon_down.runs == [["systemctl", "--user", "start", "chttr.service"]]
    assert daemon_down.notes == [("chttr is starting", "Hold the key again in a few seconds.",
                                  {"urgency": "normal", "timeout_ms": 6000})]
    assert "systemctl --user start chttr" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["release", "stop", "cancel"])
def test_a_key_release_with_the_daemon_down_stays_quiet(daemon_down, command):
    assert _send(command) == 2
    assert daemon_down.runs == [] and daemon_down.notes == []


def test_without_the_service_installed_it_points_at_setup(daemon_down):
    install.UNIT.unlink()
    assert _send("start") == 2
    assert daemon_down.runs == []
    assert daemon_down.notes[0][:2] == ("chttr is not running", "Run: chttr setup")


def test_a_service_that_will_not_start_is_not_called_starting(daemon_down):
    daemon_down.code = 1
    assert _send("start") == 2
    assert daemon_down.notes[0][0] == "chttr couldn't start"
