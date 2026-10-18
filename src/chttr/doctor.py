"""`chttr doctor`: what works, what doesn't, and the command that fixes it.

Every check is a function `(cfg, env) -> Check`. Everything a check looks at
outside chttr (programs on PATH, systemctl, the system Python, the accessibility
bus, the distribution) goes through `Env`, so tests can hand in a fake machine.
A problem's `fix` is what to run, with this distribution's package names; the
ones marked `auto` are safe to run unattended (no sudo, idempotent) and
`chttr doctor --fix` runs them.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import config as C


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""           # what to run (one command per line), with this distribution's package names
    auto: bool = False      # safe for `chttr doctor --fix`: idempotent, no sudo


def _run(argv: list[str], timeout: float | None = 10, env: dict | None = None,
         capture: bool = True) -> subprocess.CompletedProcess:
    """subprocess.run that never raises: a missing program is exit status 127.
    Uncaptured output (chttr setup's progress) goes to stderr, so it never mixes
    with `chttr doctor --json` on stdout."""
    out: dict = {"capture_output": True} if capture else {"stdout": _stderr()}
    try:
        return subprocess.run(argv, text=True, timeout=timeout, env=env, check=False, **out)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


def _stderr():
    try:
        sys.stderr.fileno()
        return sys.stderr
    except (AttributeError, OSError, ValueError):
        return None


def _read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def parse_os_release(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.startswith("#"):
            out[key.strip()] = value.strip().strip("\"'")
    return out


def _os_release() -> dict:
    return parse_os_release(_read_text(Path("/etc/os-release")) or _read_text(Path("/usr/lib/os-release")))


def _free_bytes(path: Path) -> int:
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return -1


def _desktop():
    from .desktop import detect

    return detect()


def _report(desk) -> list[str]:
    from .desktop import report

    return report(desk)


def _hotkeys(backend: str, compositor: str) -> str:
    from .hotkeys import resolve

    return resolve(backend, compositor)


def _probe() -> dict:
    from .context import probe
    from .daemon import SYSTEM_PYTHON

    return probe(SYSTEM_PYTHON, timeout=4.0)


def _send(command: str) -> dict:
    from .client import send

    return send(command, timeout=2.0)


def _missing_flags() -> list[Path]:
    from .native_host import missing_accessibility_flag

    return missing_accessibility_flag()


def _ensure_flags() -> list[str]:
    from .native_host import ensure_accessibility_flag

    return ensure_accessibility_flag()


@dataclass
class Env:
    """The machine as the checks see it."""
    which: Callable[[str], str | None] = shutil.which
    run: Callable[..., subprocess.CompletedProcess] = _run
    read_text: Callable[[Path], str] = _read_text
    exists: Callable[[Path], bool] = os.path.exists
    os_release: Callable[[], dict] = _os_release
    desktop: Callable[[], object] = _desktop
    report: Callable[[object], list[str]] = _report
    hotkeys: Callable[[str, str], str] = _hotkeys
    probe: Callable[[], dict] = _probe
    send: Callable[[str], dict] = _send
    free_bytes: Callable[[Path], int] = _free_bytes
    missing_flags: Callable[[], list[Path]] = _missing_flags
    ensure_flags: Callable[[], list[str]] = _ensure_flags
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    cache: dict = field(default_factory=dict, repr=False)


# ---- distributions -----------------------------------------------------------------

INSTALL = {"arch": "sudo pacman -S --needed {}", "debian": "sudo apt install {}", "fedora": "sudo dnf install {}"}
PACKAGES = {           # what provides each thing, per distribution family
    "overlay": {"arch": "python-gobject gtk4-layer-shell",
                "debian": "python3-gi gir1.2-gtk-4.0 gir1.2-gtk4layershell-1.0",
                "fedora": "python3-gobject gtk4-layer-shell"},
    "pipewire": {"arch": "pipewire", "debian": "pipewire-bin", "fedora": "pipewire-utils"},
    "pactl": {"arch": "libpulse", "debian": "pulseaudio-utils", "fedora": "pulseaudio-utils"},
    "atspi": {"arch": "python-gobject at-spi2-core", "debian": "python3-gi gir1.2-atspi-2.0 at-spi2-core",
              "fedora": "python3-gobject at-spi2-core"},
    "at-spi2-core": {"arch": "at-spi2-core", "debian": "at-spi2-core", "fedora": "at-spi2-core"},
    "wtype": {"arch": "wtype", "debian": "wtype", "fedora": "wtype"},
    "ydotool": {"arch": "ydotool", "debian": "ydotool", "fedora": "ydotool"},
    "xdotool": {"arch": "xdotool", "debian": "xdotool", "fedora": "xdotool"},
}


def distro(env: Env) -> str:
    """"arch", "debian" or "fedora" (by ID or ID_LIKE), else ""."""
    rel = env.os_release()
    ids = [rel.get("ID", "").lower(), *rel.get("ID_LIKE", "").lower().split()]
    if any(i in ids for i in ("rhel", "centos")):
        ids.append("fedora")
    if "ubuntu" in ids:
        ids.append("debian")
    return next((family for family in ("arch", "debian", "fedora") if family in ids), "")


def install_command(env: Env, *what: str) -> str:
    family = distro(env)
    pkgs: list[str] = []
    for w in what:
        for pkg in PACKAGES[w].get(family or "arch", "").split():
            if pkg not in pkgs:
                pkgs.append(pkg)
    if family:
        return INSTALL[family].format(" ".join(pkgs))
    return f"install {' '.join(pkgs)} (Arch names) with your package manager"


# ---- checks --------------------------------------------------------------------------

SETUP_MODELS = "chttr setup --no-service --no-keybindings --no-meet"
SETUP_SERVICE = "chttr setup --no-models --no-keybindings --no-meet"
LABELS = {"engine": "engine", "cleanup": "cleanup", "api_key_perms": "api key", "service": "daemon",
          "desktop": "desktop", "typing": "typing", "shortcuts": "shortcuts", "clipboard": "clipboard",
          "window_info": "window info", "menus": "menus", "hotkeys": "hotkeys", "audio_tools": "audio",
          "overlay": "overlay", "screen_context": "screen ctx", "disk": "disk", "modes": "modes",
          "integrations": "send notes", "meetings": "meetings", "calendar": "calendar",
          "meet_extension": "meet ext", "config": "config.toml"}


def _home(path: Path | str) -> str:
    path = Path(path)
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def engine(cfg, env: Env) -> Check:
    from .transcriber import resolve_model, resolve_parakeet

    if cfg.engine.name == "parakeet":
        try:
            where = resolve_parakeet(cfg.engine.parakeet_dir)
        except FileNotFoundError:
            if cfg.engine.parakeet_dir:
                return Check("engine", False, f"parakeet: no model in {cfg.engine.parakeet_dir} ([engine] parakeet_dir)",
                             f"empty [engine] parakeet_dir in {_home(C.CONFIG_FILE)}, then: {SETUP_MODELS}")
            return Check("engine", False, "parakeet: the speech model isn't downloaded yet (~670 MB)",
                         SETUP_MODELS, auto=True)
        if importlib.util.find_spec("onnx_asr") is None:
            return Check("engine", False, "parakeet: the onnx-asr package is missing from chttr's environment",
                         "run chttr's installer again (install.sh, or bin/setup in your checkout)")
        return Check("engine", True, f"parakeet ({_home(where)})")
    model = resolve_model(cfg.whisper.model)
    if importlib.util.find_spec("pywhispercpp") is None:
        return Check("engine", False, "whisper: pywhispercpp isn't installed",
                     "run chttr's installer again: with [engine] name = \"whisper\" it adds pywhispercpp")
    if model == cfg.whisper.model:
        return Check("engine", True, f"whisper ({model}: downloaded on first use)")
    return Check("engine", True, f"whisper ({_home(model)})")


def _ollama_answers(url: str) -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=0.5) as r:
            return r.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def cleanup(cfg, env: Env) -> Check:
    from .polish import backend

    b = backend(cfg.polish)
    if b == "none":
        return Check("cleanup", True, "local rules; add a key: chttr connect claude, or run Ollama")
    if b == "ollama" and cfg.polish.backend == "ollama" and not _ollama_answers(cfg.polish.ollama_url):
        return Check("cleanup", False, f"ollama, but nothing answers at {cfg.polish.ollama_url}",
                     f"ollama serve   (and once: ollama pull {cfg.polish.ollama_model})")
    return Check("cleanup", True, b + (f" ({cfg.polish.ollama_model})" if b == "ollama" else ""))


def api_key_perms(cfg, env: Env) -> Check | None:
    key_file = Path(cfg.polish.api_key_file).expanduser() if cfg.polish.api_key_file else None
    if not key_file or not key_file.is_file():
        return None
    if C.too_open(key_file):
        return Check("api_key_perms", False, f"{_home(key_file)} is readable by other users",
                     f"chmod 600 {shlex.quote(str(key_file))}", auto=True)
    return Check("api_key_perms", True, f"{_home(key_file)} (only you can read it)")


def _daemon_state(env: Env) -> str | None:
    try:
        return str(env.send("status").get("state") or "running")
    except Exception:  # noqa: BLE001 - not running, or not answering
        return None


FAILURE = re.compile(r"Traceback|Error|Exception|exited|[Ff]ailed|Killed|core dump|[Ss]egmentation")
QUOTED = re.compile(r"""(['"])[^'"\n]*\s[^'"\n]*\1""")


def _journal(env: Env, keep: int = 5) -> list[str]:
    """The last few lines of chttr's log that say why it stopped. Only failures
    and their tracebacks, with quoted text masked: the daemon logs what you
    dictate, and doctor output ends up in bug reports."""
    out = env.run(["journalctl", "--user", "-u", "chttr", "-n", "50", "--no-pager", "-o", "cat"])
    lines, in_traceback = [], False
    for line in (out.stdout or "").splitlines():
        if not line.strip() or line.startswith("-- "):
            continue
        if line.startswith("Traceback"):
            in_traceback = True
        elif not (in_traceback and line[:1].isspace()):
            in_traceback = False
            if not FAILURE.search(line):
                continue
        lines.append(QUOTED.sub(lambda m: m.group(1) + "…" + m.group(1), line))
    return lines[-keep:]


def _restarts(env: Env) -> int:
    out = env.run(["systemctl", "--user", "show", "chttr.service", "-p", "NRestarts", "--value"])
    try:
        return int((out.stdout or "").strip() or 0) if out.returncode == 0 else 0
    except ValueError:
        return 0


def service(cfg, env: Env) -> Check:
    from .install import UNIT

    state = _daemon_state(env)
    if not env.which("systemctl"):
        if state:
            return Check("service", True, state)
        return Check("service", False, "not running", "chttr daemon   (and add it to your desktop's autostart)")
    if not env.exists(UNIT) and env.run(["systemctl", "--user", "cat", "chttr.service"]).returncode != 0:
        if state:
            return Check("service", True, f"{state}, started outside systemd "
                                          "(chttr setup adds a service that starts it at login)")
        return Check("service", False, "not running, and there's no chttr.service", SETUP_SERVICE, auto=True)
    unit = env.read_text(UNIT) if env.exists(UNIT) else ""
    exe = next((line.split("=", 1)[1].split()[0] for line in unit.splitlines()
                if line.startswith("ExecStart=") and line.split("=", 1)[1].split()), "")
    if exe and not env.exists(Path(exe)):
        return Check("service", False, f"chttr.service starts {exe}, which isn't there any more",
                     SETUP_SERVICE, auto=True)
    active = (env.run(["systemctl", "--user", "is-active", "chttr.service"]).stdout or "").strip() or "unknown"
    enabled = (env.run(["systemctl", "--user", "is-enabled", "chttr.service"]).stdout or "").strip()
    if active in ("active", "activating", "reloading"):
        if not state and (n := _restarts(env)):
            log = _journal(env)
            return Check("service", False, "\n".join([f"chttr.service keeps restarting ({n} time{'' if n == 1 else 's'}) "
                                                     "and the daemon isn't answering",
                                                     *(f"log: {line}" for line in log)]),
                         "journalctl --user -u chttr -e   (the reason is near the end)")
        if enabled != "enabled":
            return Check("service", False, f"{state or 'starting'}, but chttr.service isn't enabled: "
                                           "it won't start when you log in",
                         "systemctl --user enable --now chttr", auto=True)
        return Check("service", True, f"{state or 'starting'} (chttr.service)")
    if state:
        return Check("service", True, f"{state}, running outside chttr.service (which is {active})")
    log = _journal(env)
    detail = f"not running: chttr.service is {active}" + ("" if enabled == "enabled" else f" and {enabled or 'disabled'}")
    return Check("service", False, "\n".join([detail, *(f"log: {line}" for line in log)]),
                 "systemctl --user enable --now chttr", auto=True)


def _desktop_lines(env: Env) -> dict[str, str]:
    """`desktop.report()`'s lines as {label: detail}, computed once per Env."""
    if "report" not in env.cache:
        desk = env.cache.setdefault("desk", env.desktop())
        lines = {}
        for line in env.report(desk):
            label, sep, detail = line.partition(":")
            if sep and not line.startswith(" "):
                lines[label.strip()] = detail.strip()
        env.cache["report"] = lines
    return env.cache["report"]


def _desk(env: Env):
    return env.cache.setdefault("desk", env.desktop())


def _info(name: str, label: str) -> Callable:
    def check(cfg, env: Env) -> Check | None:
        detail = _desktop_lines(env).get(label)
        if detail is None:
            return None
        bad = detail.startswith("PROBLEM")
        return Check(name, not bad, detail.removeprefix("PROBLEM:").strip() if bad else detail)
    check.__name__ = name
    return check


desktop = _info("desktop", "desktop")
shortcuts = _info("shortcuts", "shortcuts")
clipboard = _info("clipboard", "clipboard")
window_info = _info("window_info", "window info")
menus = _info("menus", "menus")


def _ydotool_running(env: Env) -> bool:
    socket = os.environ.get("YDOTOOL_SOCKET") or ""
    candidates = [socket] if socket else []
    candidates += [f"/run/user/{os.getuid()}/.ydotool_socket", "/tmp/.ydotool_socket"]
    if any(env.exists(Path(c)) for c in candidates):
        return True
    return (env.run(["systemctl", "--user", "is-active", "ydotool"]).stdout or "").strip() == "active"


def typing(cfg, env: Env) -> Check:
    detail = _desktop_lines(env).get("typing", "")
    desk = _desk(env)
    if not detail or detail.startswith("PROBLEM"):
        tool = "xdotool" if desk.session == "x11" else "wtype" if desk.wlroots_like else "ydotool"
        fix = install_command(env, tool)
        if tool == "ydotool":
            fix += "\nsystemctl --user enable --now ydotool"
        return Check("typing", False, "nothing can type here: text goes to the clipboard", fix)
    tool = detail.split()[0]
    if tool != "ydotool":
        return Check("typing", True, detail)
    if _ydotool_running(env):
        return Check("typing", True, "ydotool (its daemon is running)")
    if env.run(["systemctl", "--user", "cat", "ydotool.service"]).returncode == 0:
        return Check("typing", False, "ydotool, but its daemon isn't running: nothing gets typed",
                     "systemctl --user enable --now ydotool", auto=True)
    return Check("typing", False, "ydotool, but its daemon isn't running: nothing gets typed",
                 "ydotoold &   (your ydotool package has no user service; start ydotoold at login)")


def hotkeys(cfg, env: Env) -> Check:
    hk = env.hotkeys(cfg.hotkeys.backend, _desk(env).compositor)
    desc = {"compositor": "your compositor's bindings (chttr setup adds them)",
            "portal": "the desktop's global-shortcuts portal",
            "evdev": "keyboard devices (input group)"}
    if hk == "none":
        return Check("hotkeys", False, "nothing can catch chttr's keys here: join the input group (then log out "
                                       "and back in), or bind chttr start/release in your compositor",
                     "sudo usermod -aG input $USER")
    return Check("hotkeys", True, desc.get(hk, hk))


AUDIO_TOOLS = {"pw-record": ("pipewire", "recording"), "pw-play": ("pipewire", "sounds"),
               "pw-dump": ("pipewire", "call detection"), "pactl": ("pactl", "auto-unmute")}


def audio_tools(cfg, env: Env) -> Check:
    missing = [tool for tool in AUDIO_TOOLS if not env.which(tool)]
    if not missing:
        return Check("audio_tools", True, ", ".join(AUDIO_TOOLS))
    detail = "missing " + ", ".join(f"{t} ({AUDIO_TOOLS[t][1]})" for t in missing)
    pkgs = list(dict.fromkeys(AUDIO_TOOLS[t][0] for t in missing))
    return Check("audio_tools", False, detail, install_command(env, *pkgs))


OVERLAY_TEST = "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Gtk4LayerShell','1.0')"


def overlay(cfg, env: Env) -> Check:
    if not cfg.feedback.overlay:
        return Check("overlay", True, "off ([feedback] overlay = false)")
    desk = _desk(env)
    if desk.compositor == "gnome" or not desk.wayland:
        return Check("overlay", True, f"notifications only (no layer-shell on {desk.describe()})")
    from .daemon import LAYER_SHELL_LIBS, SYSTEM_PYTHON

    fix = install_command(env, "overlay")
    lib = next((lib for lib in LAYER_SHELL_LIBS if env.exists(lib)), None)
    if lib is None:
        return Check("overlay", False, "gtk4-layer-shell isn't installed: no waveform, notifications only", fix)
    out = env.run([SYSTEM_PYTHON, "-I", "-c", OVERLAY_TEST], env={**os.environ, "LD_PRELOAD": str(lib)})
    if out.returncode != 0:
        why = (out.stderr or "").strip().splitlines()
        return Check("overlay", False, f"{SYSTEM_PYTHON} can't load GTK 4 and gtk4-layer-shell: no waveform, "
                                       "notifications only" + (f"\n{why[-1]}" if why else ""), fix)
    return Check("overlay", True, "waveform (GTK 4 layer-shell)")


def _browser(flags: Path) -> str:
    stem = flags.name.removesuffix(".conf").removesuffix("-flags")
    return {"chromium": "Chromium", "chrome": "Chrome", "brave": "Brave", "vivaldi-stable": "Vivaldi",
            "microsoft-edge-stable": "Edge"}.get(stem, stem)


GSETTINGS_ON = "gsettings set org.gnome.desktop.interface toolkit-accessibility true"


def _toolkit_accessibility(env: Env) -> str:
    """"true", "false", or "" when there is no gsettings or no such key."""
    if not env.which("gsettings"):
        return ""
    out = env.run(["gsettings", "get", "org.gnome.desktop.interface", "toolkit-accessibility"])
    return (out.stdout or "").strip() if out.returncode == 0 else ""


def screen_context(cfg, env: Env) -> Check:
    if cfg.polish.screen_context == "off" and not cfg.history.learn_from_edits:
        return Check("screen_context", True, "off ([polish] screen_context, [history] learn_from_edits)")
    p = env.probe()
    if not p:
        from .context import HELPER
        from .daemon import SYSTEM_PYTHON

        return Check("screen_context", False, "the AT-SPI check didn't answer (it timed out or crashed)",
                     f"run it to see why: {SYSTEM_PYTHON} -I {shlex.quote(str(HELPER))} --probe")
    if not p.get("gi"):
        return Check("screen_context", False, "the system Python has no AT-SPI (PyGObject's Atspi): "
                                              "names on screen can't help spelling", install_command(env, "atspi"))
    toolkit = _toolkit_accessibility(env)
    if not p.get("bus"):
        fix = [install_command(env, "at-spi2-core")] + ([GSETTINGS_ON] if toolkit == "false" else [])
        return Check("screen_context", False, "no accessibility bus: Chromium, Electron and Qt apps need "
                                              "toolkit-accessibility, and the bus comes with at-spi2-core",
                     "\n".join(fix))
    found = f"AT-SPI bus reachable, {p.get('apps', 0)} app{'' if p.get('apps') == 1 else 's'}"
    flags = env.missing_flags()
    problems, fix = [], []
    if toolkit == "false":
        problems.append("toolkit-accessibility is off, so Chromium, Electron and Qt apps hide their text")
        fix.append(GSETTINGS_ON)
    for f in flags:
        problems.append(f"{_browser(f)} needs --force-renderer-accessibility")
        fix.append(f"add --force-renderer-accessibility to {_home(f)}, then restart the browser")
    if problems:
        return Check("screen_context", False, "\n".join([f"{found}, but:", *problems]), "\n".join(fix), auto=True)
    return Check("screen_context", True, found)


MIN_FREE = 1_000_000_000


def disk(cfg, env: Env) -> Check:
    free = env.free_bytes(C.DATA_DIR)
    if free < 0:
        return Check("disk", True, f"couldn't tell how much space is free in {_home(C.DATA_DIR)}")
    gb = free / 1e9
    if free < MIN_FREE:
        return Check("disk", False, f"only {gb:.1f} GB free in {_home(C.DATA_DIR)}: the speech models and "
                                    "meeting audio need at least 1 GB")
    return Check("disk", True, f"{gb:.0f} GB free in {_home(C.DATA_DIR)}")


def modes(cfg, env: Env) -> Check:
    # a mode can have several entries (the desktop app and its browser tab): name it once
    return Check("modes", True, ", ".join(dict.fromkeys(m.name for m in cfg.modes)) or "(none)")


def integrations(cfg, env: Env) -> Check:
    from . import integrations as I
    from . import vault

    linked = I.connected()
    if not linked:
        return Check("integrations", True, "nothing connected (chttr connect)")
    where = "keyring" if vault.keyring_available() else "secrets.json (no secret-tool)"
    return Check("integrations", True, f"{', '.join(I.label(d) for d in linked)} ({cfg.integrations.send}; secrets in {where})")


def meetings(cfg, env: Env) -> Check:
    return Check("meetings", True, f"notes to {_home(Path(cfg.meeting.dir).expanduser())}; auto = {cfg.meeting.auto}")


def calendar(cfg, env: Env) -> Check:
    from .gcal import read_urls

    n = len(read_urls(cfg.meeting.calendar_file))
    return Check("calendar", True, f"{n} iCal address{'' if n == 1 else 'es'}" if n
                 else "none yet (add one: chttr meeting calendar URL)")


def meet_extension(cfg, env: Env) -> Check:
    from .native_host import BROWSER_DIRS, HOST_NAME

    hosts = [d.name for d, _ in BROWSER_DIRS if env.exists(d / "NativeMessagingHosts" / f"{HOST_NAME}.json")]
    return Check("meet_extension", True, "registered for " + ", ".join(hosts) if hosts
                 else "not installed (chttr meeting install)")


CHECKS: list[Callable] = [engine, cleanup, api_key_perms, service, desktop, typing, shortcuts, clipboard, window_info,
                          menus, hotkeys, audio_tools, overlay, screen_context, disk, modes, integrations, meetings,
                          calendar, meet_extension]


def _safe(check: Callable, cfg, env: Env) -> Check | None:
    try:
        return check(cfg, env)
    except Exception as exc:  # noqa: BLE001 - one broken check must not hide the rest
        return Check(check.__name__, False, f"the check itself failed: {type(exc).__name__}: {exc}")


def config_file(error: str) -> Check:
    """config.toml didn't load: say what's wrong first, since the rest ran on the defaults."""
    return Check("config", False, f"{error}; chttr is using the defaults until it is fixed",
                 fix=f"$EDITOR {_home(C.CONFIG_FILE)}")


def run_all(cfg, env: Env | None = None) -> list[Check]:
    env = env or Env()
    return [c for c in (_safe(check, cfg, env) for check in CHECKS) if c is not None]


# ---- output and fixes -------------------------------------------------------------------

INDENT = " " * 13


def render(checks: list[Check], hint: bool = True) -> str:
    """Today's `name:        detail` layout; problems say PROBLEM and get a `fix:` line."""
    out = []
    for c in checks:
        first, *more = c.detail.splitlines() or [""]
        label = f"{LABELS.get(c.name, c.name)}:"
        out.append(f"{label:<13}{'' if c.ok else 'PROBLEM: '}{first}")
        out += [INDENT + line for line in more]
        if not c.ok:
            out += [f"{INDENT}fix: {line}" for line in c.fix.splitlines()]
    auto = [LABELS.get(c.name, c.name) for c in checks if not c.ok and c.auto and c.fix]
    if hint and auto:
        out.append(f"\nchttr doctor --fix runs the safe fixes (no sudo) for: {', '.join(auto)}")
    return "\n".join(out)


def _argv(line: str, env: Env) -> list[str]:
    argv = shlex.split(line)
    if argv and argv[0] == "chttr" and not env.which("chttr"):
        argv = [sys.executable, "-m", "chttr", *argv[1:]]
    return argv


def _fix_screen_context(check: Check, env: Env) -> list[str]:
    done = []
    for line in check.fix.splitlines():
        if line.startswith("gsettings "):
            out = env.run(shlex.split(line))
            if out.returncode != 0:
                raise RuntimeError((out.stderr or "").strip() or f"gsettings exited with {out.returncode}")
            done.append("apps started from now on expose their text (restart Chromium, Slack and the like)")
    if "--force-renderer-accessibility" in check.fix:
        done += env.ensure_flags()
    return done


FIXERS: dict[str, Callable[[Check, Env], list[str]]] = {"screen_context": _fix_screen_context}


def _fix(check: Check, env: Env) -> list[str]:
    for line in check.fix.splitlines():
        argv = _argv(line, env)
        mine = argv[:3] == [sys.executable, "-m", "chttr"] or argv[:1] == ["chttr"]
        # chttr setup downloads models: show its progress, and give it time
        out = env.run(argv, timeout=None, capture=False) if mine else env.run(argv, timeout=60)
        if out.returncode != 0:
            err = (out.stderr or "").strip().splitlines()
            raise RuntimeError(err[-1] if err else f"exit status {out.returncode}")
    return []


SETTLE = 10.0       # seconds a started daemon gets to answer (it loads the speech model first)


def _wait_for_daemon(env: Env) -> bool:
    """After starting the service: does the daemon answer within SETTLE seconds?
    systemd calls a crash-looping unit active between crashes, so only an answer counts."""
    end = env.clock() + SETTLE
    while True:
        if _daemon_state(env):
            return True
        if env.clock() >= end:
            return False
        env.sleep(0.5)


def apply_fixes(checks: list[Check], env: Env | None = None, cfg=None) -> list[str]:
    """Run the `auto` fixes of the checks that failed, then check again.
    Returns one line per fix: what ran and whether it helped."""
    env = env or Env()
    cfg = cfg if cfg is not None else C.load_config()
    by_name = {check.__name__: check for check in CHECKS}
    lines = []
    for c in checks:
        if c.ok or not c.auto or not c.fix:
            continue
        label = LABELS.get(c.name, c.name)
        try:
            notes = FIXERS.get(c.name, _fix)(c, env)
        except Exception as exc:  # noqa: BLE001 - report it and go on to the next fix
            lines.append(f"{label + ':':<13}{c.fix.splitlines()[0]}: failed ({exc})")
            continue
        env.cache.clear()
        answers = _wait_for_daemon(env) if c.name == "service" else True
        again = _safe(by_name[c.name], cfg, env) if c.name in by_name else None
        if again is not None and not again.ok:
            verdict = "still a problem: " + again.detail.splitlines()[0]
        elif not answers:
            verdict = "started, but it isn't answering yet: journalctl --user -u chttr"
        else:
            verdict = "fixed"
        ran = [line for line in c.fix.splitlines() if not line.startswith("add ")]
        lines.append(f"{label + ':':<13}" + (f"ran {' && '.join(ran)}: " if ran else "") + verdict)
        lines += [f"{INDENT}{note}" for note in notes]
    return lines
