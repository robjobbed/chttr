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
        self.offers: list[dict] = []

    def play(self, name):
        pass

    def notify(self, title, body="", **kw):
        self.notes.append(title)

    def offer(self, title, body, primary, others=(), **kw):
        self.offers.append({"title": title, "body": body, "primary": primary, "others": list(others), **kw})
        return None


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
    assert daemon.typed == ["Open @main.py and rename userId"]          # editors get no trailing period


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


def test_learn_from_edits_offers_and_learns(daemon, monkeypatch):
    field = Dm.CX.Field("Mail", "Please send the contract to Sohrab by Friday.", "")
    monkeypatch.setattr(Dm.CX, "read_field", lambda *a, **k: field)
    daemon._learn_from_edits("Please send the contract to Sorov by Friday.")
    [offer] = daemon.feedback.offers
    assert offer["title"] == "Learn \u201cSohrab\u201d?"
    label, argv = offer["primary"]
    assert label == "Learn it"
    assert argv[-5:] == ["learn", "Sohrab", "--heard", "Sorov", "--notify"]   # the click runs `chttr learn`
    daemon._learn_from_edits("Please send the contract to Sorov by Friday.")
    assert len(daemon.feedback.offers) == 1                     # offered once per session


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


# ---- local cleanup and the cleanup breaker ---------------------------------------

@pytest.fixture
def offline(monkeypatch):
    """The real module-level breaker (the daemon registered on_recover on it), reset,
    with a background probe that never touches the network."""
    b = Dm.P.breaker
    b._opened_at = None
    monkeypatch.setattr(Dm.P, "_ping", lambda which, cfg: (_ for _ in ()).throw(TimeoutError("still down")))
    yield b
    b._opened_at = None


def dictate(d, text):
    d.transcriber.texts = [text]
    d.handle("start")
    d.handle("release")
    wait_idle(d)
    return d.typed[-1]


def test_without_a_backend_terminals_get_locally_cleaned_text(daemon):
    typed = dictate(daemon, "um git commit dash m fix the parser period")
    assert "um" not in typed.lower().split() and not typed.endswith(".")
    assert typed.startswith("git commit")


def test_a_short_command_with_a_filler_takes_the_quick_path(daemon, monkeypatch):
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: pytest.fail("asked for a backend"))
    assert dictate(daemon, "Um, git status.") == "git status"


def test_without_a_backend_spoken_punctuation_works_in_other_apps(daemon, monkeypatch):
    monkeypatch.setattr(Dm, "focused_window", lambda: ("org.gnome.TextEditor", "notes"))
    assert dictate(daemon, "um so send the contract comma then new line call Dana") == \
        "So send the contract, then\nCall Dana."


def test_cleaning_up_locally_is_explained_once(daemon, monkeypatch, tmp_path):
    monkeypatch.setattr(Dm.C, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "none")
    daemon.cfg.polish.backend = "auto"                       # nothing found, not a choice you made
    dictate(daemon, "ok")                                     # short: nothing worth explaining yet
    dictate(daemon, "um this is a longer sentence for the editor")
    dictate(daemon, "and here is another one that is long enough")
    assert daemon.feedback.notes.count("Cleaning up locally") == 1
    assert (tmp_path / "data" / "hinted-cleanup").exists()


def test_no_hint_when_you_turned_cleanup_off(daemon, monkeypatch, tmp_path):
    monkeypatch.setattr(Dm.C, "DATA_DIR", tmp_path / "data")
    dictate(daemon, "um this is a longer sentence for the editor")      # the fixture sets backend = "none"
    assert "Cleaning up locally" not in daemon.feedback.notes


def test_an_unreachable_model_costs_one_notice_and_no_waiting(daemon, monkeypatch, offline):
    import anthropic

    calls = []

    def dead(system, user, cfg, timeout=None):
        calls.append(user)
        raise anthropic.APIConnectionError(request=None)

    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "_claude", dead)
    t0 = time.monotonic()
    assert dictate(daemon, "um git commit dash m fix the parser period") == "git commit -m fix the parser"
    assert time.monotonic() - t0 < 0.5
    assert dictate(daemon, "um and then run the whole test suite again") == "and then run the whole test suite again"
    assert len(calls) == 1                                     # the breaker skipped the second request
    assert daemon.feedback.notes.count("Cleanup offline") == 1
    assert "chttr: cleanup unavailable" not in daemon.feedback.notes
    offline.succeeded()                                        # back online: the next outage is announced again
    assert daemon.polish_warned is False


