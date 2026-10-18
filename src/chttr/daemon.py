"""The chttr daemon: holds the speech model, owns the state machine, listens on a Unix socket."""

from __future__ import annotations

import dataclasses
import gc
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
from . import overlay_proto as OP
from . import polish as P
from .commands import local_command
from .config import Config, Mode
from .dictionary import Dictionary
from .feedback import Feedback
from .meeting import Session, launch_processing
from .output import current_desktop, delete_selection, deliver, focused_window, grab_selection, press_enter, smart_join
from .recorder import Recorder
from .streaming import Streamer
from .transcriber import Transcriber, engine_key

log = logging.getLogger("chttr.daemon")

IDLE, RECORDING, TRANSCRIBING = "idle", "recording", "transcribing"
LOADING = "loading"                     # the daemon answers while the speech model loads
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
ATSPI_GIVE_UP = 5                       # unreadable fields in a row before screen context pauses
ATSPI_RETRY = 600                       # seconds before it tries the accessibility bus again
TAP = 0.35                              # recordings shorter than this are taps, not dictations
OVERLAY_RESPAWN = 30.0                  # seconds between attempts to restart an overlay that died
CALL_TIMEOUT = 90                       # no word from the Meet tab for this long: the call is over
MODEL_WAIT = 120                        # a dictation made while the model loads waits this long for it


