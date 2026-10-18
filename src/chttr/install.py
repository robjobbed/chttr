"""`chttr setup`: everything after `uv tool install`, safe to run again.

- config file, speech models (Parakeet if voxtype hasn't downloaded it, VAD and
  speaker embeddings for meetings)
- a systemd user service for the daemon (replacing a Hyprland autostart line)
- keybindings: Hyprland and Sway get bindings in their config (if there are no
  chttr bindings yet); GNOME and KDE use the global-shortcuts portal and other
  desktops the keyboard devices, both from inside the daemon (see hotkeys.py)
- the Google Meet extension and its native messaging host
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from . import config as C
from . import models as M

UNIT = Path.home() / ".config/systemd/user/chttr.service"
HYPR = Path.home() / ".config/hypr"

UNIT_TEXT = """\
[Unit]
Description=chttr dictation and meeting daemon
PartOf=graphical-session.target
After=graphical-session.target pipewire.service

[Service]
ExecStart={exe} daemon
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
"""

BINDINGS_LUA = """
-- chttr (https://github.com/robjobbed/chttr): hold Insert to dictate, release to type.
-- Alt+Insert: say what to do with the selected text. Super+Insert: hands-free.
-- Super+Shift+Insert: dictation history. Super+Alt+Insert: record a meeting.
o.bind("Insert", "chttr: push-to-talk", "{exe} start")
o.bind("Insert", "chttr: push-to-talk (release)", "{exe} release", {{ release = true }})
o.bind("ALT + Insert", "chttr: command mode on selection", "{exe} command")
o.bind("ALT + Insert", "chttr: command mode (release)", "{exe} release", {{ release = true }})
o.bind("SUPER + Insert", "chttr: hands-free dictation", "{exe} toggle")
o.bind("SUPER + SHIFT + Insert", "chttr: dictation history", "{exe} history")
o.bind("SUPER + ALT + Insert", "chttr: record meeting (toggle)", "{exe} meeting toggle")
"""

SWAY_BINDINGS = """
# chttr (https://github.com/robjobbed/chttr): hold Insert to dictate, release to type.
bindsym Insert exec {exe} start
bindsym --release Insert exec {exe} release
bindsym Mod1+Insert exec {exe} command
bindsym --release Mod1+Insert exec {exe} release
bindsym Mod4+Insert exec {exe} toggle
bindsym Mod4+Shift+Insert exec {exe} history
bindsym Mod4+Mod1+Insert exec {exe} meeting toggle
"""

BINDINGS_CONF = """
# chttr (https://github.com/robjobbed/chttr)
bindd = , Insert, chttr: push-to-talk, exec, {exe} start
bindrd = , Insert, chttr: push-to-talk (release), exec, {exe} release
bindd = ALT, Insert, chttr: command mode, exec, {exe} command
bindrd = ALT, Insert, chttr: command mode (release), exec, {exe} release
bindd = SUPER, Insert, chttr: hands-free dictation, exec, {exe} toggle
bindd = SUPER SHIFT, Insert, chttr: dictation history, exec, {exe} history
bindd = SUPER ALT, Insert, chttr: record meeting, exec, {exe} meeting toggle
"""


def _say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def models() -> None:
    from . import diarize
    from .transcriber import resolve_parakeet

    try:
        _say(f"speech model: Parakeet in {resolve_parakeet('')}")
    except FileNotFoundError:
        target = C.PARAKEET_SEARCH_DIRS[-1]
        _say(f"speech model: downloading Parakeet TDT 0.6B v3 (int8, ~670 MB, checksum-verified) to {target}")
        for spec in M.PARAKEET:
            M.download(spec, target / spec.name, M.console_progress(spec.name))
    _say("meeting models: voice activity + speaker embeddings (~29 MB)")
    diarize.ensure_models()


def _comment_out_autostart(exe: str) -> None:
    for name in ("autostart.lua", "autostart.conf"):
        path = HYPR / name
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        changed = False
        for i, line in enumerate(lines):
            s = line.strip()
            if "chttr daemon" in s and not s.startswith(("--", "#")):
                lines[i] = ("-- " if name.endswith(".lua") else "# ") + line + "   (now a systemd service: chttr.service)"
                changed = True
        if changed:
            shutil.copy2(path, path.with_name(path.name + ".bak-chttr"))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            _say(f"commented out the old chttr daemon line in {path}")


def service(exe: str) -> None:
    UNIT.parent.mkdir(parents=True, exist_ok=True)
    UNIT.write_text(UNIT_TEXT.format(exe=exe), encoding="utf-8")
    _comment_out_autostart(exe)
    if C.SOCKET_PATH.exists():
        subprocess.run([exe, "quit"], capture_output=True, check=False)     # a daemon started some other way
        time.sleep(1)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "chttr.service"], capture_output=True, check=False)
    subprocess.run(["systemctl", "--user", "restart", "chttr.service"], check=False)
    _say("daemon: systemd user service chttr.service (logs: journalctl --user -u chttr)")


def keybindings(exe: str) -> None:
    from .desktop import detect

    desk = detect()
    if desk.compositor == "sway":
        sway_keybindings(exe)
        return
    if desk.compositor != "hyprland" and not (HYPR / "bindings.lua").exists():
        _hotkeys_note(desk.compositor)
        return
    lua, conf = HYPR / "bindings.lua", HYPR / "bindings.conf"
    target, block = (lua, BINDINGS_LUA) if lua.exists() else (conf, BINDINGS_CONF) if conf.exists() else (None, "")
    if target is None:
        _say("keybindings: no ~/.config/hypr/bindings.lua or bindings.conf; see the README to add them")
        return
    text = target.read_text(encoding="utf-8")
    if "chttr" in text:
        _say(f"keybindings: {target} already has chttr bindings; left alone")
        return
    shutil.copy2(target, target.with_name(target.name + ".bak-chttr"))
    target.write_text(text.rstrip("\n") + "\n" + block.format(exe=exe), encoding="utf-8")
    subprocess.run(["hyprctl", "reload"], capture_output=True, check=False)
    _say(f"keybindings: added to {target} (Insert, Alt/Super+Insert, Super+Alt+Insert)")


def sway_keybindings(exe: str) -> None:
    config = Path.home() / ".config/sway/config"
    target = Path.home() / ".config/sway/config.d/chttr.conf"
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if "chttr" in text or target.exists():
        _say("keybindings: sway already has chttr bindings; left alone")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(SWAY_BINDINGS.format(exe=exe), encoding="utf-8")
    if "config.d" not in text:
        if config.exists():
            shutil.copy2(config, config.with_name("config.bak-chttr"))
        config.parent.mkdir(parents=True, exist_ok=True)
        with config.open("a", encoding="utf-8") as fh:
            fh.write(f"\ninclude {target}\n")
    subprocess.run(["swaymsg", "reload"], capture_output=True, check=False)
    _say(f"keybindings: added {target} (Insert, Alt/Super+Insert, Super+Alt+Insert)")


def _hotkeys_note(compositor: str) -> None:
    from . import hotkeys as H

    backend = H.resolve("auto", compositor)
    if backend == "portal":
        _say("keybindings: your desktop's global-shortcuts portal; when the daemon starts it asks you to confirm "
             "chttr's keys (change them later in the desktop's keyboard settings)")
    elif backend == "evdev":
        _say("keybindings: chttr reads your keyboard directly (Insert, Alt/Super+Insert, Super+Alt+Insert)")
    else:
        _say("keybindings: PROBLEM: no way to catch keys here yet. Either add yourself to the input group "
             "(sudo usermod -aG input $USER, then log out and in) or bind `chttr start` (press) and "
             "`chttr release` (release) in your compositor.")


def meet() -> None:
    from .native_host import install

    for line in install() or ["no Chromium-family browser found; skipping the Meet extension"]:
        _say(f"meet: {line}")


def run(do_models: bool = True, do_service: bool = True, do_bindings: bool = True, do_meet: bool = True) -> int:
    exe = shutil.which("chttr") or str(Path.home() / ".local/bin/chttr")
    print("chttr setup")
    if C.write_default_config():
        _say(f"config: created {C.CONFIG_FILE}")
    else:
        _say(f"config: {C.CONFIG_FILE}")
    if do_models:
        models()
    if do_bindings:
        keybindings(exe)
    if do_meet:
        meet()
    if do_service:
        service(exe)
    print("""
Done. Hold Insert and talk.

Optional, for the best results:
  Claude cleanup + meeting notes:  put an Anthropic API key in ~/.config/chttr/anthropic-api-key (chmod 600)
  Meeting attendees:               chttr meeting calendar 'https://calendar.google.com/calendar/ical/…/basic.ics'
                                   (Google Calendar > Settings > your calendar > Secret address in iCal format)
  Speaker names in Meet calls:     restart Chromium once so it loads the chttr extension
Check everything with: chttr doctor""")
    return 0


# ---- screen context ------------------------------------------------------------------

def accessibility() -> list[str]:
    """Let screen context read Chromium, Electron and Qt apps: the desktop-wide
    toolkit-accessibility switch, and Chromium's --force-renderer-accessibility."""
    from . import native_host

    done = []
    if shutil.which("gsettings"):
        try:
            out = subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility", "true"],
                                 capture_output=True, text=True, timeout=10, check=False)
            if out.returncode == 0:
                done.append("toolkit-accessibility on (apps started from now on expose their text)")
        except (OSError, subprocess.TimeoutExpired):
            pass
    return done + native_host.ensure_accessibility_flag()