def test_model_output_gets_the_terminal_rules(daemon, monkeypatch, caplog):
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "polish", lambda *a, **k: "Run the whole test suite again.")
    with caplog.at_level("INFO", logger="chttr.daemon"):
        assert dictate(daemon, "um run the whole test suite again period") == "Run the whole test suite again"
    polish_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("polish (")]
    assert polish_lines and "test suite" not in polish_lines[0]          # word count, not your words


def test_a_slow_answer_does_not_silence_the_next_outage(daemon, monkeypatch, offline):
    import anthropic

    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    answers = [Dm.P.PolishUnavailable("Ollama took longer than 10 s"), "Fine, ship it today.",
               anthropic.APIConnectionError(request=None)]

    def backend_(system, user, cfg, timeout=None):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(Dm.P, "_claude", backend_)
    notes = []
    monkeypatch.setattr(daemon.feedback, "notify", lambda title, body="", **kw: notes.append((title, body)))
    dictate(daemon, "um so ship it today then")
    dictate(daemon, "um so ship it today then")             # it worked again
    dictate(daemon, "um so ship it today then")             # a real outage: announced
    notes = [n for n in notes if "leanup" in n[0]]
    assert notes == [("chttr: cleanup unavailable", "Ollama took longer than 10 s"),       # no class name
                     ("Cleanup offline", "Typing locally cleaned text until Claude answers again.")]


def test_chat_keeps_line_breaks_when_they_are_typed_safely(daemon, monkeypatch):
    monkeypatch.setattr(Dm, "focused_window", lambda: ("Slack", "general"))
    chat = daemon.cfg.mode_for("Slack")
    assert chat.name == "chat" and chat.newline == "shift+enter"      # the default: breaks never send
    chat.newline = "enter"                                   # Enter would send: folded into one line
    assert dictate(daemon, "hi all new paragraph the build is green") == "Hi all. The build is green"
    chat.newline = "shift+enter"                             # line breaks typed without sending
    assert dictate(daemon, "hi all new paragraph the build is green") == "Hi all\n\nThe build is green"
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "polish", lambda *a, **k: "Hi all,\n\nThe build is green.")
    assert dictate(daemon, "hi all new paragraph the build is green") == "Hi all,\n\nThe build is green."


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


def pill_dictate(d, text, secs=1.0, verb="start"):
    d.transcriber.texts = [text]
    d.recorder.secs = secs
    d.handle(verb)
    d.handle("release")
    wait_idle(d)


def test_overlay_shows_the_whole_dictation(pill):
    pill_dictate(pill, "hello world")
    assert pill.overlay.commands() == ["show ", "state listening", "state transcribing", "text hello world",
                                       "state done"]
    assert pill.typed == ["hello world"]
    assert "Transcribing…" not in pill.feedback.notes and "Typed" not in pill.feedback.notes


def test_overlay_process_is_reused_across_dictations(pill):
    for words in ("one", "two", "three"):
        pill_dictate(pill, words)
    assert len(FakeOverlayProc.spawned) == 1
    assert pill.overlay.commands().count("state done") == 3


def test_overlay_is_started_hidden_with_the_daemon(pill):
    pill._overlay_warm()
    assert len(FakeOverlayProc.spawned) == 1 and pill.overlay.commands() == []
    pill_dictate(pill, "warm")
    assert len(FakeOverlayProc.spawned) == 1
    assert pill.overlay.commands()[0] == "show "


def test_a_tap_hides_the_pill_and_leaves_no_notification(pill):
    pill_dictate(pill, "", secs=0.2)
    assert pill.overlay.commands()[-1] == "hide"
    assert "Transcribing…" not in pill.feedback.notes
    assert not any("No speech" in n for n in pill.feedback.notes)


