"""Talk to a running daemon over its Unix socket."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from . import config as C


class DaemonNotRunning(RuntimeError):
    pass


def send(command: str, timeout: float = 5.0) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(C.SOCKET_PATH))
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise DaemonNotRunning("chttr daemon is not running (systemctl --user start chttr, or: chttr daemon)") from exc
    try:
        sock.sendall((command + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    return json.loads(buf or b"{}")


def read_state() -> str:
    try:
        return C.STATE_FILE.read_text(encoding="utf-8").strip() or "idle"
    except OSError:
        return "idle"


def read_state_json() -> dict:
    """What the daemon last wrote to state.json (state, pid, meeting, cleanup, keys);
    an empty dict if there is none or it can't be read."""
    try:
        info = json.loads(C.STATE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def daemon_alive(info: dict) -> bool:
    """Is the daemon that wrote `info` still running? After a crash or a stop the
    file stays behind, but its pid is gone (or, much later, belongs to something else)."""
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass                    # someone else's process: running, at least
    except OSError:
        return False
    try:
        cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return True             # no /proc to ask: trust the signal check
    return b"chttr" in cmdline


def daemon_listening(timeout: float = 0.2) -> bool:
    """Does something answer on the socket? Connects and hangs up without a command,
    so it never raises and never waits on the daemon."""
    if not C.SOCKET_PATH.exists():
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(C.SOCKET_PATH))
    except OSError:
        return False
    finally:
        sock.close()
    return True
