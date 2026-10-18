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
StartLimitIntervalSec=120
StartLimitBurst=4

[Service]
ExecStart={exe} daemon
Restart=on-failure
RestartSec=5

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
