"""`chttr daemon` never crash-loops on a bad config; meeting processing lives outside
the daemon's cgroup and leaves an honest status when it is stopped."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys

import pytest

from chttr import cli, install
from chttr import config as C
from chttr import daemon as Dm
from chttr.meeting import session


def test_a_broken_config_is_one_notification_and_the_defaults(monkeypatch):
    def broken(*a, **k):
        raise ValueError("output.mode must be type, paste or clipboard (got 'shout')")

    ran, notes = [], []
    monkeypatch.setattr(C, "write_default_config", lambda *a, **k: False)
    monkeypatch.setattr(C, "load_config", broken)
    monkeypatch.setattr(Dm, "run", ran.append)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: notes.append(cmd))
    assert cli.cmd_daemon(argparse.Namespace(verbose=False)) == 0
    assert len(notes) == 1
    argv = notes[0]
    assert argv[:2] == ["notify-send", "-a"] and "critical" in argv
    assert argv[-2] == "chttr: config.toml has a problem"
    assert "got 'shout'" in argv[-1] and "Running with the defaults" in argv[-1]
    (cfg,) = ran
    assert cfg.output.mode == "type"
    assert [m.name for m in cfg.modes] == [m.name for m in C._default_modes()]


def test_a_broken_config_without_notify_send_still_starts(monkeypatch):
    def no_notify_send(cmd, **k):
        raise FileNotFoundError(cmd[0])

    ran = []
    monkeypatch.setattr(C, "write_default_config", lambda *a, **k: False)
    monkeypatch.setattr(C, "load_config", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    monkeypatch.setattr(Dm, "run", ran.append)
    monkeypatch.setattr(subprocess, "run", no_notify_send)
    assert cli.cmd_daemon(argparse.Namespace(verbose=False)) == 0
    assert len(ran) == 1


def test_the_service_gives_up_after_a_few_quick_failures():
    unit = install.UNIT_TEXT.format(exe="/usr/bin/chttr")
    unit_section = unit.split("[Service]")[0]
    assert "StartLimitBurst=4" in unit_section and "StartLimitIntervalSec=120" in unit_section
    assert "RestartSec=5" in unit and "Restart=on-failure" in unit


def test_processing_runs_in_its_own_systemd_unit(monkeypatch, tmp_path):
    meeting = tmp_path / "2026-10-18-140000-abc-defg-hij"
    meeting.mkdir()
    runs, popens = [], []
    monkeypatch.setattr(session.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(session.subprocess, "run",
                        lambda cmd, **k: runs.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(session.subprocess, "Popen", lambda cmd, **k: popens.append(cmd))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    assert session.launch_processing(meeting) == "systemd-run"
    (cmd,) = runs
    assert cmd[:4] == ["systemd-run", "--user", "--collect", "--quiet"]
    assert cmd[cmd.index("--unit") + 1] == "chttr-process-2026-10-18-140000-abc-defg-hij"
    assert f"--property=StandardOutput=append:{meeting / 'process.log'}" in cmd
    assert "--setenv=ANTHROPIC_API_KEY" in cmd and not any("sk-secret" in a for a in cmd)
    assert cmd[-7:] == [sys.executable, "-m", "chttr", "meeting", "process", "--notify", str(meeting)]
    assert popens == []


@pytest.mark.parametrize("which, code", [(None, 0), ("/usr/bin/systemd-run", 1)])
def test_processing_falls_back_to_nice(monkeypatch, tmp_path, which, code):
    runs, popens = [], []
    monkeypatch.setattr(session.shutil, "which", lambda name: which)
    monkeypatch.setattr(session.subprocess, "run",
                        lambda cmd, **k: runs.append(cmd) or subprocess.CompletedProcess(cmd, code, "", "no bus"))
    monkeypatch.setattr(session.subprocess, "Popen",
                        lambda cmd, **k: popens.append((cmd, k["stdout"], k["start_new_session"])))
    assert session.launch_processing(tmp_path) == "nice"
    assert len(runs) == (1 if which else 0)
    ((cmd, log_file, own_session),) = popens
    assert cmd[:3] == ["nice", "-n", "10"] and cmd[-1] == str(tmp_path)
    assert own_session and log_file.closed                   # the daemon keeps no handle open
    assert (tmp_path / "process.log").exists()


def test_processing_stopped_midway_is_marked_failed_with_a_retry_hint(monkeypatch, tmp_path):
    from chttr import meeting

    directory = tmp_path / "2026-10-18-140000"
    directory.mkdir()
    (directory / "meta.json").write_text(json.dumps({"status": "processing"}), encoding="utf-8")
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda sig, fn: handlers.setdefault(sig, fn))
    monkeypatch.setattr(C, "load_config", lambda *a, **k: C.Config())

    def process(directory, cfg, summary=None):
        handlers[signal.SIGTERM](signal.SIGTERM, None)       # systemctl --user stop, mid-way

    monkeypatch.setattr(meeting, "process", process)
    args = argparse.Namespace(meeting=str(directory), no_summary=False, notify=False)
    with pytest.raises(SystemExit) as stopped:
        cli.cmd_meeting_process(args)
    assert stopped.value.code == 143
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "failed"
    assert meta["error"] == f"interrupted; retry: chttr meeting process {directory.name}"


def test_a_finished_meeting_is_not_marked_failed_by_a_late_stop(monkeypatch, tmp_path):
    directory = tmp_path / "m"
    directory.mkdir()
    (directory / "meta.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda sig, fn: handlers.setdefault(sig, fn))
    monkeypatch.setattr(C, "load_config", lambda *a, **k: C.Config())
    from chttr import meeting

    monkeypatch.setattr(meeting, "process", lambda *a, **k: handlers[signal.SIGTERM](signal.SIGTERM, None))
    with pytest.raises(SystemExit):
        cli.cmd_meeting_process(argparse.Namespace(meeting=str(directory), no_summary=False, notify=False))
    assert json.loads((directory / "meta.json").read_text(encoding="utf-8"))["status"] == "done"
