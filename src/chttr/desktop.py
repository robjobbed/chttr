"""Everything that depends on the desktop: which window has focus, typing, the
clipboard, key shortcuts, menus, terminals.

chttr started on Hyprland (Omarchy) and still prefers its native tools there; on
other desktops it picks what that desktop supports:

| | focused window | typing | shortcuts (Ctrl+C/V) | clipboard |
|---|---|---|---|---|
| Hyprland | hyprctl | wtype | hyprctl sendshortcut | wl-copy / wl-paste |
| Sway, niri, river, other wlroots | swaymsg / niri msg | wtype | wtype | wl-copy / wl-paste |
| KDE Plasma (Wayland) | kdotool | ydotool | ydotool | wl-copy / wl-paste |
| GNOME (Wayland) | "Window Calls" extension | ydotool | ydotool | wl-copy / wl-paste |
| X11 (any) | xdotool | xdotool | xdotool | xclip / xsel |

Whatever is missing degrades gracefully: no focused window means no per-app
mode, no typing tool means the text is pasted or left on the clipboard.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("chttr.desktop")

# Linux input event codes, for ydotool
KEYCODES = {"ctrl": 29, "shift": 42, "alt": 56, "super": 125, "c": 46, "v": 47, "insert": 110,
            "return": 28, "enter": 28, "backspace": 14}
# Key names for wtype and xdotool (XKB keysyms are case-sensitive)
KEYSYMS = {"return": "Return", "enter": "Return", "backspace": "BackSpace"}


@dataclass(frozen=True)
class Desktop:
    session: str        # wayland | x11 | unknown
    compositor: str     # hyprland | sway | niri | wlroots | kde | gnome | x11 | unknown

    @property
    def wayland(self) -> bool:
        return self.session == "wayland"

    @property
    def wlroots_like(self) -> bool:
        """Compositors with the virtual-keyboard protocol that wtype needs."""
        return self.compositor in ("hyprland", "sway", "niri", "wlroots")

    def describe(self) -> str:
        return f"{self.compositor} ({self.session})"


def _hyprland_running(env: Mapping[str, str]) -> bool:
    """Hyprland leaves its sockets in $XDG_RUNTIME_DIR/hypr. A systemd user service
    doesn't always inherit HYPRLAND_INSTANCE_SIGNATURE, but hyprctl finds the
    instance through these sockets anyway."""
    runtime = env.get("XDG_RUNTIME_DIR", "")
    hypr = Path(runtime) / "hypr" if runtime else None
    try:
        return bool(hypr and hypr.is_dir() and any(hypr.iterdir()))
    except OSError:
        return False


def detect(env: Mapping[str, str] | None = None) -> Desktop:
    env = os.environ if env is None else env
    session = env.get("XDG_SESSION_TYPE", "").lower()
    if session not in ("wayland", "x11"):
        session = "wayland" if env.get("WAYLAND_DISPLAY") else "x11" if env.get("DISPLAY") else "unknown"
    current = env.get("XDG_CURRENT_DESKTOP", "").lower()
    if env.get("HYPRLAND_INSTANCE_SIGNATURE") or "hyprland" in current or _hyprland_running(env):
        comp = "hyprland"
        session = "wayland"
    elif env.get("SWAYSOCK") or "sway" in current:
        comp = "sway"
        session = "wayland"
    elif env.get("NIRI_SOCKET") or "niri" in current:
        comp = "niri"
        session = "wayland"
    elif "kde" in current or env.get("KDE_FULL_SESSION"):
        comp = "kde"
    elif "gnome" in current or "unity" in current or "pop" in current:
        comp = "gnome"
    elif session == "x11":
        comp = "x11"
    elif session == "wayland":
        comp = "wlroots"            # river, labwc, wayfire, ... mostly speak wlroots protocols
    else:
        comp = "unknown"
    if session == "x11" and comp in ("kde", "gnome"):
        comp = "x11"                # an X11 session of Plasma/GNOME: X11 tools work
    return Desktop(session, comp)


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _run(cmd: list[str], timeout: float = 2, text: bool = True, check: bool = False, input: str | None = None):
    return subprocess.run(cmd, capture_output=True, text=text, timeout=timeout, check=check, input=input)


# ---- focused window ----------------------------------------------------------

def _sway_focused(tree: dict) -> dict | None:
    stack = [tree]
    while stack:
        node = stack.pop()
        if node.get("focused"):
            return node
        stack += node.get("nodes", []) + node.get("floating_nodes", [])
    return None


def focused_window(desk: Desktop | None = None) -> tuple[str, str]:
    """(app class / app id, title) of the focused window, or ("", "")."""
    desk = desk or detect()
    try:
        if desk.compositor == "hyprland" and have("hyprctl"):
            data = json.loads(_run(["hyprctl", "activewindow", "-j"]).stdout or "{}")
            return str(data.get("class") or data.get("initialClass") or ""), str(data.get("title") or "")
        if desk.compositor == "sway" and have("swaymsg"):
            node = _sway_focused(json.loads(_run(["swaymsg", "-t", "get_tree", "-r"]).stdout or "{}")) or {}
            cls = node.get("app_id") or (node.get("window_properties") or {}).get("class") or ""
            return str(cls), str(node.get("name") or "")
        if desk.compositor == "niri" and have("niri"):
            data = json.loads(_run(["niri", "msg", "--json", "focused-window"]).stdout or "null") or {}
            return str(data.get("app_id") or ""), str(data.get("title") or "")
        if desk.compositor == "kde" and have("kdotool"):
            wid = _run(["kdotool", "getactivewindow"]).stdout.strip()
            if wid:
                cls = _run(["kdotool", "getwindowclassname", wid]).stdout.strip()
                return cls, _run(["kdotool", "getwindowname", wid]).stdout.strip()
        if desk.compositor == "gnome" and have("gdbus"):
            out = _run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
                        "--object-path", "/org/gnome/Shell/Extensions/Windows",
                        "--method", "org.gnome.Shell.Extensions.Windows.List"]).stdout
            if out.startswith("('"):
                windows = json.loads(out[2:out.rindex("'")])
                win = next((w for w in windows if w.get("focus")), None)
                if win:
                    return str(win.get("wm_class") or ""), str(win.get("title") or "")
        if desk.session == "x11" and have("xdotool"):
            cls = _run(["xdotool", "getactivewindow", "getwindowclassname"]).stdout.strip()
            return cls, _run(["xdotool", "getactivewindow", "getwindowname"]).stdout.strip()
    except Exception as exc:  # noqa: BLE001 - context is best effort
        log.debug("focused window (%s): %s", desk.describe(), exc)
    return "", ""


# ---- clipboard ---------------------------------------------------------------

def clipboard_get(desk: Desktop | None = None, primary: bool = False) -> str:
    desk = desk or detect()
    if desk.wayland:
        cmd = ["wl-paste", "--no-newline"] + (["--primary"] if primary else [])
    elif have("xclip"):
        cmd = ["xclip", "-o", "-selection", "primary" if primary else "clipboard"]
    else:
        cmd = ["xsel", "--output", "--primary" if primary else "--clipboard"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.decode(errors="replace") if out.returncode == 0 else ""


def clipboard_has_text(desk: Desktop | None = None) -> bool:
    desk = desk or detect()
    cmd = ["wl-paste", "--list-types"] if desk.wayland else ["xclip", "-o", "-selection", "clipboard", "-t", "TARGETS"]
    try:
        types = _run(cmd).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(t.startswith("text/") or t in ("UTF8_STRING", "STRING", "TEXT") for t in types.split())


def clipboard_set(text: str, desk: Desktop | None = None) -> None:
    desk = desk or detect()
    if desk.wayland:
        subprocess.run(["wl-copy", "--", text], check=True, timeout=5)
    elif have("xclip"):
        subprocess.run(["xclip", "-selection", "clipboard", "-in"], input=text.encode(), check=True, timeout=5)
    else:
        subprocess.run(["xsel", "--clipboard", "--input"], input=text.encode(), check=True, timeout=5)


def clipboard_clear(desk: Desktop | None = None) -> None:
    desk = desk or detect()
    if desk.wayland:
        subprocess.run(["wl-copy", "--clear"], timeout=2, check=False)
    else:
        try:
            clipboard_set("", desk)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass


# ---- keys and typing -----------------------------------------------------------

def _ydotool_combo(combo: str) -> list[str]:
    keys = [KEYCODES[k] for k in combo.lower().split("+")]
    return [f"{k}:1" for k in keys] + [f"{k}:0" for k in reversed(keys)]


def shortcut_command(desk: Desktop, combo: str) -> list[str] | None:
    """The command that presses `combo` ("ctrl+v", "ctrl+shift+v", "shift+return",
    "return") in the focused window."""
    mods, key = combo.lower().split("+")[:-1], combo.lower().split("+")[-1]
    keysym = KEYSYMS.get(key, key)
    if desk.compositor == "hyprland" and have("hyprctl"):
        return ["hyprctl", "dispatch", "sendshortcut", f"{' '.join(m.upper() for m in mods)}, {key.upper()},"]
    if desk.wlroots_like and have("wtype"):
        cmd = ["wtype"]
        for m in mods:
            cmd += ["-M", m]
        cmd += ["-k", keysym]
        for m in reversed(mods):
            cmd += ["-m", m]
        return cmd
    if desk.session == "x11" and have("xdotool"):
        return ["xdotool", "key", "--clearmodifiers", "+".join([*mods, keysym])]
    if have("ydotool"):
        return ["ydotool", "key", *_ydotool_combo(combo)]
    return None


def send_shortcut(combo: str, desk: Desktop | None = None) -> None:
    desk = desk or detect()
    cmd = shortcut_command(desk, combo)
    if cmd is None:
        raise FileNotFoundError(f"no tool to press {combo} on {desk.describe()} (install ydotool)")
    subprocess.run(cmd, check=True, capture_output=True, timeout=5)


def typer_command(desk: Desktop, text: str, preferred: str = "auto") -> list[str] | None:
    """The command that types `text` into the focused window, or None if nothing can."""
    order = {"wtype": ["wtype"], "ydotool": ["ydotool"], "xdotool": ["xdotool"], "dotool": ["dotool"]}.get(preferred)
    if order is None:
        if desk.session == "x11":
            order = ["xdotool", "ydotool"]
        elif desk.wlroots_like:
            order = ["wtype", "ydotool", "dotool"]
        else:
            order = ["ydotool", "dotool", "wtype"]
    for tool in order:
        if not have(tool):
            continue
        if tool == "wtype":
            return ["wtype", "--", text]
        if tool == "ydotool":
            return ["ydotool", "type", "--key-delay", "2", "--", text]
        if tool == "xdotool":
            return ["xdotool", "type", "--clearmodifiers", "--delay", "2", "--", text]
        if tool == "dotool":
            return ["dotool"]               # reads "type ..." on stdin
    return None


def typer_commands(desk: Desktop, text: str, preferred: str = "auto",
                   newline: str = "enter") -> list[list[str]] | None:
    """The commands that type `text`, run in order, or None if something can't be done.

    newline = "enter": line breaks are typed as Enter, like any other character.
    newline = "shift+enter": each line break is pressed as Shift+Enter instead, so a
    chat app inserts a new line rather than sending the message. dotool types
    everything from one script on stdin, so it is a single ["dotool"] either way.
    """
    first = typer_command(desk, text, preferred)
    if first is None or first == ["dotool"] or newline == "enter" or "\n" not in text:
        return None if first is None else [first]
    shift_enter = shortcut_command(desk, "shift+return")
    if shift_enter is None:
        return None
    cmds: list[list[str]] = []
    for i, line in enumerate(text.split("\n")):
        if i:
            cmds.append(shift_enter)
        if line:
            cmd = typer_command(desk, line, preferred)
            if cmd is None:
                return None
            cmds.append(cmd)
    return cmds


def _dotool_script(text: str, newline: str) -> str:
    key = "key shift+enter" if newline == "shift+enter" else "key enter"
    return "".join((f"{key}\n" if i else "") + (f"type {line}\n" if line else "")
                   for i, line in enumerate(text.split("\n")))


def type_text(text: str, desk: Desktop | None = None, preferred: str = "auto", newline: str = "enter") -> str:
    """Type `text`; returns the tool used. Raises if no tool can type here.
    `newline` is how line breaks are pressed: "enter" or "shift+enter"."""
    desk = desk or detect()
    typer = typer_command(desk, text, preferred)
    if typer is None:
        raise FileNotFoundError(f"no typing tool for {desk.describe()} (install "
                                f"{'xdotool' if desk.session == 'x11' else 'wtype or ydotool'})")
    if typer == ["dotool"]:
        subprocess.run(typer, input=_dotool_script(text, newline).encode(),
                       check=True, capture_output=True, timeout=60)
        return "dotool"
    cmds = typer_commands(desk, text, preferred, newline)
    if cmds is None:
        raise FileNotFoundError(f"no tool to press Shift+Enter on {desk.describe()} (install ydotool)")
    for cmd in cmds:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return typer[0]


# ---- menus, prompts, terminals ----------------------------------------------------

MENUS = ["omarchy-menu-select", "fuzzel", "wofi", "rofi", "tofi", "bemenu", "dmenu"]


def match_option(choice: str, options: list[str]) -> str | None:
    """The option a menu tool's output refers to. Omarchy's picker may leave out the
    leading (icon) column, so a trailing run of the tab-separated columns also matches."""
    if not choice:
        return None
    if choice in options:
        return choice
    for option in options:
        parts = option.split("\t")
        if any("\t".join(parts[i:]) == choice for i in range(len(parts))):
            return option
    for option in options:
        if choice in option.split("\t"):
            return option
    return None


def menu(title: str, options: list[str], width: int = 720) -> str | None:
    """Pick one of `options` (which may contain tab-separated columns) in the
    desktop's launcher. Returns the chosen option exactly as given, or None."""
    if have("omarchy-menu-select"):
        out = _run(["omarchy-menu-select", title, *options, "--", "--width", str(width)], timeout=300)
        return match_option(out.stdout.strip(), options) if out.returncode == 0 else None
    shown = [o.replace("\t", "   ·   ") for o in options]
    for tool, cmd in (("fuzzel", ["fuzzel", "--dmenu", "--prompt", f"{title}: ", "--width", str(width // 10)]),
                      ("wofi", ["wofi", "--dmenu", "--prompt", title]),
                      ("rofi", ["rofi", "-dmenu", "-i", "-p", title]),
                      ("tofi", ["tofi", f"--prompt-text={title}: "]),
                      ("bemenu", ["bemenu", "-i", "-p", title]),
                      ("dmenu", ["dmenu", "-i", "-p", title])):
        if have(tool):
            out = _run(cmd, timeout=300, input="\n".join(shown))
            choice = out.stdout.strip()
            return options[shown.index(choice)] if choice in shown else None
    if have("zenity"):
        out = _run(["zenity", "--list", "--title", title, "--column", title, *shown], timeout=300)
        choice = out.stdout.strip()
        return options[shown.index(choice)] if choice in shown else None
    raise FileNotFoundError("no menu tool found (install fuzzel, wofi or rofi)")


def ask(prompt: str) -> str | None:
    """One line of input from the user."""
    if have("omarchy-menu-input"):
        out = _run(["omarchy-menu-input", prompt, "--width", "640"], timeout=600)
    elif have("fuzzel"):
        out = _run(["fuzzel", "--dmenu", "--prompt-only", f"{prompt} "], timeout=600, input="")
    elif have("rofi"):
        out = _run(["rofi", "-dmenu", "-p", prompt], timeout=600, input="")
    elif have("zenity"):
        out = _run(["zenity", "--entry", "--text", prompt], timeout=600)
    else:
        return None
    return out.stdout.strip() or None


TERMINALS = ["xdg-terminal-exec", "ghostty", "alacritty", "kitty", "foot", "wezterm", "konsole", "gnome-terminal",
             "xfce4-terminal", "xterm"]


def terminal_command(command: str) -> list[str]:
    """A terminal window running `command` (and waiting for Enter before closing)."""
    launcher = shutil.which("omarchy-launch-floating-terminal-with-presentation")
    if launcher:
        return [launcher, command]
    script = command + "; read -rp 'Press Enter to close'"
    for term in [os.environ.get("TERMINAL", ""), *TERMINALS]:
        if not term or not have(term):
            continue
        if term == "gnome-terminal":
            return [term, "--", "bash", "-c", script]
        if term in ("wezterm",):
            return [term, "start", "--", "bash", "-c", script]
        return [term, "-e", "bash", "-c", script]
    return ["bash", "-c", command]


def open_terminal(command: str) -> None:
    subprocess.Popen(terminal_command(command), start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_editor(path: str) -> None:
    if have("omarchy-launch-editor"):
        subprocess.Popen(["omarchy-launch-editor", path], start_new_session=True)
    elif os.environ.get("VISUAL") or os.environ.get("EDITOR"):
        open_terminal(f"${{VISUAL:-$EDITOR}} {path!r}")
    else:
        subprocess.Popen(["xdg-open", path], start_new_session=True)


def report(desk: Desktop | None = None) -> list[str]:
    """What chttr will use on this desktop, for `chttr doctor`."""
    desk = desk or detect()
    typer = typer_command(desk, "x")
    combo = shortcut_command(desk, "ctrl+v")
    menu_tool = next((m for m in MENUS if have(m)), "zenity" if have("zenity") else "")
    window = {"hyprland": "hyprctl", "sway": "swaymsg", "niri": "niri msg", "kde": "kdotool",
              "gnome": "Window Calls extension", "x11": "xdotool"}.get(desk.compositor, "")
    clip = "wl-clipboard" if desk.wayland else ("xclip" if have("xclip") else "xsel")
    lines = [f"desktop:     {desk.describe()}",
             f"typing:      {typer[0] if typer else 'PROBLEM: none found (text goes to the clipboard)'}",
             f"shortcuts:   {combo[0] if combo else 'none found (selection and paste unavailable)'}",
             f"clipboard:   {clip}",
             f"window info: {window or 'unavailable (per-app modes off)'}",
             f"menus:       {menu_tool or 'none found (install fuzzel, wofi or rofi)'}"]
    if typer and typer[0] == "ydotool":
        lines.append("             ydotool needs its daemon: systemctl --user enable --now ydotool")
    return lines
