"""The pre-meeting brief: what you agreed last time, a few minutes before the next call.

Built only from your own notes (each meeting's result.json) and your calendar:
the last meeting with the same title or mostly the same people, and the action
items from the past few weeks that are yours or theirs. `chttr brief` prints it;
the daemon shows it as a notification [meeting] brief_minutes before the event.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .meeting.store import meeting_dirs, read_json

log = logging.getLogger(__name__)

SUMMARY_CHARS = 160
TASK_CHARS = 90
MAX_ITEMS = 6
# Sections the notes had nothing for say so ("Not discussed.", "No decisions were made."):
# that is no summary.
FILLER = re.compile(
    r"^\W*(?:(?:not|nothing(?: else)?|no(?:ne)?(?: \w+){0,3}(?: was| were)?)"
    r" (?:discussed|covered|mentioned|came up|made|raised|identified|agreed|to report)"
    r"(?: (?:in|during|at|on|for|this|that|the|today|meeting|call|session|yet))*"
    r"|none|nothing|n/?a)\W*$", re.I)

_cache: dict[Path, tuple[float, dict]] = {}     # result.json -> (mtime, what we need of it)


@dataclass
class Brief:
    event_title: str
    starts_in_min: int
    attendees: list[str]                # the other people invited, by name (or email)
    last: dict | None                   # the previous meeting, as past_meetings() returns it
    open_items: list[dict] = field(default_factory=list)
    markdown: str = ""                  # the previous meeting's notes


# ---- your meetings ---------------------------------------------------------
def _first(name: str) -> str:
    """'Dana Lee' -> 'dana', 'dana.lee@x.com' -> 'dana'."""
    words = re.findall(r"[^\W\d_]+", name.split("@")[0].lower())
    return words[0] if words else ""


def _full(name: str) -> str:
    """'Dana Lee' -> 'dana lee', 'dana.lee@x.com' -> 'dana lee'."""
    return " ".join(re.findall(r"[^\W\d_]+", name.split("@")[0].lower()))


def _norm_title(title: str) -> str:
    return " ".join(title.lower().split())


def _summary(notes: dict) -> str:
    """The first real item of the first section that has one, cut to SUMMARY_CHARS."""
    for sec in notes.get("sections") or []:
        items = [str(x).strip() for x in sec.get("items") or [] if str(x).strip() and not FILLER.match(str(x))]
        if items:
            return _cut(items[0], SUMMARY_CHARS)
    return ""


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    return (head.rsplit(" ", 1)[0] if " " in head else head).rstrip(" ,;:.-") + "…"


def _meeting(directory: Path, result: dict) -> dict:
    meta, notes = result.get("meta") or {}, result.get("notes") or {}
    event, speakers = result.get("event") or {}, result.get("speakers") or {}
    title = event.get("title") or notes.get("title") or meta.get("title") or ""
    me = {str(meta["me"])} if meta.get("me") else set()
    me |= {str(k) for k, v in speakers.items() if v.get("source") == "me"}
    me |= {str(v.get("name")) for v in speakers.values() if v.get("source") == "me" and v.get("name")}
    items = [{"owner": str(a.get("owner") or "").strip(), "task": str(a.get("task") or "").strip(),
              "due": str(a.get("due") or "").strip()}
             for a in notes.get("action_items") or [] if isinstance(a, dict)]
    return {
        "id": directory.name,
        "started": float(meta.get("started") or 0),
        "title": str(title).strip(),
        "attendee_emails": sorted({str(a["email"]).lower() for a in event.get("attendees") or []
                                   if a.get("email") and not a.get("is_me")}),
        "speaker_emails": sorted({str(v["email"]).lower() for v in speakers.values()
                                  if v.get("email") and v.get("source") != "me"}),
        "me": sorted(me),
        "me_emails": sorted({str(a["email"]).lower() for a in event.get("attendees") or []
                             if a.get("email") and a.get("is_me")}
                            | {str(v["email"]).lower() for v in speakers.values()
                               if v.get("email") and v.get("source") == "me"}),
        "action_items": [a for a in items if a["task"]],
        "markdown": str(result.get("markdown") or meta.get("markdown") or ""),
        "summary": _summary(notes),
    }


def past_meetings() -> list[dict]:
    """Every processed meeting (one with a result.json), newest first."""
    out = []
    for d in meeting_dirs():
        path = d / "result.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        cached = _cache.get(path)
        if cached is None or cached[0] != mtime:
            result = read_json(path)
            if not isinstance(result, dict):
                continue
            try:
                cached = _cache[path] = (mtime, _meeting(d, result))
            except (AttributeError, TypeError, ValueError, KeyError):
                log.debug("brief: skipping %s", path, exc_info=True)     # hand-edited or from an old version
                continue
        out.append(cached[1])
    return sorted(out, key=lambda m: m["started"], reverse=True)


# ---- matching them to the next event ---------------------------------------
def others(event, me_names: set[str] | frozenset[str] = frozenset()) -> list:
    """The event's attendees other than you. Your first name only tells you apart when the
    feed doesn't say which attendee you are: a Rob Smith in your 1:1 is someone else."""
    known = any(a.is_me for a in event.attendees)
    return [a for a in event.attendees
            if not a.is_me and (known or not (a.name and _first(a.name) in me_names))]


