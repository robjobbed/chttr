"""Configuration and well-known paths."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

APP = "chttr"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP
CONFIG_FILE = Path(os.environ["CHTTR_CONFIG"]).expanduser() if os.environ.get("CHTTR_CONFIG") else CONFIG_DIR / "config.toml"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{APP}-{os.getuid()}") / APP
SOCKET_PATH = RUNTIME_DIR / "chttr.sock"
STATE_FILE = RUNTIME_DIR / "state"
STATE_JSON = RUNTIME_DIR / "state.json"       # the state plus pid, meeting, cleanup and keys, for the bar
HISTORY_FILE = DATA_DIR / "history.jsonl"
HISTORY_AUDIO_DIR = DATA_DIR / "history-audio"   # only with [history] keep_audio_days > 0
MEETINGS_DATA_DIR = DATA_DIR / "meetings"        # per-meeting audio, events, intermediate results
MEETING_STATE_FILE = RUNTIME_DIR / "meeting"     # present while a meeting is being recorded
MODELS_DIR = DATA_DIR / "models"
VOICES_FILE = DATA_DIR / "voices.json"

# Directories searched for ggml whisper models, in order. voxtype ships with
# Omarchy and usually has already downloaded ggml-base.en.bin, so reuse it.
MODEL_SEARCH_DIRS = [
    Path.home() / ".local/share/voxtype/models",
    Path.home() / ".local/share/pywhispercpp/models",
]
PARAKEET_SEARCH_DIRS = [
    Path.home() / ".local/share/voxtype/models/parakeet-tdt-0.6b-v3-int8",
    DATA_DIR / "models/parakeet-tdt-0.6b-v3-int8",
]

DEFAULT_CONFIG = """\
# chttr configuration. Every key is optional; these are the defaults.
# Apply changes with: chttr reload   (switching engines reloads the model)

[engine]
# "parakeet": NVIDIA Parakeet TDT 0.6B v3, int8 ONNX. Fast and accurate on CPU,
#             reuses the model Omarchy's voxtype downloads. Multilingual.
# "whisper":  whisper.cpp with the [whisper] settings below.
name = "parakeet"
# Directory with encoder-model.int8.onnx / decoder_joint-model.int8.onnx / vocab.txt.
# "" = look in voxtype's model dir, then ~/.local/share/chttr/models.
parakeet_dir = ""
# 0 = auto (half the logical CPUs)
threads = 0
# Recognize while you talk: once you've said a few seconds' worth, the next pause
# is transcribed in the background, so releasing the key leaves only the last
# sentence or so to do. false = one pass over the whole recording after release.
streaming = true

[whisper]
# ggml model name (tiny, base, base.en, small, small.en, medium, large-v3-turbo, ...)
# or an absolute path to a ggml-*.bin file. Looked up in the voxtype and
# pywhispercpp model directories and downloaded if missing.
model = "base.en"
language = "en"

[audio]
# PipeWire source name or serial (pactl list sources short); "" = default input
device = ""
# Safety limit for a single recording
max_seconds = 180
# Unmute the default source while recording (Omarchy's mic-mute toggle is
# easy to leave on); the previous mute state is restored afterwards.
unmute = true
# Hands-free mode (chttr toggle): stop automatically after this much silence
# once you have started speaking, and give up after no_speech_timeout seconds
# if nothing is heard at all.
auto_stop_silence = 1.5
no_speech_timeout = 10
# Levels above this (dBFS, per 20 ms chunk) count as speech
speech_threshold_db = -38
# Whisper mode: for dictating quietly (open office, someone asleep). Lowers the
# speech threshold to -50 dB and boosts quiet recordings before recognition.
whisper_mode = false
# Lower the speech threshold to fit the room: 10 dB above the measured noise floor
# but no lower than 20 dB under your speech, never below -62 dB and never above
# speech_threshold_db. Quiet laptop mics need this.
adaptive_threshold = true
# Double-tap the dictation key to switch to hands-free; tap once more to stop.
double_tap = true

