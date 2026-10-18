"""Desktop notifications and audio cues."""

from __future__ import annotations

import logging
import math
import shutil
import struct
import subprocess
import time
import wave
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import FeedbackCfg

log = logging.getLogger("chttr.feedback")
# Replace ids. Progress toasts ("Transcribing…", "Typed") replace each other on one id.
# An error or warning takes that id over (so it replaces a "Transcribing…" that would
# otherwise never expire), and progress moves to a free id: the next "Typed" shows
# beside the error instead of over it.
_NOTIFY_IDS = ("92110", "92111", "92112")

Action = tuple[str, list[str]]     # (button label, argv to run when it's picked)


class Feedback:
    def __init__(self, cfg: FeedbackCfg, runtime_dir: Path) -> None:
        self.cfg = cfg
        self.omarchy: bool | None = None       # None: look for omarchy-notification-send on first use
        self._progress_id = _NOTIFY_IDS[0]
        self._alert_until: dict[str, float] = {}     # replace id -> when the alert on it expires
        self.sounds = {
            "start": runtime_dir / "start.wav",
            "stop": runtime_dir / "stop.wav",
            "error": runtime_dir / "error.wav",
        }
        if cfg.sounds:
            _tone(self.sounds["start"], [(660, 0.07), (880, 0.09)])
            _tone(self.sounds["stop"], [(880, 0.07), (660, 0.09)])
            _tone(self.sounds["error"], [(330, 0.12), (0, 0.04), (330, 0.12)])

    def play(self, name: str) -> None:
        if not self.cfg.sounds:
            return
        try:
            subprocess.Popen(
                ["pw-play", "--volume", "0.35", "--media-role", "Notification", str(self.sounds[name])],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass

    def notify(self, title: str, body: str = "", *, urgency: str = "low", timeout_ms: int = 3000) -> None:
        if not self.cfg.notifications:
            return
        replace = self._progress_id
        if urgency != "low":
            self._alert_until[replace] = time.monotonic() + (timeout_ms or 8000) / 1000
            self._progress_id = min((i for i in _NOTIFY_IDS if i != replace),
                                    key=lambda i: self._alert_until.get(i, 0.0))
        cmd = ["notify-send", "-a", "chttr", "-r", replace, "-u", urgency, "-t", str(timeout_ms), title]
        if body:
            cmd.append(body)
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            log.info("%s %s", title, body)

    def offer(self, title: str, body: str, primary: Action, others: Sequence[Action] = (), *,
              glyph: str = "", urgency: str = "normal", timeout_ms: int = 120000,
              runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str | None:
        """A notification you answer by clicking it: picking an action runs its argv.

        Omarchy's shell draws no buttons and only knows a click, so there the click
        runs `primary` (omarchy-notification-send --exec) and nothing waits: returns
        None at once. Elsewhere `primary` is the default action (a click on the toast)
        and `others` become buttons; this waits for the answer, runs it detached and
        returns its label, or None when the toast is dismissed or times out. An action
        with an empty argv just closes the toast (a "Not now" button)."""
        if not self.cfg.notifications:
            return None
        label, argv = primary
        hint = f"Click to {label[:1].lower()}{label[1:]}."
        text = f"{body}\n{hint}" if body else hint
        if self._on_omarchy():
            cmd = ["omarchy-notification-send", "--app-name", "chttr", "-u", urgency, "-t", str(timeout_ms)]
            cmd += ["-g", glyph] if glyph else []
            try:
                done = runner([*cmd, title, text, "--exec", *argv], capture_output=True, text=True, timeout=10)
                if done.returncode == 0:
                    return None
                log.warning("omarchy-notification-send failed: %s", (done.stderr or "").strip())
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.warning("omarchy-notification-send failed: %s", exc)
            # fall through: a plain notification with actions still beats no question at all
        actions = [(label, argv), *others]
        cmd = ["notify-send", "-a", "chttr", "-u", urgency, "-t", str(timeout_ms), "-A", f"default={label}"]
        for i, (other, _) in enumerate(others):
            cmd += ["-A", f"{i}={other}"]
        try:
            out = runner([*cmd, "--wait", title, text], capture_output=True, text=True,
                         timeout=timeout_ms / 1000 + 60).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.info("no answer to %r: %s", title, exc)
            return None
        if out == "default":
            picked = actions[0]
        elif out.isdigit() and int(out) < len(others):
            picked = actions[int(out) + 1]
        else:
            return None             # dismissed, expired, or a server that sent nothing back
        if not picked[1]:
            return None
        try:
            subprocess.Popen(picked[1], start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.warning("could not run %s: %s", picked[1][0], exc)
            return None
        return picked[0]

    def _on_omarchy(self) -> bool:
        if self.omarchy is None:
            self.omarchy = shutil.which("omarchy-notification-send") is not None
        return self.omarchy


def _tone(path: Path, notes: list[tuple[float, float]], rate: int = 44100) -> None:
    """Write a tiny WAV of sine notes [(hz, seconds), ...]; hz=0 is silence."""
    frames = bytearray()
    for hz, secs in notes:
        n = int(rate * secs)
        for i in range(n):
            env = min(1.0, i / (rate * 0.008), (n - i) / (rate * 0.02))  # soft attack/release
            v = math.sin(2 * math.pi * hz * i / rate) * 0.6 * env if hz else 0.0
            frames += struct.pack("<h", int(v * 32767))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(frames))
