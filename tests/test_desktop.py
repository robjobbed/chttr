from __future__ import annotations

import pytest

from chttr import desktop as DT


@pytest.mark.parametrize(("env", "session", "comp"), [
    ({"XDG_SESSION_TYPE": "wayland", "HYPRLAND_INSTANCE_SIGNATURE": "x"}, "wayland", "hyprland"),
    ({"WAYLAND_DISPLAY": "wayland-1", "SWAYSOCK": "/run/sway"}, "wayland", "sway"),
    ({"XDG_SESSION_TYPE": "wayland", "NIRI_SOCKET": "/run/niri"}, "wayland", "niri"),
    ({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE"}, "wayland", "kde"),
    ({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, "wayland", "gnome"),
    ({"XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "GNOME"}, "x11", "x11"),
    ({"DISPLAY": ":0"}, "x11", "x11"),
    ({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "river"}, "wayland", "wlroots"),
    ({}, "unknown", "unknown"),
])
def test_detect(env, session, comp):
    d = DT.detect(env)
    assert (d.session, d.compositor) == (session, comp)


def only(*tools):
    return lambda name: name in tools


def test_typer_picks_the_right_tool_per_desktop(monkeypatch):
    hypr, gnome, x11 = DT.Desktop("wayland", "hyprland"), DT.Desktop("wayland", "gnome"), DT.Desktop("x11", "x11")
    monkeypatch.setattr(DT, "have", only("wtype", "ydotool", "xdotool"))
    assert DT.typer_command(hypr, "hi")[0] == "wtype"
    assert DT.typer_command(gnome, "hi")[0] == "ydotool"
    assert DT.typer_command(x11, "hi")[0] == "xdotool"
    assert DT.typer_command(hypr, "hi", preferred="ydotool")[0] == "ydotool"
    monkeypatch.setattr(DT, "have", only())
    assert DT.typer_command(hypr, "hi") is None


def test_typed_text_is_passed_after_a_double_dash(monkeypatch):
    monkeypatch.setattr(DT, "have", only("wtype"))
    assert DT.typer_command(DT.Desktop("wayland", "sway"), "-rf /") == ["wtype", "--", "-rf /"]


def test_shortcut_commands(monkeypatch):
    monkeypatch.setattr(DT, "have", only("hyprctl", "wtype", "xdotool", "ydotool"))
    assert DT.shortcut_command(DT.Desktop("wayland", "hyprland"), "ctrl+v") == \
        ["hyprctl", "dispatch", "sendshortcut", "CTRL, V,"]
    assert DT.shortcut_command(DT.Desktop("wayland", "sway"), "ctrl+shift+v") == \
        ["wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"]
    assert DT.shortcut_command(DT.Desktop("x11", "x11"), "ctrl+c") == ["xdotool", "key", "--clearmodifiers", "ctrl+c"]
    assert DT.shortcut_command(DT.Desktop("wayland", "kde"), "ctrl+v") == \
        ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"]


def test_sway_focused_node():
    tree = {"nodes": [{"nodes": [{"focused": False, "app_id": "foot"},
                                 {"focused": True, "app_id": None, "window_properties": {"class": "Slack"},
                                  "name": "general"}]}]}
    node = DT._sway_focused(tree)
    assert node["window_properties"]["class"] == "Slack"


def test_match_option_handles_menus_that_drop_the_icon_column():
    options = ["A\tGeneral\tSummary", "B\tSales: BANT\tQualification"]
    assert DT.match_option("Sales: BANT\tQualification", options) == options[1]
    assert DT.match_option("A\tGeneral\tSummary", options) == options[0]
    assert DT.match_option("General", options) == options[0]
    assert DT.match_option("", options) is None
    assert DT.match_option("Nope", options) is None


def test_menu_with_a_dmenu_style_tool(monkeypatch):
    monkeypatch.setattr(DT, "have", only("fuzzel"))
    seen = {}

    class Out:
        stdout = "Sales: BANT   ·   Qualification\n"
        returncode = 0

    def fake_run(cmd, timeout=2, text=True, check=False, input=None):
        seen["cmd"], seen["input"] = cmd, input
        return Out()

    monkeypatch.setattr(DT, "_run", fake_run)
    options = ["General\tSummary", "Sales: BANT\tQualification"]
    assert DT.menu("Notes style", options) == options[1]
    assert seen["cmd"][0] == "fuzzel" and "General   ·   Summary" in seen["input"]


def test_terminal_command_falls_back_through_known_terminals(monkeypatch):
    monkeypatch.setattr(DT.shutil, "which", lambda name: None)
    monkeypatch.setattr(DT, "have", only("gnome-terminal"))
    monkeypatch.delenv("TERMINAL", raising=False)
    cmd = DT.terminal_command("chttr doctor")
    assert cmd[:3] == ["gnome-terminal", "--", "bash"] and "chttr doctor" in cmd[-1]


def test_report_mentions_the_ydotool_daemon(monkeypatch):
    monkeypatch.setattr(DT, "have", only("ydotool"))
    text = "\n".join(DT.report(DT.Desktop("wayland", "gnome")))
    assert "typing:      ydotool" in text and "ydotool needs its daemon" in text


def test_hyprland_is_found_from_its_sockets_without_the_env_var(tmp_path):
    (tmp_path / "hypr" / "abc123").mkdir(parents=True)
    env = {"XDG_RUNTIME_DIR": str(tmp_path), "WAYLAND_DISPLAY": "wayland-1"}
    assert DT.detect(env).compositor == "hyprland"
    assert DT.detect({"XDG_CURRENT_DESKTOP": "Hyprland"}) == DT.Desktop("wayland", "hyprland")
    assert DT.detect({"XDG_RUNTIME_DIR": str(tmp_path / "nothing"), "WAYLAND_DISPLAY": "w"}).compositor == "wlroots"


def test_shortcut_commands_press_return(monkeypatch):
    monkeypatch.setattr(DT, "have", only("hyprctl", "wtype", "xdotool", "ydotool"))
    assert DT.shortcut_command(DT.Desktop("wayland", "hyprland"), "shift+return") == \
        ["hyprctl", "dispatch", "sendshortcut", "SHIFT, RETURN,"]
    assert DT.shortcut_command(DT.Desktop("wayland", "hyprland"), "return") == \
        ["hyprctl", "dispatch", "sendshortcut", ", RETURN,"]
    assert DT.shortcut_command(DT.Desktop("wayland", "sway"), "shift+return") == \
        ["wtype", "-M", "shift", "-k", "Return", "-m", "shift"]
    assert DT.shortcut_command(DT.Desktop("x11", "x11"), "shift+return") == \
        ["xdotool", "key", "--clearmodifiers", "shift+Return"]
    assert DT.shortcut_command(DT.Desktop("wayland", "kde"), "shift+return") == \
        ["ydotool", "key", "42:1", "28:1", "28:0", "42:0"]
    assert DT.shortcut_command(DT.Desktop("wayland", "kde"), "backspace") == ["ydotool", "key", "14:1", "14:0"]


def test_shift_enter_line_breaks_are_pressed_between_typed_lines(monkeypatch):
    monkeypatch.setattr(DT, "have", lambda name: True)
    hypr = DT.Desktop("wayland", "hyprland")
    shift_enter = ["hyprctl", "dispatch", "sendshortcut", "SHIFT, RETURN,"]
    assert DT.typer_commands(hypr, "line1\nline2", newline="shift+enter") == \
        [["wtype", "--", "line1"], shift_enter, ["wtype", "--", "line2"]]
    assert DT.typer_commands(hypr, "a\n\nb\n", newline="shift+enter") == \
        [["wtype", "--", "a"], shift_enter, shift_enter, ["wtype", "--", "b"], shift_enter]
    assert DT.typer_commands(hypr, "line1\nline2") == [["wtype", "--", "line1\nline2"]]      # Enter: one command
    assert DT.typer_commands(hypr, "one line", newline="shift+enter") == [["wtype", "--", "one line"]]


def test_shift_enter_without_a_shortcut_tool_is_refused(monkeypatch):
    monkeypatch.setattr(DT, "have", only("wtype"))
    gnome = DT.Desktop("wayland", "gnome")
    assert DT.typer_commands(gnome, "a\nb", newline="shift+enter") is None
    assert DT.typer_commands(gnome, "a\nb") == [["wtype", "--", "a\nb"]]
    with pytest.raises(FileNotFoundError, match="Shift\\+Enter"):
        DT.type_text("a\nb", gnome, newline="shift+enter")


def test_type_text_runs_each_command_in_order(monkeypatch):
    monkeypatch.setattr(DT, "have", only("wtype"))
    ran = []
    monkeypatch.setattr(DT.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    assert DT.type_text("hi\nthere", DT.Desktop("wayland", "sway"), newline="shift+enter") == "wtype"
    assert ran == [["wtype", "--", "hi"], ["wtype", "-M", "shift", "-k", "Return", "-m", "shift"],
                   ["wtype", "--", "there"]]


@pytest.mark.parametrize(("newline", "script"), [
    ("enter", "type first\nkey enter\ntype second\n"),
    ("shift+enter", "type first\nkey shift+enter\ntype second\n"),
])
def test_dotool_gets_line_breaks_as_keys(monkeypatch, newline, script):
    monkeypatch.setattr(DT, "have", only("dotool"))
    seen = {}
    monkeypatch.setattr(DT.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, input=kw["input"]))
    assert DT.type_text("first\nsecond", DT.Desktop("wayland", "gnome"), newline=newline) == "dotool"
    assert seen == {"cmd": ["dotool"], "input": script.encode()}