def test_nothing_heard(pill):
    pill_dictate(pill, "", secs=1.0)
    assert pill.overlay.commands()[-1] == "state nospeech"


def test_command_mode_pill(pill, monkeypatch):
    monkeypatch.setattr(Dm.Daemon, "_run_command", lambda self, *a, **k: "")   # e.g. no model to run it
    pill_dictate(pill, "make it shorter", verb="command")
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
    pill_dictate(pill, "")
    assert pill.overlay.commands()[-1] == "state error"
    assert "chttr error" in pill.feedback.notes


def test_cancel_hides_the_pill(pill):
    pill.handle("start")
    pill.handle("cancel")
    assert pill.overlay.commands()[-1] == "hide"
    assert len(FakeOverlayProc.spawned) == 1


def test_clipboard_fallback_still_notifies(pill, monkeypatch):
    monkeypatch.setattr(Dm, "deliver", lambda text, cfg, **kw: "clipboard")
    pill_dictate(pill, "copied words")
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
    pill_dictate(pill, "one")
    cmd = FakeOverlayProc.spawned[0]
    assert Dm.json.loads(cmd[cmd.index("--palette") + 1]) == theme
    assert not any(c.startswith("palette") for c in pill.overlay.commands())   # unchanged: not resent
    theme["accent"] = "#ff0000"
    pill_dictate(pill, "two")
    assert '"accent": "#ff0000"' in next(c for c in pill.overlay.commands() if c.startswith("palette"))


def test_a_dead_overlay_is_restarted_at_the_next_press(pill):
    pill_dictate(pill, "one")
    pill.overlay.returncode = 1                          # e.g. the compositor restarted
    pill.overlay_spawned_at -= Dm.OVERLAY_RESPAWN
    pill_dictate(pill, "two")
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
    pill_dictate(pill, "one")
    assert "Waveform overlay off" not in pill.feedback.notes     # once could be a fluke
    pill.overlay_spawned_at -= Dm.OVERLAY_RESPAWN
    pill_dictate(pill, "two")
    # After the dictation's own notifications, which share its slot and would replace it at once.
    assert pill.feedback.notes[-3:] == ["Transcribing…", "Typed", "Waveform overlay off"]
    assert all(p.stdin.closed for p in Doomed.procs)    # no BrokenPipeError in the journal at exit
    pill_dictate(pill, "three")                               # within 30 s: no new process, notifications instead
    assert len(Doomed.spawned) == 2
    assert pill.overlay is None
    assert pill.feedback.notes.count("Typed") == 3
    assert pill.typed == ["one", "two", "three"]


def test_overlay_switched_off_by_a_reload_stops_the_process(pill):
    pill_dictate(pill, "one")
    proc = pill.overlay
    pill.cfg.feedback.overlay = False
    pill_dictate(pill, "two")
    assert pill.overlay is None and proc.returncode == 0