[output]
# "type"      - type into the focused window (wtype, ydotool or xdotool; see typer)
# "paste"     - copy to clipboard and send Ctrl+V to the focused window
# "clipboard" - only copy to the clipboard
mode = "type"
trailing_space = true
fallback_to_clipboard = true
# Typing goes character by character; text longer than this many characters is
# pasted instead (your clipboard is restored right after). Not in terminals.
# 0 = always type.
paste_over = 400
# Typing tool: "auto" (wtype on Hyprland/Sway/niri, ydotool on GNOME/KDE,
# xdotool on X11), or "wtype" | "ydotool" | "xdotool" | "dotool"
typer = "auto"
# In terminals and code editors, ending a dictation with "run it" or "press enter" presses Enter after typing.
# In a coding agent, where Enter sends the prompt, only after a pause: "Fix the tests. Send it."
spoken_enter = true

[polish]
# Wispr Flow-style cleanup: fillers removed, punctuation fixed, self-corrections
# applied, formatting adapted to the focused app. Also powers command mode.
# backend: "auto"   - Claude if a key is available, else Ollama if it is
#                     running, else local cleanup on this machine (fillers,
#                     spoken punctuation, capitals; no model)
#          "claude" | "ollama" | "none"
backend = "auto"
# Dictation cleanup and command mode. Haiku answers in a few hundred milliseconds,
# which is what makes dictation feel instant; any Claude model id works here.
cleanup_model = "claude-haiku-5-5"
# Meeting notes (and cleanup, if cleanup_model = "")
model = "claude-opus-5-5"
effort = "low"
timeout_seconds = 20
# Dictation cleanup waits at most this long for the model; then the locally cleaned text is typed.
cleanup_timeout_seconds = 5
# Short phrases with no fillers, self-corrections or spoken punctuation ("git
# status", "sounds good") are typed as recognized, without a model round trip.
# The longest phrase that can skip cleanup, in words; 0 sends everything.
quick_max_words = 4
# How much the cleanup changes:
#   "light"  - fillers, stutters, punctuation and capitals only; your words stay
#   "medium" - also self-corrections, spoken formatting, obvious mis-hearings
#   "high"   - also tightens wording and fixes grammar, like a careful editor
#   "none"   - no cleanup (the raw transcript, after your dictionary)
level = "medium"
# Text around the cursor (read through the accessibility bus, AT-SPI) helps spell
# names that are on screen:
#   "local" - names near the cursor fix sound-alike words on this machine only
#   "names" - also give those names to the cleanup model as spellings to keep
#   "full"  - also send up to 600 characters before the cursor, for context
#   "off"
screen_context = "local"
# Optional: file containing an Anthropic API key (chmod 600). Used when
# ANTHROPIC_API_KEY is not in the daemon's environment.
api_key_file = "~/.config/chttr/anthropic-api-key"
# Local alternative (fully offline): https://ollama.com, e.g. `ollama pull llama3.2`.
# qwen2.5:1.5b is fast on a CPU laptop; llama3.2 is more accurate.
ollama_model = "llama3.2"
ollama_url = "http://127.0.0.1:11434"
# Tell the model which app is focused (its window class, e.g. "Slack") so it can
# adapt tone and formatting
use_app_context = true
# Also send the window title ("Re: Q3 budget - Mail"). It helps with names and
# context, but titles can contain email subjects and document names.
send_window_title = true

[feedback]
sounds = true
notifications = true
# Floating waveform + dB meter while recording (GTK4 layer-shell overlay)
overlay = true
# Show a rolling transcript in the overlay while you speak
live_captions = true

[history]
# Append every dictation to ~/.local/share/chttr/history.jsonl
enabled = true
# Keep each dictation's audio this many days, to replay it (chttr history --play).
# 0 = never keep audio.
keep_audio_days = 0
# A few seconds after typing, look at the text field again (AT-SPI, on this
# machine). If you corrected a name chttr got wrong, offer to learn it.
learn_from_edits = true

