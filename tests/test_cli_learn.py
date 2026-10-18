"""What notification clicks run: `chttr learn ... --notify` ("Learn it?") and
`chttr meeting start --title/--source/--call` ("Record this call?")."""

from __future__ import annotations

import subprocess

import pytest

from chttr import cli, dictionary


@pytest.fixture
def dict_file(monkeypatch, tmp_path):
    path = tmp_path / "dictionary.toml"
    monkeypatch.setattr(dictionary, "DICTIONARY_FILE", path)
    monkeypatch.setattr(dictionary.Dictionary.load.__func__, "__defaults__", (path,))
    monkeypatch.setattr(dictionary.Dictionary.save, "__defaults__", (path,))
    return path


@pytest.fixture
def notifications(monkeypatch):
    sent: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: sent.append(cmd))
    return sent


def test_learn_saves_the_word(dict_file, notifications, capsys):
    args = cli.build_parser().parse_args(["learn", "Sohrab", "--heard", "Sorov"])
    assert args.func(args) == 0
    assert dictionary.Dictionary.load(dict_file).replacements == {"sorov": "Sohrab"}
    assert "learned 'Sohrab'" in capsys.readouterr().out
    assert notifications == []                  # a terminal user reads the line above


def test_learn_from_a_notification_click_says_so(dict_file, notifications):
    args = cli.build_parser().parse_args(["learn", "Sohrab", "--heard", "Sorov", "--notify"])
    assert args.func(args) == 0
    assert dictionary.Dictionary.load(dict_file).replacements == {"sorov": "Sohrab"}
    [cmd] = notifications
    assert cmd[:2] == ["notify-send", "-a"]
    assert "Learned “Sohrab”" in cmd


def test_learn_notification_failure_is_ignored(dict_file, monkeypatch):
    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", missing)
    args = cli.build_parser().parse_args(["learn", "Sohrab", "--notify"])
    assert args.func(args) == 0
    assert "Sohrab" in dictionary.Dictionary.load(dict_file).vocabulary


def test_meeting_start_flags_go_to_the_daemon_as_json(monkeypatch, capsys):
    sent: list[str] = []
    monkeypatch.setattr(cli, "send", lambda command, timeout=5.0: sent.append(command) or
                        {"ok": True, "message": "recording"})
    for argv in (["meeting", "start"],
                 ["meeting", "start", "--title", "Zoom call", "--source", "app"],
                 ["meeting", "start", "--call", "abc-defg-hij"]):
        args = cli.build_parser().parse_args(argv)
        assert args.func(args) == 0
    assert sent[0] == "meeting start"
    assert sent[1] == 'meeting start {"title": "Zoom call", "source": "app"}'
    assert sent[2] == 'meeting start {"call": "abc-defg-hij"}'