def test_a_reload_that_turns_the_overlay_off_stops_it_at_once(pill, monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(C, "CONFIG_FILE", path)
    monkeypatch.setattr(Dm, "engine_key", lambda cfg: pill.transcriber.key)
    pill_dictate(pill, "one")
    proc = pill.overlay
    path.write_text("[feedback]\noverlay = false\n", encoding="utf-8")
    assert pill.handle("reload")["ok"] is True
    assert pill.overlay is None and proc.returncode == 0


def test_live_captions_switched_on_restarts_the_overlay(pill):
    pill_dictate(pill, "one")
    pill.cfg.feedback.live_captions = True
    pill.cfg.engine.streaming = False
    pill_dictate(pill, "two")
    assert len(FakeOverlayProc.spawned) == 2 and "--captions" in FakeOverlayProc.spawned[1]


def test_no_overlay_on_gnome_uses_notifications(pill, monkeypatch):
    monkeypatch.setattr(Dm, "current_desktop", lambda: _desk("gnome"))
    pill_dictate(pill, "hello")
    assert pill.overlay is None and FakeOverlayProc.spawned == []
    assert "Transcribing…" in pill.feedback.notes and "Typed" in pill.feedback.notes
    assert "Waveform overlay off" not in pill.feedback.notes     # expected there, not a fault
    pill_dictate(pill, "", secs=0.2)
    assert pill.feedback.notes.count("Transcribing…") == 1       # not for the tap


def test_without_an_overlay_notifications_still_appear(daemon):
    notes = []
    daemon.feedback.notify = lambda title, body="", **kw: notes.append((title, kw.get("timeout_ms")))
    pill_dictate(daemon, "hello")
    assert ("Transcribing…", 10000) in notes                     # finite: never stuck on screen
    assert any(title == "Typed" for title, _ in notes)
    notes.clear()
    pill_dictate(daemon, "", secs=0.2)
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
    pill_dictate(pill, "hello")
    assert len(FakeOverlayProc.spawned) == 1 and pill.overlay.commands()[-1] == "state done"
    assert "Waveform overlay off" not in pill.feedback.notes


def test_one_overlay_death_is_counted_once(pill):
    pill_dictate(pill, "one")
    proc = pill.overlay
    proc.returncode = 1
    threads = [threading.Thread(target=pill._overlay_lost, args=(proc,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert pill.overlay is None and pill.overlay_losses == 1
    assert proc.stdin.closed


def typed_dictate(d, text, kind="start"):
    d.transcriber.texts = [text]
    d.handle(kind)
    d.handle("release")
    wait_idle(d)


def test_dictation_joins_the_sentence_before_the_cursor(daemon, monkeypatch):
    monkeypatch.setattr(Dm, "focused_window", lambda: ("thunderbird", "Write: Re: Friday"))
    monkeypatch.setattr(Dm.CX, "read_field", lambda *a, **k: Dm.CX.Field("Mail", "as we said", ""))
    typed_dictate(daemon, "Tomorrow works")
    assert daemon.typed == [" tomorrow works"]


def test_command_mode_sorts_lines_without_a_model(daemon, monkeypatch):
    monkeypatch.setattr(Dm, "focused_window", lambda: ("code-oss", "notes.txt"))
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "b\na")
    typed_dictate(daemon, "sort lines", "command")
    assert daemon.typed == ["a\nb"]
    assert "Command mode needs a model" not in daemon.feedback.notes


def test_command_mode_needs_a_model_for_anything_else(daemon, monkeypatch):
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "some selected text")
    typed_dictate(daemon, "make it sound friendlier", "command")
    assert daemon.typed == []
    assert "Command mode needs a model" in daemon.feedback.notes


def test_delete_that_deletes_the_selection(daemon, monkeypatch):
    deleted = []
    monkeypatch.setattr(Dm, "focused_window", lambda: ("thunderbird", "Write"))
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "a sentence I regret")
    monkeypatch.setattr(Dm, "delete_selection", lambda: deleted.append(1))
    typed_dictate(daemon, "delete that", "command")
    assert deleted == [1] and daemon.typed == []
    assert "Deleted" in daemon.feedback.notes


def test_command_mode_logs_no_words_at_info(daemon, monkeypatch, caplog):
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "command", lambda *a, **k: "Secret rewritten text")
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "secret selected text")
    with caplog.at_level("INFO", logger="chttr.daemon"):
        typed_dictate(daemon, "make it sound friendlier", "command")
    assert daemon.typed == ["Secret rewritten text"]
    info = " ".join(r.getMessage() for r in caplog.records if r.levelname == "INFO" and "command" in r.getMessage())
    assert "4 words -> 3 words" in info and "friendlier" not in info and "Secret" not in info


def test_run_it_types_the_command_and_presses_enter(daemon, monkeypatch):
    pressed = []
    monkeypatch.setattr(Dm, "press_enter", lambda delay=0.0: pressed.append(delay))
    typed_dictate(daemon, "git status, run it")
    assert daemon.typed == ["git status"] and pressed == [0.0]