[integrations]
# Meeting notes to Slack, Notion, HubSpot or a webhook (connect with: chttr connect).
#   "ask"    - the notes notification has a "Send to ..." button per destination
#   "always" - send to every connected destination without asking (for meetings
#              whose calendar title matches auto_titles, if set)
#   "never"
send = "ask"
auto_titles = ""
# HubSpot: also create a task per action item, on the attendees' contacts
hubspot_tasks = true
# Your own script after every meeting: gets the Markdown path as $1 and the
# meeting (notes, transcript, speakers) as JSON on stdin. Runs without asking.
on_meeting_done = ""

[hotkeys]
# Who catches the keys:
#   "auto"       - Hyprland/Sway: the compositor's bindings (chttr setup adds them);
#                  GNOME 48+/KDE: the desktop's global-shortcuts portal (you confirm
#                  the keys once, and can change them in the desktop's settings);
#                  anything else: the keyboard devices (needs the "input" group)
#   "compositor" | "portal" | "evdev" | "none"
backend = "auto"
# Keys for the portal and evdev backends ("" turns one off). Modifiers: Ctrl, Alt,
# Shift, Super. Keys: Insert, Pause, ScrollLock, F1-F24, ...
dictate = "Insert"
command = "Alt+Insert"
hands_free = "Super+Insert"
history = "Super+Shift+Insert"
meeting = "Super+Alt+Insert"

[meeting]
# Meeting transcripts (chttr meeting start / stop, or automatic with the Meet
# extension: chttr meeting install). Your mic and the speaker output are
# recorded as separate channels; the other side is split into speakers and
# named from Meet's captions, your calendar and remembered voices.
# Where the Markdown notes go:
dir = "~/Documents/Meetings"
# When the Meet extension reports that you joined a call:
#   "ask"    - notification with a Record button
#   "always" - start recording straight away
#   "never"  - only record with chttr meeting start
# The same applies to calls in other apps when detect_calls is on.
auto = "ask"
# Notice calls in Zoom, Teams, Slack, Discord, Webex, ... (and in the browser, when
# it isn't Google Meet) by which app holds the microphone, and offer to record.
# No bot joins the call. The recording stops when the app lets go of the mic.
detect_calls = true
# Split your microphone's side into voices too, for people in the room with you
# (they show up as "In the room 1", ...; rename them like any speaker).
room_speakers = true
# Keep the audio (stereo Opus: you left, them right) next to the notes
keep_audio = false
# Notes (summary, sections, action items) with the [polish] model (Claude or Ollama); without either, on-device notes
summary = true
summary_effort = "medium"
# Note style: "auto" (Claude picks from the meeting), or a style id such as
# general, brief, bant, meddpicc, spin, customer-success, one-on-one, standup,
# interview, user-research, support, kickoff, retro, brainstorm, decision,
# minutes, investor, lecture. List them with: chttr meeting styles
# Change one meeting's style any time, even after the call: chttr meeting style bant
style = "auto"
# Styles by calendar title (regex, case-insensitive); first match wins, before `style`.
style_rules = [
  { match = '\\b(1:1|1-1|1on1|one[- ]on[- ]one)\\b', style = "one-on-one" },
  { match = '\\b(stand-?up|daily sync|scrum)\\b', style = "standup" },
  { match = '\\bretro(spective)?\\b', style = "retro" },
  { match = '\\binterview\\b', style = "interview" },
]
# Your name in transcripts; "" = from your calendar, else "Me"
me = ""
# Your email address, so you are recognised in calendar invites when the feed is not named after it
email = ""
# File with secret iCal addresses, one per line (Google Calendar > Settings >
# your calendar > "Secret address in iCal format"). Add one with: chttr meeting calendar URL
calendar_file = "~/.config/chttr/calendar-urls"
# Safety limit
max_hours = 4
# A notification this many minutes before a calendar meeting with what you agreed last time and open action items (0 = off)
brief_minutes = 5

