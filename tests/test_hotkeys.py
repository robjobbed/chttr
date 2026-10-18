from __future__ import annotations

import os
import struct
import threading

import pytest

from chttr import hotkeys as H
from chttr.config import HotkeysCfg

INSERT, LALT, LMETA, LSHIFT = 110, 56, 125, 42


@pytest.mark.parametrize(("text", "mods", "key", "portal"), [
    ("Insert", set(), "insert", "Insert"),
    ("Alt+Insert", {"alt"}, "insert", "ALT+Insert"),
    ("super + shift + insert", {"super", "shift"}, "insert", "SHIFT+LOGO+Insert"),
    ("Ctrl+F9", {"ctrl"}, "f9", "CTRL+F9"),
    ("Meta+Pause", {"super"}, "pause", "LOGO+Pause"),
])
def test_trigger_parsing(text, mods, key, portal):
    t = H.Trigger.parse(text)
    assert t.mods == frozenset(mods) and t.key == key
    assert t.portal() == portal


@pytest.mark.parametrize("bad", ["", "Hyper+Insert", "Ctrl+Banana"])
def test_bad_triggers_are_rejected(bad):
    with pytest.raises(ValueError):
        H.Trigger.parse(bad)


def test_default_bindings_cover_every_action():
    triggers = H.bindings(HotkeysCfg())
    assert set(triggers) == set(H.ACTIONS)
    assert len({(t.mods, t.key) for t in triggers.values()}) == len(triggers)    # no two actions share a key


def test_an_empty_binding_turns_the_action_off():
    assert "history" not in H.bindings(HotkeysCfg(history=""))


def test_tracker_matches_exact_modifiers_and_pairs_releases():
    tracker = H.KeyTracker(H.bindings(HotkeysCfg()))
    events = [(INSERT, 1), (INSERT, 2), (INSERT, 0),                 # plain: dictate, repeat ignored
              (LALT, 1), (INSERT, 1), (LALT, 0), (INSERT, 0),          # alt released first: still "command" up
              (LMETA, 1), (LSHIFT, 1), (INSERT, 1), (INSERT, 0), (LSHIFT, 0), (LMETA, 0)]
    hits = [h for code, value in events if (h := tracker.feed(code, value))]
    assert hits == [("dictate", True), ("dictate", False), ("command", True), ("command", False),
                    ("history", True), ("history", False)]


def test_other_keys_and_unbound_combos_are_ignored():
    tracker = H.KeyTracker(H.bindings(HotkeysCfg()))
    assert tracker.feed(30, 1) is None                                # "a"
    tracker.feed(29, 1)                                               # ctrl held
    assert tracker.feed(INSERT, 1) is None                            # Ctrl+Insert is not bound
    assert tracker.feed(INSERT, 0) is None


def test_key_bitmap_parsing():
    # bit 110 lives in the second 64-bit word (index 1, bit 46)
    words = [hex(1 << 46)[2:], "0"]           # most significant word first, as the kernel prints it
    assert H.key_bit(words, 110)
    assert not H.key_bit(words, 30)
    assert not H.key_bit(["0"], 110)


def test_decode_reads_whole_input_events():
    buf = H.EVENT.pack(0, 0, 1, INSERT, 1) + H.EVENT.pack(0, 0, 0, 0, 0) + b"\x00" * 5
    assert list(H.decode(buf)) == [(1, INSERT, 1), (0, 0, 0)]


@pytest.mark.skipif(os.name != "posix", reason="needs a pipe that select() accepts")
def test_run_evdev_end_to_end_over_a_pipe(monkeypatch, tmp_path):
    r, w = os.pipe()
    fifo = tmp_path / "event0"
    monkeypatch.setattr(H.os, "open", lambda path, flags: r)
    got, stop = [], threading.Event()

    def on_event(action, pressed):
        got.append((action, pressed))
        if len(got) == 2:
            stop.set()

    t = threading.Thread(target=H.run_evdev, args=(H.bindings(HotkeysCfg()), on_event, stop, [fifo]))
    t.start()
    os.write(w, H.EVENT.pack(0, 0, 1, INSERT, 1) + H.EVENT.pack(0, 0, 1, INSERT, 0))
    t.join(3)
    os.close(w)
    assert got == [("dictate", True), ("dictate", False)]


def test_portal_shortcut_list():
    shortcuts = H.portal_shortcuts({"dictate": H.Trigger.parse("Insert")})
    assert shortcuts == [("dictate", {"description": ("s", "chttr: " + H.DESCRIPTIONS["dictate"]),
                                      "preferred_trigger": ("s", "Insert")})]


def test_resolve(monkeypatch):
    monkeypatch.setattr(H, "compositor_has_bindings", lambda c: c == "hyprland")
    monkeypatch.setattr(H, "portal_available", lambda: True)
    monkeypatch.setattr(H, "readable_keyboards", lambda: [])
    assert H.resolve("auto", "hyprland") == "compositor"
    assert H.resolve("auto", "gnome") == "portal"
    assert H.resolve("auto", "niri") == "none"
    monkeypatch.setattr(H, "readable_keyboards", lambda: ["/dev/input/event3"])
    assert H.resolve("auto", "niri") == "evdev"
    assert H.resolve("evdev", "gnome") == "evdev"
    monkeypatch.setattr(H, "portal_available", lambda: False)
    assert H.resolve("auto", "kde") == "evdev"


