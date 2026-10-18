"""`chttr doctor` on fake machines: Arch, Debian/Ubuntu and Fedora, with nothing
real run, read or fixed."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import pytest

from chttr import cli, transcriber
from chttr import config as C
from chttr import doctor as D
from chttr import install as I
from chttr.daemon import LAYER_SHELL_LIBS, SYSTEM_PYTHON
from chttr.desktop import Desktop

RELEASES = {"arch": {"ID": "arch"}, "debian": {"ID": "ubuntu", "ID_LIKE": "debian"}, "fedora": {"ID": "fedora"}}
ALL_TOOLS = {"pw-record", "pw-play", "pw-dump", "pactl", "systemctl", "gsettings", "chttr"}
REPORT = ["desktop:     hyprland (wayland)", "typing:      wtype", "shortcuts:   hyprctl", "clipboard:   wl-clipboard",
          "window info: hyprctl", "menus:       fuzzel"]


class Machine:
    """A fake Env: `runs` maps an argv (as a space-joined string) to (returncode, stdout, stderr)."""

    def __init__(self, distro="arch", tools=ALL_TOOLS, runs=None, files=(), probe=None, desk=None,
                 report=REPORT, hotkeys="compositor", state="idle", free=50e9, missing_flags=()):
        self.calls: list[list[str]] = []
        self.runs = dict(runs or {})
        self.files = {str(f) for f in files}
        self.texts: dict[str, str] = {}
        self.flags_added = 0
        self.tools = set(tools)
        self.state = state
        self.missing = list(missing_flags)
        self.env = D.Env(
            which=lambda t: f"/usr/bin/{t}" if t in self.tools else None,
            run=self.run,
            read_text=lambda p: self.texts.get(str(p), ""),
            exists=lambda p: str(p) in self.files,
            os_release=lambda: RELEASES.get(distro, {"ID": distro}),
            desktop=lambda: desk or Desktop("wayland", "hyprland"),
            report=lambda d: report,
            hotkeys=lambda backend, comp: hotkeys,
            probe=lambda: {"gi": True, "bus": True, "apps": 4} if probe is None else probe,
            send=self.send,
            free_bytes=lambda p: int(free),
            missing_flags=lambda: self.missing,
            ensure_flags=self.ensure_flags,
        )

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        rc, out, err = self.runs.get(" ".join(argv), (1, "", ""))
        return subprocess.CompletedProcess(argv, rc, out, err)

    def send(self, command):
        if self.state is None:
            raise cli.DaemonNotRunning("not running")
        return {"state": self.state}

    def ensure_flags(self):
        self.flags_added += 1
        done = [f"--force-renderer-accessibility added to {f}" for f in self.missing]
        self.missing = []
        return done


@pytest.fixture
def cfg(tmp_path):
    """Defaults, minus anything that would look at your real key file or calendars."""
    c = C.Config(modes=C._default_modes())
    c.polish.api_key_file = ""
    c.polish.backend = "none"
    c.meeting.calendar_file = str(tmp_path / "calendars")
    return c


def overlay_argv() -> str:
    return " ".join([SYSTEM_PYTHON, "-I", "-c", D.OVERLAY_TEST])


# ---- distributions -------------------------------------------------------------------

def test_os_release_parsing_and_families():
    rel = D.parse_os_release('NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\n# comment\n')
    assert rel == {"NAME": "Ubuntu", "ID": "ubuntu", "ID_LIKE": "debian"}
    for os_release, family in (({"ID": "arch"}, "arch"), ({"ID": "endeavouros", "ID_LIKE": "arch"}, "arch"),
                               ({"ID": "pop", "ID_LIKE": "ubuntu debian"}, "debian"), ({"ID": "debian"}, "debian"),
                               ({"ID": "fedora"}, "fedora"), ({"ID": "rocky", "ID_LIKE": "rhel centos fedora"}, "fedora"),
                               ({"ID": "nixos"}, "")):
        assert D.distro(D.Env(os_release=lambda r=os_release: r)) == family


OVERLAY_FIX = {"arch": "sudo pacman -S --needed python-gobject gtk4-layer-shell",
               "debian": "sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-gtk4layershell-1.0",
               "fedora": "sudo dnf install python3-gobject gtk4-layer-shell"}


@pytest.mark.parametrize("distro", ["arch", "debian", "fedora"])
def test_overlay_names_the_packages_when_gtk_layer_shell_is_missing(cfg, distro):
    m = Machine(distro, files=[LAYER_SHELL_LIBS[0]],
                runs={overlay_argv(): (1, "", "ValueError: Namespace Gtk4LayerShell not available")})
    c = D.overlay(cfg, m.env)
    assert not c.ok and not c.auto
    assert c.fix == OVERLAY_FIX[distro]
    assert "Namespace Gtk4LayerShell not available" in c.detail


def test_overlay_without_the_library_and_when_it_works(cfg):
    assert D.overlay(cfg, Machine("debian").env).fix == OVERLAY_FIX["debian"]       # no libgtk4-layer-shell.so
    m = Machine(files=[LAYER_SHELL_LIBS[-1]], runs={overlay_argv(): (0, "", "")})
    assert D.overlay(cfg, m.env).ok


def test_overlay_is_fine_where_there_is_no_layer_shell(cfg):
    c = D.overlay(cfg, Machine(desk=Desktop("wayland", "gnome")).env)
    assert c.ok and c.detail.startswith("notifications only (no layer-shell on gnome")
    cfg.feedback.overlay = False
    assert D.overlay(cfg, Machine().env).detail.startswith("off")


AUDIO_FIX = {"arch": "sudo pacman -S --needed pipewire libpulse",
             "debian": "sudo apt install pipewire-bin pulseaudio-utils",
             "fedora": "sudo dnf install pipewire-utils pulseaudio-utils"}


@pytest.mark.parametrize("distro", ["arch", "debian", "fedora"])
def test_audio_tools_name_the_packages(cfg, distro):
    c = D.audio_tools(cfg, Machine(distro, tools=ALL_TOOLS - {"pw-dump", "pactl"}).env)
    assert not c.ok and c.fix == AUDIO_FIX[distro]
    assert c.detail == "missing pw-dump (call detection), pactl (auto-unmute)"
    assert D.audio_tools(cfg, Machine(distro).env).ok


def test_unknown_distributions_get_the_package_names_anyway(cfg):
    c = D.audio_tools(cfg, Machine("nixos", tools=ALL_TOOLS - {"pactl"}).env)
    assert c.fix == "install libpulse (Arch names) with your package manager"


# ---- typing, hotkeys -----------------------------------------------------------------

def test_ydotool_without_its_daemon_is_an_auto_fix(cfg):
    report = [*REPORT[:1], "typing:      ydotool", *REPORT[2:]]
    m = Machine(report=report, runs={"systemctl --user is-active ydotool": (3, "inactive\n", ""),
                                     "systemctl --user cat ydotool.service": (0, "[Service]", "")})
    c = D.typing(cfg, m.env)
    assert not c.ok and c.auto and c.fix == "systemctl --user enable --now ydotool"
    m.runs["systemctl --user is-active ydotool"] = (0, "active\n", "")
    assert D.typing(cfg, m.env).ok


def test_ydotool_without_a_user_service_is_explained(cfg):
    report = [*REPORT[:1], "typing:      ydotool", *REPORT[2:]]
    c = D.typing(cfg, Machine("debian", report=report).env)
    assert not c.ok and not c.auto and "ydotoold" in c.fix


def test_no_typing_tool_names_the_right_one(cfg):
    report = [*REPORT[:1], "typing:      PROBLEM: none found (text goes to the clipboard)", *REPORT[2:]]
    assert D.typing(cfg, Machine("fedora", report=report).env).fix == "sudo dnf install wtype"
    kde = D.typing(cfg, Machine("debian", report=report, desk=Desktop("wayland", "kde")).env)
    assert kde.fix.splitlines() == ["sudo apt install ydotool", "systemctl --user enable --now ydotool"]
    x11 = D.typing(cfg, Machine(report=report, desk=Desktop("x11", "x11")).env)
    assert x11.fix == "sudo pacman -S --needed xdotool" and not x11.ok


def test_hotkeys_none_says_how(cfg):
    c = D.hotkeys(cfg, Machine(hotkeys="none").env)
    assert not c.ok and c.fix == "sudo usermod -aG input $USER" and "log out" in c.detail
    assert D.hotkeys(cfg, Machine(hotkeys="portal").env).ok


def test_desktop_report_lines_become_checks(cfg):
    m = Machine()
    assert D.desktop(cfg, m.env).detail == "hyprland (wayland)"
    assert D.window_info(cfg, m.env).detail == "hyprctl"
    assert D.menus(cfg, m.env).detail == "fuzzel"


# ---- service -------------------------------------------------------------------------

def test_a_stopped_service_shows_its_log_and_the_fix(cfg):
    m = Machine(state=None, files=[I.UNIT, "/home/u/.local/bin/chttr"],
                runs={"systemctl --user is-active chttr.service": (3, "failed\n", ""),
                      "systemctl --user is-enabled chttr.service": (0, "enabled\n", ""),
                      JOURNAL: (0, "Traceback\nOSError: boom\n", "")})
    m.texts[str(I.UNIT)] = "[Service]\nExecStart=/home/u/.local/bin/chttr daemon\n"
    c = D.service(cfg, m.env)
    assert not c.ok and c.auto and c.fix == "systemctl --user enable --now chttr"
    assert c.detail.splitlines() == ["not running: chttr.service is failed", "log: Traceback", "log: OSError: boom"]


JOURNAL = "journalctl --user -u chttr -n 50 --no-pager -o cat"


def test_the_log_shows_failures_and_never_what_you_dictated():
    log = """\
