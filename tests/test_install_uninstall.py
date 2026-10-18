"""`chttr uninstall` undoes `chttr setup` byte for byte, in a throwaway home with
systemctl, hyprctl and swaymsg faked out."""

from __future__ import annotations

import subprocess

import pytest

from chttr import config as C
from chttr import desktop
from chttr import install as I
from chttr import native_host as NH

EXE = "/home/u/.local/bin/chttr"
LUA = '-- my bindings\no.bind("SUPER + Return", "Terminal", "uwsm-app -- xdg-terminal-exec")\n'
CONF = "# my bindings\nbindd = SUPER, Return, Terminal, exec, $terminal\n\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(I, "UNIT", tmp_path / ".config/systemd/user/chttr.service")
    monkeypatch.setattr(I, "HYPR", tmp_path / ".config/hypr")
    monkeypatch.setattr(I, "OMARCHY_PLUGIN", tmp_path / ".config/omarchy/plugins/robjobbed.chttr")
    monkeypatch.setattr(NH, "BROWSERS", [(("chromium",), tmp_path / ".config/chromium",
                                          tmp_path / ".config/chromium-flags.conf")])
    monkeypatch.setattr(NH, "BROWSER_DIRS", [(tmp_path / ".config/chromium", tmp_path / ".config/chromium-flags.conf")])
    monkeypatch.setattr(C, "DATA_DIR", tmp_path / ".local/share/chttr")
    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path / ".config/chttr")
    monkeypatch.setattr(C, "CONFIG_FILE", tmp_path / ".config/chttr/config.toml")
    monkeypatch.setattr(C, "SOCKET_PATH", tmp_path / "no-daemon.sock")
    monkeypatch.setattr(I.shutil, "which", lambda name: f"{tmp_path}/.local/bin/{name}" if name == "chttr" else None)
    from chttr import vault

    monkeypatch.setattr(vault, "remove", lambda name: False)        # never the real keyring
    (tmp_path / ".config/hypr").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def ran(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(I.subprocess, "run", fake_run)
    return calls


def on(monkeypatch, compositor):
    monkeypatch.setattr(desktop, "detect", lambda env=None: desktop.Desktop("wayland", compositor))


@pytest.mark.parametrize(("name", "original"), [("bindings.lua", LUA), ("bindings.conf", CONF),
                                                ("bindings.lua", LUA.rstrip("\n")), ("bindings.conf", "")])
def test_hyprland_bindings_come_back_byte_identical(home, ran, monkeypatch, name, original):
    on(monkeypatch, "hyprland")
    path = home / ".config/hypr" / name
    path.write_text(original)
    I.keybindings(EXE)
    assert f"{EXE} start" in path.read_text() and path.with_name(name + ".bak-chttr").exists()
    done = I.uninstall()
    assert path.read_text() == original
    assert not path.with_name(name + ".bak-chttr").exists()
    assert ["hyprctl", "reload"] in ran
    assert f"keybindings: restored ~/.config/hypr/{name}" in done


def test_bindings_you_added_after_setup_stay(home, ran, monkeypatch):
    on(monkeypatch, "hyprland")
    path = home / ".config/hypr/bindings.lua"
    path.write_text(LUA)
    I.keybindings(EXE)
    path.write_text(path.read_text() + 'o.bind("SUPER + B", "Browser", "chromium")\n')
    I.uninstall()
    assert path.read_text() == LUA + 'o.bind("SUPER + B", "Browser", "chromium")\n'
    assert path.with_name("bindings.lua.bak-chttr").exists()              # not the same file any more: kept


def test_bindings_setup_didnt_add_are_left_alone(home, ran):
    path = home / ".config/hypr/bindings.lua"
    mine = 'o.bind("F9", "dictate", "chttr start")\n'
    path.write_text(mine)
    done = I.uninstall()
    assert path.read_text() == mine
    assert any("remove them by hand" in line for line in done)
    assert ["hyprctl", "reload"] not in ran


@pytest.mark.parametrize("original", ["output * bg #000000 solid_color\n", None])
def test_sway_comes_back(home, ran, monkeypatch, original):
    on(monkeypatch, "sway")
    config = home / ".config/sway/config"
    if original is not None:
        config.parent.mkdir(parents=True)
        config.write_text(original)
    I.keybindings(EXE)
    assert (home / ".config/sway/config.d/chttr.conf").exists() and "include" in config.read_text()
    I.uninstall()
    assert not (home / ".config/sway/config.d/chttr.conf").exists()
    if original is None:
        assert not config.exists()                                       # setup created it for the include
    else:
        assert config.read_text() == original and not (home / ".config/sway/config.bak-chttr").exists()
    assert ["swaymsg", "reload"] in ran


def test_the_service_is_disabled_and_removed(home, ran):
    I.UNIT.parent.mkdir(parents=True)
    I.UNIT.write_text(I.UNIT_TEXT.format(exe=EXE))
    done = I.uninstall()
    assert not I.UNIT.exists()
    assert ran[:2] == [["systemctl", "--user", "disable", "--now", "chttr.service"],
                       ["systemctl", "--user", "daemon-reload"]]
    assert done[0].startswith("service: stopped and removed")


def test_the_old_autostart_line_comes_back(home, ran):
    path = home / ".config/hypr/autostart.conf"
    original = "exec-once = uwsm-app -- mako\nexec-once = chttr daemon\n"
    path.write_text(original)
    I._comment_out_autostart(EXE)
    assert path.read_text().startswith("exec-once = uwsm-app -- mako\n# exec-once = chttr daemon")
    I.uninstall()
    assert path.read_text() == original and not path.with_name("autostart.conf.bak-chttr").exists()


def test_the_meet_extension_registration_goes(home, ran):
    profile, flags = home / ".config/chromium", home / ".config/chromium-flags.conf"
    profile.mkdir(parents=True)
    original = "--ozone-platform=wayland\n"
    flags.write_text(original)
    NH.install()
    host = profile / "NativeMessagingHosts" / f"{NH.HOST_NAME}.json"
    assert host.exists() and str(NH.EXTENSION_DIR) in flags.read_text()
    done = I.uninstall()
    assert not host.exists() and flags.read_text() == original
    assert not (home / ".config/chromium-flags.conf.bak-chttr").exists()
    assert not (C.DATA_DIR / "native-host").exists()
    assert "meet: native host removed from chromium" in done


def test_purge_asks_first_and_keeps_meeting_notes(home, ran):
    (C.DATA_DIR / "models").mkdir(parents=True)
    (C.DATA_DIR / "history.jsonl").write_text("{}\n")
    C.CONFIG_DIR.mkdir(parents=True)
    notes = home / "Documents/Meetings"
    notes.mkdir(parents=True)
    (notes / "standup.md").write_text("# Standup\n")
    asked = []
    done = I.uninstall(purge=True, ask=lambda q: asked.append(q) or "n")
    assert "~/.local/share/chttr" in asked[0] and asked[0].endswith("[y/N] ")
    assert C.DATA_DIR.exists() and "data: kept (nothing deleted)" in done
    done = I.uninstall(purge=True, ask=lambda q: "y")
    assert not C.DATA_DIR.exists() and not C.CONFIG_DIR.exists()
    assert (notes / "standup.md").exists()
    assert any(line.startswith("meeting notes: left alone in ~/Documents/Meetings") for line in done)


def test_purge_with_yes_does_not_ask_and_spares_notes_inside_the_data_dir(home, ran):
    notes = C.DATA_DIR / "notes"
    notes.mkdir(parents=True)
    (notes / "call.md").write_text("x")
    (C.DATA_DIR / "voices.json").write_text("{}")
    C.CONFIG_DIR.mkdir(parents=True)
    C.CONFIG_FILE.write_text(f'[meeting]\ndir = "{notes}"\n')

    def never(question):
        raise AssertionError("asked")

    I.uninstall(purge=True, yes=True, ask=never)
    assert (notes / "call.md").exists() and not (C.DATA_DIR / "voices.json").exists()
    assert not C.CONFIG_DIR.exists()


def test_purge_without_a_terminal_deletes_nothing(home, ran):
    C.DATA_DIR.mkdir(parents=True)

    def eof(question):
        raise EOFError

    I.uninstall(purge=True, ask=eof)
    assert C.DATA_DIR.exists()


def test_the_last_steps(home, ran):
    done = I.uninstall()
    assert done[0] == "nothing from chttr setup was installed"
    assert done[-1] == "finish with: uv tool uninstall chttr"
    I.OMARCHY_PLUGIN.mkdir(parents=True)
    done = I.uninstall()
    assert done[-2:] == ["finish with: omarchy plugin remove robjobbed.chttr", "             uv tool uninstall chttr"]


def test_accessibility_turns_it_on(home, ran, monkeypatch):
    (home / ".config/chromium").mkdir(parents=True)
    monkeypatch.setattr(I.shutil, "which", lambda name: "/usr/bin/gsettings" if name == "gsettings" else None)
    done = I.accessibility()
    assert ["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility", "true"] in ran
    assert (home / ".config/chromium-flags.conf").read_text() == "--force-renderer-accessibility\n"
    assert len(done) == 2


def test_cmd_uninstall_prints_each_step(home, ran, capsys):
    from chttr import cli

    args = cli.build_parser().parse_args(["uninstall", "--purge", "-y"])
    assert args.purge and args.yes and args.func is cli.cmd_uninstall
    args = cli.build_parser().parse_args(["uninstall"])
    assert cli.cmd_uninstall(args) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "chttr uninstall" and out[-1] == "  finish with: uv tool uninstall chttr"


def test_purge_asks_after_the_rest_is_done(home, ran):
    order = []
    I.UNIT.parent.mkdir(parents=True)
    I.UNIT.write_text("x")
    I.uninstall(purge=True, ask=lambda q: order.append("ask") or "n", say=lambda line: order.append(line.split(":")[0]))
    assert order[:2] == ["service", "ask"]


def test_purge_spares_notes_reached_through_a_link_into_the_data_dir(home, ran):
    real = C.DATA_DIR / "meetings"
    real.mkdir(parents=True)
    (real / "standup.md").write_text("# Standup\n")
    (C.DATA_DIR / "history.jsonl").write_text("{}\n")
    (home / "Meetings").symlink_to(real)
    C.CONFIG_DIR.mkdir(parents=True)
    C.CONFIG_FILE.write_text(f'[meeting]\ndir = "{home / "Meetings"}"\n')
    done = I.uninstall(purge=True, yes=True)
    assert (home / "Meetings/standup.md").read_text() == "# Standup\n"
    assert not (C.DATA_DIR / "history.jsonl").exists()
    assert "data: deleted ~/.local/share/chttr except your meeting notes in ~/Meetings" in done


def test_purge_follows_a_data_dir_moved_elsewhere_and_linked_back(home, ran):
    elsewhere = home / "disk2/chttr"
    (elsewhere / "models").mkdir(parents=True)
    C.DATA_DIR.parent.mkdir(parents=True)
    C.DATA_DIR.symlink_to(elsewhere)
    done = I.uninstall(purge=True, yes=True)
    assert not elsewhere.exists() and not C.DATA_DIR.is_symlink()
    assert "data: deleted ~/.local/share/chttr and ~/disk2/chttr, the folder it linked to" in done
    assert done[-1].startswith("finish with: ")                         # the last steps still show


def test_purge_through_a_link_into_a_shared_folder_deletes_only_chttrs_files(home, ran):
    shared = home / "disk2"
    (shared / "models").mkdir(parents=True)
    (shared / "history.jsonl").write_text("{}\n")
    (shared / "photos").mkdir()
    (shared / "photos/cat.jpg").write_bytes(b"jpg")
    C.DATA_DIR.parent.mkdir(parents=True)
    C.DATA_DIR.symlink_to(shared)
    asked = []
    I.uninstall(purge=True, ask=lambda q: asked.append(q) or "n")
    assert "~/.local/share/chttr (a link to ~/disk2)" in asked[0]
    done = I.uninstall(purge=True, yes=True)
    assert (shared / "photos/cat.jpg").read_bytes() == b"jpg"
    assert not (shared / "models").exists() and not (shared / "history.jsonl").exists()
    assert not C.DATA_DIR.is_symlink()
    assert ("data: deleted chttr's files in ~/disk2, which ~/.local/share/chttr linked to, and the link; "
            "left the 1 other item there alone") in done


def test_purge_reports_what_it_couldnt_delete_and_carries_on(home, ran, monkeypatch):
    C.CONFIG_DIR.mkdir(parents=True)
    C.DATA_DIR.mkdir(parents=True)

    def refuse(path, *a, **kw):
        if path == C.CONFIG_DIR:
            raise PermissionError(13, "Permission denied")
        return real_rmtree(path, *a, **kw)

    real_rmtree = I.shutil.rmtree
    monkeypatch.setattr(I.shutil, "rmtree", refuse)
    done = I.uninstall(purge=True, yes=True)
    assert "data: couldn't delete ~/.config/chttr: Permission denied" in done
    assert "data: deleted ~/.local/share/chttr" in done and not C.DATA_DIR.exists()


def test_purge_forgets_only_the_connections_that_are_stored(home, ran, monkeypatch):
    from chttr import integrations, vault

    removed = []
    monkeypatch.setattr(integrations, "connected", lambda: ["slack"])
    monkeypatch.setattr(vault, "remove", lambda name: removed.append(name) or True)
    done = I.uninstall(purge=True, yes=True)
    assert removed == ["slack"]
    assert [line for line in done if line.startswith("secrets:")] == ["secrets: forgot the Slack connection"]