# Per-app modes. The first mode that matches the focused window wins: `match` is
# a list of regexes (case-insensitive) for the window's class, `title` a list for
# its title. With both, both must match (Claude Code in a terminal); with only
# `title`, any app whose title matches (a Gmail tab in any browser). `style` is
# extra guidance for the cleanup step; `polish = false` types the raw transcript;
# `output` and `trailing_space` override the [output] section; `code = true`
# turns on spoken file names, flags and symbols; `newline` is how line breaks are
# typed: "enter" or "shift+enter".
[[modes]]
name = "agent"
match = ["alacritty", "kitty", "foot", "wezterm", "ghostty", "xterm", "konsole", "gnome.terminal", "ptyxis", "tilix", "terminator"]
# Claude Code renames the window to the task at hand with a status glyph in front ("✳ Fix the tests")
title = ['^[·✢-✽⠀-⣿]\\s', '(?<![/.])\\b(claude|codex|aider|gemini|opencode)\\b(?![/.-])']
style = "This is a prompt to an AI coding agent in a terminal: complete sentences and paragraphs are fine; keep identifiers, paths and flags exactly as spoken."
trailing_space = false
code = true
# Enter would send the prompt half-written; Shift+Enter starts a new line
newline = "shift+enter"

[[modes]]
name = "terminal"
match = ["alacritty", "kitty", "foot", "wezterm", "ghostty", "xterm", "konsole", "gnome.terminal", "ptyxis", "tilix", "terminator"]
style = "This is a terminal. Output a single line of plain text with no trailing period; keep commands, paths and flags exactly as spoken."
trailing_space = false

[[modes]]
name = "code"
match = ["^code", "cursor", "zed", "neovide", "jetbrains", "idea", "pycharm", "webstorm", "sublime"]
style = "This is a code editor. Keep identifiers, file names and symbols as spoken; use snake_case or camelCase if the user spells them that way. No trailing period unless it is a sentence in a comment."
trailing_space = false

[[modes]]
match = ["slack", "discord", "whatsapp", "signal", "telegram", "element", "messages", "beeper"]
name = "chat"
style = "This is a chat app. Keep it casual and concise; one short message; no sign-off."
# Line breaks are typed as Shift+Enter so a dictated paragraph never sends the message early
newline = "shift+enter"

[[modes]]
name = "email"
match = ["thunderbird", "mail", "gmail", "outlook", "proton", "hey"]
style = "This is an email client. Use complete, well-punctuated sentences and paragraphs where the user pauses between thoughts."

[[modes]]
name = "docs"
match = ["docs.google", "notion", "obsidian", "libreoffice", "writer", "typora", "logseq"]
style = "This is a document editor. Use complete sentences and paragraphs; honour dictated headings and lists."

# The same apps in a browser tab, recognised by the app's name in the tab's title
[[modes]]
name = "docs"
match = ["chromium", "chrome", "firefox", "brave", "vivaldi", "zen", "librewolf"]
title = ['[-–—|] (google docs|notion|confluence|obsidian)\\b']
style = "This is a document editor. Use complete sentences and paragraphs; honour dictated headings and lists."

[[modes]]
name = "email"
match = ["chromium", "chrome", "firefox", "brave", "vivaldi", "zen", "librewolf"]
title = ['[-–—|] (gmail|outlook|proton mail|fastmail)\\b', '^inbox\\b']
style = "This is an email client. Use complete, well-punctuated sentences and paragraphs where the user pauses between thoughts."

[[modes]]
name = "chat"
match = ["chromium", "chrome", "firefox", "brave", "vivaldi", "zen", "librewolf"]
title = ['^(\\(\\d+\\) )?(slack|discord|whatsapp|telegram)\\b', '[-–—|] (slack|discord|whatsapp|telegram)\\b']
style = "This is a chat app. Keep it casual and concise; one short message; no sign-off."
newline = "shift+enter"