def test_run_it_never_presses_enter_after_the_clipboard_fallback(daemon, monkeypatch):
    pressed = []
    monkeypatch.setattr(Dm, "press_enter", lambda delay=0.0: pressed.append(delay))
    monkeypatch.setattr(Dm, "deliver", lambda text, cfg, **kw: daemon.typed.append(text) or "clipboard")
    typed_dictate(daemon, "git status, run it")
    assert daemon.typed == ["git status"] and pressed == []


def test_run_it_can_be_turned_off(daemon, monkeypatch):
    pressed = []
    monkeypatch.setattr(Dm, "press_enter", lambda delay=0.0: pressed.append(delay))
    daemon.cfg.output.spoken_enter = False
    typed_dictate(daemon, "git status, run it")
    assert daemon.typed == ["git status, run it"] and pressed == []


def test_window_title_picks_the_mode(daemon, monkeypatch):
    seen = []
    monkeypatch.setattr(Dm, "deliver", lambda text, cfg, **kw: seen.append(kw) or "type")
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "polish", lambda text, cfg, app, title, vocab, style="", **k: seen.append(style) or text)
    monkeypatch.setattr(Dm, "focused_window", lambda: ("chromium", "Inbox - Gmail"))
    typed_dictate(daemon, "thanks for the notes from this morning")
    email = daemon.cfg.mode_for("chromium", "Inbox - Gmail")
    assert email.name == "email" and seen[0] == email.style
    assert seen[1]["newline"] == "enter" and seen[1]["allow_paste"] is True


def test_chat_tabs_get_shift_enter_line_breaks(daemon, monkeypatch):
    seen = []
    monkeypatch.setattr(Dm, "deliver", lambda text, cfg, **kw: seen.append(kw) or "type")
    monkeypatch.setattr(Dm, "focused_window", lambda: ("chromium", "Slack | general | Acme"))
    typed_dictate(daemon, "sounds good")
    assert seen[0]["newline"] == "shift+enter"


def test_coding_agents_in_a_terminal_keep_their_sentences(daemon, monkeypatch):
    styles, seen = [], []
    monkeypatch.setattr(Dm, "deliver", lambda text, cfg, **kw: seen.append(kw) or "type")
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "polish", lambda text, cfg, app, title, vocab, style="", **k: styles.append(style) or text)
    monkeypatch.setattr(Dm, "focused_window", lambda: ("com.mitchellh.ghostty", "✳ Claude Code"))
    typed_dictate(daemon, "Look at the failing test. Then fix it in at main dot py.")
    assert "AI coding agent" in styles[0]
    assert seen[0]["allow_paste"] is False and seen[0]["trailing_space"] is False


def test_saying_only_run_it_presses_enter(daemon, monkeypatch):
    pressed = []
    monkeypatch.setattr(Dm, "press_enter", lambda delay=0.0: pressed.append(delay))
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "polish", lambda *a, **k: pytest.fail("nothing to clean up"))
    typed_dictate(daemon, "Run it.")
    assert daemon.typed == [""] and pressed == [0.0]


def wait_transcribing(d, left: int, timeout=2.0):
    """Until the queued transcripts are down to `left`: the job before has taken its own."""
    end = time.monotonic() + timeout
    while len(d.transcriber.texts) > left and time.monotonic() < end:
        time.sleep(0.005)


def test_a_quick_second_dictation_does_not_join_against_stale_text(daemon, monkeypatch):
    # the field is read at the key press, before the first dictation has been typed into it
    monkeypatch.setattr(Dm, "focused_window", lambda: ("thunderbird", "Write: Re: Friday"))
    monkeypatch.setattr(Dm.CX, "read_field", lambda *a, **k: Dm.CX.Field("Mail", "We met", ""))
    daemon.transcriber.texts = ["Yesterday it rained.", "It was fun."]
    daemon.transcriber.delays = [0.3, 0.0]
    daemon.handle("start")
    daemon.handle("release")
    wait_transcribing(daemon, 1)
    daemon.handle("start")
    daemon.handle("release")
    wait_idle(daemon)
    assert daemon.typed == [" yesterday it rained.", "It was fun."]


