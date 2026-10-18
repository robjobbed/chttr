"""The Anthropic API key behind Claude cleanup and meeting notes: `chttr connect claude`.

The key lives in [polish] api_key_file (~/.config/chttr/anthropic-api-key), readable
only by you. ANTHROPIC_API_KEY in the daemon's environment takes precedence.
"""

from __future__ import annotations

import getpass
import os
import time
from pathlib import Path

from . import config as C

KEY_PREFIX = "sk-ant-"
MIN_LENGTH = 30
DEFAULT_PROMPT = "Anthropic API key (sk-ant-…, from https://console.anthropic.com/settings/keys): "


def path(cfg) -> Path:
    polish = getattr(cfg, "polish", cfg)
    return Path(polish.api_key_file or C.PolishCfg.api_key_file).expanduser()


def check(key: str) -> str:
    """The key without surrounding whitespace, or ValueError if it can't be an Anthropic key."""
    key = key.strip()
    if not key.startswith(KEY_PREFIX) or len(key) < MIN_LENGTH or any(c.isspace() for c in key):
        raise ValueError("that does not look like an Anthropic API key (sk-ant-…)")
    return key


def save(key: str, cfg) -> Path:
    key = check(key)
    target = path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with C.open_private(target, append=False) as fh:
        fh.write(key + "\n")
    os.chmod(target, 0o600)                 # an older file may have been created more open
    return target


def remove(cfg) -> bool:
    try:
        path(cfg).unlink()
    except FileNotFoundError:
        return False
    return True


def _file_key(cfg) -> str:
    try:
        return path(cfg).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def status(cfg) -> str:
    """Where chttr gets its key from, in the words `chttr connect` shows."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY in environment"
    if _file_key(cfg):
        target = path(cfg)
        if C.too_open(target):
            return f"key file (readable by others: chmod 600 {target})"
        return "key file (0600)"
    return "none"


def _anthropic(**kw):
    import anthropic

    return anthropic.Anthropic(**kw)


def verify(key: str, model: str, timeout: float = 10) -> tuple[bool, str]:
    """(usable, what to tell the user). One cheap request: look the cleanup model up."""
    import anthropic

    start = time.monotonic()
    try:
        _anthropic(api_key=key, max_retries=0, timeout=timeout).models.retrieve(model)
    except anthropic.AuthenticationError:
        return False, "that key was rejected"
    except anthropic.PermissionDeniedError:
        return False, "that key is not allowed to use the API (permission denied)"
    except anthropic.NotFoundError:
        return True, f"key works, but the model {model} is not available; check [polish] cleanup_model"
    except anthropic.APIConnectionError:
        return True, "no network right now; saved anyway, used when online"
    except anthropic.APIStatusError as exc:
        return True, f"saved; Anthropic answered HTTP {exc.status_code} just now, so it is checked again when used"
    ms = (time.monotonic() - start) * 1000
    return True, f"key accepted ({model} reachable, {ms:.0f} ms)"


def _dictate_key(cfg) -> str:
    try:
        from . import hotkeys
        from .desktop import detect

        return hotkeys.effective_bindings(cfg, detect()).get("dictate") or "Insert"
    except Exception:  # noqa: BLE001 - only the wording of a tip depends on it
        return "Insert"


def connect_interactive(cfg, prompt: str = DEFAULT_PROMPT, tip: bool = True) -> bool:
    """Ask for a key (hidden), check it with Anthropic, store it 0600 and tell the daemon.
    True when a key was stored. `tip=False` leaves out the "Try it" line (setup, where
    the daemon isn't ready yet, ends with its own)."""
    from .client import DaemonNotRunning, send

    try:
        entered = getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not entered.strip():
        print("  no key entered; Claude stays off (add one any time: chttr connect claude)")
        return False
    try:
        key = check(entered)
    except ValueError as exc:
        print(f"  {exc}; nothing saved")
        return False
    model = getattr(cfg, "polish", cfg).cleanup_model
    print("  checking it with Anthropic…", flush=True)
    ok, message = verify(key, model)
    if not ok:
        print(f"  Claude: {message}; nothing saved. Copy it again from https://console.anthropic.com/settings/keys")
        return False
    target = save(key, cfg)
    print(f"  Claude: {message}")
    print(f"  saved to {target} (only you can read it)")
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("  note: ANTHROPIC_API_KEY is set in this environment and is used before this file")
    try:
        send("reload")
    except (DaemonNotRunning, OSError, ValueError):
        pass
    if tip:
        print(f'  Try it: hold {_dictate_key(cfg)} and say "um send the contract Tuesday, no, Friday".')
    return True
