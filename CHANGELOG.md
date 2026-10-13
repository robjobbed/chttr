# Changelog

All notable changes to chttr. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

### Added
- `chttr export [--format json|csv] [--since DATE] [MEETING...]`: writes meetings
  (title, date, speakers, notes, action items, transcript) as JSON or CSV for
  use in other tools.
- Lingo (`chttr lingo`, the Lingo tab in `chttr ui`, and **Lingo: fix how words
  are spelled** in the chttr menu): pick the words that came out wrong in a past
  dictation, type the right spelling, and chttr uses it from the next dictation
  on. Rename a spelling, drop one mis-hearing, add words by hand, and try a
  sentence to see what would be typed. Started from the menu, the server stops
  itself 15 minutes after the page is closed (`--idle-timeout`).

### Changed
- A heard phrase in the dictionary also matches when the recognizer puts a
  comma or hyphen between its words ("Sora, Beth Ani").

### Fixed
- The dictation history menu on Omarchy drew each dictation in the icon column,
  on top of its label, so the list couldn't be read. Rows now have an icon, the
  dictation as the label and the time and app underneath.

## [1.0.0rc1] - 2026-10-13

The launch candidate: everything above, documented for people who haven't met
chttr yet.

### Added
- A rewritten README: what chttr does in one screen, a screenshot of the
  meeting library, measured latency, a comparison with Wispr Flow, Fathom,
  Handy and Meetily, and an architecture diagram.
- `docs/configuration.md`, every config key with its explanation, generated
  from the defaults (`scripts/gen-config-docs.py`; a test keeps it in sync).
- `ROADMAP.md`: what shipped, what's next, what was considered and left out.

### Fixed
- Hyprland is recognised even when the daemon's systemd environment lacks
  `HYPRLAND_INSTANCE_SIGNATURE` (from `XDG_CURRENT_DESKTOP` or Hyprland's
  sockets), so per-app modes keep working. When the compositor can't be told,
  chttr bindings in a Hyprland or Sway config stop chttr from also reading the
  keyboard, which would have fired every press twice.
- `on_meeting_done` appended the Markdown path even when the command already
  used `$1`.
- Screen context waits at most 0.2 s for the text field and stops asking the
  accessibility bus after five unreadable fields in a row (it isn't running
  on every desktop), so it can never slow dictation down.

### Changed
- Release candidates (`vX.Y.ZrcN` tags) are published as GitHub pre-releases.
- The Meet extension's version is the release without its pre-release suffix
  (Chrome only accepts numbers).

## [0.9.0] - 2026-10-13

Integrations.

### Added
- `chttr connect slack|notion|hubspot|webhook` and `chttr send`: meeting notes to
  a Slack channel (incoming webhook, formatted blocks), a Notion page (headings,
  bullets, to-dos), HubSpot (a note and a task per action item on the attendees'
  contacts, matched by email) or any webhook (JSON, HMAC-SHA256 signed when you
  give a secret).
- The "notes saved" notification offers a "Send to …" button per connected
  destination (`[integrations] send = "ask"`, the default); `send = "always"`
  sends automatically, optionally only for calendar titles matching
  `auto_titles`.
- `[integrations] on_meeting_done`: your own script after every meeting
  (Markdown path as `$1`, the meeting as JSON on stdin).
- Secrets live in the desktop keyring through libsecret's `secret-tool`, or a
  0600 file when there is no keyring; `chttr doctor` shows which.

## [0.8.0] - 2026-10-13

Ask your meetings.

### Added
- The meeting library (`library.py`): a local SQLite FTS5 index over every
  transcript line, the notes, action items and dictations, updated
  incrementally (`~/.local/share/chttr/library.sqlite`, 0600; safe to delete).
  Porter stemming, BM25 ranking, snippets, speaker and date filters.
- `chttr ask "QUESTION"`: Claude searches the library with tools (search, read
  around a moment, notes, meetings, action items) and answers with citations
  to the meeting and the moment. `-v` shows the searches.
- `chttr mcp`: the same tools as a read-only MCP server (stdio) for Claude Code
  (`claude mcp add chttr -- chttr mcp`) and Claude Desktop.
- `chttr ui`: the library in the browser, on 127.0.0.1 with a one-time token
  (Host-checked, POSTs need the token header, strict CSP): search with
  highlights, notes, transcripts with clickable timestamps and audio, speaker
  renames, note-style switching, and every action item in one table. Works in
  light and dark mode and on narrow screens.

## [0.7.0] - 2026-10-13

Meetings beyond Google Meet.

