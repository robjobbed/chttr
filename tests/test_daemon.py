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


# ---- always on: the model loads in the background, nothing dies quietly -------

class BlockingTranscriber(FakeTranscriber):
    """A model that takes as long to load as the test says (`gate`), or is missing (`error`)."""

    gate = threading.Event()
    error: Exception | None = None
    texts_on_load: tuple[str, ...] = ()

    def __init__(self, cfg):
        if self.error:
            raise self.error
        self.gate.wait(5)
        super().__init__(cfg)
        self.texts = list(self.texts_on_load)


@pytest.fixture
def lazy_daemon(daemon, monkeypatch):
    """Like `chttr daemon`: no model yet. Shares the fixture's fakes and typed list."""
    class Model(BlockingTranscriber):
        gate = threading.Event()

    monkeypatch.setattr(Dm, "Transcriber", Model)
    d = Dm.Daemon(daemon.cfg, load_model=False)
    d.typed = daemon.typed
    d.Model = Model
    return d


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_status_reports_a_ready_model(daemon):
    assert daemon.handle("status")["model"] == "ready"


def test_a_dictation_made_while_the_model_loads_is_typed_when_it_is_ready(lazy_daemon):
    d = lazy_daemon
    d.Model.texts_on_load = ("typed after loading",)
    threading.Thread(target=d._load_model, daemon=True).start()
    assert wait_for(lambda: d.state == Dm.LOADING)
    assert d.handle("status")["model"] == "loading"
    assert d.handle("start")["state"] == Dm.RECORDING          # the key works at once
    assert d.handle("release")["state"] == Dm.TRANSCRIBING
    assert wait_for(lambda: "Loading the speech model…" in d.feedback.notes)
    assert d.typed == []
    d.Model.gate.set()
    wait_idle(d)
    assert d.typed == ["typed after loading"]
    assert d.handle("status")["model"] == "ready"


def test_a_missing_model_is_one_clear_notice_and_no_crash(lazy_daemon):
    d = lazy_daemon
    d.Model.error = FileNotFoundError("no Parakeet model in ~/.local/share/chttr/models")
    d._load_model()
    assert d.state == Dm.IDLE
    assert d.handle("status")["model"] == "missing"
    assert "chttr: the speech model is missing" in d.feedback.notes
    assert d.handle("start")["state"] == Dm.RECORDING          # still records
    assert "chttr can't transcribe: speech model missing" in d.feedback.notes
    d.handle("release")
    wait_idle(d)
    assert d.typed == []
    assert d.feedback.notes[-1] == "chttr error"


def test_a_corrupt_model_is_the_same_clear_notice(lazy_daemon):
    d = lazy_daemon
    # what onnxruntime raises for a half-downloaded model: a plain Exception, not a RuntimeError
    d.Model.error = Exception("[ONNXRuntimeError] : 7 : INVALID_PROTOBUF : Load model failed")
    d._load_model()
    assert d.state == Dm.IDLE
    assert d.handle("status")["model"] == "missing"
    assert "INVALID_PROTOBUF" in d.model_error
    assert "chttr: the speech model is missing" in d.feedback.notes


def test_a_reload_after_a_failed_model_load_does_not_say_missing_while_it_loads(lazy_daemon, monkeypatch, tmp_path):
    d = lazy_daemon
    d.Model.error = FileNotFoundError("no model")
    d._load_model()
    monkeypatch.setattr(C, "CONFIG_FILE", tmp_path / "config.toml")
    d.Model.error = None
    d.Model.gate.clear()
    d.handle("reload")
    assert d.model_error == ""
    d.feedback.notes.clear()
    d.handle("start")
    assert "chttr can't transcribe: speech model missing" not in d.feedback.notes
    d.handle("cancel")
    d.Model.gate.set()
    assert wait_for(lambda: d.handle("status")["model"] == "ready")


def test_reload_retries_a_model_that_failed_to_load(lazy_daemon, monkeypatch, tmp_path):
    d = lazy_daemon
    d.Model.error = FileNotFoundError("no model")
    d._load_model()
    monkeypatch.setattr(C, "CONFIG_FILE", tmp_path / "config.toml")
    d.Model.error = None
    d.Model.gate.set()
    reply = d.handle("reload")
    assert "loading the speech model" in reply["message"]
    assert wait_for(lambda: d.handle("status")["model"] == "ready")
    assert d.model_error == ""


def test_forever_survives_a_failing_tick(daemon):
    calls = []

    def tick():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("hiccup")
        daemon._stop.set()

    daemon._forever("test", tick, 0.001)
    assert len(calls) == 3


def test_the_watchdog_survives_a_recorder_that_raises(daemon):
    import subprocess

    daemon.transcriber.texts = ["stopped at the limit"]
    daemon.cfg.audio.max_seconds = 1
    daemon.handle("toggle")
    daemon.recorder.elapsed = 5.0
    real_stop, fails = daemon.recorder.stop, [1]

    def flaky_stop():
        if fails:
            fails.pop()
            raise subprocess.TimeoutExpired("pw-record", 2)
        return real_stop()

    daemon.recorder.stop = flaky_stop
    watchdog = threading.Thread(target=daemon._watchdog, daemon=True)
    watchdog.start()
    try:
        wait_idle(daemon)
        assert daemon.typed == ["stopped at the limit"]          # the second tick still enforced max_seconds
        assert watchdog.is_alive()
    finally:
        daemon._stop.set()
        watchdog.join(2)


