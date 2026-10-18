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


def dictate(d, text, kind="start"):
    d.transcriber.texts = [text]
    d.handle(kind)
    d.handle("release")
    wait_idle(d)


def test_dictation_joins_the_sentence_before_the_cursor(daemon, monkeypatch):
    monkeypatch.setattr(Dm, "focused_window", lambda: ("thunderbird", "Write: Re: Friday"))
    monkeypatch.setattr(Dm.CX, "read_field", lambda *a, **k: Dm.CX.Field("Mail", "as we said", ""))
    dictate(daemon, "Tomorrow works")
    assert daemon.typed == [" tomorrow works"]


def test_command_mode_sorts_lines_without_a_model(daemon, monkeypatch):
    monkeypatch.setattr(Dm, "focused_window", lambda: ("code-oss", "notes.txt"))
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "b\na")
    dictate(daemon, "sort lines", "command")
    assert daemon.typed == ["a\nb"]
    assert "Command mode needs a model" not in daemon.feedback.notes


def test_command_mode_needs_a_model_for_anything_else(daemon, monkeypatch):
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "some selected text")
    dictate(daemon, "make it sound friendlier", "command")
    assert daemon.typed == []
    assert "Command mode needs a model" in daemon.feedback.notes


def test_delete_that_deletes_the_selection(daemon, monkeypatch):
    deleted = []
    monkeypatch.setattr(Dm, "focused_window", lambda: ("thunderbird", "Write"))
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "a sentence I regret")
    monkeypatch.setattr(Dm, "delete_selection", lambda: deleted.append(1))
    dictate(daemon, "delete that", "command")
    assert deleted == [1] and daemon.typed == []
    assert "Deleted" in daemon.feedback.notes


def test_command_mode_logs_no_words_at_info(daemon, monkeypatch, caplog):
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "command", lambda *a, **k: "Secret rewritten text")
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "secret selected text")
    with caplog.at_level("INFO", logger="chttr.daemon"):
        dictate(daemon, "make it sound friendlier", "command")
    assert daemon.typed == ["Secret rewritten text"]
    info = " ".join(r.getMessage() for r in caplog.records if r.levelname == "INFO" and "command" in r.getMessage())
    assert "4 words -> 3 words" in info and "friendlier" not in info and "Secret" not in info


def test_run_it_types_the_command_and_presses_enter(daemon, monkeypatch):
    pressed = []
    monkeypatch.setattr(Dm, "press_enter", lambda delay=0.0: pressed.append(delay))
    dictate(daemon, "git status, run it")
    assert daemon.typed == ["git status"] and pressed == [0.0]


def test_run_it_never_presses_enter_after_the_clipboard_fallback(daemon, monkeypatch):
    pressed = []
    monkeypatch.setattr(Dm, "press_enter", lambda delay=0.0: pressed.append(delay))
    monkeypatch.setattr(Dm, "deliver", lambda text, cfg, **kw: daemon.typed.append(text) or "clipboard")
    dictate(daemon, "git status, run it")
    assert daemon.typed == ["git status"] and pressed == []


def test_run_it_can_be_turned_off(daemon, monkeypatch):
    pressed = []
    monkeypatch.setattr(Dm, "press_enter", lambda delay=0.0: pressed.append(delay))
    daemon.cfg.output.spoken_enter = False
    dictate(daemon, "git status, run it")
    assert daemon.typed == ["git status, run it"] and pressed == []


def test_window_title_picks_the_mode(daemon, monkeypatch):
    seen = []
    monkeypatch.setattr(Dm, "deliver", lambda text, cfg, **kw: seen.append(kw) or "type")
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "polish", lambda text, cfg, app, title, vocab, style="", **k: seen.append(style) or text)
    monkeypatch.setattr(Dm, "focused_window", lambda: ("chromium", "Inbox - Gmail"))
    dictate(daemon, "thanks for the notes from this morning")
    email = daemon.cfg.mode_for("chromium", "Inbox - Gmail")
    assert email.name == "email" and seen[0] == email.style
    assert seen[1]["newline"] == "enter" and seen[1]["allow_paste"] is True