### Added
- Call detection without a bot (`calls.py`, `[meeting] detect_calls`): chttr
  watches which programs hold the microphone (PipeWire) and offers to record
  calls in Zoom, Microsoft Teams, Slack, Discord, Webex, Signal, Telegram,
  Element, WhatsApp, Mumble and browser tabs that aren't Google Meet. The
  recording stops when the app lets go of the mic; the title comes from the
  calendar event happening now.
- People in the room (`[meeting] room_speakers`): the microphone channel is
  split into voices as well. Your voice (remembered after each meeting, so the
  right voice is you even when someone else talks more) stays yours; other
  voices with real speaking time become `In the room 1`, ... and can be named
  with `chttr meeting rename` like any speaker.

### Changed
- The Meet extension only turns on captions while chttr is recording the call,
  turns them off again when the recording ends, and shows a "chttr is
  recording" badge on the page meanwhile. Before, captions were switched on and
  hidden in every call. It also reports every 3 seconds instead of 10, so a
  recording you start from the notification picks up speaker names sooner.

## [0.6.0] - 2026-10-13

Wispr Flow parity for dictation.

### Added
- Cleanup levels (`[polish] level`): `light` keeps your exact words and only
  punctuates, `medium` (the default) is the cleanup so far, `high` edits like a
  careful editor, `none` types the raw transcript.
- Developer dictation (`devmode.py`): spoken identifier casing ("camel case
  user id" -> `userId`, plus snake, kebab, Pascal and constant case) anywhere,
  and file mentions ("at src slash app dot ts" -> `@src/app.ts`) in code
  editors and terminals. The results are passed to the cleanup as verbatim.
- Screen context (`[polish] screen_context`, AT-SPI): names near the cursor
  correct sound-alike words. `local` (the default) keeps them on this machine;
  `names` also gives them to the cleanup model; `full` also sends up to 600
  characters before the cursor.
- Learning from your edits (`[history] learn_from_edits`): eight seconds after
  typing, chttr looks at the field again; if you replaced a word with a
  similar-sounding name, a notification offers to learn the spelling.
- Double-tap the dictation key for hands-free; tap again to stop
  (`[audio] double_tap`).
- Whisper mode (`[audio] whisper_mode`): a lower speech threshold and up to
  30x gain for quiet speech. Clearly quiet recordings are raised a little even
  without it.
- `chttr history --play [N]` replays a dictation's audio when
  `[history] keep_audio_days` keeps it (off by default; files are 0600 and
  pruned on daemon start).
- `chttr doctor --mic`: record four seconds, show the level and the transcript.

## [0.5.0] - 2026-10-13

Linux-wide: GNOME, KDE Plasma, Sway, niri and X11 alongside Hyprland/Omarchy.

### Added
- A desktop layer (`desktop.py`) that picks the right tools per desktop: the
  focused window from hyprctl, swaymsg, niri, kdotool, GNOME's Window Calls
  extension or xdotool; typing with wtype, ydotool, xdotool or dotool
  (`[output] typer`); shortcuts and the clipboard on Wayland and X11.
- Global hotkeys outside Hyprland (`[hotkeys]`): the XDG GlobalShortcuts portal
  on GNOME 48+ and KDE Plasma (press and release, so push-to-talk works), or
  the keyboard devices directly elsewhere (`input` group). Keys are
  configurable; Sway gets bindings from `chttr setup`.
- `install.sh`: a one-line installer for any distribution that prints the
  system packages it needs instead of running sudo; `packaging/aur/chttr-git`.
- Menus and prompts through fuzzel, wofi, rofi, tofi, bemenu, dmenu or zenity
  when Omarchy's picker isn't there; terminals and editors from `$TERMINAL`,
  `$EDITOR` or the usual suspects.
- The Meet extension's native host registers with Chrome, Brave, Vivaldi and
  Edge as well as Chromium.
- `chttr doctor` reports the desktop, typing tool, shortcuts, clipboard, window
  info, menu tool and hotkey method it found.
- CI runs the tests on Debian 13 and Fedora as well as Arch, and shellchecks the
  scripts.

### Changed
- When typing fails, chttr pastes the text before falling back to the clipboard.
- The overlay finds gtk4-layer-shell on Debian/Fedora library paths and stays
  off on GNOME and X11 (no layer-shell there) instead of failing.
- Built with hatchling instead of uv_build, so distributions can package it.

## [0.4.0] - 2026-10-13

Speed: text arrives about as fast as you let go of the key. This release also
contains everything listed under 0.3.0 (tests, CI, pinned models, privacy
hardening), which was not released on its own.

### Added
- Streaming recognition (`[engine] streaming = true`, the default): once you
  have said a few seconds' worth, the next pause closes a segment that is
  transcribed in the background while you keep talking, so after release only
  the last stretch is left to recognize. Segments keep at least 4 seconds of
  speech, so accuracy matches a single pass over the whole recording. Live captions come from the same
  passes instead of re-transcribing the last 8 seconds every 0.8 seconds.
- `[polish] cleanup_model`, default `claude-haiku-5-5`: dictation cleanup and
  command mode run on Haiku with thinking off, which answers in a few hundred
  milliseconds. Meeting notes stay on `[polish] model` (Opus).
- Quick path: short phrases with no fillers, self-corrections or spoken
  punctuation ("git status", "sounds good") skip the model round trip
  (`[polish] quick_max_words`, default 4).
- `chttr bench FILE.wav…`: full-pass vs. after-release recognition time,
  cleanup time, real-time factor and word error rate. CI runs it on every
  change to `main` and publishes the numbers.
- `[output] paste_over`: dictations longer than 400 characters are pasted
  instead of typed character by character; your clipboard is restored.

### Changed
- One Anthropic client for the daemon's lifetime (no TLS handshake per
  dictation), the cleanup system prompt is marked for prompt caching, and the
  daemon opens the API connection at startup.
