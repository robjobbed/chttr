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


# ---- pre-meeting brief -----------------------------------------------------
BRIEF_LOOP = Dm.Daemon._brief_loop


@pytest.fixture
def no_brief_thread(monkeypatch):
    """The daemon's own brief thread stays out of the way: the tests tick by hand."""
    monkeypatch.setattr(Dm.Daemon, "_brief_loop", lambda self: None)


class Minutes:
    """A stand-in for daemon._stop: `wait` lets `n` minutes go by, then says stop."""

    def __init__(self, n, between=None):
        self.n, self.between = n, between

    def wait(self, timeout=None):
        self.n -= 1
        if self.between and self.n >= 0:
            self.between()
        return self.n < 0

    def set(self):
        self.n = 0

    def is_set(self):
        return self.n <= 0


@pytest.fixture
def briefing(no_brief_thread, daemon, monkeypatch):
    """A calendar with one meeting with Dana in four minutes, and notes from last time."""
    from datetime import UTC, datetime, timedelta

    from chttr import brief, calls, gcal

    start = datetime.now(UTC) + timedelta(minutes=4, seconds=10)
    event = gcal.Event("Weekly sync", start, start + timedelta(minutes=30),
                       attendees=[gcal.Attendee("Dana Lee", "dana@example.com"),
                                  gcal.Attendee("", "rob@example.com", is_me=True)])
    last = {"id": "m1", "started": time.time() - 7 * 86400, "title": "Weekly sync",
            "attendee_emails": ["dana@example.com"], "speaker_emails": [], "me": ["Rob"],
            "action_items": [{"owner": "Rob", "task": "Send Dana the pricing sheet", "due": ""}],
            "markdown": "/notes/m1.md", "summary": "Pricing page ships first."}
    events = [event]
    fetched: list[str] = []
    monkeypatch.setattr(gcal, "read_urls", lambda path: ["https://calendar.google.com/x/basic.ics"])
    monkeypatch.setattr(gcal, "_fetch", lambda url, max_age=600: fetched.append(url) or b"")
    monkeypatch.setattr(gcal, "_events", lambda data, around, window: (list(events), ""))
    monkeypatch.setattr(brief, "past_meetings", lambda: [last])
    monkeypatch.setattr(calls, "snapshot", lambda timeout=3.0: [])
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(daemon.feedback, "notify", lambda title, body="", **kw: sent.append((title, body)))
    daemon.sent, daemon.events, daemon.fetched = sent, events, fetched
    return daemon


def test_brief_notifies_once_per_event(briefing):
    assert briefing._brief_tick() == 1
    title, body = briefing.sent[0]
    assert title == "Weekly sync in 4 min"
    assert body.splitlines()[0].startswith("Last met ") and body.endswith("• You: Send Dana the pricing sheet")
    assert briefing._brief_tick() == 0                    # a minute later: nothing new
    assert len(briefing.sent) == 1


def test_brief_reads_the_calendar_every_few_minutes_not_every_minute(briefing):
    for _ in range(4):
        briefing._brief_tick()
    assert len(briefing.fetched) == 1
    briefing.cfg.meeting.brief_minutes = 10                # a reload: read again at once
    briefing._brief_tick()
    assert len(briefing.fetched) == 2
    read_at, key, events = briefing._brief_events
    briefing._brief_events = (read_at - 301, key, events)  # five minutes on
    briefing._brief_tick()
    assert len(briefing.fetched) == 3


def test_brief_waits_until_brief_minutes_before(briefing):
    briefing.cfg.meeting.brief_minutes = 2
    assert briefing._brief_tick() == 0 and not briefing.sent
    briefing.cfg.meeting.brief_minutes = 0                 # off after a reload
    assert briefing._brief_tick() == 0


def test_brief_skips_events_without_people_and_stays_discreet_in_a_call(briefing):
    from datetime import timedelta

    from chttr import gcal

    focus = briefing.events[0]
    briefing.events[:] = [gcal.Event("Focus time", focus.start, focus.end),
                          gcal.Event("Retro", focus.start + timedelta(seconds=5), focus.end,
                                     attendees=[gcal.Attendee("Dana Lee", "dana@example.com")])]
    briefing.call = {"code": "abc-defg-hij", "title": "Another call", "seen": time.time()}
    assert briefing._brief_tick() == 1
    assert briefing.sent == [("Retro in 4 min", "Run chttr brief to see what you agreed last time.")]


def test_brief_stays_discreet_in_a_zoom_call(briefing, monkeypatch):
    from chttr import calls

    monkeypatch.setattr(calls, "current_call", lambda users, meet_call_active: "Zoom")
    assert briefing._brief_tick() == 1
    assert briefing.sent[0][1] == "Run chttr brief to see what you agreed last time."


def test_brief_loop_waits_for_a_calendar_added_later(briefing, monkeypatch):
    from chttr import gcal

    urls: list[str] = []
    monkeypatch.setattr(gcal, "read_urls", lambda path: list(urls))
    # minute one: no calendar yet; then `chttr meeting calendar URL`
    briefing._stop = Minutes(1, between=lambda: urls.append("https://calendar.google.com/x/basic.ics"))
    BRIEF_LOOP(briefing)
    assert [t for t, _ in briefing.sent] == ["Weekly sync in 4 min"]


def test_brief_loop_ends_when_the_daemon_stops(briefing):
    briefing._stop = Minutes(0)
    briefing.cfg.meeting.brief_minutes = 0
    started = time.monotonic()
    BRIEF_LOOP(briefing)                                   # one tick (off: nothing), then stop
    assert time.monotonic() - started < 1 and not briefing.sent


def test_brief_loop_survives_an_offline_calendar(briefing, monkeypatch):
    from chttr import gcal

    briefing._stop = Minutes(0)                            # one pass, then the daemon is stopping
    BRIEF_LOOP(briefing)
    assert len(briefing.sent) == 1

    def offline(url, max_age=600):
        raise OSError("network is unreachable")

    monkeypatch.setattr(gcal, "_fetch", offline)
    briefing._brief_events = None
    briefing._stop = Minutes(2)
    BRIEF_LOOP(briefing)                                   # logged, not raised, and still going
    assert len(briefing.sent) == 1 and briefing._stop.n < 0