class Daemon:
    def __init__(self, cfg: Config, load_model: bool = True) -> None:
        self.cfg = cfg
        self.runtime = C.ensure_runtime_dir()
        self.state = IDLE
        self.lock = threading.Lock()
        self.feedback = Feedback(cfg.feedback, self.runtime)
        self.recorder = Recorder(cfg.audio.device, on_level=self._on_level)
        # `chttr daemon` loads the model in the background (see serve), so the key
        # works from the first second; tests and tools load it right here.
        self.transcriber: Transcriber | None = Transcriber(cfg) if load_model else None
        self.model_ready = threading.Event()
        self.model_error = ""
        if self.transcriber is not None:
            self.model_ready.set()
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
        self.atspi_retry_at = 0.0
        self.field: Future | None = None             # the focused text field, read at key press
        self.field_stale = False                     # ...while an earlier dictation was still to be typed into it
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chttr-context")
        self.offered: set[tuple[str, str]] = set()   # corrections already offered this session
        self.briefed: set = set()                    # (title, start) of events already briefed
        self._brief_events: tuple | None = None      # (read at, (urls, minutes), events) for the brief
        self._stop = threading.Event()
        C.MEETING_STATE_FILE.unlink(missing_ok=True)
        self._write_state(IDLE)
        self._tighten_permissions()
        self._prune_audio()
        self._overlay_warm()
        threading.Thread(target=self._brief_loop, name="brief", daemon=True).start()

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
        """Recording beats transcribing beats loading the model beats idle."""
        if self.recorder.active:
            state = RECORDING
        elif self.jobs:
            state = TRANSCRIBING
        else:
            state = LOADING if self.transcriber is None and not self.model_ready.is_set() else IDLE
        if state != self.state:
            self._write_state(state)

    def _load_model(self) -> None:
        """Load the speech model (a few seconds) while the daemon already answers."""
        with self.lock:
            if self.state == IDLE:
                self._write_state(LOADING)
        t0 = time.monotonic()
        try:
            transcriber = Transcriber(self.cfg)
            with self.lock:
                self.transcriber, self.model_error = transcriber, ""
            log.info("speech model loaded in %.1fs", time.monotonic() - t0)
        except Exception as exc:  # noqa: BLE001 - onnxruntime raises plain Exceptions for a corrupt model
            log.error("the speech model could not be loaded: %s", exc)
            self.model_error = str(exc)
            self.feedback.notify("chttr: the speech model is missing", f"{exc}\nRun: chttr setup",
                                 urgency="critical", timeout_ms=0)
        finally:
            self.model_ready.set()
            with self.lock:
                self._refresh_state()

    def _model_status(self) -> str:
        if self.transcriber is not None:
            return "ready"
        return "missing" if self.model_ready.is_set() else "loading"

    def _write_state(self, state: str) -> None:
        # The plain file is the state alone; state.json, which the bar reads, adds who
        # wrote it (a pid that is gone means chttr is off), the meeting, the cleanup
        # backend and the keys. The backend is probed off-thread at most once a minute
        # (the Ollama check can take half a second) and the file rewritten when it changes.
        # A minute's refresh keeps it true while chttr sits idle: Ollama coming or going,
        # a reload, a meeting cancelled.
        lock = self.__dict__.setdefault("_state_lock", threading.Lock())
        cache = getattr(self, "_cleanup_cache", None)
        if cache is None:
            cache = self._cleanup_cache = {"backend": "", "polish": None, "at": 0.0, "probing": False,
                                           "written": None, "refreshing": False}

        def snapshot() -> dict:
            m = getattr(self, "meeting", None)
            since = getattr(self, "_state_since", None)
            if not since or since[0] != self.state:
                since = self._state_since = (self.state, time.time())
            return {"state": self.state, "kind": getattr(self, "kind", "") if self.state == RECORDING else "",
                    "since": since[1], "pid": os.getpid(),
                    "meeting": {"title": m.title, "code": m.code, "started": m.recorder.started} if m else None,
                    "cleanup": cache["backend"],
                    "hotkeys": {k: v for k, v in vars(self.cfg.hotkeys).items() if k != "backend"},
                    "model": "loading" if self.state == "loading" else "ready"}

        def dump(only_if_changed: bool = False) -> None:
            info = snapshot()
            if only_if_changed and info == cache["written"]:
                return
            try:
                tmp = C.STATE_JSON.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(info), encoding="utf-8")
                os.replace(tmp, C.STATE_JSON)
            except OSError as exc:
                log.debug("could not write %s: %s", C.STATE_JSON, exc)
                return
            cache["written"] = info

        def probe(polish) -> None:
            try:
                found = P.backend(polish)
            except Exception as exc:  # noqa: BLE001 - the bar's extra never takes the daemon down
                log.debug("cleanup backend probe failed: %s", exc)
                found = cache["backend"]
            with lock:
                cache["probing"] = False
                if self.cfg.polish is not polish or polish.backend != "auto":   # reloaded meanwhile
                    return
                cache.update(backend=found, polish=polish)
                dump(only_if_changed=True)

        def check_backend() -> None:          # with the lock held
            polish = self.cfg.polish
            if polish.backend != "auto":             # nothing to probe (and probe again if it becomes auto)
                cache.update(backend=polish.backend, polish=None)
            elif (cache["polish"] is not polish or time.monotonic() - cache["at"] > 60) and not cache["probing"]:
                cache.update(probing=True, at=time.monotonic())
                try:
                    threading.Thread(target=probe, args=(polish,), name="cleanup-probe", daemon=True).start()
                except RuntimeError:                 # interpreter shutting down
                    cache["probing"] = False

        def refresh(stop: threading.Event) -> None:
            while not stop.wait(61):
                with lock:
                    check_backend()
                    dump(only_if_changed=True)

        with lock:
            self.state = state
            tmp = C.STATE_FILE.with_suffix(".tmp")
            tmp.write_text(state, encoding="utf-8")
            os.replace(tmp, C.STATE_FILE)
            check_backend()
            dump()
            stop = getattr(self, "_stop", None)
            if stop is not None and not cache["refreshing"]:
                cache["refreshing"] = True
                try:
                    threading.Thread(target=refresh, args=(stop,), name="state-refresh", daemon=True).start()
                except RuntimeError:
                    cache["refreshing"] = False

    # ---- overlay -----------------------------------------------------------
    # The overlay stays resident: started hidden with the daemon (or at the first key
    # press), shown on press, moved through transcribing -> done, hidden in between.
    overlay_spawned_at = 0.0            # monotonic time of the last spawn
    overlay_losses = 0                  # overlay processes that died in a row
    overlay_unsupported = ""            # GNOME, which has no layer-shell
    overlay_skipped = ""                # the desktop last found without Wayland (logged once each)
    overlay_warning = ""                # a "Waveform overlay off" notice waiting for the dictation to end
    overlay_captions = False            # the running overlay was started with --captions
    overlay_palette: dict | None = None  # the theme it was last given
    overlay_mode = ""                   # the last state sent; "hidden" after a hide

    def _overlay_warm(self) -> None:
        """Start the overlay hidden, so the first key press doesn't wait for GTK to load."""
        try:
            if self.cfg.feedback.overlay and self.overlay is None:
                self._overlay_spawn()
            self._overlay_warn()
        except Exception:                   # the overlay is cosmetic; never stop the daemon over it
            log.debug("could not start the overlay", exc_info=True)

    def _overlay_spawn(self) -> bool:
        if self.overlay_unsupported:
            return False
        if self.overlay_spawned_at and time.monotonic() - self.overlay_spawned_at < OVERLAY_RESPAWN:
            return False                    # it died moments ago; notifications until it can be retried
        desk = current_desktop()
        if desk.compositor == "gnome":
            # GNOME (Mutter) has no layer-shell: a regular window would steal focus.
            self.overlay_unsupported = desk.describe()
            self._overlay_fail(f"no layer-shell on {desk.describe()}; notifications only", tell=False)
            return False
        if not desk.wayland:
            # X11 has none either. Asked again at each press: a daemon started before
            # Hyprland's sockets appear sees no Wayland yet.
            if desk.describe() != self.overlay_skipped:
                self.overlay_skipped = desk.describe()
                log.info("no waveform overlay on %s; notifications only", desk.describe())
            return False
        env = dict(os.environ)
        for lib in LAYER_SHELL_LIBS:
            if lib.exists():
                env["LD_PRELOAD"] = str(lib)
                break
        palette, captions = OP.omarchy_palette(), self.cfg.feedback.live_captions
        cmd = [SYSTEM_PYTHON, "-I", str(OVERLAY_SCRIPT)]
        if palette:
            cmd += ["--palette", json.dumps(palette)]
        if captions:
            cmd.append("--captions")
        self.overlay_spawned_at = time.monotonic()
        try:
            # stderr to a file: a resident process could fill a pipe nobody reads.
            with open(self.runtime / "overlay.log", "wb") as err:
                self.overlay = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                                stderr=err, env=env)
        except OSError as exc:
            self._overlay_fail(str(exc))
            return False
        self.overlay_palette, self.overlay_captions, self.overlay_mode = palette, captions, "hidden"
        return True

    def _overlay_open(self, label: str) -> None:
        """Show the pill for a new recording ("", "command" or "hands-free" above the dot)."""
        if not self.cfg.feedback.overlay:
            self._overlay_quit()            # switched off in config.toml since it started
            return
        proc = self.overlay
        if proc is not None and proc.poll() is not None:
            self._overlay_lost(proc)
        elif proc is not None and self.overlay_captions != self.cfg.feedback.live_captions:
            self._overlay_quit()            # live captions need a taller window
            self.overlay_spawned_at = 0.0
        elif proc is not None and time.monotonic() - self.overlay_spawned_at > 10:
            self.overlay_losses = 0         # this one has stayed up
        if self.overlay is None and not self._overlay_spawn():
            return
        palette = OP.omarchy_palette()      # a few hundred bytes; follows theme switches
        if palette != self.overlay_palette:
            self.overlay_palette = palette
            self._overlay_send(OP.format("palette", json.dumps(palette or {})))
        self._overlay_send(OP.format("show", label))
        self._overlay_state({COMMAND: "command", HANDSFREE: "handsfree"}.get(label, "listening"))

    def _overlay_state(self, state: str) -> None:
        """Move the pill on: listening, transcribing, then done / nospeech / error, or
        "hide". A dictation that finishes while a newer one is recording, or while
        another is still being transcribed, leaves the pill to that one."""
        if state in ("done", "nospeech", "error", "hide") and self._overlay_busy():
            return
        self._overlay_send(OP.format("hide") if state == "hide" else OP.format("state", state))
        self.overlay_mode = "hidden" if state == "hide" else state

    def _overlay_busy(self) -> bool:
        """Is the pill showing a newer recording, or another dictation still in progress?"""
        return self.recorder.active or self.starting or self.jobs > 1

    def _overlay_text(self, text: str) -> None:
        """The words the "done" pill shows next to its check."""
        if not self._overlay_busy():
            self._overlay_send(OP.format("text", text[:60]))

    def _overlay_settle(self) -> None:
        """The end of a dictation. One that ended without saying how (command mode with
        no model, say) doesn't leave the pill shimmering, and a pending "Waveform overlay
        off" notice is sent now that the dictation's own notifications are done."""
        if self.overlay_mode == "transcribing":
            self._overlay_state("hide")
        self._overlay_warn()

    def _overlay_fail(self, detail: str, tell: bool = True) -> None:
        """The overlay can't run: say so once, in the log and (on desktops that could
        show it) in a notification. That waits for the dictation to end: its own
        notifications share one slot and would replace it at once."""
        if not self.overlay_warned:
            self.overlay_warned = True
            log.warning("level overlay unavailable: %s", detail)
            if tell:
                self.overlay_warning = f"{detail[:160]}. See: chttr doctor"

    def _overlay_warn(self) -> None:
        """Send the pending "Waveform overlay off" notice, unless a dictation is still going."""
        if not self.overlay_warning or self._overlay_busy():
            return
        with self.overlay_lock:
            body, self.overlay_warning = self.overlay_warning, ""
        if body:
            self.feedback.notify("Waveform overlay off", body, urgency="normal", timeout_ms=8000)

    def _overlay_lost(self, proc: subprocess.Popen) -> None:
        """The overlay exited or stopped reading. It's started again at the next press
        (at most every OVERLAY_RESPAWN seconds); dying twice in a row is reported."""
        with self.overlay_lock:             # the audio thread and a key press can both get here
            if self.overlay is not proc:
                return                      # already dealt with
            self.overlay = None
        # A writer whose last flush failed would try again when garbage-collected and
        # print a BrokenPipeError to the journal; closing it drops the data.
        try:
            if proc.stdin:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.kill()
            proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        detail = self._overlay_error()
        self.overlay_losses += 1
        if self.overlay_losses >= 2:
            self._overlay_fail(detail)
        else:
            log.info("overlay exited (%s); starting it again at the next key press", detail)

    def _overlay_error(self) -> str:
        """The last line the overlay wrote to stderr."""
        try:
            text = (self.runtime / "overlay.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        rows = [r.strip() for r in text.splitlines() if r.strip()]
        return rows[-1] if rows else "overlay exited"

    def _overlay_send(self, line: str) -> None:
        proc = self.overlay
        if proc is None or proc.stdin is None:
            return
        try:
            with self.overlay_lock:
                proc.stdin.write((line.replace("\n", " ") + "\n").encode())
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self._overlay_lost(proc)

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
        """Hide the pill; the process stays for the next press."""
        if self.overlay is not None:
            self._overlay_send(OP.format("hide"))
            self.overlay_mode = "hidden"

    def _overlay_quit(self) -> None:
        """Stop the overlay process (daemon shutdown, or the overlay switched off)."""
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
        if self.transcriber is None:
            return                              # still loading: the whole recording is transcribed at the end
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
                return self._meeting_command(arg.strip())
            if cmd == "meet":
                return self._meet_event(json.loads(arg))
            if cmd == "status":
                return {"ok": True, "state": self.state, "kind": self.kind if self.state == RECORDING else "",
                        "seconds": round(self.recorder.elapsed, 1), "model": self._model_status()}
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
        verb, _, extra = arg.partition(" ")
        arg = verb.lower()
        if arg in ("", "status"):
            return self._meeting_status()
        if arg == "start":
            # `meeting start {"title": ..., "source": "app", "call": CODE}` from a "Record?" click
            try:
                opts = json.loads(extra) if extra.strip() else {}
            except ValueError:
                opts = None
            if not isinstance(opts, dict):
                return {"ok": False, "state": self.state, "error": f"bad meeting start options {extra!r}"}
            call = str(opts.get("call") or "")
            if call and not self.meeting and not (self.call and self.call["code"] == call):
                self.feedback.notify("That call has ended", "Nothing was recorded.", timeout_ms=3000)
                return {"ok": False, "state": self.state, "error": "that Meet call has ended"}
            source = "app" if opts.get("source") == "app" else "manual"
            if source == "app" and not self.meeting and not self._app_call_live():
                # an old toast clicked from history: nothing would ever stop this recording
                self.feedback.notify("That call has ended", "Nothing was recorded.", timeout_ms=3000)
                return {"ok": False, "state": self.state, "error": "that call has ended"}
            return self._meeting_start(source, str(opts.get("title") or ""))
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
        self._write_state(self.state)
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
        self._write_state(self.state)
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
            if self.meeting and self.meeting.source == "manual" and not self.meeting.code:
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

    def _brief_loop(self) -> None:
        """A notification [meeting] brief_minutes before each calendar meeting. Runs even
        without a calendar: one added later (chttr meeting calendar URL), or brief_minutes
        turned back on with chttr reload, works without a restart. Without one a tick is a
        file read."""
        failing = False
        while True:
            try:
                self._brief_tick()
                failing = False
            except Exception as exc:  # noqa: BLE001 - an offline minute must not end the briefs
                if not failing:
                    log.warning("pre-meeting brief: %s", exc)
                failing = True
            if self._stop.wait(60):
                return

    def _brief_tick(self) -> int:
        """Brief every event that starts within brief_minutes (and hasn't been briefed).
        Returns how many notifications went out."""
        from . import brief as B
        from . import gcal

        minutes = self.cfg.meeting.brief_minutes
        urls = gcal.read_urls(self.cfg.meeting.calendar_file)
        if minutes <= 0 or not urls:
            return 0
        now = datetime.now(UTC).astimezone()
        due = [e for e in self._brief_calendar(urls, minutes, now)
               if -60 < (e.start - now).total_seconds() <= minutes * 60 + 30
               and (e.title, e.start.isoformat()) not in self.briefed and B.worth_a_brief(e)]
        if not due:
            return 0
        meetings = B.past_meetings()
        sent = 0
        for event in due:
            self.briefed.add((event.title, event.start.isoformat()))
            brief = B.brief_for(event, meetings, B.me_names(self.cfg.meeting.me, event), now=now)
            if not brief.attendees and not brief.last:
                continue                # a focus block or a reminder: nothing to brief
            title, body = B.render(brief)
            if self._in_a_call():
                # you may be sharing your screen in this call: no notes on it
                body = "Run chttr brief to see what you agreed last time."
            self.feedback.notify(title, body, urgency="normal", timeout_ms=20000)
            log.info("pre-meeting brief for %r in %d min (%s, %d open items)", event.title, brief.starts_in_min,
                     "met before" if brief.last else "first meeting", len(brief.open_items))
            sent += 1
        return sent

    def _brief_calendar(self, urls: list[str], minutes: int, now: datetime) -> list:
        """The events a brief may be due for in the next few minutes. Feeds can be several MB:
        they are downloaded at most every 10 min (gcal's file cache) and parsed at most every
        5, not every minute; a new calendar or brief_minutes is read at once."""
        from datetime import timedelta

        from . import gcal

        key = (tuple(urls), minutes)
        if self._brief_events and self._brief_events[1] == key and time.monotonic() - self._brief_events[0] < 300:
            return self._brief_events[2]
        # from just before now to past the next refresh, plus brief_minutes
        half = timedelta(seconds=(minutes * 60 + 300 + 240) / 2)
        around = now - timedelta(minutes=2) + half
        events = [e for url in urls for e in gcal._events(gcal._fetch(url), around, half, self.cfg.meeting.email)[0]]
        self._brief_events = (time.monotonic(), key, events)
        return events

    def _in_a_call(self) -> bool:
        """In a Meet call, or a Zoom/Teams/Slack one (PipeWire), right now."""
        if self.meeting or self.call:
            return True
        try:
            from . import calls as K

            return bool(K.current_call(K.snapshot(), meet_call_active=False))
        except Exception:
            log.debug("call detection failed", exc_info=True)
            return False

    # ---- calls in other apps (PipeWire) ------------------------------------
    def _watch_calls(self) -> None:
        from . import calls as K

        watcher = K.CallWatcher()
        self._forever("calls", lambda: self._calls_tick(watcher), 3.0)

    def _calls_tick(self, watcher) -> None:
        from . import calls as K

        if not self.cfg.meeting.detect_calls:
            return
        try:
            app = K.current_call(K.snapshot(), meet_call_active=self.call is not None)
        except Exception:
            log.debug("call detection failed", exc_info=True)
            return
        decision = watcher.update(app, time.monotonic())
        if decision is None:
            return
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
            event = gcal.find_event(gcal.read_urls(self.cfg.meeting.calendar_file), "",
                                     me_email=self.cfg.meeting.email)
            return event.title if event else ""
        except Exception:  # noqa: BLE001
            return ""

    def _app_call_live(self) -> bool:
        """Does a call app still hold the mic? (A failed check counts as no.)"""
        from . import calls as K

        try:
            return K.current_call(K.snapshot(), meet_call_active=self.call is not None) is not None
        except Exception:
            log.debug("call check failed", exc_info=True)
            return False

    @staticmethod
    def _chttr_argv() -> list[str]:
        """How a notification click calls chttr back (the service's PATH may lack ~/.local/bin)."""
        exe = shutil.which("chttr")
        return [exe] if exe else [sys.executable, "-m", "chttr"]

    def _ask_to_record_app(self, app: str) -> None:
        from . import gcal

        title, body = self._calendar_title(), "chttr will save a transcript and notes."
        try:
            event = gcal.find_event(gcal.read_urls(self.cfg.meeting.calendar_file), "",
                                     me_email=self.cfg.meeting.email)
            people = [a.name or a.email for a in event.attendees if not a.is_me] if event else []
            if people:
                body = ", ".join(people[:5]) + (f" +{len(people) - 5}" if len(people) > 5 else "")
        except Exception:  # noqa: BLE001
            pass
        label = title or f"this {app} call"
        # The click runs `chttr meeting start`, which comes back here through the socket.
        self.feedback.offer(f"Record \u201c{label}\u201d?", body,
                            ("Record", [*self._chttr_argv(), "meeting", "start", "--title", title or f"{app} call",
                                        "--source", "app"]),
                            [("Not now", [])], glyph="󰑊")

    def _ask_to_record(self, code: str, title: str) -> None:
        from . import gcal

        label, body = title or "this Meet call", "chttr will save a transcript with speaker names."
        try:
            event = gcal.find_event(gcal.read_urls(self.cfg.meeting.calendar_file), code,
                                     me_email=self.cfg.meeting.email)
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
        # --call: a click after you've left the call records nothing (the toast outlives it).
        self.feedback.offer(f"Record \u201c{label}\u201d?", body,
                            ("Record", [*self._chttr_argv(), "meeting", "start", "--call", code]),
                            [("Not now", [])], glyph="󰑊")

    def _reload(self) -> dict:
        """Re-read config.toml and apply it now: audio levels, sounds, hotkeys and
        (when idle) a different speech engine. A broken file changes nothing."""
        try:
            cfg = C.load_config(C.CONFIG_FILE)
        except (ValueError, OSError) as exc:
            return {"ok": False, "state": self.state, "error": f"config.toml: {exc}"}
        old = self.cfg
        before, after = dataclasses.asdict(old), dataclasses.asdict(cfg)
        changed = [f"{section}.{key}" for section, values in after.items() if isinstance(values, dict)
                   for key in values if before.get(section, {}).get(key) != values[key]]
        if before.get("modes") != after.get("modes"):
            changed.append("modes")
        message = "config reloaded: " + ", ".join(changed) if changed else "config reloaded; nothing changed"
        if self.transcriber is None:
            if self.model_ready.is_set():
                # the model failed to load: maybe it has been downloaded (or the engine fixed) since
                self.model_ready.clear()
                self.model_error = ""           # loading again, not missing; _load_model sets it if this fails
                self.cfg = cfg
                threading.Thread(target=self._load_model, name="model", daemon=True).start()
                message += "; loading the speech model"
            else:
                message += "; the speech model is still loading"
        elif engine_key(cfg) != self.transcriber.key:
            if self.state != IDLE:
                return {"ok": False, "state": self.state, "error": "busy; reload again when idle to switch engines"}
            self.transcriber = None             # free the old model before loading the new one
            gc.collect()
            try:
                self.transcriber = Transcriber(cfg)
                self.model_error = ""
                message += f"; {cfg.engine.name} loaded"
            except Exception as exc:  # noqa: BLE001 - see _load_model
                log.error("could not load %s: %s", cfg.engine.name, exc)
                try:
                    self.transcriber = Transcriber(old)
                except Exception as again:  # noqa: BLE001
                    self.model_error = str(again)
                    self._refresh_state()
                return {"ok": False, "state": self.state,
                        "error": f"could not load {cfg.engine.name}: {exc}"
                                 + ("; kept the previous engine and config" if self.transcriber else "")}
        if self.transcriber is not None:
            self.transcriber.threshold_db = cfg.audio.threshold_db
            self.transcriber.boost = cfg.audio.whisper_mode
            if hasattr(self.transcriber, "adaptive"):
                self.transcriber.adaptive = getattr(cfg.audio, "adaptive_threshold", True)
        self.cfg = cfg
        if not cfg.feedback.overlay:
            self._overlay_quit()                # turned off: don't leave the pill running until the next press
        self.recorder.device = cfg.audio.device
        self.feedback = Feedback(cfg.feedback, self.runtime)    # writes the tones if sounds just turned on
        if dataclasses.asdict(old.hotkeys) != dataclasses.asdict(cfg.hotkeys) and self.hotkeys is not None:
            self.hotkeys.stop.set()
            self._start_hotkeys()
        self._write_state(self.state)           # the bar's tooltip names the new keys now, not in a minute
        log.info("%s", message)
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
                mode = self.cfg.mode_for(app, title)
                selection = grab_selection(terminal=bool(mode and mode.in_terminal)).strip()
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
            and (self.atspi_misses < ATSPI_GIVE_UP or time.monotonic() >= self.atspi_retry_at)
        self.field = self.pool.submit(self._read_field) if want_field and kind != COMMAND else None
        self.field_stale = self.jobs > 0
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
        if self.model_error:
            self.feedback.notify("chttr can't transcribe: speech model missing", "Run: chttr setup",
                                 urgency="critical", timeout_ms=8000)
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
        self._overlay_state("transcribing")
        self._restore_mute_state()
        self.feedback.play("stop")
        if self.overlay is None and secs >= TAP:     # the overlay shows this itself; a tap shows nothing
            self.feedback.notify("Transcribing…", f"{secs:.1f}s of audio, peak {peak_db:.0f} dB", timeout_ms=10000)
        job = (seq, audio, secs, peak_db, self.kind, self.app, self.title, self.selection, captions, self.field,
               self.field_stale)
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
                 field_future: Future | None = None, field_stale: bool = False) -> None:
        raw = text = ""
        method = ""
        mode: Mode | None = None
        enter_after = False
        try:
            if not self.model_ready.wait(0.3):
                self.feedback.notify("Loading the speech model\u2026", "Your dictation is kept and typed when it is ready.",
                                     timeout_ms=8000)
                self.model_ready.wait(MODEL_WAIT)
            if self.transcriber is None:
                raise RuntimeError(self.model_error or "the speech model took too long to load")
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
            log.info("%s %.1fs audio (peak %.0f dB) -> %.2fs after release: %d words",
                     self.transcriber.engine.name, secs, peak_db, time.monotonic() - t0, len(raw.split()))
            log.debug("raw: %r", raw)
            if not raw:
                self._overlay_state("nospeech" if secs >= TAP else "hide")
                if secs >= TAP:                     # a tap (maybe half of a double tap) is not a dictation
                    self._no_speech(secs, peak_db)
                return
            field = self._field(field_future)
            names = CX.names_in(field.text) if field and self.cfg.polish.screen_context != "off" else []
            fixed = dictionary.apply(raw, names)
            if fixed != raw:
                log.debug("dictionary: %r", fixed)
            mode = self.cfg.mode_for(app, title)
            if kind == COMMAND:
                text = self._run_command(fixed, selection, app, title, mode, dictionary, seq)
                if not text:
                    return
            else:
                coded = devmode.transform(fixed, bool(mode and mode.is_code), prose=bool(mode and mode.name != "terminal"))
                coded, enter_after = (devmode.strip_enter(coded, strict=mode.name != "terminal")
                                      if (mode and mode.is_code and self.cfg.output.spoken_enter) else (coded, False))
                keep = devmode.keep_list(fixed, coded)
                if coded != fixed:
                    log.debug("dev mode: %r", coded)
                text = self._polish(coded, app, title, mode, dictionary, keep, names,
                                    field.before if field and self.cfg.polish.screen_context == "full" else "") \
                    if coded else ""        # it was only "run it": nothing to type, just Enter
                text = dictionary.expand_snippets(text)
            ts_override = None
            # an earlier dictation typed after the field was read changed what is before the cursor
            if kind != COMMAND and field is not None and not field_stale and self.cfg.polish.screen_context != "off":
                text, ts_override = smart_join(field.before, field.after, text, dictionary.vocabulary + names,
                                               code=bool(mode and mode.is_code))
            self._wait_turn(seq)
            method = deliver(text, self.cfg.output,
                             mode=(mode.output if mode else ""),
                             trailing_space=False if kind == COMMAND else (
                                 ts_override if ts_override is not None else (mode.trailing_space if mode else None)),
                             allow_paste=not (mode and mode.in_terminal),
                             newline=(mode.newline if mode else "enter"))
            if enter_after and method in ("type", "paste"):
                press_enter(0.2 if method == "paste" else 0.0)
                if not text:            # it was only "run it"
                    self._overlay_text("Pressed Enter")
                    self._overlay_state("done")
                    if self.overlay is None:
                        self.feedback.notify("Pressed Enter", "", timeout_ms=1500)
                    return
            label = "Replaced" if kind == COMMAND and selection else "Typed"
            if self.cfg.history.learn_from_edits and kind != COMMAND and method in ("type", "paste") \
                    and self.atspi_misses < ATSPI_GIVE_UP:
                timer = threading.Timer(8.0, self._learn_from_edits, args=(text,))
                timer.daemon = True
                timer.start()
            if method == "clipboard":
                self._overlay_state("hide")
                self.feedback.notify("Copied to clipboard", text[:200], timeout_ms=4000)
            else:
                self._overlay_text(text)
                self._overlay_state("done")
                if self.overlay is None:            # no overlay to show the check
                    self.feedback.notify(label, text[:200], timeout_ms=2500)
        except Exception as exc:
            log.exception("dictation failed")
            self._overlay_state("error")
            self.feedback.play("error")
            self.feedback.notify("chttr error", f"{type(exc).__name__}: {exc}", urgency="critical", timeout_ms=8000)
        finally:
            self._overlay_settle()
            self._done_turn(seq)
            with self.lock:
                self.jobs -= 1
                self._refresh_state()
            if raw and self.cfg.history.enabled:
                self._append_history(raw, text, app, secs, method, kind, mode.name if mode else "", audio)

    def _read_field(self) -> CX.Field | None:
        """The focused text field. If the accessibility bus never answers (it isn't
        running on every desktop), pause after a few tries and try once more every
        ten minutes (accessibility may be on by then, or you're in another app)."""
        if self.atspi_misses >= ATSPI_GIVE_UP:
            self.atspi_misses = ATSPI_GIVE_UP - 1    # the retry: one more miss pauses again
        field = CX.read_field(SYSTEM_PYTHON)
        if field is None:
            self.atspi_misses += 1
            if self.atspi_misses >= ATSPI_GIVE_UP:
                self.atspi_retry_at = time.monotonic() + ATSPI_RETRY
                log.info("screen context: no readable text field %d times; trying again in %d min",
                         ATSPI_GIVE_UP, ATSPI_RETRY // 60)
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
            log.info("you corrected a word; offering to learn it")
            log.debug("you changed %r to %r", c.heard, c.right)
            # The click runs `chttr learn`, which saves the word and says so.
            self.feedback.offer(f"Learn \u201c{c.right}\u201d?",
                                f"You changed \u201c{c.heard}\u201d to \u201c{c.right}\u201d. "
                                "chttr can spell it that way from now on.",
                                ("Learn it", [*self._chttr_argv(), "learn", c.right, "--heard", c.heard, "--notify"]),
                                glyph="󰗊", timeout_ms=15000)

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
        # A coding agent's prompt is prose (sentences, a full stop), though it runs in a terminal.
        code = bool(mode and mode.is_code and mode.name != "agent")
        chat = bool(mode and mode.name == "chat")
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
                     mode: Mode | None, dictionary: Dictionary, seq: int | None = None) -> str:
        if selection:
            # with a model at hand, only exact phrases: a near miss is the model's to read
            local = local_command(instruction, selection, fuzzy=P.backend(self.cfg.polish) == "none")
            if local == "" and mode and mode.in_terminal:          # "delete that"
                self.feedback.notify("Can't delete in a terminal", "Terminals have no key that deletes a selection.",
                                     timeout_ms=3000)
                return ""
            if local == "":
                log.info("command handled locally: delete")
                if seq is not None:
                    self._wait_turn(seq)        # after the dictations before it have been typed
                delete_selection()
                n = len(selection.split())
                self.feedback.notify("Deleted", f"{n} word{'s' if n != 1 else ''}", timeout_ms=2000)
                return ""
            if local:
                log.info("command handled locally")
                return local
        if P.backend(self.cfg.polish) == "none":
            self.feedback.play("error")
            self.feedback.notify("Command mode needs a model",
                                 "Add a key (chttr connect claude) or run Ollama. Without a model these work on a "
                                 "selection: upper case, lower case, title case, join lines, sort lines, bullet list, "
                                 "numbered list, wrap in quotes, remove duplicates.",
                                 urgency="normal", timeout_ms=10000)
            return ""
        try:
            t1 = time.monotonic()
            out = P.command(instruction, selection, self.cfg.polish, app, title, dictionary.vocabulary,
                            style=mode.style if mode else "")
            log.info("command %.2fs: %d words -> %d words", time.monotonic() - t1, len(instruction.split()),
                     len(out.split()))
            log.debug("command: %r -> %r", instruction, out[:200])
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
    def _forever(self, name: str, fn, interval: float) -> None:
        """Run fn every interval seconds until the daemon stops. An exception is logged
        and the next tick runs anyway: auto-stop and the time limits must never die."""
        last = ""
        while not self._stop.wait(interval):
            try:
                fn()
                last = ""
            except Exception as exc:
                if repr(exc) != last:           # a tick that keeps failing logs its traceback once
                    log.exception("%s failed; continuing", name)
                else:
                    log.debug("%s failed again: %s", name, exc)
                last = repr(exc)

    def _watchdog(self) -> None:
        self._forever("watchdog", self._watchdog_tick, 0.2)

    def _watchdog_tick(self) -> None:
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
                return
            if not self.recorder.active:
                self._recorder_quit()
                return
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

    def _recorder_quit(self) -> None:
        """The microphone went away mid-recording (unplugged, PipeWire restarted).
        Whatever was said until then is still typed; then say what happened."""
        if not self.heard_speech:
            self._cancel()                      # nothing was said: don't report it as silence
        else:
            self._stop_and_transcribe()
        error = getattr(self.recorder, "error", "")
        log.warning("the recorder stopped by itself: %s", error or "no reason given")
        if error:
            self.feedback.notify("chttr: the microphone went away", error[:160], urgency="critical", timeout_ms=8000)

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
        from . import calls as K

        threading.Thread(target=self._forever, args=("watchdog", self._watchdog_tick, 0.2),
                         name="watchdog", daemon=True).start()
        watcher = K.CallWatcher()
        threading.Thread(target=self._forever, args=("calls", lambda: self._calls_tick(watcher), 3.0),
                         name="calls", daemon=True).start()
        threading.Thread(target=P.warm, args=(self.cfg.polish,), daemon=True).start()
        self._start_hotkeys()
        if self.transcriber is None:
            threading.Thread(target=self._load_model, name="model", daemon=True).start()
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
                self._overlay_quit()
                self._restore_mute_state()
                self._write_state(IDLE)
            server.close()
            sock_path.unlink(missing_ok=True)
            log.info("stopped")


def run(cfg: Config) -> None:
    Daemon(cfg, load_model=False).serve()