def test_chat_tabs_get_shift_enter_line_breaks(daemon, monkeypatch):
    seen = []
    monkeypatch.setattr(Dm, "deliver", lambda text, cfg, **kw: seen.append(kw) or "type")
    monkeypatch.setattr(Dm, "focused_window", lambda: ("chromium", "Slack | general | Acme"))
    dictate(daemon, "sounds good")
    assert seen[0]["newline"] == "shift+enter"


def test_coding_agents_in_a_terminal_keep_their_sentences(daemon, monkeypatch):
    styles, seen = [], []
    monkeypatch.setattr(Dm, "deliver", lambda text, cfg, **kw: seen.append(kw) or "type")
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "polish", lambda text, cfg, app, title, vocab, style="", **k: styles.append(style) or text)
    monkeypatch.setattr(Dm, "focused_window", lambda: ("com.mitchellh.ghostty", "✳ Claude Code"))
    dictate(daemon, "Look at the failing test. Then fix it in at main dot py.")
    assert "AI coding agent" in styles[0]
    assert seen[0]["allow_paste"] is False and seen[0]["trailing_space"] is False


def test_saying_only_run_it_presses_enter(daemon, monkeypatch):
    pressed = []
    monkeypatch.setattr(Dm, "press_enter", lambda delay=0.0: pressed.append(delay))
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "polish", lambda *a, **k: pytest.fail("nothing to clean up"))
    dictate(daemon, "Run it.")
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
    dictate(daemon, "Run it.")
    assert "Pressed Enter" in daemon.feedback.notes and "Typed" not in daemon.feedback.notes
    time.sleep(0.05)
    assert learned == []


def test_coding_agents_press_enter_only_after_a_pause(daemon, monkeypatch):
    pressed = []
    monkeypatch.setattr(Dm, "press_enter", lambda delay=0.0: pressed.append(delay))
    monkeypatch.setattr(Dm, "focused_window", lambda: ("com.mitchellh.ghostty", "✳ Fix the parser tests"))
    dictate(daemon, "The tests pass but pytest won't run it")
    dictate(daemon, "Check the parser send it")
    assert pressed == []
    dictate(daemon, "Check the parser. Send it.")
    assert daemon.typed[-1].rstrip(".") == "Check the parser" and pressed == [0.0]


def test_delete_that_in_a_terminal_explains_itself(daemon, monkeypatch):
    deleted = []
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "git status")
    monkeypatch.setattr(Dm, "delete_selection", lambda: deleted.append(1))
    dictate(daemon, "delete that", "command")
    assert deleted == [] and daemon.typed == []
    assert "Can't delete in a terminal" in daemon.feedback.notes
    assert "Command mode needs a model" not in daemon.feedback.notes


def test_deleted_counts_one_word(daemon, monkeypatch):
    bodies = []
    monkeypatch.setattr(Dm, "focused_window", lambda: ("thunderbird", "Write"))
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "oops")
    monkeypatch.setattr(daemon.feedback, "notify", lambda title, body="", **kw: bodies.append((title, body)))
    dictate(daemon, "delete that", "command")
    assert ("Deleted", "1 word") in bodies


def test_with_a_model_a_near_miss_goes_to_the_model(daemon, monkeypatch):
    asked = []
    monkeypatch.setattr(Dm.P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(Dm.P, "command", lambda instruction, *a, **k: asked.append(instruction) or "ab")
    monkeypatch.setattr(Dm, "focused_window", lambda: ("code-oss", "notes.txt"))
    monkeypatch.setattr(Dm, "grab_selection", lambda terminal: "a b")
    dictate(daemon, "remove spaces", "command")
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
    assert events == [("type", "first words"), ("delete", "")]