- Every file chttr reads or writes (notes, config, dictionary, history) is UTF-8
  whatever the system locale.
- End-to-end tests with the real models (`uv run pytest -m model`): the whole
  meeting pipeline on a synthesized two-voice call, and streaming vs. full-pass
  accuracy. CI runs them with the benchmark.

## [0.3.0] - 2026-10-13 (not released separately; shipped in 0.4.0)

Foundations: a test suite, CI, pinned model downloads, privacy hardening and a
more responsive daemon. No change to how you use chttr.

### Added
- Test suite (pytest) covering the dictionary, config, silence trimming, the
  meeting pipeline (echo removal, caption votes, speaker naming, rendering,
  renames), diarization and voice memory, note styles and schemas, the cleanup
  prompts, the calendar, the browser bridge, model downloads and the daemon's
  state machine.
- GitHub Actions CI: ruff, pyright and pytest on Python 3.12, 3.13 and 3.14,
  plus Arch Linux.
- `chttr doctor --verify` hashes every model file against its pinned checksum.
- `[polish] send_window_title`: send the focused app to the cleanup model but
  keep the window title (email subjects, document names) on your machine.
- Release workflow: pushing a `v*` tag publishes a GitHub release with the
  changelog entry.

### Changed
- A new dictation can start while the previous one is still being cleaned up;
  results are typed in the order you spoke them.
- The daemon serves each request on its own thread and reads the focused window
  (and, in command mode, the selection) without holding its lock, so a key
  release is never stuck behind a slow request. Releasing the key while
  recording is still starting stops it right away.
- Model downloads are pinned to a Hugging Face commit and verified against a
  SHA-256 before use.
- `meeting.py` is now the `chttr.meeting` package: `store`, `session`,
  `recorder`, `analysis`, `naming`, `pipeline`, `styles`, `render`, `edit`.
- Python 3.12+ (was 3.14+). whisper.cpp moved to an optional extra
  (`chttr[whisper]`); `bin/setup` keeps it installed if your config uses it.
- One version number for the package, the plugin and the Meet extension.

### Fixed
- A snippet with a line break (`chttr snippet sig "Thanks\nRob"`) corrupted
  `dictionary.toml`; strings are now escaped properly.
- With two invitees sharing a first name, a caption reading only that first
  name was matched to one of them by email; it is now left unmatched.
- Meeting filenames no longer contain runs of spaces where punctuation was
  removed.

### Security
- Dictation history, meeting data and the voiceprint store are created
  readable only by you, and files written by earlier versions are tightened on
  daemon start. `chttr doctor` warns when the API key file is readable by
  other users.

## [0.2.1] - 2026-10-11

The state of chttr before this changelog was kept (earlier releases: v0.1.0,
v0.2.0): push-to-talk, hands-free and command-mode dictation with
local Parakeet speech-to-text and Claude or Ollama cleanup, per-app modes, the
personal dictionary with sound-alike name correction, snippets, history and
stats, and Google Meet transcripts with speaker names and 18 note styles, as an
Omarchy plugin.

[Unreleased]: https://github.com/robjobbed/chttr/compare/v1.0.0rc1...HEAD
[1.0.0rc1]: https://github.com/robjobbed/chttr/compare/v0.9.0...v1.0.0rc1
[0.9.0]: https://github.com/robjobbed/chttr/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/robjobbed/chttr/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/robjobbed/chttr/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/robjobbed/chttr/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/robjobbed/chttr/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/robjobbed/chttr/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/robjobbed/chttr/compare/v0.2.1...v0.4.0
[0.2.1]: https://github.com/robjobbed/chttr/commit/7953228
