"""A meeting being recorded (owned by the daemon), and the hand-off to processing."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..config import Config
from .store import new_meeting_dir, update_meta

log = logging.getLogger("chttr.meeting")


# Passed on to the processing unit when the daemon has them (names only: values,
# such as an API key, never appear on a command line).
PASS_ENV = ("CHTTR_CONFIG", "ANTHROPIC_API_KEY", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
            "WAYLAND_DISPLAY", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "PATH")


def launch_processing(directory: Path) -> str:
    """Run `chttr meeting process` at low priority, outside the daemon: in a systemd
    unit of its own when there is a user manager (restarting chttr.service no longer
    kills it), else in its own session. Returns how: "systemd-run" or "nice"."""
    process = [sys.executable, "-m", "chttr", "meeting", "process", "--notify", str(directory)]
    if shutil.which("systemd-run"):
        unit = "chttr-process-" + re.sub(r"[^A-Za-z0-9_.-]", "-", directory.name)
        cmd = ["systemd-run", "--user", "--collect", "--quiet", "--unit", unit, "--property=Nice=10",
               f"--property=StandardOutput=append:{directory.absolute() / 'process.log'}", "--property=StandardError=inherit"]
        cmd += [f"--setenv={name}" for name in PASS_ENV if name in os.environ]
        try:
            done = subprocess.run(cmd + process, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=15, check=False)
            if done.returncode == 0:
                log.info("processing %s in %s.service", directory.name, unit)
                return "systemd-run"
            log.warning("systemd-run failed (%s); processing in the background instead",
                        (done.stderr or "").strip() or f"exit {done.returncode}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("systemd-run failed (%s); processing in the background instead", exc)
    with (directory / "process.log").open("ab") as log_file:
        subprocess.Popen(["nice", "-n", "10", *process], stdin=subprocess.DEVNULL, stdout=log_file,
                         stderr=subprocess.STDOUT, start_new_session=True)
    return "nice"


class Session:
    """One meeting being recorded (owned by the daemon)."""

    def __init__(self, cfg: Config, code: str = "", title: str = "", source: str = "manual") -> None:
        from .recorder import MeetingRecorder

        self.code, self.title, self.source = code, title, source
        self.dir = new_meeting_dir(time.time(), code)
        self.recorder = MeetingRecorder(self.dir, cfg.audio.device)
        self._events = None

    @property
    def elapsed(self) -> float:
        return self.recorder.elapsed

    def start(self) -> None:
        self.recorder.start()
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        self.save_meta(status="recording")

    def save_meta(self, **extra) -> None:
        update_meta(self.dir, id=self.dir.name, started=self.recorder.started, code=self.code,
                    title=self.title, source=self.source, **extra)

    def log(self, event: dict) -> None:
        if self._events:
            self._events.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._events.flush()

    def stop(self) -> float:
        secs = self.recorder.stop()
        if self._events:
            self._events.close()
            self._events = None
        self.save_meta(status="recorded", duration=round(secs, 1))
        return secs

    def discard(self) -> None:
        self.recorder.stop()
        if self._events:
            self._events.close()
            self._events = None
        shutil.rmtree(self.dir, ignore_errors=True)