[[modes]]
name = "assistant"
match = ["chromium", "chrome", "firefox", "brave", "vivaldi", "zen", "librewolf"]
title = ['claude', 'chatgpt', 'gemini', 'copilot']
style = "This is a chat with an AI assistant: complete sentences; keep code, paths and identifiers exactly as spoken."
"""


@dataclass
class EngineCfg:
    name: str = "parakeet"
    parakeet_dir: str = ""
    threads: int = 0
    streaming: bool = True


@dataclass
class WhisperCfg:
    model: str = "base.en"
    language: str = "en"
    threads: int = 0


@dataclass
class AudioCfg:
    device: str = ""
    max_seconds: int = 180
    unmute: bool = True
    auto_stop_silence: float = 1.5
    no_speech_timeout: float = 10
    speech_threshold_db: float = -38
    whisper_mode: bool = False
    adaptive_threshold: bool = True
    double_tap: bool = True

    @property
    def threshold_db(self) -> float:
        """The speech threshold in effect (whisper mode lowers it)."""
        return min(self.speech_threshold_db, -50.0) if self.whisper_mode else self.speech_threshold_db


@dataclass
class OutputCfg:
    mode: str = "type"
    trailing_space: bool = True
    fallback_to_clipboard: bool = True
    paste_over: int = 400
    typer: str = "auto"
    spoken_enter: bool = True


@dataclass
class PolishCfg:
    backend: str = "auto"
    cleanup_model: str = "claude-haiku-5-5"
    model: str = "claude-opus-5-5"
    effort: str = "low"
    timeout_seconds: float = 20
    cleanup_timeout_seconds: float = 5
    quick_max_words: int = 4
    level: str = "medium"
    screen_context: str = "local"
    api_key_file: str = "~/.config/chttr/anthropic-api-key"
    ollama_model: str = "llama3.2"
    ollama_url: str = "http://127.0.0.1:11434"
    use_app_context: bool = True
    send_window_title: bool = True
    # legacy key, maps to backend = "none" when false
    enabled: bool = True


@dataclass
class FeedbackCfg:
    sounds: bool = True
    notifications: bool = True
    overlay: bool = True
    live_captions: bool = True


@dataclass
class HistoryCfg:
    enabled: bool = True
    keep_audio_days: int = 0
    learn_from_edits: bool = True


@dataclass
class IntegrationsCfg:
    send: str = "ask"
    auto_titles: str = ""
    hubspot_tasks: bool = True
    on_meeting_done: str = ""


@dataclass
class HotkeysCfg:
    backend: str = "auto"
    dictate: str = "Insert"
    command: str = "Alt+Insert"
    hands_free: str = "Super+Insert"
    history: str = "Super+Shift+Insert"
    meeting: str = "Super+Alt+Insert"


@dataclass
class MeetingCfg:
    dir: str = "~/Documents/Meetings"
    auto: str = "ask"
    detect_calls: bool = True
    room_speakers: bool = True
    keep_audio: bool = False
    summary: bool = True
    summary_effort: str = "medium"
    style: str = "auto"
    style_rules: list = field(default_factory=lambda: [
        {"match": r"\b(1:1|1-1|1on1|one[- ]on[- ]one)\b", "style": "one-on-one"},
        {"match": r"\b(stand-?up|daily sync|scrum)\b", "style": "standup"},
        {"match": r"\bretro(spective)?\b", "style": "retro"},
        {"match": r"\binterview\b", "style": "interview"},
    ])
    me: str = ""
    email: str = ""
    calendar_file: str = "~/.config/chttr/calendar-urls"
    max_hours: float = 4
    brief_minutes: int = 5


@dataclass
class Mode:
    name: str = ""
    match: list[str] = field(default_factory=list)
    style: str = ""
    polish: bool = True
    output: str = ""              # "" = use [output].mode
    trailing_space: bool | None = None
    code: bool = False            # spoken file references ("at main dot py" -> @main.py)
    title: list[str] = field(default_factory=list)   # regexes for the window title
    newline: str = "enter"        # "enter" | "shift+enter" (chat apps: Enter sends)

    @property
    def is_code(self) -> bool:
        return self.code or self.name in ("code", "terminal")

    @property
    def in_terminal(self) -> bool:
        """Terminals: no Ctrl+C to copy (it interrupts), no Ctrl+V to paste."""
        return self.name in ("terminal", "agent")

    def matches(self, window_class: str, title: str = "") -> bool:
        """`match` against the class and `title` against the title; a mode with only
        `title` patterns matches any app with that title."""
        if not self.match and not self.title:
            return False
        if self.match and not any(re.search(p, window_class, re.IGNORECASE) for p in self.match):
            return False
        return not self.title or any(re.search(p, title, re.IGNORECASE) for p in self.title)


@dataclass
class Config:
    engine: EngineCfg = field(default_factory=EngineCfg)
    whisper: WhisperCfg = field(default_factory=WhisperCfg)
    audio: AudioCfg = field(default_factory=AudioCfg)
    output: OutputCfg = field(default_factory=OutputCfg)
    polish: PolishCfg = field(default_factory=PolishCfg)
    feedback: FeedbackCfg = field(default_factory=FeedbackCfg)
    history: HistoryCfg = field(default_factory=HistoryCfg)
    hotkeys: HotkeysCfg = field(default_factory=HotkeysCfg)
    integrations: IntegrationsCfg = field(default_factory=IntegrationsCfg)
    meeting: MeetingCfg = field(default_factory=MeetingCfg)
    modes: list[Mode] = field(default_factory=list)

    def mode_for(self, window_class: str, title: str = "") -> Mode | None:
        if not window_class and not title:
            return None
        for m in self.modes:
            if m.matches(window_class, title):
                return m
        return None


def _fill(obj, data: dict) -> None:
    for key, value in data.items():
        if hasattr(obj, key):
            setattr(obj, key, value)


def _default_modes() -> list[Mode]:
    data = tomllib.loads(DEFAULT_CONFIG)
    return [_mode_from(d) for d in data.get("modes", [])]


# The [[modes]] chttr 1.0 wrote into config.toml at setup. A config whose modes are
# still exactly these gets today's defaults instead: nobody chose them, and this way
# new modes (agent, assistant, browser tabs) reach installs made before them.
_MODES_1_0 = """\
[[modes]]
name = "terminal"
match = ["alacritty", "kitty", "foot", "wezterm", "ghostty", "xterm", "konsole", "gnome-terminal"]
style = "This is a terminal. Output a single line of plain text with no trailing period; keep commands, paths and flags exactly as spoken."
trailing_space = false

