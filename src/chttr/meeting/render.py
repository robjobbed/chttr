"""The meeting notes as Markdown (frontmatter, notes, transcript, speakers)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from ..config import Config
from .analysis import _fmt_ts, _turns
from .store import Speaker, Utterance


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _bar(share: float, cells: int = 10) -> str:
    full = round(share * cells)
    return "▇" * full + "░" * (cells - full)


def talk_shares(speakers: dict[str, Speaker]) -> dict[str, float]:
    """Each speaker's share of the talking (0..1), for those who spoke."""
    total = sum(sp.seconds for sp in speakers.values() if sp.seconds > 0)
    return {sp.label: sp.seconds / total for sp in speakers.values() if sp.seconds > 0} if total else {}


def _filename(title: str, when: datetime) -> str:
    safe = " ".join(re.sub(r"[/\\:*?\"<>|\n\r\t]+", " ", title).split())[:80] or "Meeting"
    return f"{when:%Y-%m-%d %H%M} {safe}"


def render(directory: Path, result: dict, cfg: Config) -> Path:
    meta, notes, event = result["meta"], result.get("notes") or {}, result.get("event")
    speakers = {k: Speaker(**v) for k, v in result["speakers"].items()}
    utts = [Utterance(**u) for u in result["utterances"]]
    started = datetime.fromtimestamp(float(meta["started"])).astimezone()
    ended = started + timedelta(seconds=result["duration"])
    title = (event or {}).get("title") or notes.get("title") or meta.get("title") or "Meeting"
    attendees = (event or {}).get("attendees", [])
    spoke = {sp.email for sp in speakers.values() if sp.email and sp.seconds} | \
            {sp.name.lower() for sp in speakers.values() if sp.seconds}

    lines = ["---", f"title: {_q(title)}", f"date: {started:%Y-%m-%d}", f"start: {_q(f'{started:%H:%M}')}",
             f"end: {_q(f'{ended:%H:%M}')}", f"duration_minutes: {round(result['duration'] / 60)}"]
    if meta.get("code"):
        lines.append(f"meet: {_q('https://meet.google.com/' + meta['code'])}")
    if event:
        lines.append(f"calendar_event: {_q(event['title'])}")
    if attendees:
        lines.append("attendees:")
        for a in attendees:
            lines += [f"  - name: {_q(a['name'])}", f"    email: {_q(a['email'])}",
                      f"    spoke: {'true' if a['email'] in spoke or (a['name'] and a['name'].lower() in spoke) else 'false'}"]
    lines.append("speakers: [" + ", ".join(_q(s.label) for s in speakers.values() if s.seconds) + "]")
    shares = talk_shares(speakers)
    if shares:
        lines.append("talk_time:")
        lines += [f"  {_q(label)}: {round(share * 100)}" for label, share in shares.items()]
    if notes.get("style"):
        lines.append(f"notes_style: {_q(notes['style'])}")
    lines += ["tags: [meeting]", "---", "", f"# {title}", ""]
    when = f"{started:%a %b} {started.day}, {started:%Y} · {started:%H:%M}–{ended:%H:%M} ({round(result['duration'] / 60)} min)"
    lines.append(f"**{when}**" + (f" · Google Meet `{meta['code']}`" if meta.get("code") else ""))
    if attendees:
        names = [(a["name"] or a["email"]) + (" (organizer)" if a["organizer"] else "") for a in attendees
                 if a["status"] != "DECLINED"]
        lines += ["", "**Invited:** " + ", ".join(names)]
    extra = [s.label for s in speakers.values() if s.seconds and s.source != "me"
             and not any(s.email and s.email == a["email"] for a in attendees)
             and not any(a["name"] and a["name"].lower() == s.name.lower() for a in attendees)]
    if attendees and extra:
        lines.append("**Also speaking:** " + ", ".join(extra))
    lines.append("")

    lite = notes.get("style") == "lite"
    if notes.get("error"):
        retry = ""
        if lite:     # the style that was asked for, so trying again doesn't change it
            wanted = notes.get("wanted") or meta.get("style") or "general"
            extra = re.sub(r'["`]', "'", meta.get("style_extra", ""))[:200]
            how = f'--prompt "{extra}"' if wanted == "custom" and extra else \
                (wanted if wanted != "custom" else "general")
            retry = f" Try again with `chttr meeting style {how} --meeting {directory.name}`."
        lines += [f"> Notes could not be written: {str(notes['error']).rstrip('.')}.{retry}", ""]
    if lite:
        hint = "" if notes.get("error") else " · add a Claude key (chttr connect claude) or run Ollama for full notes"
        lines += ["*On-device notes*" + hint, ""]
    elif notes.get("sections"):
        how = f"*{notes.get('style_name', 'Notes')} notes*"
        if notes.get("style") == "custom" and notes.get("extra"):
            how = f"*Custom notes: {notes['extra'][:200]}*"
        elif notes.get("extra"):
            how += f" *({notes['extra'][:200]})*"
        lines += [how + f" · switch with `chttr meeting style <style> --meeting {directory.name}`", ""]
    for sec in notes.get("sections", []):
        items = [x.strip() for x in sec.get("items", []) if x.strip()]
        if not items:
            continue
        lines += [f"## {sec['title']}", ""]
        if sec.get("kind") == "paragraph":
            lines += [x for item in items for x in (item, "")]
        elif sec.get("kind") == "quotes":
            lines += [x for item in items for x in (f"> {item}", "")]
        else:
            lines += [f"- {x}" for x in items] + [""]
    actions = notes.get("action_items", [])
    if actions:
        lines += [f"## {notes.get('actions_heading') or 'Action items'}", ""]
        lines += ["- [ ] " + (f"**{a['owner']}**: " if a.get("owner") else "") + a["task"]
                  + (f" *(due {a['due']})*" if a.get("due") else "") for a in actions] + [""]

    lines += ["## Transcript", ""]
    for label, start, text in _turns(utts):
        lines += [f"**{label}** · {_fmt_ts(start)}  ", text, ""]

    how = {"me": "your microphone", "captions": "Meet captions", "voice": "remembered voice",
           "calendar": "calendar (only other invitee)", "claude": "inferred by Claude", "corrected": "corrected by you",
           "unknown": "not identified"}
    lines += ["## Speakers", ""]
    for sp in speakers.values():
        if not sp.seconds:
            continue
        who = sp.label + (f" <{sp.email}>" if sp.email else "")
        detail = f" ({sp.evidence})" if sp.evidence and sp.source in ("claude", "voice") else ""
        share = shares.get(sp.label, 0.0)
        lines.append(f"- {who}: {how.get(sp.source, sp.source)}{detail}, {sp.seconds / 60:.1f} min"
                     f" · {share:.0%} {_bar(share)}")
    unsure = [sp.label for sp in speakers.values() if sp.source in ("unknown", "claude") and sp.seconds]
    if unsure:
        lines += ["", f"Wrong or missing name? `chttr meeting rename \"{unsure[0]}\" \"Their Name\" --meeting {directory.name}`"]
    lines.append("")

    out_dir = Path(cfg.meeting.dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    previous = result.get("markdown") or meta.get("markdown")
    if previous and Path(previous).parent == out_dir and Path(previous).exists():
        path = Path(previous)          # re-render in place (rename, reprocess)
    else:
        base = _filename(title, started)
        path = out_dir / f"{base}.md"
        n = 2
        while path.exists():
            path = out_dir / f"{base} ({n}).md"
            n += 1
    path.write_text("\n".join(lines), encoding="utf-8")
    return path