# ---- uninstall -------------------------------------------------------------------------

BLOCK_HEADER = "chttr (https://github.com/robjobbed/chttr)"
AUTOSTART_MARK = "   (now a systemd service: chttr.service)"
OMARCHY_PLUGIN = Path.home() / ".config/omarchy/plugins/robjobbed.chttr"


def _quiet(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _restore(path: Path, text: str) -> str:
    """Write what's left of a config file after taking chttr out. If that is what
    the .bak-chttr copy holds, put the copy back byte for byte and drop it."""
    backup = path.with_name(path.name + ".bak-chttr")
    if backup.exists() and backup.read_text(encoding="utf-8").rstrip("\n") == text.rstrip("\n"):
        shutil.copy2(backup, path)
        backup.unlink()
        return "restored"
    path.write_text(text, encoding="utf-8")
    return "updated"


def _without_block(text: str, template: str) -> str | None:
    """`text` minus the block keybindings() appended (the header line, the lines
    after it that mention chttr or are the template's own comments, and the blank
    line before it). None if there is no such block."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if BLOCK_HEADER in line), None)
    if start is None:
        return None
    own = {line.strip() for line in template.splitlines() if line.strip()}
    end = start + 1
    while end < len(lines) and lines[end].strip() and ("chttr" in lines[end] or lines[end].strip() in own):
        end += 1
    if start > 0 and not lines[start - 1].strip():
        start -= 1
    return "\n".join(lines[:start] + lines[end:])


def _uninstall_service() -> list[str]:
    done = []
    if UNIT.exists():
        _quiet(["systemctl", "--user", "disable", "--now", "chttr.service"])
        UNIT.unlink()
        _quiet(["systemctl", "--user", "daemon-reload"])
        done.append(f"service: stopped and removed {_home(UNIT)}")
    if C.SOCKET_PATH.exists():                          # a daemon started some other way
        from .client import DaemonNotRunning, send

        try:
            send("quit", timeout=5)
            done.append("daemon: stopped")
        except (DaemonNotRunning, OSError, ValueError):
            pass
    return done


def _uninstall_hyprland() -> list[str]:
    done = []
    changed = False
    for name, template in (("bindings.lua", BINDINGS_LUA), ("bindings.conf", BINDINGS_CONF)):
        path = HYPR / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        rest = _without_block(text, template)
        if rest is None:
            if "chttr" in text:
                done.append(f"keybindings: {_home(path)} has chttr lines setup didn't add; remove them by hand")
            continue
        how = _restore(path, rest)
        changed = True
        done.append(f"keybindings: {how} {_home(path)}")
    for name in ("autostart.lua", "autostart.conf"):
        path = HYPR / name
        if not path.exists() or AUTOSTART_MARK not in path.read_text(encoding="utf-8"):
            continue
        prefix = "-- " if name.endswith(".lua") else "# "
        lines = [line[len(prefix):-len(AUTOSTART_MARK)] if line.startswith(prefix) and line.endswith(AUTOSTART_MARK)
                 else line for line in path.read_text(encoding="utf-8").split("\n")]
        _restore(path, "\n".join(lines))
        done.append(f"autostart: put back your old chttr daemon line in {_home(path)} "
                    "(delete it if you're removing chttr for good)")
    if changed:
        _quiet(["hyprctl", "reload"])
    return done


def _uninstall_sway() -> list[str]:
    done = []
    config = Path.home() / ".config/sway/config"
    target = Path.home() / ".config/sway/config.d/chttr.conf"
    if target.exists():
        target.unlink()
        done.append(f"keybindings: removed {_home(target)}")
    if config.exists():
        text = config.read_text(encoding="utf-8")
        include = f"include {target}"
        if include in text.split("\n"):
            lines = text.split("\n")
            i = lines.index(include)
            del lines[i]
            if i > 0 and not lines[i - 1].strip():
                del lines[i - 1]                       # the blank line setup put before it
            rest = "\n".join(lines)
            backup = config.with_name("config.bak-chttr")
            if backup.exists() and backup.read_text(encoding="utf-8").rstrip("\n") == rest.rstrip("\n"):
                shutil.copy2(backup, config)
                backup.unlink()
            elif not rest.strip() and not backup.exists():
                config.unlink()                         # setup created it just for the include
            else:
                config.write_text(rest, encoding="utf-8")
            done.append(f"keybindings: took the chttr include out of {_home(config)}")
    if done:
        _quiet(["swaymsg", "reload"])
    return done


def _home(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _purge(yes: bool, ask) -> list[str]:
    from . import integrations as I
    from . import vault

    try:
        notes = Path(C.load_config(C.CONFIG_FILE).meeting.dir).expanduser()
    except (OSError, ValueError):
        notes = Path(C.MeetingCfg().dir).expanduser()
    if not yes:
        code = " (and the chttr code installed there)" if (C.DATA_DIR / "src").exists() else ""
        try:
            answer = ask(f"Delete history, meeting data, voices, the library index and the models in "
                         f"{_home(C.DATA_DIR)}{code}, and your settings in {_home(C.CONFIG_DIR)}? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if not answer.strip().lower().startswith("y"):
            return ["data: kept (nothing deleted)"]
    done = []
    for d in I.DESTINATIONS:
        try:
            if vault.remove(d):
                done.append(f"secrets: forgot the {I.label(d)} connection")
        except (OSError, subprocess.SubprocessError):
            pass
    for root in (C.CONFIG_DIR, C.DATA_DIR):
        if not root.exists():
            continue
        if notes == root or root in notes.parents:      # meeting notes kept inside chttr's own folder
            for child in root.iterdir():
                if child == notes or child in notes.parents:
                    continue
                shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
            done.append(f"data: deleted {_home(root)} except your meeting notes in {_home(notes)}")
        else:
            shutil.rmtree(root)
            done.append(f"data: deleted {_home(root)}")
    done.append(f"meeting notes: left alone in {_home(notes)} ([meeting] dir); they're yours")
    return done


def uninstall(purge: bool = False, yes: bool = False, ask=None, say=None) -> list[str]:
    """Undo `chttr setup`: the service, keybindings, Meet extension registration and
    the autostart edit. With purge, also chttr's data and settings (after asking:
    `ask(question)`, input() by default). Meeting notes in [meeting] dir are never
    touched. Returns what was done; `say(line)` hears each line as it happens."""
    from . import native_host

    done: list[str] = []

    def step(lines: list[str]) -> None:
        for line in lines:
            done.append(line)
            if say:
                say(line)

    step(_uninstall_service())
    step(_uninstall_hyprland())
    step(_uninstall_sway())
    step([f"meet: {line}" for line in native_host.uninstall()])
    if not done:
        step(["nothing from chttr setup was installed"])
    if purge:
        step(_purge(yes, ask or input))
    else:
        step([f"data: kept {_home(C.DATA_DIR)} and {_home(C.CONFIG_DIR)} (chttr uninstall --purge deletes them)"])
    exe = shutil.which("chttr") or ""
    steps = ["omarchy plugin remove robjobbed.chttr"] if OMARCHY_PLUGIN.exists() else []
    steps.append("sudo pacman -Rns chttr-git" if exe.startswith("/usr/") else "uv tool uninstall chttr")
    step([f"finish with: {steps[0]}", *(f"             {s}" for s in steps[1:])])
    return done
