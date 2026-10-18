"""The bridge between the Meet extension and the daemon, and its installer.

Chromium starts `chttr native-host` (through a tiny wrapper script, because a
native messaging manifest can't pass arguments) when the extension connects,
and talks to it over stdin/stdout: each message is a 4-byte little-endian
length followed by JSON. Every message is forwarded to the daemon's socket as
`meet <json>`; the daemon's reply goes back to the extension.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import struct
import sys
from pathlib import Path

from . import config as C
from .client import DaemonNotRunning, send

HOST_NAME = "com.chttr.meet"
EXTENSION_DIR = Path(__file__).with_name("extension")
_C = Path.home() / ".config"
BROWSERS = [               # (executables, config dir, flags file) of Chromium-family browsers
    (("chromium", "chromium-browser"), _C / "chromium", _C / "chromium-flags.conf"),
    (("google-chrome-stable", "google-chrome"), _C / "google-chrome", _C / "chrome-flags.conf"),
    (("brave", "brave-browser"), _C / "BraveSoftware/Brave-Browser", _C / "brave-flags.conf"),
    (("vivaldi", "vivaldi-stable"), _C / "vivaldi", _C / "vivaldi-stable.conf"),
    (("microsoft-edge-stable", "microsoft-edge"), _C / "microsoft-edge", _C / "microsoft-edge-stable-flags.conf"),
]
BROWSER_DIRS = [(d, f) for exes, d, f in BROWSERS if any(shutil.which(e) for e in exes)]


def run() -> int:
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        header = stdin.read(4)
        if len(header) < 4:
            return 0
        (length,) = struct.unpack("<I", header)
        msg: dict = {}
        try:
            msg = json.loads(stdin.read(length))
            reply = send("meet " + json.dumps(msg, separators=(",", ":")), timeout=10)
        except DaemonNotRunning as exc:
            reply = {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - never die on one bad message
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if isinstance(msg, dict) and "tab" in msg:
            reply["tab"] = msg["tab"]
        data = json.dumps(reply).encode()
        stdout.write(struct.pack("<I", len(data)) + data)
        stdout.flush()


def extension_id() -> str:
    """Chromium derives an unpacked extension's ID from the manifest's public key."""
    key = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))["key"]
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def _chttr_command() -> str:
    exe = shutil.which("chttr") or str(Path.home() / ".local/bin/chttr")
    return f'exec "{exe}" native-host' if Path(exe).exists() else f'exec "{sys.executable}" -m chttr native-host'


def _is_chttr_extension(path: Path) -> bool:
    try:
        return json.loads((path / "manifest.json").read_text(encoding="utf-8")).get("name") == "chttr for Google Meet"
    except (OSError, ValueError):
        return path.name == "extension" and "chttr" in str(path)


def _tilde(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _ours(path: str) -> bool:
    return path == str(EXTENSION_DIR) or _is_chttr_extension(Path(path))


def _extension_lists(text: str) -> list[list[str]]:
    """Each --load-extension line's paths."""
    return [[x for x in line.strip().split("=", 1)[1].split(",") if x]
            for line in text.splitlines() if line.strip().startswith("--load-extension=")]


def _chttr_wrote(text: str) -> bool:
    """True when every line is chttr's own --load-extension line: a file chttr
    created, so there is no original of the user's to keep."""
    lines = [line for line in text.splitlines() if line.strip()]
    lists = _extension_lists(text)
    return bool(lines) and len(lists) == len(lines) and all(_ours(x) for paths in lists for x in paths)


def _backup(flags: Path) -> None:
    """Keep the flags file as it was before chttr first changed it (uninstall puts it back)."""
    backup = flags.with_name(flags.name + ".bak-chttr")
    if flags.exists() and not backup.exists() and not _chttr_wrote(flags.read_text(encoding="utf-8")):
        shutil.copy2(flags, backup)


def _add_flag(flags: Path, flag: str, value: str | None = None) -> str:
    """Add a flag to a browser's flags file (Omarchy loads its own extensions the
    same way). `--load-extension` takes a comma-separated list: `value` joins it,
    replacing other checkouts of this extension. A bare flag (value None) is added
    once. Returns what was done."""
    lines = flags.read_text(encoding="utf-8").splitlines() if flags.exists() else []
    if value is None:
        if any(line.strip() == flag or line.strip().startswith(flag + "=") for line in lines):
            return "already set"
        _backup(flags)
        flags.parent.mkdir(parents=True, exist_ok=True)
        flags.write_text("\n".join([*lines, flag]) + "\n", encoding="utf-8")
        return f"added to {flags}"
    for i, line in enumerate(lines):
        if line.strip().startswith(flag + "="):
            paths = line.strip().split("=", 1)[1].split(",")
            if value in paths:
                return "already loaded"
            # another checkout of this extension (same key, same ID) would clash
            paths = [x for x in paths if x and not _is_chttr_extension(Path(x))]
            _backup(flags)
            lines[i] = f"{flag}=" + ",".join([*paths, value])
            flags.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return f"added to {flags}"
    _backup(flags)
    flags.parent.mkdir(parents=True, exist_ok=True)
    flags.write_text("\n".join([*lines, f"{flag}={value}"]) + "\n", encoding="utf-8")
    return f"added to {flags}"


