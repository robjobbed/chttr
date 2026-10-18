"""Meeting notes in a style: choosing the style, writing (or reusing) the notes, and
renaming speakers inside them."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ..config import Config
from .analysis import _fmt_ts, _turns
from .render import render, talk_shares
from .store import Speaker, Utterance, read_json, update_meta, write_json

log = logging.getLogger("chttr.meeting")


def _relabel(result: dict, old: str, name: str, email: str, source: str, evidence: str) -> None:
    """Rename a speaker everywhere in a result (merging into an existing speaker of that name)."""
    speakers = {k: Speaker(**v) for k, v in result["speakers"].items()}
    sp = speakers.pop(old)
    target = speakers.get(name)
    if target:
        target.clusters += [c for c in sp.clusters if c not in target.clusters]
        target.seconds += sp.seconds
        target.email = target.email or email
    else:
        sp.label, sp.name, sp.email, sp.source, sp.evidence = name, name, email or sp.email, source, evidence
        speakers[name] = sp
    result["speakers"] = {k: asdict(v) for k, v in speakers.items()}
    for u in result["utterances"]:
        if u["speaker"] == old:
            u["speaker"] = name
    pattern = re.compile(r"\b" + re.escape(old) + r"\b")
    seen = set()
    for notes in [result.get("notes") or {}, *(result.get("notes_by_style") or {}).values()]:
        if id(notes) in seen:
            continue
        seen.add(id(notes))
        for sec in notes.get("sections", []):
            sec["items"] = [pattern.sub(name, x) for x in sec.get("items", [])]
        for item in notes.get("action_items", []):
            item["owner"] = name if item.get("owner") == old else item.get("owner", "")
            item["task"] = pattern.sub(name, item.get("task", ""))



SOURCE_DESC = {"me": "the user who recorded the meeting", "captions": "named by Meet's captions",
               "voice": "recognised by voice", "calendar": "the only other invitee",
               "claude": "identified from the conversation", "corrected": "named by the user",
               "unknown": "not identified"}


def _notes_inputs(result: dict) -> tuple[str, str, str, list[str], str]:
    """(meeting, invited, speakers, unidentified, transcript) for the notes prompt."""
    meta, event = result["meta"], result.get("event") or {}
    speakers = {k: Speaker(**v) for k, v in result["speakers"].items()}
    utts = [Utterance(**u) for u in result["utterances"]]
    me = meta.get("me") or next((s.label for s in speakers.values() if s.source == "me"), "Me")
    when = datetime.fromtimestamp(float(meta["started"])).astimezone()
    meeting = "\n".join(x for x in (
        f"Calendar title: {event['title']}" if event.get("title") else "",
        f"Started: {when:%A %Y-%m-%d %H:%M} ({result['duration'] / 60:.0f} min)",
        f"Recorded by: {me} (their own microphone; their lines are labelled {me})",
        f"Calendar description: {event['description'][:1500]}" if (event.get("description") or "").strip() else "",
    ) if x)
    invited = "\n".join(f"- {a['name'] or '(no name)'} <{a['email']}>" + (" (organizer)" if a["organizer"] else "")
                        + (" (the user)" if a["is_me"] else "") + (" (declined)" if a["status"] == "DECLINED" else "")
                        for a in event.get("attendees", []))
    shares = talk_shares(speakers)
    speakers_desc = "\n".join(f"- {sp.label}: {SOURCE_DESC.get(sp.source, sp.source)}"
                              f" ({shares.get(sp.label, 0):.0%} of talk time)"
                              for sp in speakers.values() if sp.seconds)
    unidentified = [sp.label for sp in speakers.values() if sp.source == "unknown" and sp.seconds]
    transcript = "\n".join(f"[{_fmt_ts(s)}] {label}: {text}" for label, s, text in _turns(utts))
    return meeting, invited, speakers_desc, unidentified, transcript


def choose_style(result: dict, cfg: Config) -> str:
    """The meeting's own choice (set during the call), else a calendar-title rule, else
    [meeting] style, where "auto" lets Claude pick."""
    from .. import notes as N

    styles = N.load_styles()
    chosen = result["meta"].get("style", "")
    if chosen in styles or (chosen == N.CUSTOM and result["meta"].get("style_extra")):
        return chosen
    title = (result.get("event") or {}).get("title") or result["meta"].get("title") or ""
    for rule in cfg.meeting.style_rules:
        try:
            if title and rule.get("style") in styles and re.search(rule.get("match", ""), title, re.IGNORECASE):
                return rule["style"]
        except re.error as exc:
            log.warning("style rule %r: %s", rule, exc)
    if cfg.meeting.style in styles:
        return cfg.meeting.style
    if cfg.meeting.style == "auto":
        _, invited, _, _, transcript = _notes_inputs(result)
        try:
            picked = N.pick_style(cfg.polish, styles, title, invited, transcript[:6000])
            log.info("style picked automatically: %s", picked)
            return picked
        except Exception as exc:  # noqa: BLE001
            log.warning("automatic style pick failed (%s); using general", exc)
    return "general"


def write_notes(result: dict, cfg: Config, style_id: str, extra: str = "", fresh: bool = True) -> dict:
    """Write (or reuse) the notes for one style and make them the meeting's current notes.
    style_id "custom" with `extra` = free-form instructions."""
    from .. import notes as N

    styles = N.load_styles()
    if style_id == N.CUSTOM:
        if not extra:
            raise ValueError("a custom style needs instructions (--prompt)")
        style = N.custom_style(extra)
        extra_for_style = ""
    elif style_id in styles:
        style, extra_for_style = styles[style_id], extra
    else:
        raise ValueError(f"no note style {style_id!r} (see: chttr meeting styles)")
    key = style_id + (f"+{hashlib.sha1(extra.encode()).hexdigest()[:8]}" if extra else "")
    cache = result.setdefault("notes_by_style", {})
    if not fresh and key in cache:
        result["notes"] = cache[key]
        return cache[key]
    meeting, invited, speakers_desc, unidentified, transcript = _notes_inputs(result)
    notes = N.write_notes(cfg.polish, cfg.meeting.summary_effort, style, meeting, invited, speakers_desc,
                          unidentified, transcript, extra_for_style)
    if style_id == N.CUSTOM:
        notes["extra"] = extra
    cache[key] = notes
    result["notes"] = notes
    for guess in notes.get("speakers", []):
        label = guess.get("label", "")
        sp = result["speakers"].get(label)
        if sp and sp["source"] == "unknown" and guess.get("name") and guess.get("confidence") in ("high", "medium"):
            _relabel(result, label, guess["name"], guess.get("email", ""), "claude",
                     f"{guess['confidence']} confidence: {guess.get('evidence', '')}")
    return notes


def restyle(directory: Path, cfg: Config, style_id: str, extra: str = "", fresh: bool = False) -> Path:
    """Switch a processed meeting's notes to another style (from the cache when it was
    written before, unless `fresh`) and rewrite the Markdown."""
    from .. import notes as N

    result = read_json(directory / "result.json")
    if not result:
        raise SystemExit(f"{directory.name} has not been processed yet")
    if not result["utterances"]:
        raise SystemExit("this meeting has no transcript to write notes from")
    if not N.available(cfg.polish):
        raise SystemExit("note styles need Claude or Ollama: add a Claude key (chttr connect claude) or run Ollama")
    write_notes(result, cfg, style_id, extra, fresh=fresh)
    md = render(directory, result, cfg)
    result["markdown"] = str(md)
    write_json(directory / "result.json", result)
    update_meta(directory, style=style_id)
    return md