INFO heard: 'meet me at the clinic at noon'
INFO you changed 'Pryanka' to 'Priyanka'
Traceback (most recent call last):
  File "/x/daemon.py", line 3, in _process
    text = polish(raw)
RuntimeError: polish failed on 'meet me at the clinic'
chttr.service: Main process exited, code=exited, status=1/FAILURE
INFO listening on /run/user/1000/chttr.sock
"""
    m = Machine(runs={JOURNAL: (0, log, "")})
    assert D._journal(m.env, keep=10) == [
        "Traceback (most recent call last):",
        '  File "/x/daemon.py", line 3, in _process',
        "    text = polish(raw)",
        "RuntimeError: polish failed on '…'",
        "chttr.service: Main process exited, code=exited, status=1/FAILURE"]


def test_a_crash_looping_service_is_a_problem_even_while_systemd_calls_it_active(cfg):
    m = Machine(state=None, files=[I.UNIT],
                runs={"systemctl --user is-active chttr.service": (0, "activating\n", ""),
                      "systemctl --user is-enabled chttr.service": (0, "enabled\n", ""),
                      "systemctl --user show chttr.service -p NRestarts --value": (0, "4\n", ""),
                      JOURNAL: (0, "OSError: no input device\n", "")})
    c = D.service(cfg, m.env)
    assert not c.ok and not c.auto
    assert c.detail.splitlines() == ["chttr.service keeps restarting (4 times) and the daemon isn't answering",
                                     "log: OSError: no input device"]


def test_service_states(cfg):
    m = Machine(files=[I.UNIT], runs={"systemctl --user is-active chttr.service": (0, "active\n", ""),
                                      "systemctl --user is-enabled chttr.service": (0, "enabled\n", "")})
    assert D.service(cfg, m.env) == D.Check("service", True, "idle (chttr.service)")
    m.runs["systemctl --user is-enabled chttr.service"] = (1, "disabled\n", "")
    c = D.service(cfg, m.env)
    assert not c.ok and c.auto and "won't start when you log in" in c.detail
    none = D.service(cfg, Machine(state=None).env)                 # no unit, no daemon
    assert not none.ok and none.auto and none.fix == D.SETUP_SERVICE
    outside = D.service(cfg, Machine(state="idle").env)            # no unit, daemon started by hand
    assert outside.ok and "outside systemd" in outside.detail


def test_a_unit_pointing_at_a_missing_chttr_is_reinstalled(cfg):
    m = Machine(files=[I.UNIT])
    m.texts[str(I.UNIT)] = "[Service]\nExecStart=/old/venv/bin/chttr daemon\n"
    c = D.service(cfg, m.env)
    assert not c.ok and c.auto and c.fix == D.SETUP_SERVICE and "/old/venv/bin/chttr" in c.detail


# ---- screen context --------------------------------------------------------------------

def test_screen_context_without_an_accessibility_bus(cfg):
    m = Machine("debian", probe={"gi": True, "bus": False, "apps": 0},
                runs={"gsettings get org.gnome.desktop.interface toolkit-accessibility": (0, "false\n", "")})
    c = D.screen_context(cfg, m.env)
    assert not c.ok and not c.auto
    assert c.detail.startswith("no accessibility bus: Chromium, Electron and Qt apps need toolkit-accessibility")
    assert c.fix.splitlines() == ["sudo apt install at-spi2-core", D.GSETTINGS_ON]


def test_screen_context_needs_toolkit_accessibility_and_the_chromium_flag(cfg, tmp_path):
    flags = tmp_path / "chromium-flags.conf"
    m = Machine(missing_flags=[flags],
                runs={"gsettings get org.gnome.desktop.interface toolkit-accessibility": (0, "false\n", "")})
    c = D.screen_context(cfg, m.env)
    assert not c.ok and c.auto
    assert c.detail.splitlines()[0] == "AT-SPI bus reachable, 4 apps, but:"
    assert "Chromium needs --force-renderer-accessibility" in c.detail
    assert c.fix.splitlines()[0] == D.GSETTINGS_ON


def test_screen_context_ok_missing_pygobject_and_off(cfg):
    m = Machine(runs={"gsettings get org.gnome.desktop.interface toolkit-accessibility": (0, "true\n", "")})
    assert D.screen_context(cfg, m.env) == D.Check("screen_context", True, "AT-SPI bus reachable, 4 apps")
    no_gi = D.screen_context(cfg, Machine("fedora", probe={"gi": False, "bus": False, "apps": 0}).env)
    assert not no_gi.ok and no_gi.fix == "sudo dnf install python3-gobject at-spi2-core"
    silent = D.screen_context(cfg, Machine(probe={}).env)            # the helper didn't answer at all
    assert not silent.ok and "didn't answer" in silent.detail and "pacman" not in silent.fix
    assert silent.fix.startswith(f"run it to see why: {SYSTEM_PYTHON} -I ") and silent.fix.endswith(" --probe")
    cfg.polish.screen_context, cfg.history.learn_from_edits = "off", False
    assert D.screen_context(cfg, Machine(probe={}).env).ok


# ---- engine, keys, disk ------------------------------------------------------------------

def test_engine_without_the_model_downloads_it(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(transcriber, "PARAKEET_SEARCH_DIRS", [tmp_path / "nowhere"])
    c = D.engine(cfg, Machine().env)
    assert not c.ok and c.auto and c.fix == "chttr setup --no-service --no-keybindings --no-meet"
    cfg.engine.parakeet_dir = str(tmp_path / "custom")
    custom = D.engine(cfg, Machine().env)
    assert not custom.ok and not custom.auto and "parakeet_dir" in custom.fix


def test_engine_with_the_model(cfg, tmp_path):
    for name in transcriber._PARAKEET_FILES:
        (tmp_path / name).write_text("x")
    cfg.engine.parakeet_dir = str(tmp_path)
    c = D.engine(cfg, Machine().env)
    assert c.ok and c.detail.startswith("parakeet (")


def test_an_open_api_key_file_is_an_auto_fix(cfg, tmp_path):
    key = tmp_path / "anthropic-api-key"
    key.write_text("sk-ant-test")
    key.chmod(0o644)
    cfg.polish.api_key_file = str(key)
    c = D.api_key_perms(cfg, Machine().env)
    assert not c.ok and c.auto and c.fix == f"chmod 600 {key}"
    key.chmod(0o600)
    assert D.api_key_perms(cfg, Machine().env).ok
    cfg.polish.api_key_file = str(tmp_path / "missing")
    assert D.api_key_perms(cfg, Machine().env) is None                   # nothing to say


def test_cleanup_without_a_backend_points_at_the_options(cfg, monkeypatch):
    from chttr import polish

    monkeypatch.setattr(polish, "backend", lambda pc: "none")
    c = D.cleanup(cfg, Machine().env)
    assert c.ok and c.detail == "local rules; add a key: chttr connect claude, or run Ollama"


def test_disk(cfg):
    assert D.disk(cfg, Machine(free=50e9).env).ok
    low = D.disk(cfg, Machine(free=0.4e9).env)
    assert not low.ok and "0.4 GB" in low.detail


# ---- run_all, render, fixes ---------------------------------------------------------------

def test_run_all_survives_a_broken_check(cfg, monkeypatch):
    def broken(cfg, env):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(D, "CHECKS", [D.modes, broken, D.disk])
    checks = D.run_all(cfg, Machine().env)
    assert [c.name for c in checks] == ["modes", "broken", "disk"]
    assert not checks[1].ok and "kaboom" in checks[1].detail


def test_render():
    checks = [D.Check("engine", True, "parakeet (~/models)"),
              D.Check("service", False, "not running: chttr.service is failed\nlog: OSError: boom",
                      "systemctl --user enable --now chttr", auto=True),
              D.Check("overlay", False, "no waveform", "sudo pacman -S --needed python-gobject gtk4-layer-shell"),
              D.Check("window_info", True, "hyprctl")]
    assert D.render(checks) == """\
