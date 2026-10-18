from __future__ import annotations

import argparse

import pytest

from chttr import apikey, cli, vault
from chttr import config as C
from chttr import hotkeys as H
from chttr.client import DaemonNotRunning
from chttr.desktop import Desktop


def test_chttr_keys_prints_the_bound_shortcuts(monkeypatch, capsys, tmp_path):
    lua = tmp_path / "bindings.lua"
    monkeypatch.setattr("chttr.desktop.detect", lambda env=None: Desktop("wayland", "hyprland"))
    monkeypatch.setattr(H, "resolve", lambda backend, compositor: "compositor")
    monkeypatch.setattr(H, "effective_bindings", lambda cfg, desk, backend=None, home=None:
                        {"dictate": "F9", "command": "Alt+F9"})
    monkeypatch.setattr(H, "compositor_bindings", lambda compositor, home=None: ({}, lua))
    assert cli.cmd_keys(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "  Dictate (hold, release to type)        F9\n" in out
    assert "  Command mode on the selection (hold)   Alt+F9\n" in out
    assert f"Bound in {lua}." in out


def test_chttr_keys_with_nothing_bound(monkeypatch, capsys):
    monkeypatch.setattr("chttr.desktop.detect", lambda env=None: Desktop("wayland", "niri"))
    monkeypatch.setattr(H, "resolve", lambda backend, compositor: "none")
    assert cli.cmd_keys(argparse.Namespace()) == 1
    assert "Nothing catches the keys yet" in capsys.readouterr().out


def test_parser_knows_keys_and_connect_claude():
    p = cli.build_parser()
    assert p.parse_args(["keys"]).func is cli.cmd_keys
    args = p.parse_args(["connect", "claude", "--remove"])
    assert args.service == "claude" and args.remove


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = C.Config()
    cfg.polish.api_key_file = str(tmp_path / "anthropic-api-key")
    monkeypatch.setattr(C, "load_config", lambda path=None: cfg)

    def down(command, timeout=5.0):
        raise DaemonNotRunning("down")

    monkeypatch.setattr(cli, "send", down)
    return cfg


def test_connect_claude_runs_the_guided_flow(key_file, monkeypatch):
    seen = []
    monkeypatch.setattr(apikey, "connect_interactive", lambda cfg: seen.append(cfg) or True)
    assert cli.cmd_connect(argparse.Namespace(service="claude", remove=False)) == 0
    assert seen == [key_file]
    monkeypatch.setattr(apikey, "connect_interactive", lambda cfg: False)
    assert cli.cmd_connect(argparse.Namespace(service="claude", remove=False)) == 1


def test_connect_claude_remove(key_file, capsys):
    apikey.save("sk-ant-api03-" + "x" * 40, key_file)
    assert cli.cmd_connect(argparse.Namespace(service="claude", remove=True)) == 0
    assert not apikey.path(key_file).exists()
    assert "Claude disconnected" in capsys.readouterr().out
    cli.cmd_connect(argparse.Namespace(service="claude", remove=True))
    assert "no Claude key file" in capsys.readouterr().out


def test_connect_list_starts_with_claude(key_file, monkeypatch, capsys):
    monkeypatch.setattr(vault, "load", lambda name: None)
    monkeypatch.setattr(vault, "keyring_available", lambda: False)
    cli.cmd_connect(argparse.Namespace(service=None, remove=False))
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "  Claude   none"
    assert lines[1].startswith("  Slack")