[[modes]]
name = "code"
match = ["^code", "cursor", "zed", "neovide", "jetbrains", "idea", "pycharm", "webstorm", "sublime"]
style = "This is a code editor. Keep identifiers, file names and symbols as spoken; use snake_case or camelCase if the user spells them that way. No trailing period unless it is a sentence in a comment."
trailing_space = false

[[modes]]
match = ["slack", "discord", "whatsapp", "signal", "telegram", "element", "messages", "beeper"]
name = "chat"
style = "This is a chat app. Keep it casual and concise; one short message; no sign-off."

[[modes]]
name = "email"
match = ["thunderbird", "mail", "gmail", "outlook", "proton", "hey"]
style = "This is an email client. Use complete, well-punctuated sentences and paragraphs where the user pauses between thoughts."

[[modes]]
name = "docs"
match = ["docs.google", "notion", "obsidian", "libreoffice", "writer", "typora", "logseq"]
style = "This is a document editor. Use complete sentences and paragraphs; honour dictated headings and lists."
"""


def _untouched_defaults(modes: list) -> bool:
    """`modes` (from a config file) are the defaults of an earlier release, unedited."""
    return modes == tomllib.loads(_MODES_1_0)["modes"]


def _mode_from(d: dict) -> Mode:
    m = Mode()
    _fill(m, d)
    if isinstance(m.match, str):
        m.match = [m.match]
    if isinstance(m.title, str):
        m.title = [m.title]
    if m.newline not in ("enter", "shift+enter"):
        raise ValueError(f"modes.newline must be enter or shift+enter (got {m.newline!r})")
    for pattern in [*m.match, *m.title]:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"modes: {pattern!r} is not a valid regex ({exc})") from None
    return m


def load_config(path: Path = CONFIG_FILE) -> Config:
    cfg = Config(modes=_default_modes())
    if not path.exists():
        return cfg
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    for section in ("engine", "whisper", "audio", "output", "polish", "feedback", "history", "hotkeys", "integrations", "meeting"):
        if isinstance(data.get(section), dict):
            _fill(getattr(cfg, section), data[section])
    if isinstance(data.get("modes"), list) and not _untouched_defaults(data["modes"]):
        cfg.modes = [_mode_from(d) for d in data["modes"] if isinstance(d, dict)]
    if not cfg.polish.enabled:
        cfg.polish.backend = "none"
    if cfg.output.mode not in ("type", "paste", "clipboard"):
        raise ValueError(f"output.mode must be type, paste or clipboard (got {cfg.output.mode!r})")
    if cfg.engine.name not in ("parakeet", "whisper"):
        raise ValueError(f"engine.name must be parakeet or whisper (got {cfg.engine.name!r})")
    if cfg.meeting.auto not in ("ask", "always", "never"):
        raise ValueError(f"meeting.auto must be ask, always or never (got {cfg.meeting.auto!r})")
    if cfg.hotkeys.backend not in ("auto", "compositor", "portal", "evdev", "none"):
        raise ValueError(f"hotkeys.backend must be auto, compositor, portal, evdev or none (got {cfg.hotkeys.backend!r})")
    if cfg.output.typer not in ("auto", "wtype", "ydotool", "xdotool", "dotool"):
        raise ValueError(f"output.typer must be auto, wtype, ydotool, xdotool or dotool (got {cfg.output.typer!r})")
    if cfg.integrations.send not in ("ask", "always", "never"):
        raise ValueError(f"integrations.send must be ask, always or never (got {cfg.integrations.send!r})")
    if cfg.polish.level not in ("none", "light", "medium", "high"):
        raise ValueError(f"polish.level must be none, light, medium or high (got {cfg.polish.level!r})")
    if cfg.polish.screen_context not in ("off", "local", "names", "full"):
        raise ValueError(f"polish.screen_context must be off, local, names or full (got {cfg.polish.screen_context!r})")
    if cfg.polish.backend not in ("auto", "claude", "ollama", "none"):
        raise ValueError(f"polish.backend must be auto, claude, ollama or none (got {cfg.polish.backend!r})")
    return cfg


def write_default_config(path: Path = CONFIG_FILE) -> bool:
    """Create the default config file if missing. Returns True if written."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG, encoding="utf-8")
    return True


def ensure_runtime_dir() -> Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    return RUNTIME_DIR


def ensure_data_dir() -> Path:
    """~/.local/share/chttr holds dictation history, meeting audio and voiceprints: yours only."""
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    return DATA_DIR


def open_private(path: Path, append: bool = True):
    """Open a text file for writing that only you can read (created 0600)."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    return os.fdopen(os.open(path, flags, 0o600), "a" if append else "w", encoding="utf-8")


def too_open(path: Path) -> bool:
    """True if a secret file is readable by other users."""
    try:
        return os.name == "posix" and bool(path.stat().st_mode & 0o077)
    except OSError:
        return False
