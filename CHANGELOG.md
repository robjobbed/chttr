# Changelog

All notable changes to chttr. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

## [1.1.0] - 2026-10-18

It just works: on a quiet mic, with no key, the moment you let go, and with a
bar, overlay and notifications that tell you what is going on.

### Added
- Local cleanup when no Claude key or Ollama is set up: fillers and stutters
  are removed, spoken punctuation becomes punctuation ("comma", "period",
  "question mark", "new line", "bullet point", "open quote"…), "scratch that"
  removes the phrase before it, and capitals are fixed. Ordinary sentences
  stay as you said them: "Did you delete that?", "a difficult period" and "the
  next bullet point" are not treated as commands. Terminals and editors get no
  trailing period and keep shell syntax like `if ! grep` and `sudo !!`; a
  prompt to a coding agent is cleaned up as prose. The same rules run while
  the model is unreachable, so you always get clean text. chttr says once
  that it is cleaning up locally. `chttr polish --local` uses only these rules.
- `chttr connect claude`: paste an Anthropic API key and chttr checks it with
  Anthropic, then stores it readable only by you (0600). `--remove` takes it
  out again, and `chttr connect` now lists Claude first. At a terminal with
  nothing doing cleanup yet, `chttr setup` offers to add one.
- When no cleanup backend is set up (or `[polish] backend = "claude"` has no
  key), the chttr menu offers "Turn on Claude cleanup…". When Ollama is
  installed it also offers "Use Ollama for cleanup (offline)", which starts
  Ollama, downloads the cleanup model and keeps Ollama running after the
  window closes.