def me_names(me_setting: str, event=None) -> set[str]:
    """Your first name(s): [meeting] me, and your entry on the invite."""
    names = {_first(me_setting)} if me_setting else set()
    for a in (event.attendees if event is not None else []):
        if a.is_me:
            names |= {_first(a.name), _first(a.email)}
    return {n for n in names if n}


def worth_a_brief(event) -> bool:
    """Not for all-day events or ones you declined."""
    if event.end - event.start >= timedelta(hours=12):
        return False
    return not any(a.is_me and a.status == "DECLINED" for a in event.attendees)


def _people(m: dict) -> set[str]:
    return set(m["attendee_emails"]) | set(m["speaker_emails"])


def _their_emails(event, meetings: list[dict], me_names: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """The other invitees' emails. Without your address in the feed, your own email would
    count as someone else's; the meetings you recorded know it."""
    yours = {e for m in meetings for e in m.get("me_emails", [])}
    return {a.email.lower() for a in others(event, me_names) if a.email} - yours


def _same_people(want: set[str], m: dict) -> bool:
    """A meeting with the same title is the same meeting unless the emails say otherwise."""
    return not want or not _people(m) or bool(want & _people(m))


def previous_meeting(event, meetings: list[dict], me_names: set[str] | frozenset[str] = frozenset()) -> dict | None:
    """The newest meeting with the same title (ignoring case) and nobody to say it was with
    other people, else the newest one that had at least half of the event's other invitees
    in it. Never this very meeting. An "Interview" or "1:1" with Zed is not last time with Cara."""
    start = event.start.timestamp()
    # a recording that began just before the event is this very meeting
    earlier = sorted((m for m in meetings if m["started"] < start - 300), key=lambda m: m["started"], reverse=True)
    want = _their_emails(event, meetings, me_names)
    title = _norm_title(event.title)
    if title and title != "(untitled)":
        same = next((m for m in earlier if _norm_title(m["title"]) == title and _same_people(want, m)), None)
        if same:
            return same
    if not want:
        return None
    return next((m for m in earlier if _people(m) and len(want & _people(m)) >= 0.5 * len(want)), None)


def open_items(meetings: list[dict], emails: set[str], names: set[str], since_days: float = 60, *,
               me: set[str] | frozenset[str] = frozenset(), title: str = "",
               now: datetime | None = None) -> list[dict]:
    """Action items from the last `since_days` days that are theirs (owner's first name in
    `names`, which may be full names) or yours from a meeting with them (an email in `emails`,
    or the same title with nobody else's emails on it). Newest first, at most MAX_ITEMS,
    each with yours: bool. When they share your first name, full names tell you apart."""
    cutoff = (now or datetime.now(UTC)).timestamp() - since_days * 86400
    title = _norm_title(title)
    firsts = {_first(n) for n in names} - {""}
    fulls = {f for n in names if " " in (f := _full(n))}

    def theirs(full: str, first: str) -> bool:
        if full in fulls:
            return True
        # "Dana Smith" is not the Dana Lee you're meeting
        return first in firsts and not (" " in full and any(f.split()[0] == first for f in fulls))

    seen: set[str] = set()
    out: list[dict] = []
    for m in sorted(meetings, key=lambda m: m["started"], reverse=True):
        if m["started"] < cutoff:
            break
        same_title = bool(title and _norm_title(m["title"]) == title)
        with_them = bool(emails & _people(m)) or (same_title and (not emails or not _people(m)))
        mine = {_first(n) for n in m["me"]} | set(me)
        mine_full = {_full(n) for n in m["me"]}
        for a in m["action_items"]:
            full = _full(a["owner"])
            owner = full.split()[0] if full else ""
            if not owner:
                continue
            if full in mine_full:
                yours = True
            elif full in fulls:
                yours = False
            else:
                yours = owner in mine
            if not (with_them if yours else theirs(full, owner)):
                continue
            key = " ".join(re.findall(r"\w+", a["task"].lower()))
            if key in seen:
                continue                # the same task carried over from meeting to meeting
            seen.add(key)
            out.append({**a, "yours": yours, "meeting": m["id"], "started": m["started"], "title": m["title"]})
            if len(out) >= MAX_ITEMS:
                return out
    return out


def brief_for(event, meetings: list[dict], me_names: set[str], now: datetime | None = None) -> Brief:
    now = now or datetime.now(UTC).astimezone()
    people = others(event, me_names)
    emails = _their_emails(event, meetings, me_names)
    names = {a.name or a.email for a in people} - {""}
    last = previous_meeting(event, meetings, me_names)
    return Brief(
        event_title=event.title,
        starts_in_min=round((event.start - now).total_seconds() / 60),
        attendees=[a.name or a.email for a in people if a.status != "DECLINED"],
        last=last,
        open_items=open_items(meetings, emails, names, me=me_names, title=event.title, now=now),
        markdown=last["markdown"] if last else "",
    )


# ---- the words ---------------------------------------------------------------
def _in(minutes: int) -> str:
    if minutes <= 0:
        return "starting now"
    if minutes < 60:
        return f"in {minutes} min"
    h, m = divmod(minutes, 60)
    return f"in {h} h {m} min" if m else f"in {h} h"


def _day(started: float, now: datetime | None = None) -> str:
    day = datetime.fromtimestamp(started).astimezone().date()
    today = (now or datetime.now(UTC)).astimezone().date()
    if day == today:
        return "today"
    if day == today - timedelta(days=1):
        return "yesterday"
    return f"{day:%b} {day.day}" + (f" {day.year}" if day.year != today.year else "")


def _names(people: list[str]) -> str:
    if len(people) > 2:
        firsts = [p.split()[0] if "@" not in p else p.split("@")[0] for p in people]
        return ", ".join(firsts[:2]) + f" and {len(people) - 2} other" + ("s" if len(people) > 3 else "")
    return " and ".join(people)


def render(brief: Brief, *, cli: bool = False, now: datetime | None = None) -> tuple[str, str]:
    """(title, body) for the notification; with cli=True the body also says where the last notes are."""
    title = f"{brief.event_title} {_in(brief.starts_in_min)}"
    lines = []
    if brief.last:
        when = _day(brief.last["started"], now)
        lines.append(f"Last met {when}: {brief.last['summary']}" if brief.last["summary"] else f"Last met {when}")
    elif brief.attendees:
        lines.append(f"First time with {_names(brief.attendees)}")
    else:
        lines.append("No notes from an earlier meeting")
    items = brief.open_items
    if items:
        k = len(items)
        lines.append(f"{k} open item{'s' if k != 1 else ''}" + (", the latest two:" if k > 2 else ":"))
        for a in items[:2]:
            who = "You" if a["yours"] else a["owner"].split()[0]
            due = f" (due {a['due']})" if a["due"] else ""
            lines.append(f"• {who}: {_cut(a['task'], TASK_CHARS)}{due}")
    if cli and brief.markdown:
        lines.append(f"Open last notes: {brief.markdown}")
    return title, "\n".join(lines)