def test_saying_only_run_it_says_it_pressed_enter(daemon, monkeypatch):
    learned = []
    monkeypatch.setattr(daemon, "_learn_from_edits", learned.append)
    daemon.cfg.history.learn_from_edits = True
    typed_dictate(daemon, "Run it.")
    assert "Pressed Enter" in daemon.feedback.notes and "Typed" not in daemon.feedback.notes
    time.sleep(0.05)
    assert learned == []


def test_coding_agents_press_enter_only_after_a_pause(daemon, monkeypatch):
    pressed = []
    monkeypatch.setattr(Dm, "press_enter", lambda delay=0.0: pressed.append(delay))
    monkeypatch.setattr(Dm, "focused_window", lambda: ("com.mitchellh.ghostty", "✳ Fix the parser tests"))
    typed_dictate(daemon, "The tests pass but pytest won't run it")
    typed_dictate(daemon, "Check the parser send it")
    assert pressed == []
    typed_dictate(daemon, "Check the parser. Send it.")
    assert daemon.typed[-1].rstrip(".") == "Check the parser" and pressed == [0.0]


def test_delete_that_in_a_terminal_explains_itself(daemon, monkeypatch):
    deleted = []
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "git status")
    monkeypatch.setattr(Dm, "delete_selection", lambda: deleted.append(1))
    typed_dictate(daemon, "delete that", "command")
    assert deleted == [] and daemon.typed == []
    assert "Can't delete in a terminal" in daemon.feedback.notes
    assert "Command mode needs a model" not in daemon.feedback.notes


def test_deleted_counts_one_word(daemon, monkeypatch):
    bodies = []
    monkeypatch.setattr(Dm, "focused_window", lambda: ("thunderbird", "Write"))
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "oops")
    monkeypatch.setattr(daemon.feedback, "notify", lambda title, body="", **kw: bodies.append((title, body)))
    typed_dictate(daemon, "delete that", "command")
    assert ("Deleted", "1 word") in bodies


def test_with_a_model_a_near_miss_goes_to_the_model(daemon, monkeypatch):
    asked = []
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "command", lambda instruction, *a, **k: asked.append(instruction) or "ab")
    monkeypatch.setattr(Dm, "focused_window", lambda: ("code-oss", "notes.txt"))
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "a b")
    typed_dictate(daemon, "remove spaces", "command")
    assert asked == ["remove spaces"] and daemon.typed == ["ab"]


def test_delete_that_waits_for_the_dictation_before_it(daemon, monkeypatch):
    events = []
    monkeypatch.setattr(Dm, "focused_window", lambda: ("thunderbird", "Write"))
    monkeypatch.setattr(Dm, "deliver", lambda text, cfg, **kw: events.append(("type", text)) or "type")
    monkeypatch.setattr(Dm, "delete_selection", lambda: events.append(("delete", "")))
    daemon.transcriber.texts = ["first words", "delete that"]
    daemon.transcriber.delays = [0.3, 0.0]
    daemon.handle("start")
    daemon.handle("release")
    wait_transcribing(daemon, 1)
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "old text")
    daemon.handle("command")
    daemon.handle("release")
    wait_idle(daemon)
    assert events == [("type", "First words"), ("delete", "")]      # tidied: no model in the fixture


def test_saying_only_run_it_shows_on_the_overlay(pill):
    pill_dictate(pill, "Run it.")
    assert pill.overlay.commands()[-2:] == ["text Pressed Enter", "state done"]
    assert "Pressed Enter" not in pill.feedback.notes


# ---- "Record this call?" notifications -------------------------------------

class FakeSession:
    def __init__(self, cfg, code, title, source):
        self.code, self.title, self.source = code, title, source
        self.dir = "meeting-dir"
        self.recorder = type("R", (), {"started": 0.0})()
        self.elapsed = 0.0

    def start(self):
        pass

    def discard(self):
        pass


@pytest.fixture
def fake_sessions(daemon, monkeypatch, tmp_path):
    made: list[FakeSession] = []
    monkeypatch.setattr(Dm, "Session", lambda *a: made.append(FakeSession(*a)) or made[-1])
    monkeypatch.setattr(C, "MEETING_STATE_FILE", tmp_path / "meeting.json")
    return made


