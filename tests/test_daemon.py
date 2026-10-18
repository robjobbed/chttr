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


def test_meeting_start_takes_title_and_source_as_json(daemon, fake_sessions):
    reply = daemon.handle('meeting start {"title": "Zoom call", "source": "app"}')
    assert reply["ok"], reply
    [session] = fake_sessions
    assert (session.title, session.source) == ("Zoom call", "app")   # case kept
    assert daemon.meeting is session


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
