from __future__ import annotations

import tomllib

import pytest

from chttr import config as C


def test_default_config_parses_and_matches_the_dataclasses(tmp_path):
    path = tmp_path / "config.toml"
    assert C.write_default_config(path)
    assert not C.write_default_config(path)          # never overwrites
    cfg = C.load_config(path)
    defaults = C.Config(modes=C._default_modes())
    for section in ("engine", "whisper", "audio", "output", "polish", "feedback", "history", "hotkeys", "integrations", "meeting"):
        assert getattr(cfg, section) == getattr(defaults, section), section


def test_every_default_key_exists_on_its_dataclass():
    data = tomllib.loads(C.DEFAULT_CONFIG)
    cfg = C.Config()
    for section, values in data.items():
        if section == "modes":
            continue
        for key in values:
            assert hasattr(getattr(cfg, section), key), f"[{section}] {key} is not read by config.py"


def test_missing_file_gives_defaults(tmp_path):
    cfg = C.load_config(tmp_path / "missing.toml")
    assert cfg.engine.name == "parakeet"
    assert [m.name for m in cfg.modes][:3] == ["agent", "terminal", "code"]


def test_modes_match_window_class_case_insensitively():
    cfg = C.load_config(C.Path("/nonexistent"))
    assert cfg.mode_for("Alacritty").name == "terminal"
    assert cfg.mode_for("code-oss").name == "code"
    assert cfg.mode_for("Slack").name == "chat"
    assert cfg.mode_for("") is None
    assert cfg.mode_for("some-unknown-app") is None


@pytest.mark.parametrize(("window_class", "title", "mode"), [
    ("chromium", "Inbox - Gmail", "email"),
    ("chromium", "Inbox (3) - me@example.com - Gmail - Chromium", "email"),
    ("firefox", "Mail - Rob - Outlook \u2014 Mozilla Firefox", "email"),
    ("thunderbird", "Write: Re: budget - Thunderbird", "email"),       # native apps still match by class
    ("ghostty", "\u2733 Claude Code", "agent"),
    ("kitty", "codex", "agent"),
    ("ghostty", "~", "terminal"),
    ("ghostty", "", "terminal"),
    ("chromium", "general (Channel) - Acme - Slack - Chromium", "chat"),
    ("Slack", "general", "chat"),
    ("chromium", "Plan - Google Docs - Chromium", "docs"),
    ("chromium", "New chat - Claude - Chromium", "assistant"),
    ("chromium", "Hacker News", None),
    ("code-oss", "notion.md - project", "code"),                         # a title never beats the editor
])
def test_modes_match_the_window_title(window_class, title, mode):
    cfg = C.load_config(C.Path("/nonexistent"))
    found = cfg.mode_for(window_class, title)
    assert (found.name if found else None) == mode


def test_chat_apps_type_line_breaks_as_shift_enter():
    cfg = C.load_config(C.Path("/nonexistent"))
    assert cfg.mode_for("Slack").newline == "shift+enter"
    assert cfg.mode_for("chromium", "Slack | general").newline == "shift+enter"
    assert cfg.mode_for("thunderbird").newline == "enter"
    agent = cfg.mode_for("ghostty", "\u2733 Claude Code")
    assert agent.is_code and agent.in_terminal and agent.trailing_space is False
    assert agent.newline == "shift+enter"                   # Enter would send the prompt


def test_title_only_modes_match_any_app(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[[modes]]\nname = "jira"\ntitle = "jira"\n')
    cfg = C.load_config(path)
    assert cfg.modes[0].title == ["jira"]                  # a single string becomes a list
    assert cfg.mode_for("chromium", "PROJ-12 - Jira").name == "jira"
    assert cfg.mode_for("chromium", "Inbox") is None
    assert cfg.mode_for("", "PROJ-12 - Jira").name == "jira"


def test_user_modes_replace_the_defaults(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[[modes]]\nname = "notes"\nmatch = "obsidian"\npolish = false\n')
    cfg = C.load_config(path)
    assert [m.name for m in cfg.modes] == ["notes"]
    assert cfg.modes[0].match == ["obsidian"]          # a single string becomes a list
    assert cfg.mode_for("obsidian").polish is False


def test_legacy_polish_enabled_false_means_no_backend(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[polish]\nenabled = false\n")
    assert C.load_config(path).polish.backend == "none"


@pytest.mark.parametrize("snippet", [
    '[output]\nmode = "shout"\n',
    '[engine]\nname = "vosk"\n',
    '[meeting]\nauto = "maybe"\n',
    '[polish]\nbackend = "gpt"\n',
    '[[modes]]\nname = "x"\nmatch = "x"\nnewline = "tab"\n',
    '[[modes]]\nname = "x"\ntitle = "(unclosed"\n',
])
def test_invalid_values_are_rejected(tmp_path, snippet):
    path = tmp_path / "c.toml"
    path.write_text(snippet)
    with pytest.raises(ValueError):
        C.load_config(path)


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[audio]\nmax_seconds = 60\nfuture_option = true\n")
    assert C.load_config(path).audio.max_seconds == 60