def test_a_microphone_that_goes_away_still_types_what_was_said(daemon):
    daemon.transcriber.texts = ["half a sentence"]
    daemon.handle("start")
    daemon.recorder.active = False                               # pw-record quit by itself
    daemon.recorder.error = "stream error: connection lost"
    daemon.recorder.chunk_count = 50                             # one second of audio
    daemon.heard_speech = True
    daemon._watchdog_tick()
    wait_idle(daemon)
    assert daemon.typed == ["half a sentence"]
    assert "chttr: the microphone went away" in daemon.feedback.notes


def test_a_microphone_gone_after_a_silent_pause_is_not_called_silence(daemon):
    daemon.transcriber.texts = [""]
    daemon.handle("start")
    daemon.recorder.active = False
    daemon.recorder.error = "stream error: connection lost"
    daemon.recorder.chunk_count = 75                             # 1.5 s, nothing said
    daemon._watchdog_tick()
    wait_idle(daemon)
    assert daemon.typed == []
    assert daemon.feedback.notes[-1] == "chttr: the microphone went away"
    assert not any("No speech" in n for n in daemon.feedback.notes)


def test_a_microphone_gone_before_a_word_is_not_called_silence(daemon):
    daemon.handle("start")
    daemon.recorder.active = False
    daemon.recorder.error = "connection lost"
    daemon._watchdog_tick()
    wait_idle(daemon)
    assert daemon.typed == []
    assert daemon.feedback.notes[-1] == "chttr: the microphone went away"
    assert not any("No speech" in n for n in daemon.feedback.notes)


def test_reload_applies_audio_and_feedback_and_says_what_changed(daemon, monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(C, "CONFIG_FILE", path)
    monkeypatch.setattr(Dm, "engine_key", lambda cfg: daemon.transcriber.key)
    old_feedback = daemon.feedback
    path.write_text("[audio]\nwhisper_mode = true\n\n[feedback]\nsounds = false\n", encoding="utf-8")
    reply = daemon.handle("reload")
    assert reply["ok"] is True
    assert "audio.whisper_mode" in reply["message"] and "feedback.sounds" in reply["message"]
    assert "engine." not in reply["message"]
    assert daemon.transcriber.boost is True
    assert daemon.transcriber.threshold_db == -50
    assert daemon.feedback is not old_feedback and daemon.feedback.cfg.sounds is False
    assert daemon.cfg.audio.whisper_mode is True


def test_reload_restarts_hotkeys_only_when_they_change(daemon, monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(C, "CONFIG_FILE", path)
    monkeypatch.setattr(Dm, "engine_key", lambda cfg: daemon.transcriber.key)
    started = []
    monkeypatch.setattr(daemon, "_start_hotkeys", lambda: started.append(daemon.cfg.hotkeys.dictate))
    daemon.hotkeys = old = type("H", (), {"stop": threading.Event()})()
    path.write_text("[audio]\nwhisper_mode = true\n", encoding="utf-8")
    daemon.handle("reload")
    assert started == [] and not old.stop.is_set()
    path.write_text('[hotkeys]\ndictate = "F9"\n', encoding="utf-8")
    assert "hotkeys.dictate" in daemon.handle("reload")["message"]
    assert old.stop.is_set() and started == ["F9"]


@pytest.mark.parametrize("text", ['[output]\nmode = "shout"\n', "[audio\nwhisper_mode = true\n"])
def test_a_broken_config_keeps_the_running_one(daemon, monkeypatch, tmp_path, text):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(C, "CONFIG_FILE", path)
    path.write_text(text, encoding="utf-8")
    cfg = daemon.cfg
    reply = daemon.handle("reload")
    assert reply["ok"] is False and reply["error"].startswith("config.toml: ")
    assert daemon.cfg is cfg


def test_the_log_holds_word_counts_not_your_words(daemon, caplog):
    import logging

    daemon.transcriber.texts = ["the launch date is the fourteenth", "the launch date is the fourteenth"]
    with caplog.at_level(logging.INFO, logger="chttr"):
        daemon.handle("start")
        daemon.handle("release")
        wait_idle(daemon)
    assert daemon.typed == ["the launch date is the fourteenth"]
    messages = [r.getMessage() for r in caplog.records]
    assert any("6 words" in m for m in messages)
    assert not any("launch date" in m for m in messages)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="chttr"):            # chttr daemon -v
        daemon.handle("start")
        daemon.handle("release")
        wait_idle(daemon)
    assert any("launch date" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)


def test_the_daemon_answers_on_its_socket_while_the_model_loads(lazy_daemon, monkeypatch):
    from chttr import client

    d = lazy_daemon
    monkeypatch.setattr(Dm.signal, "signal", lambda *a: None)       # serve() runs off the main thread here
    monkeypatch.setattr(Dm.P, "warm", lambda cfg: None)
    monkeypatch.setattr(d, "_start_hotkeys", lambda: None)
    monkeypatch.setattr(d, "_calls_tick", lambda watcher: None)
    server = threading.Thread(target=d.serve, daemon=True)
    server.start()
    try:
        assert wait_for(C.SOCKET_PATH.exists)
        reply = client.send("status")
        assert reply["model"] == "loading" and reply["state"] == Dm.LOADING
        assert C.STATE_FILE.read_text(encoding="utf-8") == "loading"
        d.Model.gate.set()
        assert wait_for(lambda: client.send("status")["model"] == "ready")
        assert client.send("status")["state"] == Dm.IDLE
    finally:
        d._stop.set()
        server.join(3)
    assert not C.SOCKET_PATH.exists()