def _add_load_extension(flags: Path, ext: str) -> str:
    return _add_flag(flags, "--load-extension", ext)


def _remove_extension(flags: Path) -> str | None:
    """Take this extension (any checkout of it) out of --load-extension, dropping
    the line when nothing else is left. When the file is then what it was before
    chttr touched it, the backup goes too; a file chttr created is deleted."""
    if not flags.exists():
        return None
    text = flags.read_text(encoding="utf-8")
    lines, changed = [], False
    for line in text.splitlines():
        if line.strip().startswith("--load-extension="):
            paths = line.strip().split("=", 1)[1].split(",")
            keep = [x for x in paths if x and x != str(EXTENSION_DIR) and not _is_chttr_extension(Path(x))]
            if keep != [x for x in paths if x]:
                changed = True
                if keep:
                    lines.append("--load-extension=" + ",".join(keep))
                continue
        lines.append(line)
    if not changed:
        return None
    backup = flags.with_name(flags.name + ".bak-chttr")
    result = "\n".join(lines) + "\n" if lines else ""
    if backup.exists() and any(_ours(x) for paths in _extension_lists(backup.read_text(encoding="utf-8"))
                               for x in paths):
        backup.unlink()                 # taken after chttr's own first edit: not the user's original
    if backup.exists() and backup.read_text(encoding="utf-8").rstrip("\n") == result.rstrip("\n"):
        shutil.copy2(backup, flags)
        backup.unlink()
    elif not result.strip():
        flags.unlink()
        return f"removed {_tilde(flags)} (only chttr's extension was in it)"
    else:
        flags.write_text(result, encoding="utf-8")
    return f"extension removed from {_tilde(flags)}"


def install() -> list[str]:
    done = []
    wrapper = C.DATA_DIR / "native-host"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(f"#!/bin/sh\n# Started by the browser for the chttr Meet extension.\n{_chttr_command()}\n", encoding="utf-8")
    wrapper.chmod(0o755)
    manifest = {
        "name": HOST_NAME,
        "description": "chttr: Google Meet calls and captions to the chttr daemon",
        "path": str(wrapper),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id()}/"],
    }
    for config_dir, flags in BROWSER_DIRS:
        if not config_dir.exists():
            continue
        hosts = config_dir / "NativeMessagingHosts"
        hosts.mkdir(exist_ok=True)
        (hosts / f"{HOST_NAME}.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        done.append(f"native host registered for {config_dir.name}")
        done.append(f"extension {_add_load_extension(flags, str(EXTENSION_DIR))}")
    return done


def ensure_accessibility_flag() -> list[str]:
    """Chromium only exposes page text to the accessibility bus with
    --force-renderer-accessibility; add it to each installed browser's flags file."""
    done = []
    for config_dir, flags in BROWSER_DIRS:
        if not config_dir.exists() and not flags.exists():
            continue                    # installed but never started: no profile to read yet
        result = _add_flag(flags, "--force-renderer-accessibility")
        if result != "already set":
            done.append(f"--force-renderer-accessibility {result} (restart {config_dir.name} to apply)")
    return done


def missing_accessibility_flag() -> list[Path]:
    """Flags files of installed browsers that don't have --force-renderer-accessibility yet."""
    out = []
    for config_dir, flags in BROWSER_DIRS:
        if not config_dir.exists() and not flags.exists():
            continue
        lines = flags.read_text(encoding="utf-8").splitlines() if flags.exists() else []
        if not any(x.strip() == "--force-renderer-accessibility" for x in lines):
            out.append(flags)
    return out


def uninstall() -> list[str]:
    """Undo install(): the native host manifests, the --load-extension entry and the
    wrapper script. Looks at every known browser, installed or not any more."""
    done = []
    for _, config_dir, flags in BROWSERS:
        manifest = config_dir / "NativeMessagingHosts" / f"{HOST_NAME}.json"
        if manifest.exists():
            manifest.unlink()
            done.append(f"native host removed from {config_dir.name}")
        result = _remove_extension(flags)
        if result:
            done.append(result)
    wrapper = C.DATA_DIR / "native-host"
    if wrapper.exists():
        wrapper.unlink()
        done.append(f"removed {_tilde(wrapper)}")
    return done
