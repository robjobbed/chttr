"""Talk to a running daemon over its Unix socket."""

from __future__ import annotations

import json
import socket

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
