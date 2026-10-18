from __future__ import annotations

from chttr import theme as T


def test_windarchy_is_light_with_its_lavender_accent(fixtures):
    t = T.load_theme(fixtures / "colors.toml")
    assert t["mode"] == "light"
    v = t["vars"]
    assert v["--accent"] == "#5b2bb5" and v["--bg"] == "#f3eefa" and v["--text"] == "#221c33"
    assert v["--danger"] == "#b42318" and v["--accent-soft"] == "rgba(91, 43, 181, 0.18)"
    assert v["--mark"].startswith("rgba(") and v["--mark"].endswith(", 0.35)")
    assert v["--c1"] == "#5b2bb5" and v["--c8"] == "#5b2bb5"           # blue is color4, the accent here
    assert "--c5" not in v                                             # no orange: app.css keeps its own


def test_dark_theme_without_mode_is_read_from_the_background(fixtures):
    t = T.load_theme(fixtures / "colors-dark.toml")
    assert t["mode"] == "dark"
    v = t["vars"]
    assert v["--accent"] == "#7aa2f7" and v["--danger"] == "#f7768e" and v["--c5"] == "#ff9e64"
    assert v["--line"] != v["--bg"]                                    # a 12 % mix of text into the page


def test_small_text_stays_readable(fixtures):
    for name in ("colors.toml", "colors-dark.toml"):
        v = T.load_theme(fixtures / name)["vars"]
        assert T.contrast(v["--muted"], v["--panel"]) >= 4.5
        for i in range(1, 9):
            if f"--c{i}" in v:
                assert T.contrast(v[f"--c{i}"], v["--bg"]) >= 4.5, i


def test_mode_and_legacy_theme_type(tmp_path):
    f = tmp_path / "colors.toml"
    f.write_text('theme_type = "light"\nbackground = "#000000"\nforeground = "#ffffff"\n')
    assert T.load_theme(f)["mode"] == "light"
    f.write_text('mode = "dark"\ntheme_type = "light"\nbackground = "#ffffff"\n')
    assert T.load_theme(f)["mode"] == "dark"


def test_terminal_colours_and_odd_values(tmp_path):
    f = tmp_path / "colors.toml"
    f.write_text('color0 = "#FFF"\ncolor7 = "222222"\ncolor4 = "#3355ffcc"\naccent = "not a colour"\nred = 12\n')
    t = T.load_theme(f)
    assert t["mode"] == "light"
    assert t["vars"]["--bg"] == "#ffffff" and t["vars"]["--text"] == "#222222" and t["vars"]["--accent"] == "#3355ff"
    assert "--danger" not in t["vars"]


def test_missing_or_broken_theme_is_none(tmp_path, monkeypatch):
    assert T.load_theme(tmp_path / "nope.toml") is None
    bad = tmp_path / "bad.toml"
    bad.write_text("accent = ")
    assert T.load_theme(bad) is None
    empty = tmp_path / "empty.toml"
    empty.write_text('name = "nothing useful"\n')
    assert T.load_theme(empty) is None
    monkeypatch.setattr(T, "OMARCHY_COLORS", tmp_path / "nope.toml")
    assert T.load_theme() is None
