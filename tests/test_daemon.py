"""The daemon's state machine with fake audio, recognizer and output: no PipeWire,
no models, no compositor."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from chttr import config as C
from chttr import daemon as Dm


class FakeRecorder:
    def __init__(self, device="", on_level=None):
        self.active = False
        self.peak_db = -12.0
        self.elapsed = 0.0
        self.device = device
        self.secs = 1.0              # length of the next recording
        self.levels_db: list[float] = []
        self.chunk_count = 0

    def start(self):
        self.active = True

    def stop(self):
        self.active = False
        return np.zeros(int(16000 * self.secs), dtype=np.float32), self.secs

    def cancel(self):
        self.active = False

    def snapshot(self, max_seconds=0):
        return np.zeros(0, dtype=np.float32)


class FakeTranscriber:
    """Returns queued transcripts; `delays` slows down particular calls."""

    def __init__(self, cfg):
        self.texts: list[str] = []
        self.delays: list[float] = []
        self.engine = type("E", (), {"name": "fake"})()
        self.key = ("fake",)

    def transcribe(self, audio, prompt=None, trim=True):
        delay = self.delays.pop(0) if self.delays else 0.0
        text = self.texts.pop(0) if self.texts else ""
        time.sleep(delay)
        return text


class QuietFeedback:
    def __init__(self, cfg, runtime):
        self.cfg = cfg
        self.notes: list[str] = []

    def play(self, name):
        pass

    def notify(self, title, body="", **kw):
        self.notes.append(title)


@pytest.fixture
def daemon(monkeypatch):
    monkeypatch.setattr(Dm, "Recorder", FakeRecorder)
    monkeypatch.setattr(Dm, "Transcriber", FakeTranscriber)
    monkeypatch.setattr(Dm, "Feedback", QuietFeedback)
    typed: list[str] = []
    monkeypatch.setattr(Dm, "deliver", lambda text, cfg, **kw: typed.append(text) or "type")
    monkeypatch.setattr(Dm, "focused_window", lambda: ("kitty", "~"))
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "")
    monkeypatch.setattr(Dm.CX, "read_field", lambda *a, **k: None)
    cfg = C.Config(modes=C._default_modes())
    cfg.audio.unmute = False
    cfg.feedback.overlay = False
    cfg.feedback.live_captions = False
    cfg.polish.backend = "none"
    cfg.history.enabled = False
    d = Dm.Daemon(cfg)
    d.typed = typed
    return d


def wait_idle(d, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if d.state == Dm.IDLE and not d.jobs:
            return
        time.sleep(0.01)
    raise AssertionError(f"daemon stuck in {d.state} with {d.jobs} jobs")


def test_push_to_talk_types_the_transcript(daemon):
    daemon.transcriber.texts = ["hello world"]
    assert daemon.handle("start")["state"] == Dm.RECORDING
    assert daemon.handle("release")["state"] == Dm.TRANSCRIBING
    wait_idle(daemon)
    assert daemon.typed == ["hello world"]


def test_release_during_a_slow_start_still_stops(daemon, monkeypatch):
    daemon.transcriber.texts = ["quick tap"]
    gate = threading.Event()

    def slow_window():
        gate.wait(2)
        return "kitty", "~"

    monkeypatch.setattr(Dm, "focused_window", slow_window)
    replies = {}
    t = threading.Thread(target=lambda: replies.setdefault("start", daemon.handle("start")))
    t.start()
    time.sleep(0.05)
    assert "will stop" in daemon.handle("release")["message"]
    gate.set()
    t.join(2)
    wait_idle(daemon)
    assert daemon.typed == ["quick tap"]


def test_a_new_dictation_can_start_while_the_last_one_finishes(daemon):
    daemon.transcriber.texts = ["first", "second"]
    daemon.transcriber.delays = [0.3, 0.0]
    daemon.handle("start")
    daemon.handle("release")
    assert daemon.handle("start")["state"] == Dm.RECORDING      # no "busy transcribing"
    daemon.handle("release")
    wait_idle(daemon)
    assert daemon.typed == ["first", "second"]                   # typed in the order spoken


def test_hands_free_ignores_key_release_and_stops_on_toggle(daemon):
    daemon.transcriber.texts = ["hands free"]
    assert daemon.handle("toggle")["kind"] == Dm.HANDSFREE
    assert "ignoring" in daemon.handle("release")["message"]
    daemon.handle("toggle")
    wait_idle(daemon)
    assert daemon.typed == ["hands free"]


def test_no_speech_types_nothing(daemon):
    daemon.transcriber.texts = [""]
    daemon.handle("start")
    daemon.handle("release")
    wait_idle(daemon)
    assert daemon.typed == []
    assert any("No speech" in n for n in daemon.feedback.notes)


def test_cancel_discards(daemon):
    daemon.handle("start")
    assert daemon.handle("cancel")["state"] == Dm.IDLE
    assert daemon.typed == []


def test_command_mode_without_a_model_explains_itself(daemon):
    daemon.transcriber.texts = ["make it shorter"]
    daemon.handle("command")
    daemon.handle("release")
    wait_idle(daemon)
    assert daemon.typed == []
    assert "Command mode needs a model" in daemon.feedback.notes


def test_unknown_command(daemon):
    assert daemon.handle("dance")["ok"] is False


def test_double_tap_starts_hands_free_and_a_tap_stops_it(daemon):
    daemon.transcriber.texts = ["", "long hands free dictation"]
    daemon.recorder.secs = 0.2
    daemon.handle("start")
    daemon.handle("release")                                   # a quick tap
    wait_idle(daemon)
    assert daemon.handle("start")["kind"] == Dm.HANDSFREE      # second tap: hands-free
    assert "ignoring" in daemon.handle("release")["message"]
    daemon.recorder.secs = 4.0
    daemon.handle("start")                                     # a tap ends it
    wait_idle(daemon)
    assert daemon.typed == ["long hands free dictation"]
    assert not any("No speech" in n for n in daemon.feedback.notes)   # the tap itself is silent


def test_double_tap_can_be_turned_off(daemon):
    daemon.cfg.audio.double_tap = False
    daemon.recorder.secs = 0.2
    daemon.handle("start")
    daemon.handle("release")
    wait_idle(daemon)
    assert daemon.handle("start")["kind"] == Dm.PTT


def test_dev_mode_in_code_editors(daemon, monkeypatch):
    monkeypatch.setattr(Dm, "focused_window", lambda: ("code-oss", "main.py - project"))
    daemon.transcriber.texts = ["Open at main dot py and rename camel case user id."]
    daemon.handle("start")
    daemon.handle("release")
    wait_idle(daemon)
    assert daemon.typed == ["Open @main.py and rename userId."]


def test_level_none_skips_cleanup(daemon, monkeypatch):
    calls = []
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "polish", lambda *a, **k: calls.append(a) or "polished")
    daemon.cfg.polish.level = "none"
    daemon.transcriber.texts = ["um this is a longer sentence that would normally be cleaned"]
    daemon.handle("start")
    daemon.handle("release")
    wait_idle(daemon)
    assert calls == [] and daemon.typed[0].startswith("um this")


def test_names_on_screen_fix_sound_alikes(daemon, monkeypatch):
    field = Dm.CX.Field("Mail", "Hi team, as discussed with Priyanka yesterday, ", "")
    monkeypatch.setattr(Dm.CX, "read_field", lambda *a, **k: field)
    daemon.transcriber.texts = ["Priyanca will send it"]
    daemon.handle("start")
    daemon.handle("release")
    wait_idle(daemon)
    assert daemon.typed == ["Priyanka will send it"]


def test_learn_from_edits_offers_and_learns(daemon, monkeypatch, tmp_path):
    from chttr import dictionary

    monkeypatch.setattr(dictionary, "DICTIONARY_FILE", tmp_path / "d.toml")
    monkeypatch.setattr(Dm.Dictionary.load.__func__, "__defaults__", (tmp_path / "d.toml",))
    monkeypatch.setattr(Dm.Dictionary.save, "__defaults__", (tmp_path / "d.toml",))
    field = Dm.CX.Field("Mail", "Please send the contract to Sohrab by Friday.", "")
    monkeypatch.setattr(Dm.CX, "read_field", lambda *a, **k: field)

    class Out:
        stdout = "learn\n"

    asked = []
    monkeypatch.setattr(Dm.subprocess, "run", lambda cmd, **k: asked.append(cmd) or Out())
    daemon._learn_from_edits("Please send the contract to Sorov by Friday.")
    assert asked and "Sohrab" in asked[0][-2]
    assert dictionary.Dictionary.load(tmp_path / "d.toml").replacements == {"sorov": "Sohrab"}
    daemon._learn_from_edits("Please send the contract to Sorov by Friday.")
    assert len(asked) == 1                                      # offered once per session


def test_screen_context_backs_off_when_atspi_never_answers(daemon, monkeypatch):
    calls = []
    monkeypatch.setattr(Dm.CX, "read_field", lambda *a, **k: calls.append(1))
    daemon.transcriber.texts = ["one two three four five six"] * 8
    for _ in range(8):
        daemon.handle("start")
        daemon.handle("release")
        wait_idle(daemon)
    assert len(calls) == Dm.ATSPI_GIVE_UP


# ---- the resident overlay pill ----------------------------------------------

class FakeOverlayProc:
    """Stands in for overlay.py: records what the daemon writes to it."""

    spawned = []                # noqa: RUF012 - every spawn's command, reset by the fixture
    procs = []                  # noqa: RUF012 - and the process

    def __init__(self, cmd, **kw):
        assert str(Dm.OVERLAY_SCRIPT) in cmd
        FakeOverlayProc.spawned.append(cmd)
        FakeOverlayProc.procs.append(self)
        self.cmd = cmd
        self.written = bytearray()
        self.returncode = None
        proc = self

        class Stdin:
            closed = False

            def write(self, data):
                if proc.returncode is not None:
                    raise BrokenPipeError
                proc.written += data

            def flush(self):
                pass

            def close(self):
                self.closed = True
                proc.returncode = 0

        self.stdin = Stdin()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def commands(self):
        """What it was told, level lines left out."""
        lines = self.written.decode().splitlines()
        return [line for line in lines if not line[:1].isdigit()]


@pytest.fixture
def pill(daemon, monkeypatch):
    """The daemon on Hyprland with the overlay switched on (and a fake overlay process)."""
    FakeOverlayProc.spawned, FakeOverlayProc.procs = [], []
    monkeypatch.setattr(Dm.subprocess, "Popen", FakeOverlayProc)
    monkeypatch.setattr(Dm, "current_desktop", lambda: _desk("hyprland"))
    monkeypatch.setattr(Dm.OP, "omarchy_palette", lambda path=None: None)
    daemon.cfg.feedback.overlay = True
    return daemon


def _desk(compositor, session="wayland"):
    from chttr.desktop import Desktop

    return Desktop(session, compositor)


def dictate(d, text, secs=1.0, verb="start"):
    d.transcriber.texts = [text]
    d.recorder.secs = secs
    d.handle(verb)
    d.handle("release")
    wait_idle(d)


def test_overlay_shows_the_whole_dictation(pill):
    dictate(pill, "hello world")
    assert pill.overlay.commands() == ["show ", "state listening", "state transcribing", "text hello world",
                                       "state done"]
    assert pill.typed == ["hello world"]
    assert "Transcribing…" not in pill.feedback.notes and "Typed" not in pill.feedback.notes


def test_overlay_process_is_reused_across_dictations(pill):
    for words in ("one", "two", "three"):
        dictate(pill, words)
    assert len(FakeOverlayProc.spawned) == 1
    assert pill.overlay.commands().count("state done") == 3


def test_overlay_is_started_hidden_with_the_daemon(pill):
    pill._overlay_warm()
    assert len(FakeOverlayProc.spawned) == 1 and pill.overlay.commands() == []
    dictate(pill, "warm")
    assert len(FakeOverlayProc.spawned) == 1
    assert pill.overlay.commands()[0] == "show "


def test_a_tap_hides_the_pill_and_leaves_no_notification(pill):
    dictate(pill, "", secs=0.2)
    assert pill.overlay.commands()[-1] == "hide"
    assert "Transcribing…" not in pill.feedback.notes
    assert not any("No speech" in n for n in pill.feedback.notes)


def test_nothing_heard(pill):
    dictate(pill, "", secs=1.0)
    assert pill.overlay.commands()[-1] == "state nospeech"


def test_command_mode_pill(pill, monkeypatch):
    monkeypatch.setattr(Dm.Daemon, "_run_command", lambda self, *a, **k: "")   # e.g. no model to run it
    dictate(pill, "make it shorter", verb="command")
    commands = pill.overlay.commands()
    assert commands[:2] == ["show command", "state command"]
    assert commands[-1] == "hide"                       # the notification explains; the pill goes
    assert pill.overlay_mode == "hidden"


def test_hands_free_pill(pill):
    pill.transcriber.texts = ["hands free"]
    pill.handle("toggle")
    pill.handle("toggle")
    wait_idle(pill)
    assert pill.overlay.commands()[:2] == ["show hands-free", "state handsfree"]
    assert pill.overlay.commands()[-1] == "state done"


def test_a_failing_transcriber_shows_the_error_pill(pill):
    def boom(*a, **k):
        raise RuntimeError("model fell over")

    pill.transcriber.transcribe = boom
    dictate(pill, "")
    assert pill.overlay.commands()[-1] == "state error"
    assert "chttr error" in pill.feedback.notes


def test_cancel_hides_the_pill(pill):
    pill.handle("start")
    pill.handle("cancel")
    assert pill.overlay.commands()[-1] == "hide"
    assert len(FakeOverlayProc.spawned) == 1


def test_clipboard_fallback_still_notifies(pill, monkeypatch):
    monkeypatch.setattr(Dm, "deliver", lambda text, cfg, **kw: "clipboard")
    dictate(pill, "copied words")
    assert pill.overlay.commands()[-1] == "hide"
    assert "Copied to clipboard" in pill.feedback.notes


def test_a_dictation_finishing_during_the_next_recording_leaves_the_pill_alone(pill):
    pill.transcriber.texts = ["first"]
    pill.transcriber.delays = [0.3]
    pill.handle("start")
    pill.handle("release")
    pill.handle("start")                                 # recording again while "first" is transcribed
    time.sleep(0.5)
    assert pill.overlay.commands()[-1] == "state listening"
    pill.transcriber.texts = ["second"]
    pill.handle("release")
    wait_idle(pill)
    assert pill.typed == ["first", "second"]
    assert pill.overlay.commands()[-2:] == ["text second", "state done"]


def test_theme_colours_are_passed_and_follow_theme_switches(pill, monkeypatch):
    theme = {"bg": "#f3eefa", "fg": "#221c33", "accent": "#5b2bb5", "hot": "#b42318", "dim": "#6b6480", "dark": False}
    monkeypatch.setattr(Dm.OP, "omarchy_palette", lambda path=None: dict(theme))
    dictate(pill, "one")
    cmd = FakeOverlayProc.spawned[0]
    assert Dm.json.loads(cmd[cmd.index("--palette") + 1]) == theme
    assert not any(c.startswith("palette") for c in pill.overlay.commands())   # unchanged: not resent
    theme["accent"] = "#ff0000"
    dictate(pill, "two")
    assert '"accent": "#ff0000"' in next(c for c in pill.overlay.commands() if c.startswith("palette"))


def test_a_dead_overlay_is_restarted_at_the_next_press(pill):
    dictate(pill, "one")
    pill.overlay.returncode = 1                          # e.g. the compositor restarted
    pill.overlay_spawned_at -= Dm.OVERLAY_RESPAWN
    dictate(pill, "two")
    assert len(FakeOverlayProc.spawned) == 2
    assert pill.overlay.commands()[-2:] == ["text two", "state done"]
    assert "Waveform overlay off" not in pill.feedback.notes


def test_an_overlay_that_keeps_dying_falls_back_to_notifications(pill, monkeypatch):
    class Doomed(FakeOverlayProc):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            self.returncode = 1                          # dies before reading anything

    monkeypatch.setattr(Dm.subprocess, "Popen", Doomed)
    (pill.runtime / "overlay.log").write_text("ValueError: Namespace Gtk4LayerShell not available\n")
    dictate(pill, "one")
    assert "Waveform overlay off" not in pill.feedback.notes     # once could be a fluke
    pill.overlay_spawned_at -= Dm.OVERLAY_RESPAWN
    dictate(pill, "two")
    # After the dictation's own notifications, which share its slot and would replace it at once.
    assert pill.feedback.notes[-3:] == ["Transcribing…", "Typed", "Waveform overlay off"]
    assert all(p.stdin.closed for p in Doomed.procs)    # no BrokenPipeError in the journal at exit
    dictate(pill, "three")                               # within 30 s: no new process, notifications instead
    assert len(Doomed.spawned) == 2
    assert pill.overlay is None
    assert pill.feedback.notes.count("Typed") == 3
    assert pill.typed == ["one", "two", "three"]


def test_overlay_switched_off_by_a_reload_stops_the_process(pill):
    dictate(pill, "one")
    proc = pill.overlay
    pill.cfg.feedback.overlay = False
    dictate(pill, "two")
    assert pill.overlay is None and proc.returncode == 0


def test_live_captions_switched_on_restarts_the_overlay(pill):
    dictate(pill, "one")
    pill.cfg.feedback.live_captions = True
    pill.cfg.engine.streaming = False
    dictate(pill, "two")
    assert len(FakeOverlayProc.spawned) == 2 and "--captions" in FakeOverlayProc.spawned[1]


def test_no_overlay_on_gnome_uses_notifications(pill, monkeypatch):
    monkeypatch.setattr(Dm, "current_desktop", lambda: _desk("gnome"))
    dictate(pill, "hello")
    assert pill.overlay is None and FakeOverlayProc.spawned == []
    assert "Transcribing…" in pill.feedback.notes and "Typed" in pill.feedback.notes
    assert "Waveform overlay off" not in pill.feedback.notes     # expected there, not a fault
    dictate(pill, "", secs=0.2)
    assert pill.feedback.notes.count("Transcribing…") == 1       # not for the tap


def test_without_an_overlay_notifications_still_appear(daemon):
    notes = []
    daemon.feedback.notify = lambda title, body="", **kw: notes.append((title, kw.get("timeout_ms")))
    dictate(daemon, "hello")
    assert ("Transcribing…", 10000) in notes                     # finite: never stuck on screen
    assert any(title == "Typed" for title, _ in notes)
    notes.clear()
    dictate(daemon, "", secs=0.2)
    assert not any(title == "Transcribing…" for title, _ in notes)


def test_overlay_warning_waits_for_the_next_recording_to_end(pill, monkeypatch):
    class Doomed(FakeOverlayProc):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            self.returncode = 1

    monkeypatch.setattr(Dm.subprocess, "Popen", Doomed)
    pill.overlay_losses = 1                              # it died once already
    pill.transcriber.texts = ["first"]
    pill.transcriber.delays = [0.3]
    pill.handle("start")
    pill.handle("release")
    pill.handle("start")                                 # recording again before "first" is typed
    time.sleep(0.5)
    assert pill.typed == ["first"] and "Waveform overlay off" not in pill.feedback.notes
    pill.transcriber.texts = ["second"]
    pill.handle("release")
    wait_idle(pill)
    assert pill.feedback.notes[-1] == "Waveform overlay off"
    assert pill.feedback.notes.count("Waveform overlay off") == 1


def test_an_overlay_that_cannot_start_with_the_daemon_says_so_then(pill, monkeypatch):
    def no_python(cmd, **kw):
        raise FileNotFoundError("/usr/bin/python3")

    monkeypatch.setattr(Dm.subprocess, "Popen", no_python)
    pill._overlay_warm()
    assert pill.feedback.notes == ["Waveform overlay off"]


def test_overlay_starts_once_wayland_shows_up(pill, monkeypatch):
    """A daemon started before Hyprland's sockets exist sees no Wayland yet."""
    monkeypatch.setattr(Dm, "current_desktop", lambda: _desk("unknown", session="unknown"))
    pill._overlay_warm()
    assert FakeOverlayProc.spawned == []
    monkeypatch.setattr(Dm, "current_desktop", lambda: _desk("hyprland"))
    dictate(pill, "hello")
    assert len(FakeOverlayProc.spawned) == 1 and pill.overlay.commands()[-1] == "state done"
    assert "Waveform overlay off" not in pill.feedback.notes


def test_one_overlay_death_is_counted_once(pill):
    dictate(pill, "one")
    proc = pill.overlay
    proc.returncode = 1
    threads = [threading.Thread(target=pill._overlay_lost, args=(proc,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert pill.overlay is None and pill.overlay_losses == 1
    assert proc.stdin.closed