def test_ask_to_record_app_offers_a_click_that_starts_the_meeting(daemon, monkeypatch):
    from chttr import gcal

    monkeypatch.setattr(gcal, "read_urls", lambda *a: [])
    monkeypatch.setattr(gcal, "find_event", lambda *a: None)
    monkeypatch.setattr(daemon, "_calendar_title", lambda: "")
    daemon._ask_to_record_app("Zoom")
    [offer] = daemon.feedback.offers
    assert offer["title"] == "Record \u201cthis Zoom call\u201d?"
    label, argv = offer["primary"]
    assert label == "Record"
    assert argv[-6:] == ["meeting", "start", "--title", "Zoom call", "--source", "app"]
    assert offer["glyph"]


def test_ask_to_record_meet_call_names_the_call(daemon, monkeypatch):
    from chttr import gcal

    monkeypatch.setattr(gcal, "read_urls", lambda *a: [])
    monkeypatch.setattr(gcal, "find_event", lambda *a: None)
    daemon._ask_to_record("abc-defg-hij", "Weekly sync")
    [offer] = daemon.feedback.offers
    assert offer["title"] == "Record \u201cWeekly sync\u201d?"
    assert offer["primary"][1][-4:] == ["meeting", "start", "--call", "abc-defg-hij"]


def fake_mic(monkeypatch, app):
    from chttr import calls as K

    monkeypatch.setattr(K, "snapshot", lambda *a, **k: [])
    monkeypatch.setattr(K, "current_call", lambda users, meet_call_active: app)


def test_meeting_start_takes_title_and_source_as_json(daemon, fake_sessions, monkeypatch):
    fake_mic(monkeypatch, "Zoom")
    reply = daemon.handle('meeting start {"title": "Zoom call", "source": "app"}')
    assert reply["ok"], reply
    [session] = fake_sessions
    assert (session.title, session.source) == ("Zoom call", "app")   # case kept
    assert daemon.meeting is session


def test_record_click_after_the_app_call_ended_records_nothing(daemon, fake_sessions, monkeypatch):
    # Omarchy keeps old toasts clickable in its history; nothing would stop this recording
    fake_mic(monkeypatch, None)
    reply = daemon.handle('meeting start {"title": "Zoom call", "source": "app"}')
    assert not reply["ok"] and reply["error"] == "that call has ended"
    assert not fake_sessions and daemon.meeting is None
    assert "That call has ended" in daemon.feedback.notes


def test_record_click_counts_a_failed_mic_check_as_no_call(daemon, fake_sessions, monkeypatch):
    from chttr import calls as K

    def broken(*a, **k):
        raise RuntimeError("pw-dump went away")

    monkeypatch.setattr(K, "snapshot", broken)
    assert not daemon.handle('meeting start {"title": "Zoom call", "source": "app"}')["ok"]
    assert not fake_sessions


def test_meeting_start_alone_still_records_by_hand(daemon, fake_sessions):
    assert daemon.handle("meeting START")["ok"]
    [session] = fake_sessions
    assert (session.title, session.source) == ("", "manual")


def test_meeting_start_rejects_bad_options(daemon, fake_sessions):
    reply = daemon.handle("meeting start {not json")
    assert not reply["ok"] and "bad meeting start options" in reply["error"]
    assert not fake_sessions


def test_record_click_after_the_meet_call_ended_records_nothing(daemon, fake_sessions):
    reply = daemon.handle('meeting start {"call": "abc-defg-hij"}')
    assert not reply["ok"]
    assert not fake_sessions
    assert "That call has ended" in daemon.feedback.notes


def test_record_click_during_the_meet_call_records_it(daemon, fake_sessions):
    daemon.call = {"code": "abc-defg-hij", "title": "Weekly sync", "seen": time.time()}
    assert daemon.handle('meeting start {"call": "abc-defg-hij"}')["ok"]
    [session] = fake_sessions
    assert (session.code, session.title, session.source) == ("abc-defg-hij", "Weekly sync", "extension")