def test_daemon_hotkey_dispatch(monkeypatch):
    """A quick tap through the in-daemon hotkeys still types: the release is handled
    after the press has been marked as started."""
    from chttr import config as C
    from chttr import daemon as Dm
    from tests.test_daemon import FakeRecorder, FakeTranscriber, QuietFeedback

    monkeypatch.setattr(Dm, "Recorder", FakeRecorder)
    monkeypatch.setattr(Dm, "Transcriber", FakeTranscriber)
    monkeypatch.setattr(Dm, "Feedback", QuietFeedback)
    typed = []
    monkeypatch.setattr(Dm, "deliver", lambda text, cfg, **kw: typed.append(text) or "type")
    gate = threading.Event()
    monkeypatch.setattr(Dm, "focused_window", lambda: (gate.wait(1), ("kitty", "~"))[1])
    cfg = C.Config(modes=C._default_modes())
    cfg.audio.unmute = cfg.feedback.overlay = cfg.history.enabled = False
    cfg.polish.backend = "none"
    d = Dm.Daemon(cfg)
    d.transcriber.texts = ["quick"]
    d.on_hotkey("dictate", True)
    d.on_hotkey("dictate", False)          # arrives while the press is still reading the window
    gate.set()
    for _ in range(200):
        if typed:
            break
        threading.Event().wait(0.01)
    assert typed == ["quick"]


@pytest.mark.skipif(struct.calcsize("l") != 8, reason="input_event layout differs on 32-bit")
def test_input_event_is_24_bytes_on_64_bit_linux():
    assert H.EVENT.size == 24


def test_compositor_bindings_anywhere_prevent_double_presses(monkeypatch):
    monkeypatch.setattr(H, "compositor_has_bindings", lambda c: c == "hyprland")
    monkeypatch.setattr(H, "readable_keyboards", lambda: ["/dev/input/event3"])
    # even if detection misses Hyprland, its chttr bindings mean: don't also read the keyboard
    assert H.resolve("auto", "wlroots") == "compositor"


# ---- effective_bindings: the keys that are really bound ------------------------------------

DEFAULT_KEYS = {"dictate": "Insert", "command": "Alt+Insert", "hands_free": "Super+Insert",
                "history": "Super+Shift+Insert", "meeting": "Super+Alt+Insert"}


def _desk(compositor):
    from chttr.desktop import Desktop

    return Desktop("wayland", compositor)


def _cfg(backend="compositor"):
    from chttr import config as C

    cfg = C.Config()
    cfg.hotkeys.backend = backend
    return cfg


@pytest.mark.parametrize(("rel", "block", "compositor"), [
    (".config/hypr/bindings.lua", "BINDINGS_LUA", "hyprland"),
    (".config/hypr/bindings.conf", "BINDINGS_CONF", "hyprland"),
    (".config/sway/config.d/chttr.conf", "SWAY_BINDINGS", "sway"),
])
def test_effective_bindings_read_what_setup_writes(tmp_path, rel, block, compositor):
    from chttr import install

    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text(getattr(install, block).format(exe="/home/me/.local/bin/chttr"))
    assert H.effective_bindings(_cfg(), _desk(compositor), home=tmp_path) == DEFAULT_KEYS


def test_effective_bindings_follow_keys_the_user_changed(tmp_path):
    (tmp_path / ".config/hypr").mkdir(parents=True)
    (tmp_path / ".config/hypr/bindings.lua").write_text(
        'o.bind("F9", "chttr: push-to-talk", "chttr start")\n'
        'o.bind("F9", "chttr: push-to-talk (release)", "chttr release", { release = true })\n'
        "-- o.bind(\"F8\", \"old\", \"chttr command\")\n"
        "o.bind('CTRL + SHIFT + space', 'chttr: command', 'uwsm app -- chttr command')\n")
    (tmp_path / ".config/hypr/bindings.conf").write_text(
        "$mainMod = SUPER\nbind = $mainMod, F10, exec, ~/.local/bin/chttr meeting  toggle\n"
        "bindr = , F9, exec, chttr release\n")
    got = H.effective_bindings(_cfg(), _desk("hyprland"), home=tmp_path)
    assert got == {"dictate": "F9", "command": "Ctrl+Shift+Space", "meeting": "Super+F10"}


def test_effective_bindings_on_sway_main_config(tmp_path):
    (tmp_path / ".config/sway").mkdir(parents=True)
    (tmp_path / ".config/sway/config").write_text(
        "bindsym --to-code Mod4+Mod1+Pause exec chttr toggle\nbindsym --release Pause exec chttr release\n"
        "bindsym Pause exec chttr start\n")
    got = H.effective_bindings(_cfg(), _desk("sway"), home=tmp_path)
    assert got == {"dictate": "Pause", "hands_free": "Super+Alt+Pause"}


def test_effective_bindings_fall_back_to_the_config(tmp_path):
    cfg = _cfg("portal")
    cfg.hotkeys.meeting = ""
    expected = {k: v for k, v in DEFAULT_KEYS.items() if k != "meeting"}
    assert H.effective_bindings(cfg, _desk("gnome"), home=tmp_path) == expected
    assert H.effective_bindings(_cfg("evdev"), _desk("niri"), home=tmp_path) == DEFAULT_KEYS
    # the compositor backend with no readable bindings: the configured keys are the best guess
    assert H.effective_bindings(_cfg(), _desk("hyprland"), home=tmp_path) == DEFAULT_KEYS
    assert H.effective_bindings(_cfg("none"), _desk("niri"), home=tmp_path) == {}


def test_key_labels_are_normalised():
    assert H.key_label(["Mod1", "Insert"]) == "Alt+Insert"
    assert H.key_label(["SUPER", "SHIFT", "INSERT"]) == "Super+Shift+Insert"
    assert H.key_label(["mod4", "f12"]) == "Super+F12"
