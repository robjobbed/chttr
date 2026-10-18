"""The chttr daemon: holds the speech model, owns the state machine, listens on a Unix socket."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import wave
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from . import config as C
from . import context as CX
from . import devmode, levels, tidy
from . import polish as P
from .config import Config, Mode
from .dictionary import Dictionary
from .feedback import Feedback
from .meeting import Session, launch_processing
from .output import current_desktop, deliver, focused_window, grab_selection
from .recorder import Recorder
from .streaming import Streamer
from .transcriber import Transcriber, engine_key

log = logging.getLogger("chttr.daemon")

IDLE, RECORDING, TRANSCRIBING = "idle", "recording", "transcribing"
# What a recording is for. PTT and COMMAND end when the key is released;
# HANDSFREE ends on silence or a second press.
PTT, HANDSFREE, COMMAND = "dictate", "hands-free", "command"
OVERLAY_SCRIPT = Path(__file__).with_name("overlay.py")
# The overlay runs under the distribution's Python, which has PyGObject (python-gobject /
# python3-gi) and can see gtk4-layer-shell; chttr's own environment has neither.
SYSTEM_PYTHON = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else (shutil.which("python3") or "python3")
# gtk4-layer-shell must be loaded before libwayland-client; with Python that means LD_PRELOAD.
LAYER_SHELL_LIBS = [Path(d) / name
                    for d in ("/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu",
                              "/usr/local/lib", "/usr/local/lib64")
                    for name in ("libgtk4-layer-shell.so", "libgtk4-layer-shell.so.0")]
QUIET_PEAK_DB = -45.0                   # below this the mic is probably not picking anything up
CAPTION_INTERVAL = 0.8                  # seconds between live-caption passes
CAPTION_WINDOW = 8.0                    # only the last this-many seconds are re-transcribed
DOUBLE_TAP = 0.45                       # a second press this soon after a short tap: hands-free
ATSPI_GIVE_UP = 5                       # unreadable fields in a row before screen context stops
TAP = 0.35                              # recordings shorter than this are taps, not dictations
CALL_TIMEOUT = 90                       # no word from the Meet tab for this long: the call is over


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.runtime = C.ensure_runtime_dir()
        self.state = IDLE
        self.lock = threading.Lock()
        self.feedback = Feedback(cfg.feedback, self.runtime)
        self.recorder = Recorder(cfg.audio.device, on_level=self._on_level)
        self.transcriber = Transcriber(cfg)
        self.polish_warned = False
        P.breaker.on_recover = lambda: setattr(self, "polish_warned", False)
        self.overlay: subprocess.Popen | None = None
        self.overlay_lock = threading.Lock()
        self.overlay_warned = False
        self.restore_mute = False
        # per-recording
        self.kind = PTT
        self.app = self.title = ""
        self.selection = ""
        self.heard_speech = False
        self.levels = levels.Tracker()
        self.last_speech = 0.0
        self.captions_stop: threading.Event | None = None
        self.captions_thread: threading.Thread | None = None
        self.streamer: Streamer | None = None
        # meetings
        self.meeting: Session | None = None
        self.call: dict | None = None           # the Meet call the extension says we're in
        self.dictation_started = 0.0
        # A key press reads the focused window (and the selection, in command mode)
        # outside the lock; a release that arrives meanwhile is remembered here.
        self.starting = False
        self.release_requested = False
        # Dictations still being transcribed / cleaned up. A new recording may start
        # while earlier ones finish; their text is typed strictly in order.
        self.jobs = 0
        self.job_seq = 0
        self.next_delivery = 0
        self.delivery = threading.Condition()
        self.hotkeys = None
        self.last_tap = 0.0
        self.atspi_misses = 0
        self.field: Future | None = None             # the focused text field, read at key press
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chttr-context")
        self.offered: set[tuple[str, str]] = set()   # corrections already offered this session
        self._stop = threading.Event()
        C.MEETING_STATE_FILE.unlink(missing_ok=True)
        self._write_state(IDLE)
        self._tighten_permissions()
        self._prune_audio()

    def _tighten_permissions(self) -> None:
        """Files written by versions before 0.3 were created with the default umask."""
        C.ensure_data_dir()
        for path in (C.DATA_DIR, C.MEETINGS_DATA_DIR, C.HISTORY_FILE, C.VOICES_FILE):
            try:
                if path.exists() and C.too_open(path):
                    path.chmod(0o700 if path.is_dir() else 0o600)
            except OSError as exc:
                log.debug("could not tighten %s: %s", path, exc)

    # ---- state -------------------------------------------------------------
    def _refresh_state(self) -> None:
        """Recording beats transcribing beats idle."""
        if self.recorder.active:
            state = RECORDING
        else:
            state = TRANSCRIBING if self.jobs else IDLE
        if state != self.state:
            self._write_state(state)

    def _write_state(self, state: str) -> None:
        self.state = state
        tmp = C.STATE_FILE.with_suffix(".tmp")
        tmp.write_text(state, encoding="utf-8")
        os.replace(tmp, C.STATE_FILE)

    # ---- overlay -----------------------------------------------------------
    def _overlay_open(self, label: str) -> None:
        if not self.cfg.feedback.overlay or self.overlay is not None:
            return
        desk = current_desktop()
        if desk.compositor == "gnome" or not desk.wayland:
            # GNOME (Mutter) and X11 have no layer-shell: a regular window would steal focus.
            self._overlay_fail(f"no layer-shell on {desk.describe()}; notifications only")
            return
        env = dict(os.environ)
        for lib in LAYER_SHELL_LIBS:
            if lib.exists():
                env["LD_PRELOAD"] = str(lib)
                break
        cmd = [SYSTEM_PYTHON, "-I", str(OVERLAY_SCRIPT), "--label", label]
        if self.cfg.feedback.live_captions:
            cmd.append("--captions")
        try:
            self.overlay = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
            )
        except OSError as exc:
            self._overlay_fail(str(exc))

    def _overlay_fail(self, detail: str) -> None:
        self.overlay = None
        if not self.overlay_warned:
            self.overlay_warned = True
            log.warning("level overlay unavailable: %s", detail)

    def _overlay_send(self, line: str) -> None:
        proc = self.overlay
        if proc is None or proc.stdin is None:
            return
        try:
            with self.overlay_lock:
                proc.stdin.write((line.replace("\n", " ") + "\n").encode())
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace").strip()
            self._overlay_fail(err.splitlines()[-1] if err else "overlay exited")

    def _on_level(self, rms: float, peak_db: float) -> None:
        rms_db = 20 * np.log10(max(rms, 1e-6))
        self.levels.update(rms_db)
        a = self.cfg.audio
        thr = self.levels.threshold(a.threshold_db) if a.adaptive_threshold else a.threshold_db
        if rms_db > thr:
            self.heard_speech = True
            self.last_speech = time.monotonic()
        streamer = self.streamer
        if streamer is not None:
            streamer.threshold_db = thr     # segments close on your pauses, at any mic level
        self._overlay_send(f"{rms:.5f} {peak_db:.1f}")

    def _overlay_close(self) -> None:
        proc, self.overlay = self.overlay, None
        if proc is None:
            return
        try:
            with self.overlay_lock:
                if proc.stdin:
                    proc.stdin.close()
            proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

    # ---- streaming recognition / live captions ----------------------------
    def _captions_start(self) -> None:
        captions = self.cfg.feedback.live_captions and self.cfg.feedback.overlay
        if self.cfg.engine.streaming:
            prompt = Dictionary.load().whisper_prompt()
            self.streamer = Streamer(
                self.recorder, lambda audio: self.transcriber.transcribe(audio, prompt),
                self.cfg.audio.threshold_db,
                on_caption=(lambda text: self._overlay_send("caption " + text)) if captions else None,
            ).start()
            return
        if not captions:
            return
        self.captions_stop = threading.Event()
        self.captions_thread = threading.Thread(target=self._captions_loop, args=(self.captions_stop,), daemon=True)
        self.captions_thread.start()

    def _captions_loop(self, stop: threading.Event) -> None:
        last_len = 0
        while not stop.wait(CAPTION_INTERVAL):
            audio = self.recorder.snapshot(CAPTION_WINDOW)
            if len(audio) == last_len or not self.heard_speech:
                continue
            last_len = len(audio)
            try:
                text = self.transcriber.transcribe(audio)
            except Exception as exc:  # noqa: BLE001 - captions are cosmetic
                log.debug("caption pass failed: %s", exc)
                continue
            if not stop.is_set() and text:
                self._overlay_send("caption " + text)

    def _captions_detach(self) -> threading.Thread | Streamer | None:
        """Tell this recording's caption loop to stop; returns its thread to join (or
        the streamer that has already recognized part of the recording)."""
        if self.streamer is not None:
            streamer, self.streamer = self.streamer, None
            return streamer
        stop, thread = self.captions_stop, self.captions_thread
        self.captions_stop = self.captions_thread = None
        if stop:
            stop.set()
        return thread

    # ---- mic mute ----------------------------------------------------------
    def _unmute_if_needed(self) -> None:
        self.restore_mute = False
        if not self.cfg.audio.unmute:
            return
        try:
            out = subprocess.run(["pactl", "get-source-mute", "@DEFAULT_SOURCE@"], capture_output=True, text=True, timeout=2).stdout
            if "yes" in out:
                subprocess.run(["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "0"], timeout=2, check=False)
                self.restore_mute = True
                log.info("mic was muted; unmuted for recording")
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("could not query mic mute: %s", exc)

    def _restore_mute_state(self) -> None:
        if self.restore_mute:
            self.restore_mute = False
            subprocess.run(["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "1"], timeout=2, check=False)

    # ---- commands ----------------------------------------------------------
    def handle(self, line: str) -> dict:
        verb, _, arg = line.strip().partition(" ")
        cmd = verb.lower()
        if cmd in ("start", "command") or (cmd == "toggle" and not self.recorder.active and not self.starting):
            return self._press({"start": PTT, "command": COMMAND, "toggle": HANDSFREE}[cmd])
        with self.lock:
            if cmd == "meeting":
                return self._meeting_command(arg.strip().lower())
            if cmd == "meet":
                return self._meet_event(json.loads(arg))
            if cmd == "status":
                return {"ok": True, "state": self.state, "kind": self.kind if self.state == RECORDING else "",
                        "seconds": round(self.recorder.elapsed, 1)}
            if cmd == "toggle":
                return self._stop_and_transcribe()
            if cmd == "release":
                # Key-up of any Insert binding. Hands-free recordings ignore it, so
                # letting go of Super+Insert in either order doesn't end them.
                if self.starting:
                    if self.kind != HANDSFREE:
                        self.release_requested = True
                    return {"ok": True, "state": self.state, "message": "released while starting; will stop"}
                if self.state == RECORDING and self.kind == HANDSFREE:
                    return {"ok": True, "state": self.state, "message": "hands-free; ignoring key release"}
                return self._stop_and_transcribe()
            if cmd == "stop":
                return self._stop_and_transcribe()
            if cmd == "cancel":
                return self._cancel()
            if cmd == "reload":
                return self._reload()
            if cmd == "quit":
                self._stop.set()
                return {"ok": True, "state": self.state, "message": "bye"}
            return {"ok": False, "state": self.state, "error": f"unknown command {cmd!r}"}

    # ---- meetings ----------------------------------------------------------
    def _log_dictation(self) -> None:
        if self.meeting and self.dictation_started:
            self.meeting.log({"type": "dictation", "start": self.dictation_started, "end": time.time()})
        self.dictation_started = 0.0

    def _meeting_status(self) -> dict:
        m = self.meeting
        return {"ok": True, "state": self.state, "recording": m is not None,
                "meeting": {"title": m.title, "code": m.code, "seconds": round(m.elapsed), "dir": str(m.dir)} if m else None,
                "call": {k: self.call[k] for k in ("code", "title")} if self.call else None}

    def _meeting_command(self, arg: str) -> dict:
        if arg in ("", "status"):
            return self._meeting_status()
        if arg == "start":
            return self._meeting_start("manual")
        if arg == "stop":
            return self._meeting_stop()
        if arg == "toggle":
            return self._meeting_stop() if self.meeting else self._meeting_start("manual")
        if arg == "cancel":
            if not self.meeting:
                return {"ok": True, "state": self.state, "message": "no meeting is being recorded"}
            self.meeting.discard()
            self.meeting = None
            C.MEETING_STATE_FILE.unlink(missing_ok=True)
            self.feedback.play("error")
            self.feedback.notify("Meeting recording discarded", timeout_ms=3000)
            return {"ok": True, "state": self.state, "message": "discarded"}
        return {"ok": False, "state": self.state, "error": f"unknown meeting command {arg!r}"}

    def _meeting_start(self, source: str, title: str = "") -> dict:
        if self.meeting:
            return {**self._meeting_status(), "message": "already recording a meeting"}
        code = self.call["code"] if self.call else ""
        title = (self.call.get("title", "") if self.call else "") or title
        session = Session(self.cfg, code, title, source if not self.call else "extension")
        try:
            session.start()
        except RuntimeError as exc:
            session.discard()
            self.feedback.play("error")
            self.feedback.notify("chttr: can't record the meeting", str(exc), urgency="critical", timeout_ms=8000)
            return {"ok": False, "state": self.state, "error": str(exc)}
        self.meeting = session
        C.MEETING_STATE_FILE.write_text(json.dumps({"title": title, "code": code, "started": session.recorder.started}), encoding="utf-8")
        log.info("meeting recording started in %s", session.dir)
        self.feedback.play("start")
        self.feedback.notify("Recording meeting" + (f": {title}" if title else ""),
                             "Notes are written when the call ends." if code else "Stop with: chttr meeting stop",
                             timeout_ms=4000)
        return {**self._meeting_status(), "message": "recording"}

    def _meeting_stop(self) -> dict:
        session = self.meeting
        if not session:
            return {"ok": True, "state": self.state, "recording": False, "message": "no meeting is being recorded"}
        self.meeting = None
        C.MEETING_STATE_FILE.unlink(missing_ok=True)
        secs = session.stop()
        log.info("meeting recording stopped after %.0fs", secs)
        self.feedback.play("stop")
        if secs < 10:
            session.discard()
            self.feedback.notify("Meeting recording too short; discarded", timeout_ms=3000)
            return {"ok": True, "state": self.state, "recording": False, "message": "too short; discarded"}
        launch_processing(session.dir)
        self.feedback.notify("Meeting recorded", f"{secs / 60:.0f} min. Writing the transcript and notes…", timeout_ms=4000)
        return {"ok": True, "state": self.state, "recording": False, "message": f"recorded {secs:.0f}s; processing",
                "dir": str(session.dir)}

    def _meet_event(self, msg: dict) -> dict:
        """A message from the Meet extension (through `chttr native-host`)."""
        kind, code = msg.get("type", ""), msg.get("code", "")
        now = time.time()
        if kind == "joined" and code:
            fresh = not self.call or self.call["code"] != code
            self.call = {"code": code, "title": msg.get("title", ""), "seen": now}
            if self.meeting and not self.meeting.code:
                self.meeting.code, self.meeting.title = code, msg.get("title", "")    # started by hand, now we know the call
                self.meeting.save_meta(status="recording")
            elif fresh and not self.meeting:
                log.info("Meet call %s joined", code)
                if self.cfg.meeting.auto == "always":
                    self._meeting_start("extension")
                elif self.cfg.meeting.auto == "ask":
                    threading.Thread(target=self._ask_to_record, args=(code, msg.get("title", "")), daemon=True).start()
        elif kind == "left":
            if self.meeting and code and self.meeting.code == code:
                log.info("Meet call %s left", code)
                self._meeting_stop()
            if self.call and self.call["code"] == code:
                self.call = None
        elif code and self.call and self.call["code"] == code:
            self.call["seen"] = now
            if self.meeting and self.meeting.code == code:
                for e in msg.get("events", []):
                    e = dict(e)
                    e["t"] = float(e.get("t", now * 1000)) / 1000    # the page sends milliseconds
                    self.meeting.log(e)
        elif code and kind in ("heartbeat", "events") and not self.call:
            # daemon restarted mid-call: the page keeps talking, so pick the call back up
            self.call = {"code": code, "title": msg.get("title", ""), "seen": now}
        return {"ok": True, "recording": bool(self.meeting and self.meeting.code == code)}

    # ---- calls in other apps (PipeWire) ------------------------------------
    def _watch_calls(self) -> None:
        from . import calls as K

        watcher = K.CallWatcher()
        while not self._stop.wait(3.0):
            if not self.cfg.meeting.detect_calls:
                continue
            try:
                app = K.current_call(K.snapshot(), meet_call_active=self.call is not None)
            except Exception:
                log.debug("call detection failed", exc_info=True)
                continue
            decision = watcher.update(app, time.monotonic())
            if decision is None:
                continue
            what, label = decision
            if what == "start" and not self.meeting and self.call is None:
                log.info("a call seems to have started in %s", label)
                if self.cfg.meeting.auto == "always":
                    with self.lock:
                        self._meeting_start("app", self._calendar_title() or f"{label} call")
                elif self.cfg.meeting.auto == "ask":
                    threading.Thread(target=self._ask_to_record_app, args=(label,), daemon=True).start()
            elif what == "end":
                with self.lock:
                    if self.meeting and self.meeting.source == "app":
                        log.info("%s let go of the microphone; the call is over", label)
                        self._meeting_stop()

    def _calendar_title(self) -> str:
        from . import gcal

        try:
            event = gcal.find_event(gcal.read_urls(self.cfg.meeting.calendar_file), "")
            return event.title if event else ""
        except Exception:  # noqa: BLE001
            return ""

    def _ask_to_record_app(self, app: str) -> None:
        from . import gcal

        title, body = self._calendar_title(), "chttr will save a transcript and notes."
        try:
            event = gcal.find_event(gcal.read_urls(self.cfg.meeting.calendar_file), "")
            people = [a.name or a.email for a in event.attendees if not a.is_me] if event else []
            if people:
                body = ", ".join(people[:5]) + (f" +{len(people) - 5}" if len(people) > 5 else "")
        except Exception:  # noqa: BLE001
            pass
        label = title or f"this {app} call"
        try:
            out = subprocess.run(["notify-send", "-a", "chttr", "-u", "normal", "-t", "120000",
                                  "-A", "record=Record", "-A", "skip=Not now", "--wait",
                                  f"Record \u201c{label}\u201d?", body],
                                 capture_output=True, text=True, timeout=180).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return
        if out == "record":
            with self.lock:
                if not self.meeting:
                    self._meeting_start("app", title or f"{app} call")

    def _ask_to_record(self, code: str, title: str) -> None:
        from . import gcal

        label, body = title or "this Meet call", "chttr will save a transcript with speaker names."
        try:
            event = gcal.find_event(gcal.read_urls(self.cfg.meeting.calendar_file), code)
            if event:
                label = event.title
                people = [a.name or a.email for a in event.attendees if not a.is_me]
                if people:
                    body = ", ".join(people[:5]) + (f" +{len(people) - 5}" if len(people) > 5 else "")
                with self.lock:
                    if self.call and self.call["code"] == code and not self.call.get("title"):
                        self.call["title"] = event.title
        except Exception as exc:  # noqa: BLE001
            log.warning("calendar lookup failed: %s", exc)
        try:
            out = subprocess.run(["notify-send", "-a", "chttr", "-u", "normal", "-t", "120000",
                                  "-A", "record=Record", "-A", "skip=Not now", "--wait",
                                  f"Record \u201c{label}\u201d?", body],
                                 capture_output=True, text=True, timeout=180).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return
        if out == "record":
            with self.lock:
                if self.call and self.call["code"] == code and not self.meeting:
                    self._meeting_start("extension")

    def _reload(self) -> dict:
        cfg = C.load_config()
        message = "config reloaded"
        if engine_key(cfg) != self.transcriber.key:
            if self.state != IDLE:
                return {"ok": False, "state": self.state, "error": "busy; reload again when idle to switch engines"}
            self.transcriber = Transcriber(cfg)
            message += f"; {cfg.engine.name} loaded"
        self.cfg = cfg
        self.recorder.device = cfg.audio.device
        self.feedback.cfg = cfg.feedback
        return {"ok": True, "state": self.state, "message": message}

    def _press(self, kind: str) -> dict:
        """A key press. Reading the focused window and grabbing the selection talk to
        the compositor and the app, so they run without holding the lock."""
        with self.lock:
            if kind == PTT and self.recorder.active and self.kind == HANDSFREE:
                return self._stop_and_transcribe()         # a tap ends hands-free
        if not self._press_begin(kind):
            return {"ok": True, "state": self.state, "message": "already recording"}
        return self._press_finish(self.kind)

    def _press_begin(self, kind: str) -> bool:
        """Mark a recording as starting (instant). False if one is already going."""
        with self.lock:
            if self.recorder.active or self.starting:
                return False
            if kind == PTT and self.cfg.audio.double_tap and time.monotonic() - self.last_tap < DOUBLE_TAP:
                kind = HANDSFREE                            # double tap: hands-free until the next tap
                self.last_tap = 0.0
                log.info("double tap: hands-free")
            self.starting, self.release_requested, self.kind = True, False, kind
            return True

    def press_async(self, kind: str) -> None:
        """A press from the in-daemon hotkeys: marked as started right away, so the
        matching release (handled next, on the same thread) can't overtake it."""
        if self._press_begin(kind):
            threading.Thread(target=self._press_finish, args=(self.kind,), daemon=True).start()

    def on_hotkey(self, action: str, pressed: bool) -> None:
        """Key events from hotkeys.py (GNOME/KDE portal, or evdev)."""
        if action in ("dictate", "command"):
            if not pressed:
                self.handle("release")
            elif self.recorder.active and self.kind in (PTT, COMMAND):
                self.handle("release")          # pressed again: a release got lost somewhere
            elif self.recorder.active and self.kind == HANDSFREE and action == "dictate":
                self.handle("stop")             # a tap ends hands-free
            else:
                self.press_async(PTT if action == "dictate" else COMMAND)
        elif pressed and action == "hands_free":
            threading.Thread(target=self.handle, args=("toggle",), daemon=True).start()
        elif pressed and action == "meeting":
            threading.Thread(target=self.handle, args=("meeting toggle",), daemon=True).start()
        elif pressed and action == "history":
            subprocess.Popen([sys.executable, "-m", "chttr", "history"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _press_finish(self, kind: str) -> dict:
        try:
            app, title = focused_window()
            selection = ""
            if kind == COMMAND:
                mode = self.cfg.mode_for(app)
                selection = grab_selection(terminal=bool(mode and mode.name == "terminal")).strip()
        except Exception:
            log.exception("reading the focused window failed")
            app, title, selection = "", "", ""
        with self.lock:
            self.starting = False
            reply = self._start(kind, app, title, selection)
            if self.release_requested and self.recorder.active:
                self.release_requested = False
                log.info("key released before recording started; stopping right away")
                return self._stop_and_transcribe()
            return reply

    def _start(self, kind: str, app: str, title: str, selection: str) -> dict:
        self.kind = kind
        want_field = (self.cfg.polish.screen_context != "off" or self.cfg.history.learn_from_edits) \
            and self.atspi_misses < ATSPI_GIVE_UP
        self.field = self.pool.submit(self._read_field) if want_field and kind != COMMAND else None
        self.app, self.title, self.selection = app, title, selection
        self.heard_speech = False
        self.levels = levels.Tracker()
        self.last_speech = time.monotonic()
        self._unmute_if_needed()
        self._overlay_open(kind if kind != PTT else "")
        try:
            self.recorder.start()
        except RuntimeError as exc:
            self._overlay_close()
            self._restore_mute_state()
            self._refresh_state()
            self.feedback.play("error")
            self.feedback.notify("chttr: can't record", str(exc), urgency="critical", timeout_ms=6000)
            return {"ok": False, "state": self.state, "error": str(exc)}
        self._write_state(RECORDING)
        self.dictation_started = time.time()
        self._captions_start()
        self.feedback.play("start")
        if kind == COMMAND:
            what = f"{len(self.selection.split())} selected words" if self.selection else "no selection: new text"
            self.feedback.notify("Command mode", f"Say what to do ({what})", timeout_ms=2500)
        return {"ok": True, "state": self.state, "kind": kind}

    def _stop_and_transcribe(self) -> dict:
        if self.state != RECORDING:
            return {"ok": True, "state": self.state, "message": "not recording"}
        audio, secs = self.recorder.stop()
        peak_db = self.recorder.peak_db
        if self.kind == PTT and secs < TAP:
            self.last_tap = time.monotonic()
        self._log_dictation()
        self.jobs += 1
        seq, self.job_seq = self.job_seq, self.job_seq + 1
        self._write_state(TRANSCRIBING)
        captions = self._captions_detach()
        self._overlay_close()
        self._restore_mute_state()
        self.feedback.play("stop")
        self.feedback.notify("Transcribing…", f"{secs:.1f}s of audio, peak {peak_db:.0f} dB", timeout_ms=0)
        job = (seq, audio, secs, peak_db, self.kind, self.app, self.title, self.selection, captions, self.field)
        self.field = None
        threading.Thread(target=self._process, args=job, daemon=True).start()
        return {"ok": True, "state": self.state, "seconds": round(secs, 1), "peak_db": round(peak_db, 1)}

    def _cancel(self) -> dict:
        if self.state != RECORDING:
            return {"ok": True, "state": self.state, "message": "nothing to cancel"}
        self.recorder.cancel()
        self._log_dictation()
        self._overlay_close()
        self._restore_mute_state()
        self._refresh_state()
        detached = self._captions_detach()
        if isinstance(detached, Streamer):
            detached.abort()
        self.feedback.play("error")
        self.feedback.notify("Dictation cancelled", timeout_ms=1500)
        return {"ok": True, "state": self.state}

    # ---- pipeline (worker thread) -----------------------------------------
    def _wait_turn(self, seq: int) -> None:
        """Block until every earlier dictation has been delivered (or given up)."""
        with self.delivery:
            # an earlier dictation may still be cleaning up (twice the budget on Ollama) or typing
            self.delivery.wait_for(lambda: self.next_delivery >= seq,
                                   timeout=max(60, 2 * (self.cfg.polish.cleanup_timeout_seconds + 10)))

    def _done_turn(self, seq: int) -> None:
        with self.delivery:
            self.next_delivery = max(self.next_delivery, seq + 1)
            self.delivery.notify_all()

    def _process(self, seq: int, audio: np.ndarray, secs: float, peak_db: float, kind: str,
                 app: str, title: str, selection: str, captions: threading.Thread | Streamer | None,
                 field_future: Future | None = None) -> None:
        raw = text = ""
        method = ""
        mode: Mode | None = None
        try:
            t0 = time.monotonic()
            dictionary = Dictionary.load()
            if isinstance(captions, Streamer):
                try:
                    raw = captions.finish(audio)
                except Exception:  # fall back to one pass over everything
                    log.exception("streaming recognition failed; transcribing the whole recording")
                    raw = self.transcriber.transcribe(audio, dictionary.whisper_prompt())
            else:
                if captions:
                    captions.join(timeout=5)   # let an in-flight caption pass finish first
                raw = self.transcriber.transcribe(audio, dictionary.whisper_prompt())
            log.info("%s %.1fs audio (peak %.0f dB) -> %.2fs after release: %r",
                     self.transcriber.engine.name, secs, peak_db, time.monotonic() - t0, raw)
            if not raw:
                if secs >= TAP:                     # a tap (maybe half of a double tap) is not a dictation
                    self._no_speech(secs, peak_db)
                return
            field = self._field(field_future)
            names = CX.names_in(field.text) if field and self.cfg.polish.screen_context != "off" else []
            fixed = dictionary.apply(raw, names)
            if fixed != raw:
                log.info("dictionary: %r", fixed)
            mode = self.cfg.mode_for(app)
            if kind == COMMAND:
                text = self._run_command(fixed, selection, app, title, mode, dictionary)
                if not text:
                    return
            else:
                coded = devmode.transform(fixed, bool(mode and mode.is_code))
                keep = devmode.keep_list(fixed, coded)
                if coded != fixed:
                    log.info("dev mode: %r", coded)
                text = self._polish(coded, app, title, mode, dictionary, keep, names,
                                    field.before if field and self.cfg.polish.screen_context == "full" else "")
                text = dictionary.expand_snippets(text)
            self._wait_turn(seq)
            method = deliver(text, self.cfg.output,
                             mode=(mode.output if mode else ""),
                             trailing_space=False if kind == COMMAND else (mode.trailing_space if mode else None),
                             allow_paste=not (mode and mode.name == "terminal"))
            label = "Replaced" if kind == COMMAND and selection else "Typed"
            if self.cfg.history.learn_from_edits and kind != COMMAND and method in ("type", "paste"):
                timer = threading.Timer(8.0, self._learn_from_edits, args=(text,))
                timer.daemon = True
                timer.start()
            if method == "clipboard":
                self.feedback.notify("Copied to clipboard", text[:200], timeout_ms=4000)
            else:
                self.feedback.notify(label, text[:200], timeout_ms=2500)
        except Exception as exc:
            log.exception("dictation failed")
            self.feedback.play("error")
            self.feedback.notify("chttr error", f"{type(exc).__name__}: {exc}", urgency="critical", timeout_ms=8000)
        finally:
            self._done_turn(seq)
            with self.lock:
                self.jobs -= 1
                self._refresh_state()
            if raw and self.cfg.history.enabled:
                self._append_history(raw, text, app, secs, method, kind, mode.name if mode else "", audio)

    def _read_field(self) -> CX.Field | None:
        """The focused text field. If the accessibility bus never answers (it isn't
        running on every desktop), stop asking after a few tries this session."""
        field = CX.read_field(SYSTEM_PYTHON)
        if field is None:
            self.atspi_misses += 1
            if self.atspi_misses == ATSPI_GIVE_UP:
                log.info("no text field readable through AT-SPI %d times; screen context off until restart",
                         ATSPI_GIVE_UP)
        else:
            self.atspi_misses = 0
        return field

    @staticmethod
    def _field(future: Future | None) -> CX.Field | None:
        if future is None:
            return None
        try:
            return future.result(timeout=0.2)       # usually done long before the key is released
        except Exception:  # noqa: BLE001 - screen context is optional
            return None

    def _learn_from_edits(self, typed: str) -> None:
        """A few seconds after typing: did you fix a name we got wrong? Offer to learn it."""
        field = CX.read_field(SYSTEM_PYTHON, timeout=0.8, before=2000, after=2000)
        if field is None:
            return
        for c in CX.corrections(typed, field.text):
            if (c.heard, c.right) in self.offered:
                continue
            self.offered.add((c.heard, c.right))
            log.info("you changed %r to %r", c.heard, c.right)
            try:
                out = subprocess.run(["notify-send", "-a", "chttr", "-t", "15000", "-A", "learn=Learn it",
                                      "--wait", f"Learn \u201c{c.right}\u201d?",
                                      f"You changed \u201c{c.heard}\u201d to \u201c{c.right}\u201d. "
                                      "chttr can spell it that way from now on."],
                                     capture_output=True, text=True, timeout=30).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                return
            if out == "learn":
                d = Dictionary.load()
                d.learn(c.right, [c.heard])
                d.save()
                self.feedback.notify(f"Learned \u201c{c.right}\u201d", timeout_ms=2500)

    @staticmethod
    def _save_audio(audio: np.ndarray, when: datetime) -> str:
        C.HISTORY_AUDIO_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = C.HISTORY_AUDIO_DIR / f"{when:%Y%m%d-%H%M%S-%f}.wav"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh, wave.open(fh, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        return path.name

    def _prune_audio(self) -> None:
        days = self.cfg.history.keep_audio_days
        if not C.HISTORY_AUDIO_DIR.exists():
            return
        cutoff = time.time() - days * 86400
        for f in C.HISTORY_AUDIO_DIR.glob("*.wav"):
            if days <= 0 or f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)

    def _no_speech(self, secs: float, peak_db: float) -> None:
        if peak_db < QUIET_PEAK_DB:
            omarchy = current_desktop().compositor == "hyprland" and shutil.which("omarchy-menu")
            hint = " (Super+Ctrl+A opens Audio)." if omarchy else "."
            self.feedback.notify("No speech detected: the mic is silent",
                                 f"Peak {peak_db:.0f} dB. Check the input device and mute" + hint,
                                 urgency="normal", timeout_ms=6000)
        elif peak_db < -25:
            # The tracker holds the recording that just ended, unless you have started
            # another since (_start replaces it); then its numbers are not this one's.
            a, tracker = self.cfg.audio, self.levels
            levels_text = ""
            if tracker.count and self.state != RECORDING and self.jobs <= 1:
                thr = tracker.threshold(a.threshold_db) if a.adaptive_threshold else a.threshold_db
                levels_text = f" over a {tracker.floor:.0f} dB floor"
                if thr < a.threshold_db:
                    levels_text += f"; chttr listened down to {thr:.0f} dB"
            self.feedback.notify("No speech detected: the mic is quiet",
                                 f"Peak {peak_db:.0f} dB{levels_text}. Raise the input level for best results.",
                                 urgency="normal", timeout_ms=6000)
        else:
            self.feedback.notify("No speech detected", f"{secs:.1f}s, peak {peak_db:.0f} dB", timeout_ms=2500)

    def _polish(self, text: str, app: str, title: str, mode: Mode | None, dictionary: Dictionary,
                keep: list[str] | None = None, names: list[str] | None = None, screen_text: str = "") -> str:
        level = self.cfg.polish.level
        code, chat = bool(mode and mode.is_code), bool(mode and mode.name == "chat")
        # Line breaks are folded only where typing one would send the message.
        fold = chat and getattr(mode, "newline", "enter") != "shift+enter"
        if (mode and not mode.polish) or level == "none":
            return text
        # Fillers, stutters, spacing and the terminal's trailing period go first, so
        # "um git status" takes the quick path.
        text = tidy.tidy(text, "light", code=code, chat=chat, fold_newlines=fold)
        if P.quick_path(text, self.cfg.polish.quick_max_words):
            log.info("short and clean; typed without a model round trip")
            return text
        which = P.backend(self.cfg.polish)
        if which == "none":
            out = tidy.tidy(text, level, code=code, chat=chat, fold_newlines=fold)
            self._hint_cleanup_off(text)
            return out
        try:
            t1 = time.monotonic()
            vocabulary = dictionary.vocabulary + (names or [] if self.cfg.polish.screen_context in ("names", "full") else [])
            out = P.polish(text, self.cfg.polish, app, title, vocabulary, style=mode.style if mode else "",
                           keep=list(dictionary.snippets) + (keep or []), screen_text=screen_text)
            out = tidy.for_app(out, code=code, chat=chat, fold_newlines=fold)   # a stray trailing period in a terminal
            self.polish_warned = False                         # it works: the next failure is news
            log.info("polish (%s) %.2fs: %d words", mode.name if mode else "default", time.monotonic() - t1,
                     len(out.split()))
            log.debug("polished: %r", out)
            return out
        except Exception as exc:  # noqa: BLE001 - any failure falls back to local cleanup
            log.warning("polish failed (%s); local cleanup instead", exc)
            if not self.polish_warned:
                self.polish_warned = True
                if P.breaker.open:
                    self.feedback.notify("Cleanup offline", "Typing locally cleaned text until "
                                         f"{'Claude' if which == 'claude' else 'Ollama'} answers again.",
                                         urgency="normal", timeout_ms=8000)
                else:
                    why = str(exc) if isinstance(exc, P.PolishUnavailable) else f"{type(exc).__name__}: {exc}"
                    self.feedback.notify("chttr: cleanup unavailable", why[:160],
                                         urgency="normal", timeout_ms=8000)
            return tidy.tidy(text, level, code=code, chat=chat, fold_newlines=fold)

    def _hint_cleanup_off(self, text: str) -> None:
        """Once per install: say that cleanup runs locally and how to get Claude's
        (unless you chose backend = "none" yourself)."""
        flag = C.DATA_DIR / "hinted-cleanup"
        if self.cfg.polish.backend == "none" or len(text.split()) <= self.cfg.polish.quick_max_words \
                or flag.exists():
            return
        self.feedback.notify("Cleaning up locally", "Fillers and spoken punctuation are handled on this machine. "
                             "For Claude cleanup, run: chttr connect claude", timeout_ms=10000)
        try:
            C.ensure_data_dir()
            flag.touch()
        except OSError as exc:
            log.debug("could not write %s: %s", flag, exc)

    def _run_command(self, instruction: str, selection: str, app: str, title: str,
                     mode: Mode | None, dictionary: Dictionary) -> str:
        if P.backend(self.cfg.polish) == "none":
            self.feedback.play("error")
            self.feedback.notify("Command mode needs a model",
                                 "Add an Anthropic key to ~/.config/chttr/anthropic-api-key, or install and start Ollama.",
                                 urgency="normal", timeout_ms=8000)
            return ""
        try:
            t1 = time.monotonic()
            out = P.command(instruction, selection, self.cfg.polish, app, title, dictionary.vocabulary,
                            style=mode.style if mode else "")
            log.info("command %.2fs: %r -> %r", time.monotonic() - t1, instruction, out[:200])
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("command failed: %s", exc)
            self.feedback.play("error")
            self.feedback.notify("Command failed", f"{type(exc).__name__}: {str(exc)[:160]}", urgency="normal", timeout_ms=8000)
            return ""

    def _append_history(self, raw: str, text: str, app: str, secs: float, method: str, kind: str, mode_name: str,
                        audio: np.ndarray | None = None) -> None:
        try:
            C.ensure_data_dir()
            now = datetime.now(UTC)
            record = {"ts": now.isoformat(timespec="seconds"), "seconds": round(secs, 1), "app": app, "kind": kind,
                      "mode": mode_name, "raw": raw, "text": text, "output": method}
            if self.cfg.history.keep_audio_days > 0 and audio is not None and len(audio):
                record["audio"] = self._save_audio(audio, now)
            with C.open_private(C.HISTORY_FILE) as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            log.warning("history write failed: %s", exc)

    # ---- server ------------------------------------------------------------
    def _watchdog(self) -> None:
        while not self._stop.wait(0.2):
            with self.lock:
                m = self.meeting
                if m and m.elapsed >= self.cfg.meeting.max_hours * 3600:
                    log.info("meeting max_hours reached; stopping")
                    self._meeting_stop()
                elif m and m.code and self.call and time.time() - self.call["seen"] > CALL_TIMEOUT:
                    log.info("no word from the Meet tab for %ds; treating the call as over", CALL_TIMEOUT)
                    self.call = None
                    self._meeting_stop()
                if self.state != RECORDING:
                    continue
                a = self.cfg.audio
                if self.recorder.elapsed >= a.max_seconds:
                    log.info("max_seconds reached; stopping")
                    self._stop_and_transcribe()
                elif self.kind == HANDSFREE:
                    quiet = time.monotonic() - self.last_speech
                    if self.heard_speech and quiet >= a.auto_stop_silence:
                        log.info("hands-free: %.1fs of silence; stopping", quiet)
                        self._stop_and_transcribe()
                    elif not self.heard_speech and self.recorder.elapsed >= a.no_speech_timeout:
                        log.info("hands-free: nothing heard in %.0fs; cancelling", a.no_speech_timeout)
                        self._cancel()

    def _start_hotkeys(self) -> None:
        from . import hotkeys as H

        try:
            triggers = H.bindings(self.cfg.hotkeys)
        except ValueError as exc:
            log.warning("hotkeys: %s", exc)
            self.feedback.notify("chttr: bad hotkey in config.toml", str(exc), urgency="normal", timeout_ms=8000)
            return
        backend = H.resolve(self.cfg.hotkeys.backend, current_desktop().compositor)
        log.info("hotkeys: %s", {"compositor": "bound by the compositor", "none": "none (see chttr doctor)"}
                 .get(backend, backend))
        self.hotkeys = H.Hotkeys(backend, triggers, self.on_hotkey).start()

    def _serve_one(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(2)
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                reply = self.handle(buf.decode(errors="replace"))
            except Exception as exc:
                log.exception("request failed")
                reply = {"ok": False, "state": self.state, "error": str(exc)}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass

    def serve(self) -> None:
        sock_path = C.SOCKET_PATH
        if sock_path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(1)
                probe.connect(str(sock_path))
                probe.sendall(b"status\n")
                raise SystemExit("chttr daemon is already running")
            except (ConnectionRefusedError, FileNotFoundError, TimeoutError):
                sock_path.unlink(missing_ok=True)
            finally:
                probe.close()

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        server.listen(8)
        server.settimeout(0.5)

        def on_signal(signum, _frame):
            log.info("signal %s; shutting down", signum)
            self._stop.set()

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self._watch_calls, name="calls", daemon=True).start()
        threading.Thread(target=P.warm, args=(self.cfg.polish,), daemon=True).start()
        self._start_hotkeys()
        log.info("listening on %s (cleanup backend: %s)", sock_path, P.backend(self.cfg.polish))
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                # One thread per request: a slow one (a meeting stopping, the calendar)
                # never delays a key release.
                threading.Thread(target=self._serve_one, args=(conn,), daemon=True).start()
        finally:
            with self.lock:
                if self.meeting:
                    self._meeting_stop()
                if self.recorder.active:
                    self.recorder.cancel()
                self._overlay_close()
                self._restore_mute_state()
                self._write_state(IDLE)
            server.close()
            sock_path.unlink(missing_ok=True)
            log.info("stopped")


def run(cfg: Config) -> None:
    Daemon(cfg).serve()
