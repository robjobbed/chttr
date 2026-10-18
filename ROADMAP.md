# Roadmap

Where chttr is going, roughly in order. Issues welcome on any of it.

## Shipped

| Release | What |
|---|---|
| 0.4 | Tests and CI, pinned model downloads, privacy hardening, streaming recognition, Haiku cleanup, `chttr bench` |
| 0.5 | GNOME, KDE, Sway, niri and X11: typing, focused window, global-shortcuts portal and evdev hotkeys, one-line installer |
| 0.6 | Cleanup levels, developer dictation, names from the screen, learning from your corrections, double-tap, whisper mode |
| 0.7 | Bot-free call detection for Zoom, Teams, Slack, Discord, Webex and browser calls; people in the room |
| 0.8 | The meeting library: `chttr ask`, the MCP server, `chttr ui` |
| 0.9 | Slack, Notion, HubSpot, webhooks and a post-meeting hook |
| 1.0 rc | README, configuration reference, launch |
| 1.1 | Works out of the box: quiet mics, local cleanup and on-device meeting notes with no key, instant release, an always-on daemon, a resident overlay, clickable notifications, `chttr doctor --fix`, `chttr uninstall`, pre-meeting briefs, a Home and Dictations view, and a bar widget that tells the truth |

## Next

- **Confirmed on GNOME 48+ and KDE Plasma 6 hardware.**
- **A demo video** at the top of the README.
- **Settings in `chttr ui`**: every config key, edited in place and applied with `chttr reload`.
- **A popup panel for the Omarchy bar widget**: the last dictations and quick toggles, on top of the 1.1 `state.json` feed.
- **Timestamps on every bullet in the notes**, with click-to-jump in the library.
- **Recurring meetings that remember**: a "Since last time" section in the notes and a People view with action items you can tick off.
- **Type as you speak**: closed segments typed while you are still talking.
- **During a call**: a live transcript tab and hotkeys to mark a moment or add a note to self, once CPU headroom on a real Zoom call is measured.
- **Lighter on the machine**: event-driven call detection (no PipeWire poll every 3 s) and a smaller memory footprint for the speech model.
- **A tray icon** for GNOME and KDE.
- **Change the dictation key from the menu** (`chttr hotkey set`), rewriting the compositor bindings.
- **Softer sounds**: a done tick and a distinct no-speech cue.
- **Meeting recordings that survive a PipeWire restart** mid-call.
- **Speaker names from Teams and Zoom web captions** ([#9](https://github.com/robjobbed/chttr/issues/9)).
- **Firefox** for the Meet extension ([#10](https://github.com/robjobbed/chttr/issues/10)).
- **`chttr export`** on the command line ([#7](https://github.com/robjobbed/chttr/issues/7)); the library exports Markdown, JSON and CSV since 1.1.
- **Notifications in more languages** ([#8](https://github.com/robjobbed/chttr/issues/8)).

## Considered, not planned

- **Video recording and clips.** Fathom's video features need a bot or screen
  capture; chttr stays audio-only and bot-free.
- **Batched recognition for meetings.** Measured at 1.3× faster but it changed
  the transcript for some utterances; not worth the accuracy risk.
- **macOS and Windows.** chttr is a Linux tool; Handy and VoiceInk cover those.
