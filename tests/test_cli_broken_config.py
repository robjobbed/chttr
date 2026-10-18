"""A config.toml with a mistake in it: the commands that help you fix it still run, and
`chttr doctor` names the mistake. The broken file lives in the test sandbox."""

from __future__ import annotations

import argparse
import json

import pytest

from chttr import cli
from chttr import config as C
from chttr import doctor as D
from chttr import ui as U
from chttr.client import DaemonNotRunning


@pytest.fixture(params=["[polish\nlevel = 'high'\n", '[output]\nmode = "shout"\n'], ids=["toml", "value"])
def broken(request, monkeypatch, tmp_path):
    """config.toml as `chttr` reads it, with a syntax error or a bad value."""
    path = tmp_path / "config.toml"
    path.write_text(request.param, encoding="utf-8")
    real = C.load_config
    monkeypatch.setattr(C, "load_config", lambda p=path: real(p))
    monkeypatch.setattr(C, "CONFIG_FILE", path)
    return path


def test_the_helper_hands_back_the_defaults_and_the_problem(broken):
    cfg, err = cli._config_or_defaults()
    assert cfg.output.mode == "type" and err


def test_doctor_runs_every_check_and_names_the_problem_first(broken, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(D, "run_all", lambda cfg, env=None: ran.append(cfg) or [D.Check("engine", True, "parakeet")])
    args = argparse.Namespace(json=True, fix=False, verify=False, mic=False)
    assert cli.cmd_doctor(args) == 1
    checks = json.loads(capsys.readouterr().out)
    assert [c["name"] for c in checks] == ["config", "engine"] and ran
    assert not checks[0]["ok"] and "using the defaults" in checks[0]["detail"]
    assert checks[0]["fix"] == f"$EDITOR {D._home(broken)}"


def test_the_menu_still_opens(broken, monkeypatch):
    from chttr import desktop as DT
    from chttr import meeting
    from chttr import polish as P

    shown = []
    monkeypatch.setattr(P, "backend", lambda cfg: "claude")       # never your key file or Ollama
    monkeypatch.setattr(cli, "send", lambda command, timeout=5.0: (_ for _ in ()).throw(DaemonNotRunning("no")))
    monkeypatch.setattr(meeting, "meeting_dirs", lambda: [])
    monkeypatch.setattr(DT, "menu", lambda title, options, **kw: shown.append(options) or "")
    assert cli.cmd_menu(None) == 0
    labels = [label.split("  ", 1)[-1] for label in shown[0]]
    assert "Edit settings" in labels and "Check setup (doctor)" in labels


@pytest.mark.parametrize("argv", [["ui", "--no-browser"], ["lingo", "--no-browser"]])
def test_the_browser_pages_still_open(broken, monkeypatch, capsys, argv):
    served = []
    monkeypatch.setattr(U, "serve", lambda **kw: served.append(kw))
    args = cli.build_parser().parse_args(argv)
    assert args.func(args) == 0
    assert served[0]["cfg"].output.mode == "type"
    assert "using the defaults" in capsys.readouterr().err


def test_a_daemon_that_does_not_answer_is_an_error_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(cli, "send", lambda command, timeout=5.0: (_ for _ in ()).throw(TimeoutError("timed out")))
    with pytest.raises(SystemExit) as stop:
        cli._send_or_exit("meeting stop")
    assert stop.value.code == 1 and "didn't answer" in capsys.readouterr().err