- Per-app modes can match the window title (`title = [...]`) and choose how
  line breaks are typed (`newline = "shift+enter"`). New `agent` mode for
  Claude Code, Codex, Aider and friends in a terminal: full sentences, flags
  and paths kept, and the terminal is not treated like a plain shell. It
  follows Claude Code when it renames the window to the task at hand ("✳ Fix
  the parser tests"). New `assistant` mode for Claude, ChatGPT, Gemini and
  Copilot in the browser. Gmail, Outlook, Google Docs, Notion and Slack tabs
  get their email, docs and chat modes too. Ptyxis, Tilix and Terminator count
  as terminals. If your config still has the modes chttr 1.0 wrote at setup,
  you get the new ones automatically; if you changed your `[[modes]]`, yours
  are kept as they are (copy the new ones from `docs/configuration.md`).
- Chat apps and coding agents type line breaks as Shift+Enter, so a dictated
  paragraph never sends the message early.
- Dictation fits the text before the cursor: one space after a word, a
  lowercase first word mid-sentence ("as we said" + "Tomorrow works" → "as we
  said tomorrow works"), a capital after a full stop, and no extra space before
  punctuation. Names, days and months keep their capital ("let's meet on
  Monday").
- In terminals and code editors, ending a dictation with "run it", "press
  enter" or "send it" types the command and presses Enter
  (`[output] spoken_enter`). In a coding agent, where Enter sends the prompt,
  it only fires after a pause ("Fix the parser tests. Send it."). It never
  fires after the clipboard fallback, or when the phrase is part of the
  sentence ("fix the test and run it", "pytest won't run it").
- Spoken flags and symbols in terminals and editors: "dash dash verbose" →
  `--verbose`, "dash m" → `-m`, "tilde slash notes" → `~/notes`, "pipe",
  "double colon", "arrow", "fat arrow", "ampersand ampersand", "open paren" /
  "close paren" and backticks. In a prompt to a coding agent they stay words
  where they read as words ("the down arrow", "the water pipe").
- Common edits in command mode work on a selection without a model: upper
  case, lower case, title case, sentence case, join lines, sort lines (also in
  reverse), remove blank lines, remove duplicates, trim, wrap in quotes /
  backticks / parentheses / brackets, bullet list, numbered list, remove
  bullets, and "delete that". With a model set up, anything that isn't one of
  these exact phrases goes to the model.
- Meetings without a key get on-device notes: a summary, the questions asked,
  numbers and dates, commitments and action items, all found on your machine.
  Every meeting also shows each speaker's share of the talk as a bar, and the
  frontmatter lists it as `talk_time`.
- Meeting notes and all 18 styles also work with Ollama. Notes use structured
  output, and long calls are summarized in 15-minute parts first.
- `chttr ask` without a key is a ranked local search that prints citations
  (`[meeting, date, hh:mm:ss] speaker: words`). With Ollama, the local model
  answers from those results. `--local` only searches.
- `[meeting] email` marks you as yourself in any calendar feed, including
  secondary or renamed calendars. The address a feed is named after still
  counts too.
- A pre-meeting brief: five minutes before a calendar meeting, a notification
  with what you agreed last time and the open action items, built only from
  your own notes and your calendar (`[meeting] brief_minutes`, 0 turns it
  off). It finds the last meeting with the same people, or with the same
  title when the people don't differ, so an "Interview" or "1:1" with someone
  new never shows someone else's notes, plus your to-dos and theirs from the
  past 60 days. During a call in Meet, Zoom, Teams or Slack it only names the
  meeting, so your notes don't appear on a shared screen. `chttr brief` prints
  the brief for your next meeting with other people, or for one by title
  (`chttr brief standup`), with the path to the last notes.
- `chttr doctor` names the fix for every problem: distro package names for the
  waveform overlay (python-gobject + gtk4-layer-shell) and the audio tools
  (pw-record, pw-play, pw-dump, pactl), the ydotool daemon, the service and its
  last log lines, and the accessibility bus (toolkit-accessibility and
  Chromium's `--force-renderer-accessibility`, which `chttr setup` now turns
  on). `chttr doctor --fix` runs the safe ones (no sudo) and checks again, and
  only calls the service fixed once the daemon answers; `chttr doctor --json`
  prints the checks for scripts. It notices a service that keeps restarting,
  even while systemd calls it active. The log lines it shows are only the
  errors, with quoted text hidden, so what you dictated never ends up in a bug
  report.
- `chttr uninstall [--purge]` removes the service, key bindings, Meet extension
  registration and the old autostart edit, putting each file back exactly as
  it was. With `--purge` it asks first, then deletes chttr's data and
  settings. Your meeting notes are never touched, even when they're reached
  through a link into chttr's folder.
- `chttr keys` shows the shortcuts that are actually bound (read from your
  Hyprland or Sway config, or from `[hotkeys]`) and what catches them.
- `chttr setup` waits for the daemon to answer, then ends with a card of your
  shortcuts, what does the cleanup and the notes, and a "chttr is ready"
  notification.
- Pressing the dictation key while the daemon is down starts it and tells you:
  "chttr is starting. Hold the key again in a few seconds."
- `[audio] adaptive_threshold` (on by default): the speech threshold fits the
  room and your mic (see Fixed). Turn it off to go back to the fixed
  `speech_threshold_db`.
- `chttr ui` opens on a Home page: words dictated, speaking speed against
  typing, time saved, your streak, a 30-day activity strip, top apps, recent
  meetings, open action items and what the daemon is doing right now
  (recording with a timer, transcribing, idle, or not running).
- A Dictations tab in `chttr ui`: every dictation grouped by day and searchable
  from the header, showing what chttr heard against what it typed (dropped
  words struck through, added ones highlighted). Voice commands show what you
  said, and dictations where nothing got typed say so. Copy a dictation (or
  press `c`), replay it when `[history] keep_audio_days` kept the audio, and
  jump into Lingo to fix a word.
- The meeting library follows the active Omarchy theme, light or dark. It opens
  where you left it: deep links, Back/Forward, and
  `chttr ui --open "<citation>"` to jump to a `chttr ask` citation at the exact
  second. It is fully keyboard-driven (`?` lists the keys). Search runs as you
  type, with prefix matching, `speaker:`/`after:`/`before:`/`on:` filters and
  per-meeting facets. Transcripts read as turns in speaker colours, with
  talk-time bars, follow-along playback, find-in-transcript, inline speaker
  renames and export to Markdown, JSON and CSV. Every view has a real empty
  state, and a banner offers to start chttr when it isn't running. "Meeting
  library" is in the chttr menu, in place of Stats.
- Lingo (`chttr lingo`, the Lingo tab in `chttr ui`, and **Lingo: fix how words
  are spelled** in the chttr menu): pick the words that came out wrong in a past
  dictation, type the right spelling, and chttr uses it from the next dictation
  on. Rename a spelling, drop one mis-hearing, add words by hand, and try a
  sentence to see what would be typed. Started from the menu, the server stops
  itself 15 minutes after the page is closed (`--idle-timeout`).
- The bar widget tells you what's going on: a dimmed, slashed microphone when
  chttr is off (click it to start chttr), an hourglass while the speech model
  loads, a pulse while it transcribes, a red dot with a live timer while a
  meeting is recorded, and a small dot when cleanup is off. Its tooltip names
  the keys you've set up ("hold F9 to dictate · Alt+F9: command mode") and
  stays current while chttr sits idle. Settings: "Show the meeting timer", and
  "Right click" to record a meeting (the default) or start hands-free
  dictation.
- `chttr status` can also print `off` and `loading`; during a meeting it still
  prints the dictation state. `chttr status --json` adds `meeting_seconds`,
  `cleanup` and `hotkey`, plus the `off`, `loading` and `meeting` states, for
  Waybar and other bars.

### Changed
- Letting go of the key during a pause reuses the recognition pass that
  already ran in that pause, so short dictations appear right away instead of
  after a second or so. A segment that closes in the same pause reuses that
  pass too, and if nothing was said after the last pause there is nothing left
  to recognize. Words spoken more softly than the speech threshold after that
  pause are not recognized again.
- Live captions re-recognize at most the last six seconds of what you are
  saying and no longer run over silence (not even through a long pause), so
  long dictations cost the same per caption as short ones. When you talk for
  more than six seconds without a pause long enough to end a segment, chttr
  stops running pause passes over it; that segment is recognized when it
  closes or when you let go, as in 1.0.
- `chttr bench` reports two numbers. `release` is letting go in the pause after
  your last word (0.5 s after it), reusing the pass from that pause; `tail` is
  the old measurement, letting go 0.2 s after the last word with no reuse.
  Compare older "after release" figures with `tail`.
- Dictation cleanup waits at most 5 s for the model
  (`[polish] cleanup_timeout_seconds`), then types the locally cleaned text.
  After a connection failure or an overloaded API, chttr stops asking the
  model for 30 s and checks in the background: a dead VPN or an Anthropic
  incident costs one notice instead of a long wait on every dictation, and
  cleanup comes back as soon as a request works again. Command mode waits up
  to `timeout_seconds` (longer for a long selection), and a slow command no
  longer switches dictation to local cleanup.
- Ollama keeps its model loaded (keep_alive 30m, loaded when chttr starts) and
  gets a shorter prompt written for small models; `qwen2.5:1.5b` is a fast
  choice on a CPU laptop. Long dictations get a context and answer length to
  match.
- Model preambles like "Here is the cleaned text:", code fences and quotes
  around the whole answer are stripped. Words you dictated yourself, such as
  "Here's the updated version:", are kept.
- The daemon answers the key the moment it starts and loads the speech model
  in the background; a dictation made meanwhile is typed when the model is
  ready ("Loading the speech model…").
- A broken `config.toml` or a missing or damaged speech model gives one clear
  notification instead of a silent restart loop: chttr keeps running (with the
  defaults, for a bad config) and tells you to run `chttr setup` when the
  model is missing or can't be loaded. The service stops retrying after four
  quick failures, so `systemctl --user status chttr` shows the problem.
- `chttr reload` applies audio (whisper mode, speech threshold, device),
  feedback (sounds, notifications, the overlay) and hotkey changes, says which
  settings changed, and keeps the running config when the file has an error.
  After a model failed to load, `chttr reload` tries loading it again.
- The log holds timings and word counts, not your words: dictations, cleanup
  results, commands and corrected words are only logged with
  `chttr daemon -v`.
- The waveform overlay stays running and follows the whole dictation:
  listening, transcribing (a shimmer runs across the bars), done (a check with
  the first words typed), then "nothing heard" or "error" when that's what
  happened. It uses the active Omarchy theme's colours, light or dark, and
  switches when you change themes. It appears as soon as you press the key. On
  a quiet mic the bars scale to your voice, so they fill the pill while
  silence stays flat, and after a loud moment they fall back smoothly.
- On Hyprland/Sway the "Transcribing…" and "Typed" notifications are gone,
  since the overlay shows both. On other desktops they still appear, and
  "Transcribing…" disappears after 10 seconds. If the overlay can't start,
  chttr says so once ("Waveform overlay off") after the dictation's own
  notifications, and falls back to notifications.
- Model downloads continue after a dropped connection, retry with backoff, and
  show a progress bar with speed and ETA. They check for free disk space first
  and run while the Python packages install.
- The installer uses your system Python when it is 3.12 or newer, instead of
  always downloading one. On Arch-based systems it keeps uv's own copy of
  Python, so a pacman update of python can't break chttr (set
  `UV_PYTHON_PREFERENCE=system` to use the system one anyway). It also checks
  for `pactl` and `pw-dump`, and recommends the overlay packages when they're
  missing (not on GNOME or X11, where the overlay can't run).
- "Daemon is not running" messages suggest `systemctl --user start chttr`.
- A heard phrase in the dictionary also matches when the recognizer puts a
  comma or hyphen between its words ("Sora, Beth Ani").
- The bar widget uses the bar's own font, like the built-in widgets.

### Fixed
- Quiet microphones no longer lose whole dictations. The speech threshold now
  fits the room and your mic: 10 dB above the noise floor, but no lower than
  20 dB under your speech, never below -62 dB and never above
  `speech_threshold_db`. Quiet audio is boosted before silence is trimmed, and
  the threshold still applies to the mic's own level, so boosting never turns
  fan noise into speech. A dictation on a quiet built-in mic now comes back as
  text instead of "No speech detected", and a loud mic in a quiet room still
  treats a TV or keyboard as a pause. `chttr bench` measures with the same
  threshold.
- Hands-free stops on your pause at any mic level instead of cancelling after
  `no_speech_timeout`, and streaming segments close on your pauses while you
  talk.
- `chttr doctor --mic` reports your speech level, the noise floor and the
  threshold in use, and agrees with the daemon. It says "quiet but usable" when
  the lowered threshold still catches your speech, and never calls a mic too
  quiet when it heard you.
- The no-speech notice says whether the mic was silent or just quiet and gives
  the noise floor. It says how far down chttr listened only when it actually
  lowered the threshold, and mentions the Omarchy audio shortcut
  (Super+Ctrl+A) only on Omarchy.
- A microphone that disappears mid-dictation is reported as such ("the
  microphone went away") instead of "No speech detected", even after a silent
  pause, and whatever you said before it went is still typed.
- Live captions no longer make short dictations slower after release.
- A dictation started before the previous one finished processing can no
  longer leak into it, and a recognition pass that outlives the finish timeout
  can no longer change the result. Letting go at the exact moment a pass was
  starting no longer loses what was said since the last pause.
- Two dictations in a row into the same field no longer get a double space or
  a lowercase letter after the full stop.
- `chttr polish` no longer crashes with a traceback when there is no cleanup
  backend or the network is down; it prints locally cleaned text.
- A dropped connection no longer delays a dictation by up to 40 s or holds up
  the next one.
- A long dictation is no longer cut off by Ollama or Claude: if the answer
  stops early, the full locally cleaned text is typed instead.
- After a slow cleanup answer, a later outage is announced again, and the
  notice no longer shows an internal error name.
- With dotool as the typing tool, line breaks are no longer dropped.
- Command mode in Claude Code reads the terminal's selection instead of
  pressing Ctrl+C.
- A pw-record that won't quit can no longer hang or break a dictation, and its
  error messages are kept instead of piling up unread.
- Hands-free auto-stop and the dictation and meeting time limits keep working
  after an error.
- Post-meeting processing runs in its own systemd unit, so restarting the
  daemon no longer kills it, and an interrupted run is marked failed with a
  retry hint. A meeting recorded while `config.toml` has an error is still
  transcribed, with the defaults.
- On Omarchy, the "Record this call?" and "Learn it?" notifications work: click
  the toast to record or to learn the word. Omarchy's notifications have no
  buttons, so before this they could not be answered and calls were never
  recorded with `[meeting] auto = "ask"`. Clicking an old "Record" toast after
  the call has ended no longer starts a recording.
- An error notification is no longer covered by the next "Typed" toast, and it
  replaces "Transcribing…", so that toast no longer stays on screen after a
  failure. A short tap of the dictation key no longer leaves a "Transcribing…"
  notification that never goes away.
- The overlay: a dictation that finishes while you're already recording the
  next one no longer overwrites it, a daemon that starts before Hyprland is
  fully up still gets it at the first key press, and one that exits no longer
  leaves a BrokenPipeError in chttr's log.
- `chttr setup` no longer says it is done before the daemon is ready. It gives
  up after 45 seconds even when the daemon hangs, shows the last lines of the
  daemon's log when it doesn't come up, and prints a PROBLEM line when no key
  starts dictation. A failed model download prints a PROBLEM line with how to
  retry, instead of a traceback.
- The installers work on a system with no python3: uv fetches its own Python.
  Before, they stopped without a message.
- Screen context retries the accessibility bus every 10 minutes instead of
  giving up until restart.
- The waveform overlay no longer fails silently on Debian and Fedora:
  `chttr doctor` says what's missing and how to install it.
- One- and two-word meeting answers ("Yes.", "Agreed.") are no longer dropped
  as echo.
- Renaming a speaker only to fix the spelling or add a surname keeps their
  email and their learned voice. Renaming a wrongly named speaker to someone
  else forgets the voice and the address stored under the wrong name. When two
  remembered voices match equally, the one heard most recently wins.
- Joining a Meet while a Zoom/Teams recording runs no longer takes over that
  recording.
- `read_transcript around="12:40"` means 12 minutes 40 seconds, and bad values
  such as "inf" return an error instead of failing.
- The post-call notification says "Transcript saved" or "On-device notes saved"
  when that is what happened, and lists the people and the number of action
  items. On Omarchy, clicking it opens the notes, and it tells you to use
  `chttr send` to send them on.
- When Claude or Ollama fails on a meeting, the on-device notes no longer tell
  you to add a key, and the "try again" command keeps the style you chose.
- `chttr ask` with an Ollama model that isn't downloaded yet tells you to run
  `ollama pull <model>`.
- `chttr status` (and `chttr status --json`, which the bar uses) no longer
  crashes with a traceback when the daemon died and left its socket behind; it
  says chttr is off. The bar no longer shows a ready microphone when chttr
  isn't running, its tooltip no longer says "hold Insert" when you've set other
  keys, and it shows an hourglass instead of a thumbs-up while transcribing.
- `chttr stats` no longer prints a negative time saved for slow or short
  dictations; it says "none yet" instead.
- In the meeting library: Lingo's "what chttr now types" preview works for
  dictations over 120 characters, and Lingo keeps what you typed when you
  extend the selection. Native controls follow dark mode. The notes style
  picker no longer claims a style for meetings without notes, and shows the
  real state after a failed rewrite. Seeking in meeting audio no longer
  re-reads the whole recording. A slow search or meeting load can no longer
  replace newer results. Screen readers and the keyboard get a status region,
  real buttons and tab semantics; Space on a focused button presses it instead
  of playing or pausing. Small grey text stays readable (at least 4.5:1) in any
  theme. Follow-along playback stops pulling the transcript back while you
  read elsewhere. Searching as you type no longer flashes "Nothing matches"
  after the first letters. Switching meetings during playback stops the old
  recording.
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

[Unreleased]: https://github.com/robjobbed/chttr/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/robjobbed/chttr/compare/v1.0.0rc1...v1.1.0
[1.0.0rc1]: https://github.com/robjobbed/chttr/compare/v0.9.0...v1.0.0rc1
[0.9.0]: https://github.com/robjobbed/chttr/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/robjobbed/chttr/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/robjobbed/chttr/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/robjobbed/chttr/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/robjobbed/chttr/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/robjobbed/chttr/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/robjobbed/chttr/compare/v0.2.1...v0.4.0
[0.2.1]: https://github.com/robjobbed/chttr/commit/7953228
