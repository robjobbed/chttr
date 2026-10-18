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


# ---- quiet mics --------------------------------------------------------------
def db(level: float) -> float:
    return 10 ** (level / 20)


def run_watchdog(d, until, timeout=1.5) -> bool:
    """Run the hands-free watchdog until `until()` holds (or the timeout)."""
    t = threading.Thread(target=d._watchdog, daemon=True)
    t.start()
    end = time.monotonic() + timeout
    while time.monotonic() < end and not until():
        time.sleep(0.02)
    d._stop.set()
    t.join(1)
    return until()


@pytest.mark.parametrize("adaptive", [True, False])
def test_hands_free_quiet_speech_is_heard(daemon, adaptive):
    daemon.cfg.audio.adaptive_threshold = adaptive
    daemon.cfg.audio.no_speech_timeout = 0.3
    daemon.cfg.audio.auto_stop_silence = 0.2
    daemon.transcriber.texts = ["said quietly"]
    daemon.handle("toggle")
    daemon.recorder.elapsed = 1.0                      # past no_speech_timeout
    for _ in range(15):
        daemon._on_level(db(-70), -60.0)               # the room, before you speak
    for _ in range(30):
        daemon._on_level(db(-44), -40.0)               # a quiet laptop mic
    assert daemon.heard_speech is adaptive
    assert run_watchdog(daemon, lambda: daemon.state != Dm.RECORDING)
    wait_idle(daemon)
    if adaptive:                                       # stopped on the pause, and typed
        assert daemon.typed == ["said quietly"]
        assert "Dictation cancelled" not in daemon.feedback.notes
    else:                                              # 1.0: "nothing heard", cancelled
        assert daemon.typed == []
        assert "Dictation cancelled" in daemon.feedback.notes


def test_on_level_lowers_the_streaming_threshold(daemon):
    from types import SimpleNamespace

    daemon.streamer = SimpleNamespace(threshold_db=-38.0)
    for _ in range(20):
        daemon._on_level(db(-70), -60.0)
    assert daemon.streamer.threshold_db == pytest.approx(-60)
    daemon.cfg.audio.adaptive_threshold = False
    daemon._on_level(db(-70), -60.0)
    assert daemon.streamer.threshold_db == -38
    daemon.streamer = None


def test_loud_speech_is_heard_as_before(daemon):
    daemon._on_level(db(-20), -10.0)
    assert daemon.heard_speech


class NotedFeedback(QuietFeedback):
    def notify(self, title, body="", **kw):
        self.notes.append(f"{title} | {body}")


def no_speech_note(daemon, monkeypatch, peak_db, compositor="hyprland", omarchy=True) -> str:
    from chttr.desktop import Desktop

    monkeypatch.setattr(Dm, "current_desktop", lambda: Desktop("wayland", compositor))
    monkeypatch.setattr(Dm.shutil, "which", lambda name: f"/usr/bin/{name}" if omarchy else None)
    daemon.feedback = NotedFeedback(daemon.cfg.feedback, daemon.runtime)
    daemon._no_speech(2.0, peak_db)
    return daemon.feedback.notes[-1]


def test_no_speech_message_mentions_the_floor(daemon, monkeypatch):
    for _ in range(20):
        daemon._on_level(db(-66), -50.0)
    note = no_speech_note(daemon, monkeypatch, -31.0)
    assert note.startswith("No speech detected: the mic is quiet")
    assert "Peak -31 dB over a -66 dB floor; chttr listened down to -56 dB." in note
    assert "Raise the input level" in note


def test_no_speech_message_without_levels_skips_the_floor(daemon, monkeypatch):
    note = no_speech_note(daemon, monkeypatch, -31.0)
    assert note.endswith("| Peak -31 dB. Raise the input level for best results.")


def test_no_speech_on_a_loud_mic_stays_short(daemon, monkeypatch):
    assert no_speech_note(daemon, monkeypatch, -12.0) == "No speech detected | 2.0s, peak -12 dB"


@pytest.mark.parametrize("compositor", ["hyprland", "sway", "gnome", "kde"])
def test_silent_mic_names_the_omarchy_shortcut_only_on_hyprland(daemon, monkeypatch, compositor):
    note = no_speech_note(daemon, monkeypatch, -60.0, compositor)
    assert note.startswith("No speech detected: the mic is silent | Peak -60 dB. Check the input device and mute")
    assert ("Super+Ctrl+A" in note) is (compositor == "hyprland")
    assert note.endswith(".")


def test_no_speech_message_claims_no_lowering_that_never_happened(daemon, monkeypatch):
    daemon.cfg.audio.adaptive_threshold = False
    for _ in range(20):
        daemon._on_level(db(-66), -50.0)
    note = no_speech_note(daemon, monkeypatch, -31.0)
    assert "Peak -31 dB over a -66 dB floor. Raise the input level" in note
    assert "listened down" not in note


def test_no_speech_message_skips_the_levels_of_a_newer_recording(daemon, monkeypatch):
    for _ in range(20):
        daemon._on_level(db(-66), -50.0)
    daemon.jobs = 2                      # you started (and stopped) another dictation meanwhile
    note = no_speech_note(daemon, monkeypatch, -31.0)
    assert note.endswith("| Peak -31 dB. Raise the input level for best results.")


def test_silent_mic_on_plain_hyprland_skips_the_omarchy_shortcut(daemon, monkeypatch):
    note = no_speech_note(daemon, monkeypatch, -60.0, "hyprland", omarchy=False)
    assert "Super+Ctrl+A" not in note
    assert note.endswith("Check the input device and mute.")


def test_hands_free_loud_speech_keeps_a_tv_as_a_pause(daemon):
    # Lowering only goes as far as the speech needs: a -50 dB TV is not speech next
    # to someone talking at -20 dB, so hands-free can still stop on your pause.
    for _ in range(40):
        daemon._on_level(db(-70), -60.0)
    for _ in range(160):
        daemon._on_level(db(-20), -10.0)
    daemon.last_speech = 0.0
    daemon._on_level(db(-50), -45.0)
    assert daemon.last_speech == 0.0
