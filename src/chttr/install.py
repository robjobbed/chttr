"""`chttr setup`: everything after `uv tool install`, safe to run again.

- config file, speech models (Parakeet if voxtype hasn't downloaded it, VAD and
  speaker embeddings for meetings)
- a systemd user service for the daemon (replacing a Hyprland autostart line)
- keybindings: Hyprland and Sway get bindings in their config (if there are no
  chttr bindings yet); GNOME and KDE use the global-shortcuts portal and other
  desktops the keyboard devices, both from inside the daemon (see hotkeys.py)
- the Google Meet extension and its native messaging host
- optionally an Anthropic key (at a terminal, when nothing does cleanup yet)

It ends only once the daemon answers `status` with its model loaded, with a card of
the shortcuts that are really bound and a "chttr is ready" notification.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config as C
from . import models as M
from .client import DaemonNotRunning, send

UNIT = Path.home() / ".config/systemd/user/chttr.service"
HYPR = Path.home() / ".config/hypr"
READY_TIMEOUT = 45.0        # seconds for the daemon to answer after a restart
POLL = 0.25
FAILED_CHECK = 2.0          # how often to ask systemd whether it gave up on the service
GLYPH = "\U000f036c"       # nf-md-microphone, for Omarchy's notifications

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


def models() -> bool:
    """Parakeet (unless voxtype or bin/setup already fetched it) and the meeting models.
    False, after a PROBLEM line, when a download fails."""
    from . import diarize
    from .transcriber import resolve_parakeet

    try:
        try:
            _say(f"speech model: Parakeet in {resolve_parakeet('')}")
        except FileNotFoundError:
            target = C.PARAKEET_SEARCH_DIRS[-1]
            _say(f"speech model: downloading Parakeet TDT 0.6B v3 (int8, ~670 MB, checksum-verified) to {target}")
            for spec in M.PARAKEET:
                M.download(spec, target / spec.name, M.console_progress(spec.name))
        missing = {rel: spec for rel, spec in diarize.MODELS.items()
                   if not _have(diarize.MODELS_DIR / rel, spec.size)}
        if missing:
            _say(f"meeting models: downloading voice activity + speaker embeddings "
                 f"(~{sum(s.size for s in missing.values()) / 2**20:.0f} MB)")
            for rel, spec in missing.items():
                M.download(spec, diarize.MODELS_DIR / rel, M.console_progress(spec.name))
        else:
            _say("meeting models: voice activity + speaker embeddings")
    except (OSError, M.ChecksumError) as exc:
        print()
        _say(f"speech model: PROBLEM: {exc}")
        _say("retry with: chttr setup  (it continues where the download stopped)")
        return False
    return True


def _have(path: Path, size: int) -> bool:
    try:
        return path.stat().st_size == size
    except OSError:
        return False


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


def service(exe: str) -> bool:
    """Install and (re)start chttr.service, then wait until the daemon answers."""
    UNIT.parent.mkdir(parents=True, exist_ok=True)
    UNIT.write_text(UNIT_TEXT.format(exe=exe), encoding="utf-8")
    _comment_out_autostart(exe)
    if C.SOCKET_PATH.exists():
        subprocess.run([exe, "quit"], capture_output=True, check=False)     # a daemon started some other way
        time.sleep(1)
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
        subprocess.run(["systemctl", "--user", "enable", "chttr.service"], capture_output=True, check=False)
        restart = subprocess.run(["systemctl", "--user", "restart", "chttr.service"], capture_output=True,
                                 text=True, check=False)
    except FileNotFoundError:
        _say("daemon: PROBLEM: no systemd here; start it with your session instead: chttr daemon")
        return False
    _say("daemon: systemd user service chttr.service (logs: journalctl --user -u chttr)")
    if restart.returncode != 0:
        why = (restart.stderr or "").strip().splitlines()
        _say(f"daemon: PROBLEM: systemctl couldn't start it{': ' + why[-1] if why else ''}")
        return False
    return wait_ready()


def wait_ready(timeout: float = READY_TIMEOUT) -> bool:
    """Poll `status` until the daemon has its speech model; on a timeout show why."""
    start = time.monotonic()
    deadline, next_check = start + timeout, start + FAILED_CHECK
    _say("daemon: starting…")
    loading = False
    while True:
        try:
            reply = send("status", timeout=min(2.0, max(POLL, deadline - time.monotonic())))
        except (DaemonNotRunning, OSError, ValueError):
            reply = None
        if reply and reply.get("ok", True):
            model = reply.get("model", "ready")
            if model == "loading":
                if not loading:
                    _say("daemon: loading the speech model…")
                    loading = True
            elif model == "missing":
                _say("daemon: PROBLEM: running, but it has no speech model; run chttr setup again")
                return False
            else:
                _say(f"daemon: ready ({time.monotonic() - start:.0f} s)")
                return True
        now = time.monotonic()
        if now >= deadline:
            break
        if now >= next_check:
            next_check = now + FAILED_CHECK
            if _service_failed():                       # crash-looped into systemd's start limit
                break
        time.sleep(POLL)
    _journal_tail()
    _say("daemon: PROBLEM: not answering; see the log above")
    return False


def _service_failed() -> bool:
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", "chttr.service"], capture_output=True,
                             text=True, check=False).stdout
    except FileNotFoundError:
        return False
    return (out or "").strip() == "failed"


def _journal_tail(lines: int = 8) -> None:
    try:
        out = subprocess.run(["journalctl", "--user", "-u", "chttr", "--no-pager", "-n", str(lines)],
                             capture_output=True, text=True, check=False).stdout
    except FileNotFoundError:
        return
    for line in (out or "").splitlines()[-lines:]:
        print(f"    | {line}", flush=True)


def notify(title: str, body: str = "", *, urgency: str = "normal", timeout_ms: int = 6000) -> None:
    """A desktop notification from a short-lived command (Omarchy's styled one when present)."""
    from .feedback import as_text

    if shutil.which("omarchy-notification-send"):
        cmd = ["omarchy-notification-send", "--app-name", "chttr", "-g", GLYPH, "-u", urgency, "-t", str(timeout_ms)]
        title, body = as_text(title), as_text(body)
    else:
        cmd = ["notify-send", "-a", "chttr", "-u", urgency, "-t", str(timeout_ms), "--"]
    try:
        subprocess.run([*cmd, title, *([body] if body else [])], capture_output=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


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


def offer_claude(cfg) -> None:
    """At a terminal with no cleanup backend yet, offer to paste an Anthropic key."""
    from . import apikey
    from . import polish as P

    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, ValueError):
        interactive = False
    if not interactive or cfg.polish.backend != "auto" or P.backend(cfg.polish) != "none":
        return
    print()
    _say("Claude (optional): smarter cleanup and meeting notes. Keys: https://console.anthropic.com/settings/keys")
    try:
        apikey.connect_interactive(cfg, prompt="  Paste an Anthropic API key for Claude cleanup and meeting notes "
                                               "(Enter to skip): ", tip=False)
    except (EOFError, KeyboardInterrupt):
        print()


def _box(title: str, rows: list[tuple[str, str]]) -> list[str]:
    left = max([len(a) for a, _ in rows] + [0])
    body = [f"{a:<{left}}   {b}" for a, b in rows] or ["no keys are bound yet"]
    width = max(len(line) for line in [*body, title + "  "])
    top = f"╭─ {title} " + "─" * (width - len(title) - 1) + "╮"
    return [top, *(f"│ {line:<{width}} │" for line in body), "╰" + "─" * (width + 2) + "╯"]


def card(exe: str, cfg, desk=None, meet_installed: bool = True) -> dict[str, str]:
    """The closing summary: the shortcuts as they are really bound, and what does the cleanup."""
    from . import hotkeys as H
    from . import polish as P
    from .desktop import detect
    from .gcal import read_urls

    keys = H.effective_bindings(cfg, desk or detect())
    print()
    for line in _box("Shortcuts", [(H.DESCRIPTIONS[a], k) for a, k in keys.items()]):
        print(f"  {line}")
    backend = P.backend(cfg.polish)
    cleanup = {"claude": "Claude", "ollama": f"Ollama ({cfg.polish.ollama_model})"}.get(
        backend, "local rules (add a key any time: chttr connect claude)")
    notes = {"claude": "Claude", "ollama": "Ollama"}.get(backend, "on-device")
    _say(f"Cleanup: {cleanup}")
    _say(f"Notes: {notes}")
    if not read_urls(cfg.meeting.calendar_file):
        _say("Meeting attendees: chttr meeting calendar '<secret iCal address>' "
             "(Google Calendar > Settings > your calendar)")
    if meet_installed:
        _say("Speaker names in Meet: restart Chromium once so it loads the chttr extension")
    _say("Check anything: chttr doctor")
    return keys


def ready_message(keys: dict[str, str]) -> str:
    text = f"Hold {keys.get('dictate', 'Insert')} and talk; let go to type."
    if keys.get("command"):
        text += f" {keys['command']}: say what to do with selected text."
    return text


def run(do_models: bool = True, do_service: bool = True, do_bindings: bool = True, do_meet: bool = True) -> int:
    exe = shutil.which("chttr") or str(Path.home() / ".local/bin/chttr")
    print("chttr setup")
    ok = []
    if C.write_default_config():
        _say(f"config: created {C.CONFIG_FILE}")
    else:
        _say(f"config: {C.CONFIG_FILE}")
    try:
        cfg = C.load_config()
    except ValueError as exc:
        _say(f"config: PROBLEM: {exc}")
        cfg = C.Config(modes=C._default_modes())
        ok.append(False)
    if do_models:
        ok.append(models())
    if do_bindings:
        ok.append(keybindings(exe) is not False)
    if do_meet:
        ok.append(meet() is not False)
    if cfg.polish.screen_context != "off":
        for line in accessibility():         # so screen context can read Chromium, Electron and Qt apps
            _say(f"screen context: {line}")
    offer_claude(cfg)
    if do_service:
        ok.append(service(exe))
    keys = card(exe, cfg, meet_installed=do_meet)
    if "dictate" not in keys:
        _say("keybindings: PROBLEM: no key starts dictation (bind `chttr start` on press and `chttr release` "
             "on release); see: chttr keys")
        ok.append(False)
    print()
    if not all(ok):
        print("Not quite ready: fix the PROBLEM lines above, then run chttr setup again.")
        return 1
    if not do_service:
        msg = ready_message(keys)
        print(f"Set up. Start chttr (systemctl --user start chttr, or: chttr daemon), then {msg[:1].lower()}{msg[1:]}")
        return 0
    print(f"Ready. Hold {keys['dictate']} and talk; let go to type.")
    notify("chttr is ready", ready_message(keys), timeout_ms=12000)
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


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


# What chttr keeps in ~/.config/chttr and ~/.local/share/chttr. A folder of chttr's that
# links somewhere holding anything else (a shared disk) loses only these.
OWNED = frozenset({
    "config.toml", "dictionary.toml", "note-styles.toml", "secrets.json", "anthropic-api-key", "calendar-urls",
    "history.jsonl", "history-audio", "meetings", "models", "voices.json", "library.sqlite", "library.sqlite-wal",
    "library.sqlite-shm", "library.sqlite-journal", "calendar-cache", "src", "native-host", "hinted-cleanup"})


def _remove(path: Path) -> None:
    shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()


def _delete_root(root: Path, notes: Path) -> str:
    """Delete one of chttr's folders, sparing the meeting notes when they live in it.
    Paths are compared resolved, so a notes folder reached through a symlink into
    chttr's data counts as inside it, and a data folder moved to another disk and
    linked back is deleted where it really is (then the link), unless that folder
    holds more than chttr's files: then only chttr's go."""
    target, notes_r, link = root.resolve(), notes.resolve(), root.is_symlink()
    try:
        others = sorted(c for c in target.iterdir() if c.name not in OWNED) if link and target.is_dir() else []
        if others:
            for child in target.iterdir():
                if child.name in OWNED and not _inside(notes_r, child.resolve()):
                    _remove(child)
            root.unlink()
            return (f"data: deleted chttr's files in {_home(target)}, which {_home(root)} linked to, and the link; "
                    f"left the {len(others)} other item{'' if len(others) == 1 else 's'} there alone")
        if _inside(notes_r, target):
            for child in target.iterdir():
                if _inside(notes_r, child.resolve()):
                    continue
                _remove(child)
            return f"data: deleted {_home(root)} except your meeting notes in {_home(notes)}"
        if target.is_dir():
            shutil.rmtree(target)
        if link:
            root.unlink()
    except OSError as exc:
        return f"data: couldn't delete {_home(root)}: {exc.strerror or exc}"
    if link:
        return f"data: deleted {_home(root)} and {_home(target)}, the folder it linked to"
    return f"data: deleted {_home(root)}"


def _purge(yes: bool, ask) -> list[str]:
    from . import integrations as I
    from . import vault

    try:
        notes = Path(C.load_config(C.CONFIG_FILE).meeting.dir).expanduser()
    except (OSError, ValueError):
        notes = Path(C.MeetingCfg().dir).expanduser()
    def where(root: Path) -> str:
        return f"{_home(root)} (a link to {_home(root.resolve())})" if root.is_symlink() else _home(root)

    if not yes:
        code = " (and the chttr code installed there)" if (C.DATA_DIR / "src").exists() else ""
        try:
            answer = ask(f"Delete history, meeting data, voices, the library index and the models in "
                         f"{where(C.DATA_DIR)}{code}, and your settings in {where(C.CONFIG_DIR)}? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if not answer.strip().lower().startswith("y"):
            return ["data: kept (nothing deleted)"]
    done = []
    for d in I.connected():                 # only what is stored: `secret-tool clear` succeeds on nothing too
        try:
            if vault.remove(d):
                done.append(f"secrets: forgot the {I.label(d)} connection")
        except (OSError, subprocess.SubprocessError):
            pass
    for root in (C.CONFIG_DIR, C.DATA_DIR):
        if root.exists() or root.is_symlink():
            done.append(_delete_root(root, notes))
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