engine:      parakeet (~/models)
daemon:      PROBLEM: not running: chttr.service is failed
             log: OSError: boom
             fix: systemctl --user enable --now chttr
overlay:     PROBLEM: no waveform
             fix: sudo pacman -S --needed python-gobject gtk4-layer-shell
window info: hyprctl

chttr doctor --fix runs the safe fixes (no sudo) for: daemon"""
    assert "--fix" not in D.render(checks, hint=False)


def test_apply_fixes_runs_only_the_auto_fixes_and_checks_again(cfg, monkeypatch):
    monkeypatch.setattr(D, "CHECKS", [D.typing, D.hotkeys, D.modes])
    report = [*REPORT[:1], "typing:      ydotool", *REPORT[2:]]
    m = Machine(report=report, hotkeys="none",
                runs={"systemctl --user cat ydotool.service": (0, "", ""),
                      "systemctl --user enable --now ydotool": (0, "", "")})
    checks = D.run_all(cfg, m.env)
    typing, hotkeys = (next(c for c in checks if c.name == n) for n in ("typing", "hotkeys"))
    assert not typing.ok and typing.auto and not hotkeys.ok and not hotkeys.auto
    m.runs["systemctl --user is-active ydotool"] = (0, "active\n", "")   # what enabling it does
    m.calls.clear()
    lines = D.apply_fixes([typing, hotkeys], m.env, cfg)
    assert ["systemctl", "--user", "enable", "--now", "ydotool"] in m.calls
    assert not any("usermod" in " ".join(call) for call in m.calls)     # sudo: never run
    assert lines == ["typing:      ran systemctl --user enable --now ydotool: fixed"]


def test_apply_fixes_reports_a_failing_fix(cfg):
    m = Machine(runs={"systemctl --user enable --now chttr": (1, "", "Failed to enable unit: Unit file chttr.service does not exist.")})
    check = D.Check("service", False, "not running", "systemctl --user enable --now chttr", auto=True)
    assert D.apply_fixes([check], m.env, cfg) == [
        "daemon:      systemctl --user enable --now chttr: failed (Failed to enable unit: Unit file chttr.service does not exist.)"]


def test_apply_fixes_runs_chttr_through_this_python_when_it_isnt_on_path(cfg):
    m = Machine(tools=ALL_TOOLS - {"chttr"})
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "", "")

    m.env.run = run
    check = D.Check("engine", False, "no model", D.SETUP_MODELS, auto=True)
    D.apply_fixes([check], m.env, cfg)
    argv, kw = seen[0]
    assert argv[:3] == [sys.executable, "-m", "chttr"] and argv[3] == "setup"
    assert kw == {"timeout": None, "capture": False}                     # progress shows, no time limit


def test_uncaptured_output_goes_to_stderr_so_json_stays_clean(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, None, None)

    monkeypatch.setattr(D.subprocess, "run", fake_run)
    monkeypatch.setattr(D, "_stderr", lambda: sys.__stderr__)
    D._run(["chttr", "setup"], timeout=None, capture=False)
    assert seen["stdout"] is sys.__stderr__ and "capture_output" not in seen
    D._run(["true"])
    assert seen["capture_output"] is True


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


START = "systemctl --user enable --now chttr"


def _service_machine(**kw):
    m = Machine(files=[I.UNIT], state=None,
                runs={START: (0, "", ""), "systemctl --user is-active chttr.service": (0, "active\n", ""),
                      "systemctl --user is-enabled chttr.service": (0, "enabled\n", "")}, **kw)
    m.env.clock = clock = Clock()
    m.env.sleep = clock.sleep
    return m, clock


def test_a_started_service_counts_as_fixed_once_the_daemon_answers(cfg):
    m, clock = _service_machine()
    check = D.Check("service", False, "not running", START, auto=True)
    answers = iter([None, None, None, "idle"])

    def send(command):
        state = next(answers, "idle")
        if state is None:
            raise cli.DaemonNotRunning("not yet")
        return {"state": state}

    m.env.send = send
    assert D.apply_fixes([check], m.env, cfg) == [f"daemon:      ran {START}: fixed"]
    assert 0 < clock.now <= D.SETTLE


def test_a_service_that_crashes_right_after_starting_is_not_fixed(cfg):
    m, clock = _service_machine()
    check = D.Check("service", False, "not running", START, auto=True)

    def sleep(seconds):                 # it dies a moment later and systemd gives up
        clock.now += seconds
        m.runs["systemctl --user is-active chttr.service"] = (3, "failed\n", "")

    m.env.sleep = sleep
    lines = D.apply_fixes([check], m.env, cfg)
    assert lines == [f"daemon:      ran {START}: still a problem: not running: chttr.service is failed"]
    assert clock.now >= D.SETTLE


def test_a_started_service_that_never_answers_says_so(cfg):
    m, _ = _service_machine()
    check = D.Check("service", False, "not running", START, auto=True)
    assert D.apply_fixes([check], m.env, cfg) == [
        f"daemon:      ran {START}: started, but it isn't answering yet: journalctl --user -u chttr"]


def test_apply_fixes_turns_on_screen_context(cfg, tmp_path):
    flags = tmp_path / "chromium-flags.conf"
    gsettings = "gsettings get org.gnome.desktop.interface toolkit-accessibility"
    m = Machine(missing_flags=[flags], runs={gsettings: (0, "false\n", ""), D.GSETTINGS_ON: (0, "", "")})
    check = D.screen_context(cfg, m.env)
    m.runs[gsettings] = (0, "true\n", "")
    lines = D.apply_fixes([check], m.env, cfg)
    assert D.GSETTINGS_ON.split() in m.calls and m.flags_added == 1
    assert lines[0] == f"screen ctx:  ran {D.GSETTINGS_ON}: fixed"
    assert f"{D.INDENT}--force-renderer-accessibility added to {flags}" in lines


# ---- the command ---------------------------------------------------------------------------

def _args(**kw):
    return argparse.Namespace(**{"verify": False, "mic": False, "json": False, "fix": False, **kw})


SAMPLE = [D.Check("engine", True, "parakeet"),
          D.Check("service", False, "not running", "systemctl --user enable --now chttr", auto=True)]


def test_doctor_json(monkeypatch, capsys):
    monkeypatch.setattr(D, "run_all", lambda cfg, env=None: SAMPLE)
    assert cli.cmd_doctor(_args(json=True)) == 1
    data = json.loads(capsys.readouterr().out)
    assert data[1] == {"name": "service", "ok": False, "detail": "not running",
                       "fix": "systemctl --user enable --now chttr", "auto": True}


def test_doctor_fix_applies_then_shows_the_new_state(monkeypatch, capsys):
    rounds = [SAMPLE, [SAMPLE[0], D.Check("service", True, "idle (chttr.service)")]]
    monkeypatch.setattr(D, "run_all", lambda cfg, env=None: rounds.pop(0))
    monkeypatch.setattr(D, "apply_fixes", lambda checks, env=None, cfg=None: ["daemon:      ran it: fixed"])
    assert cli.cmd_doctor(_args(fix=True)) == 0
    out = capsys.readouterr().out
    assert "fixing 1 problem…" in out and "daemon:      ran it: fixed" in out
    assert out.rstrip().endswith("daemon:      idle (chttr.service)")


@pytest.mark.parametrize(("checks", "message"), [
    ([SAMPLE[0]], "nothing to fix"),
    ([SAMPLE[0], D.Check("hotkeys", False, "none", "sudo usermod -aG input $USER")],
     "nothing --fix can do on its own; see the fix: lines below")])
def test_doctor_fix_with_nothing_to_run(monkeypatch, capsys, checks, message):
    monkeypatch.setattr(D, "run_all", lambda cfg, env=None: checks)
    monkeypatch.setattr(D, "apply_fixes", lambda checks, env=None, cfg=None: [])
    cli.cmd_doctor(_args(fix=True))
    assert capsys.readouterr().out.splitlines()[0] == message


def test_doctor_text(monkeypatch, capsys):
    monkeypatch.setattr(D, "run_all", lambda cfg, env=None: SAMPLE)
    assert cli.cmd_doctor(_args()) == 1
    out = capsys.readouterr().out
    assert out.startswith("config:      ") and "fix: systemctl --user enable --now chttr" in out


def test_doctor_parser_flags():
    args = cli.build_parser().parse_args(["doctor", "--json", "--fix"])
    assert args.json and args.fix and args.func is cli.cmd_doctor


def test_meet_extension_and_info_checks(cfg, monkeypatch, tmp_path):
    from chttr import native_host

    profile = tmp_path / "chromium"
    monkeypatch.setattr(native_host, "BROWSER_DIRS", [(profile, tmp_path / "chromium-flags.conf")])
    m = Machine()
    assert D.meet_extension(cfg, m.env).detail == "not installed (chttr meeting install)"
    m.files.add(str(profile / "NativeMessagingHosts" / f"{native_host.HOST_NAME}.json"))
    assert D.meet_extension(cfg, m.env).detail == "registered for chromium"
    assert D.meetings(cfg, m.env).detail.startswith("notes to ")
    assert D.modes(cfg, m.env).detail.startswith("terminal")
