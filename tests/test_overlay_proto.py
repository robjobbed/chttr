"""The overlay's stdin protocol and the Omarchy theme it's drawn in."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from chttr import overlay_proto as OP

OVERLAY = Path(OP.__file__).with_name("overlay.py")

LINES = [
    "0.01234 -23.5",
    "0.5 -1.0\n",
    "1e-06 -90.0",
    "show ",
    "show",
    "show command",
    "show hands-free",
    "hide",
    "hide\n",
    "state listening",
    "state transcribing",
    "state done",
    "state nospeech",
    "text hello world",
    "text   spaced out  ",
    "caption so what I was saying",
    'palette {"bg": "#f3eefa", "dark": false}',
    "palette {}",
    "palette",
    "",
    "   ",
    "garbage",
    "1 2 3",
    "nan -20",
    "0.1 inf",
    "showing off",
    "states done",
]


def test_level_lines():
    assert OP.parse("0.01234 -23.5") == ("level", 0.01234, -23.5)
    assert OP.parse("0.5 -1.0\n") == ("level", 0.5, -1.0)
    assert OP.parse("nan -20") is None
    assert OP.parse("0.1 inf") is None
    assert OP.parse("1 2 3") is None
    assert OP.parse("garbage") is None
    assert OP.parse("") is None


def test_commands():
    assert OP.parse("show ") == ("show", "")
    assert OP.parse("show command") == ("show", "command")
    assert OP.parse("hide\n") == ("hide", "")
    assert OP.parse("state done") == ("state", "done")
    assert OP.parse("caption so what I was saying") == ("caption", "so what I was saying")
    assert OP.parse("showing off") is None          # a command is a whole word


@pytest.mark.parametrize("cmd, arg", [
    ("show", ""), ("show", "command"), ("hide", ""), ("state", "transcribing"),
    ("text", "Hello, world."), ("caption", "two\nlines"), ("palette", '{"bg": "#000000"}'),
])
def test_format_round_trips(cmd, arg):
    line = OP.format(cmd, arg)
    assert "\n" not in line
    assert OP.parse(line) == (cmd, arg.replace("\n", " "))


def test_format_keeps_one_message_per_line():
    assert OP.format("text", "first line\nsecond\r\nthird") == "text first line second  third"
    assert OP.format("show") == "show "
    assert OP.format("hide") == "hide"
    assert OP.format("state", "done") == "state done"


def test_every_state_is_known():
    assert set(OP.STATES) == {"listening", "command", "handsfree", "transcribing", "done", "error", "nospeech"}


def test_hex_to_rgb():
    assert OP.hex_to_rgb("#ff8000") == (1.0, 128 / 255, 0.0)
    assert OP.hex_to_rgb("#fff") == (1.0, 1.0, 1.0)
    for bad in ("", "#12345", "red", "#gg0000", "#+10000"):
        with pytest.raises(ValueError):
            OP.hex_to_rgb(bad)


def test_windarchy_palette(fixtures):
    assert OP.omarchy_palette(fixtures / "colors.toml") == {
        "bg": "#f3eefa", "fg": "#221c33", "accent": "#5b2bb5", "hot": "#b42318", "dim": "#6b6480", "dark": False,
    }


def test_dark_theme_without_mode_falls_back_to_luminance(tmp_path):
    path = tmp_path / "colors.toml"
    path.write_text('color0 = "#1E1E2E"\ncolor1 = "#f38ba8"\ncolor4 = "#89b4fa"\ncolor7 = "#cdd6f4"\n')
    assert OP.omarchy_palette(path) == {
        "bg": "#1e1e2e", "fg": "#cdd6f4", "accent": "#89b4fa", "hot": "#f38ba8",
        "dim": "#767a91", "dark": True,                 # no muted/color8: halfway between text and background
    }


def test_palette_key_precedence(tmp_path):
    path = tmp_path / "colors.toml"
    path.write_text('theme_type = "dark"\nbackground = "#eeeeee"\ncolor0 = "#000000"\n'
                    'foreground = "#111111"\naccent = "#123456"\ncolor4 = "#654321"\ncolor8 = "#888888"\n')
    p = OP.omarchy_palette(path)
    assert (p["bg"], p["accent"], p["dim"], p["dark"]) == ("#eeeeee", "#123456", "#888888", True)


def test_light_background_without_mode_is_light(tmp_path):
    path = tmp_path / "colors.toml"
    path.write_text('background = "#fafafa"\nforeground = "#222222"\naccent = "#0055ff"\n')
    assert OP.omarchy_palette(path)["dark"] is False


@pytest.mark.parametrize("text", [
    "",                                                  # nothing usable
    'background = "#000000"\nforeground = "#ffffff"\n',  # no accent
    'background = "#000000"\nforeground = "#ffffff"\naccent = "purple"\n',
    "this is not toml = = =",
])
def test_unusable_palettes(tmp_path, text):
    path = tmp_path / "colors.toml"
    path.write_text(text)
    assert OP.omarchy_palette(path) is None


def test_missing_theme_file(tmp_path):
    assert OP.omarchy_palette(tmp_path / "nope" / "colors.toml") is None


def test_theme_file_follows_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert OP.theme_colors_file() == tmp_path / "omarchy/current/theme/colors.toml"


def _overlay_loader_namespace() -> dict:
    """Run the part of overlay.py before its GTK imports: the parser loader."""
    source = OVERLAY.read_text(encoding="utf-8")
    head, sep, _ = source.partition("\nimport gi\n")
    assert sep, "overlay.py should load its parser before importing gi"
    ns = {"__file__": str(OVERLAY), "__name__": "overlay_head"}
    exec(compile(head, str(OVERLAY), "exec"), ns)
    return ns


def test_overlay_parses_lines_like_the_daemon():
    lines = _overlay_loader_namespace()["lines"]
    for line in LINES:
        assert lines.parse(line) == OP.parse(line), line
    assert lines.STATES == OP.STATES


def test_overlay_loads_its_parser_under_an_isolated_python(tmp_path):
    """overlay.py runs as `python3 -I`: no chttr on the path, only the file next to it."""
    head = OVERLAY.read_text(encoding="utf-8").partition("\nimport gi\n")[0]
    script = tmp_path / "overlay.py"
    script.write_text(head + "\nprint(lines.parse('state done'), lines.parse('0.5 -3'))\n")
    (tmp_path / "overlay_lines.py").write_text(OVERLAY.with_name("overlay_lines.py").read_text(encoding="utf-8"))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    out = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == "('state', 'done') ('level', 0.5, -3.0)"


def test_overlay_handles_every_command():
    source = OVERLAY.read_text(encoding="utf-8")
    for cmd in OP.COMMANDS:
        assert f'cmd == "{cmd}"' in source, cmd
    assert "--palette" in source and "--captions" in source and "--label" in source
