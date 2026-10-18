"""After the call: correct a speaker's name, and the desktop notification."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

from .. import diarize as D
from ..config import Config
from .render import render
from .store import Speaker, Utterance, meeting_dirs, read_json, write_json
from .styles import _relabel

log = logging.getLogger("chttr.meeting")
GLYPH = "\U000f0219"      # nf-md-file_document


def _same_person(old: str, new: str) -> bool:
    """Whether a rename only corrects the spelling or adds a surname ("Dana" -> "Dana
    Smith", "Jon" -> "John"), rather than naming someone else."""
    a, b = (re.sub(r"[^\w]", "", (n.split() or [""])[0].lower()) for n in (old, new))
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a) or SequenceMatcher(None, a, b).ratio() > 0.75


def rename(directory: Path, old: str, name: str, email: str, cfg: Config) -> Path:
    """Fix a speaker's name in a processed meeting and remember their voice."""
    result = read_json(directory / "result.json")
    if not result:
        raise SystemExit(f"{directory.name} has not been processed yet")
    speakers = {k: Speaker(**v) for k, v in result["speakers"].items()}
    if old not in speakers:
        raise SystemExit(f"no speaker {old!r} in this meeting; speakers: {', '.join(speakers)}")
    if speakers[old].source == "me":
        raise SystemExit("that's you; set [meeting] me = \"Your Name\" in the config instead")
    was = speakers[old]
    clusters = list(was.clusters)
    # A spelling fix keeps the person's address and their learned voice; naming someone
    # else drops the address that belonged to the wrong name.
    same = (was.source != "unknown" and _same_person(old, name)
            and (not email or not was.email or email.lower() == was.email.lower()))
    new_email = email or (was.email if same else "")
    if not same and not email:
        result["speakers"][old]["email"] = ""
    # The voice stored under the old name. For a spelling fix it moves to the new name
    # with its history. A name chttr was sure of (captions, the invite, a remembered
    # voice) was stored with this voice after the call; when that name was wrong, forget
    # it so later calls don't repeat the mistake.
    old_key = was.email.lower() or old.lower()
    if not same and was.source not in ("captions", "calendar", "voice"):
        old_key = ""
    _relabel(result, old, name, new_email, "corrected", "corrected by hand")
    new_email = result["speakers"][name]["email"]      # merged into a speaker who has one
    new_key = new_email.lower() or name.strip().lower()
    utts = [Utterance(**u) for u in result["utterances"]]
    md = render(directory, result, cfg)
    result["markdown"] = str(md)
    write_json(directory / "result.json", result)
    npz = directory / "speakers.npz"
    if npz.exists() and clusters:
        stored = np.load(npz)
        vecs = [stored[str(c)] for c in clusters if str(c) in stored]
        secs = sum(u.end - u.start for u in utts if u.speaker == name and u.channel == "them")
        if vecs:
            voices = D.load_voices()
            voice = D.centroid(np.stack(vecs))
            prior = voices.get(old_key)
            if prior is not None and old_key != new_key and float(prior.embedding @ voice) >= D.VOICE_MATCH_ATTENDEE:
                del voices[old_key]
                if same:
                    if new_key not in voices or voices[new_key].count < prior.count:
                        prior.key, prior.name, prior.email = new_key, name, new_email or prior.email
                        voices[new_key] = prior
                    log.info("moved the voice stored for %s to %s", old, name)
                else:
                    log.info("forgot the voice stored for %s: it was %s", prior.name or old_key, name)
            D.remember(voices, name, new_email, voice, max(secs, 60))
            D.save_voices(voices)
    return md


def _directory_for(md: Path) -> Path | None:
    """The meeting whose notes are `md`."""
    for d in reversed(meeting_dirs()):
        if (read_json(d / "meta.json", {}) or {}).get("markdown") == str(md):
            return d
    return None


def done_title(directory: Path | None) -> str:
    """What the post-call notification says was saved: the notes, on-device notes, or
    only the transcript."""
    result = (read_json(directory / "result.json", {}) or {}) if directory else {}
    notes = result.get("notes") or {}
    if notes.get("style") == "lite":
        return "On-device notes saved"
    return "Notes saved" if notes.get("sections") else "Transcript saved"


def notify_done(md: Path, summary_line: str, offer: list[str] | None = None, on_send=None,
                directory: Path | None = None) -> None:
    """Notification with an Open button, and a "Send to ..." button per destination in
    `offer` (on_send(destination) does the sending). Waits for the click in this
    (background) process. On Omarchy the notification itself opens the notes when
    clicked; sending stays in the menu and `chttr send`."""
    from ..integrations import label

    title = f"{done_title(directory or _directory_for(md))}: {md.stem}"
    if shutil.which("omarchy-notification-send"):
        # No buttons here, so say where sending went
        body = summary_line + "\nClick to open." + (
            f" Send it to {', '.join(label(d) for d in offer)} with: chttr send" if offer else "")
        try:
            subprocess.run(["omarchy-notification-send", "--app-name", "chttr", "-g", GLYPH, "-u", "normal",
                            "-t", "120000", title, body, "--exec", "xdg-open", str(md)],
                           capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    offer = offer or []
    actions = ["-A", "open=Open"]
    for d in offer:
        actions += ["-A", f"send:{d}=Send to {label(d)}"]
    if len(offer) > 1:
        actions += ["-A", "send:all=Send everywhere"]
    try:
        out = subprocess.run(["notify-send", "-a", "chttr", *actions, "-t", "120000", "--wait", "--", title, summary_line],
                             capture_output=True, text=True, timeout=180)
        choice = out.stdout.strip()
        if choice == "open":
            subprocess.Popen(["xdg-open", str(md)], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif choice.startswith("send:") and on_send:
            targets = offer if choice == "send:all" else [choice.removeprefix("send:")]
            for d in targets:
                if d in offer:
                    on_send(d)
    except (OSError, subprocess.TimeoutExpired):
        pass


def summary_line(result_md: Path, directory: Path) -> str:
    """"42 min · Rob, Dana, Ali (+2) · 3 action items" for the notification."""
    result = read_json(directory / "result.json", {}) or {}
    names = [k for k, v in result.get("speakers", {}).items() if v.get("seconds")]
    mins = round(result.get("duration", 0) / 60)
    line = f"{mins} min · " + ", ".join(names[:3]) + (f" (+{len(names) - 3})" if len(names) > 3 else "")
    actions = len((result.get("notes") or {}).get("action_items") or [])
    return line + (f" · {actions} action item{'s' if actions != 1 else ''}" if actions else "")
