from __future__ import annotations

import pytest

from chttr import cli
from chttr import ui as U
from tests.test_library import lib  # noqa: F401 - the fixture


def test_ui_flags_parse():
    args = cli.build_parser().parse_args(["ui", "--idle-timeout", "5"])
    assert args.idle_timeout == 5 and args.open is None and args.func is cli.cmd_ui
    args = cli.build_parser().parse_args(["ui"])
    assert args.idle_timeout == 0                                   # a terminal `chttr ui` runs until Ctrl+C
    args = cli.build_parser().parse_args(["ui", "--open", "Pricing review, 2026-10-01, 00:02:32", "--no-browser"])
    assert args.open == "Pricing review, 2026-10-01, 00:02:32" and args.no_browser


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


@pytest.fixture
def served(monkeypatch):
    calls = []
    monkeypatch.setattr(U, "serve", lambda **kw: calls.append(kw))
    return calls


def test_ui_opens_at_a_citation(lib, served, capsys):  # noqa: F811
    assert run(["ui", "--no-browser", "--idle-timeout", "60", "--open", "[Pricing review, 2026-10-01, 00:00:40]"]) == 0
    assert served[-1]["view"] == "meeting/2026-10-01-140000?t=40" and served[-1]["idle_timeout"] == 60
    assert served[-1]["open_browser"] is False


def test_ui_falls_back_to_a_search_for_an_unknown_citation(lib, served, capsys):  # noqa: F811
    run(["ui", "--no-browser", "--open", "Board meeting, 2026-01-01, 00:00:05"])
    assert served[-1]["view"] == "meetings?q=Board%20meeting"
    assert "no meeting matches" in capsys.readouterr().err


def test_ui_without_open_starts_on_the_default_view(lib, served):  # noqa: F811
    run(["ui", "--no-browser"])
    assert served[-1]["view"] == "" and served[-1]["idle_timeout"] == 0


def test_route_for_citation_uses_the_library(lib):  # noqa: F811
    from chttr import library as L

    assert U.route_for_citation("Launch sync, 2026-10-08, 00:00:20", L.meetings()) == "meeting/2026-10-08-140000?t=20"


def test_menu_has_the_meeting_library_after_dictation_history(monkeypatch):
    from chttr import desktop as DT

    shown = {}

    def menu(prompt, rows, **kw):
        shown["rows"] = rows
        return ""

    def down(cmd, timeout=5.0):
        raise cli.DaemonNotRunning("no")

    monkeypatch.setattr(cli, "send", down)
    monkeypatch.setattr(DT, "menu", menu)
    assert cli.cmd_menu(None) == 0
    labels = [r.split("  ", 1)[-1] for r in shown["rows"]]
    assert labels[labels.index("Dictation history") + 1] == "Meeting library"
    assert "Stats" not in labels


def test_menu_meeting_library_starts_ui_with_an_idle_timeout(monkeypatch):
    import subprocess

    from chttr import desktop as DT

    started = []
    monkeypatch.setattr(cli, "send", lambda cmd, timeout=5.0: (_ for _ in ()).throw(cli.DaemonNotRunning("no")))
    monkeypatch.setattr(DT, "menu", lambda prompt, rows, **kw: next(r for r in rows if r.endswith("Meeting library")))
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append(argv))
    cli.cmd_menu(None)
    assert started and started[0][1:] == ["ui", "--idle-timeout", "900"]
