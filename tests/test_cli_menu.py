"""The bar widget's menu: what it offers depends on the daemon and the cleanup backend.
The picker, terminal and daemon are fakes."""

from __future__ import annotations

import shutil

import pytest

from chttr import cli
from chttr import config as C
from chttr import desktop as DT
from chttr import polish as P
from chttr.client import DaemonNotRunning

CLEANUP = "󰚩  Turn on Claude cleanup…"
OLLAMA = "󰚩  Use Ollama for cleanup (offline)"


@pytest.fixture
def menu(monkeypatch):
    """Runs `chttr menu`; returns the labels shown and the terminal commands launched."""
    from chttr import meeting

    shown: list[list[str]] = []
    terminal: list[str] = []
    pick = {"label": None}

    def fake_menu(title, options, width=720):
        shown.append(options)
        return options[0] if pick["label"] is None else pick["label"]

    def fake_send(command, timeout=5.0):
        raise DaemonNotRunning("not running")

    monkeypatch.setattr(DT, "menu", fake_menu)
    monkeypatch.setattr(cli, "_terminal", terminal.append)
    monkeypatch.setattr(cli, "send", fake_send)
    monkeypatch.setattr(meeting, "meeting_dirs", lambda: [])
    monkeypatch.setattr(shutil, "which", lambda name: None)

    def run(cfg=None, pick_label=None):
        monkeypatch.setattr(C, "load_config", lambda: cfg or C.Config())
        pick["label"] = pick_label
        assert cli.cmd_menu(None) == 0
        return shown[-1], terminal

    return run


def test_no_cleanup_backend_offers_claude_first(menu, monkeypatch):
    monkeypatch.setattr(P, "backend", lambda cfg: "none")
    labels, terminal = menu()
    assert labels[0] == CLEANUP
    assert terminal == ["chttr connect claude"]     # the guided key entry, in a terminal


def test_working_cleanup_hides_it(menu, monkeypatch):
    monkeypatch.setattr(P, "backend", lambda cfg: "claude")
    labels, terminal = menu(pick_label="nothing")
    assert CLEANUP not in labels and OLLAMA not in labels
    assert labels[0] == "󰑓  Start the chttr daemon"
    assert terminal == []


def test_cleanup_turned_off_on_purpose_is_left_alone(menu, monkeypatch):
    monkeypatch.setattr(P, "backend", lambda cfg: "none")
    cfg = C.Config()
    cfg.polish.backend = "none"
    labels, _ = menu(cfg, pick_label="nothing")
    assert CLEANUP not in labels
    cfg = C.Config()
    cfg.polish.level = "none"
    labels, _ = menu(cfg, pick_label="nothing")
    assert CLEANUP not in labels


def test_installed_ollama_is_offered_too(menu, monkeypatch):
    monkeypatch.setattr(P, "backend", lambda cfg: "none")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ollama" if name == "ollama" else None)
    labels, terminal = menu(pick_label=OLLAMA)
    assert labels[:2] == [CLEANUP, OLLAMA]
    [script] = terminal
    assert script.startswith("setsid -f ollama serve")      # outlives the terminal window
    assert "ollama pull llama3.2" in script                   # the model cleanup asks for


def test_claude_chosen_without_a_key_offers_the_key(menu, monkeypatch):
    monkeypatch.setattr(P, "_claude_configured", lambda polish: False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ollama" if name == "ollama" else None)
    cfg = C.Config()
    cfg.polish.backend = "claude"
    labels, terminal = menu(cfg)
    assert labels[0] == CLEANUP
    assert OLLAMA not in labels            # backend = "claude" would never pick Ollama up
    assert terminal == ["chttr connect claude"]
    monkeypatch.setattr(P, "_claude_configured", lambda polish: True)
    labels, _ = menu(cfg, pick_label="nothing")
    assert CLEANUP not in labels
