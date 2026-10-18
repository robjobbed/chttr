"""Google Calendar through its secret iCal address: which event a meeting belongs to, and who was invited.

The secret address (Calendar settings > your calendar > "Secret address in iCal
format") needs no OAuth app or token refresh, and Google includes every
attendee's name and email plus the event's Meet link. The URLs live one per line
in [meeting] calendar_file (chmod 600: anyone with the URL can read the calendar).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger("chttr.calendar")

CACHE_DIR = DATA_DIR / "calendar-cache"
CACHE_SECONDS = 600
MEET_CODE = re.compile(r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})")


@dataclass
class Attendee:
    name: str
    email: str
    organizer: bool = False
    status: str = ""        # ACCEPTED / DECLINED / TENTATIVE / NEEDS-ACTION
    is_me: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "email": self.email, "organizer": self.organizer,
                "status": self.status, "is_me": self.is_me}


@dataclass
class Event:
    title: str
    start: datetime
    end: datetime
    meet_code: str = ""
    description: str = ""
    attendees: list[Attendee] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"title": self.title, "start": self.start.isoformat(), "end": self.end.isoformat(),
                "meet_code": self.meet_code, "description": self.description[:2000],
                "attendees": [a.to_dict() for a in self.attendees]}


def read_urls(path: str) -> list[str]:
    p = Path(path).expanduser()
    if not p.is_file():
        return []
    return [line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]


def add_url(path: str, url: str) -> Path:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    urls = read_urls(path)
    if url not in urls:
        urls.append(url)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(urls) + "\n")
    os.chmod(p, 0o600)
    return p


def _fetch(url: str, max_age: float = CACHE_SECONDS) -> bytes:
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".ics")
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age:
        return cache.read_bytes()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "chttr"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
    except OSError as exc:
        if cache.exists():
            log.warning("calendar fetch failed (%s); using the cached copy", exc)
            return cache.read_bytes()
        raise
    fd = os.open(cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return data


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    return datetime(value.year, value.month, value.day).astimezone()    # all-day event


def _email(value) -> str:
    return str(value).removeprefix("mailto:").removeprefix("MAILTO:").strip().lower()


def _events(data: bytes, around: datetime, window: timedelta, me_email: str = "") -> tuple[list[Event], str]:
    """Events within `window` of `around`, and whose calendar it is. You are the attendee
    whose address names the feed (a primary Google calendar is named after its owner; a
    secondary one is not) or is `me_email` ([meeting] email); both count, so several
    feeds under different addresses all know you."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(data)
    calname = str(cal.get("X-WR-CALNAME", "")).strip().lower()
    mine = ({me_email.strip().lower()} | ({calname} if "@" in calname else set())) - {""}
    owner = calname if "@" in calname else me_email.strip().lower()
    events = []
    for comp in recurring_ical_events.of(cal).between(around - window, around + window):
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start, end = comp.get("DTSTART"), comp.get("DTEND")
        if start is None:
            continue
        start_dt = _as_datetime(start.dt)
        end_dt = _as_datetime(end.dt) if end is not None else start_dt + timedelta(hours=1)
        text = " ".join(str(comp.get(k, "")) for k in ("X-GOOGLE-CONFERENCE", "LOCATION", "DESCRIPTION"))
        m = MEET_CODE.search(text)
        attendees = []
        organizer = comp.get("ORGANIZER")
        organizer_email = _email(organizer) if organizer is not None else ""
        raw = comp.get("ATTENDEE") or []
        for a in raw if isinstance(raw, list) else [raw]:
            email = _email(a)
            name = str(a.params.get("CN", "")).strip()
            if not name or name.lower() == email:
                name = ""
            if a.params.get("CUTYPE", "").upper() in ("RESOURCE", "ROOM"):
                continue
            attendees.append(Attendee(name=name, email=email, organizer=email == organizer_email,
                                      status=str(a.params.get("PARTSTAT", "")).upper(),
                                      is_me=email in mine))
        events.append(Event(title=str(comp.get("SUMMARY", "")).strip() or "(untitled)", start=start_dt, end=end_dt,
                            meet_code=m.group(1) if m else "", description=str(comp.get("DESCRIPTION", "")),
                            attendees=attendees))
    return events, owner


def find_event(urls: list[str], code: str = "", when: datetime | None = None, me_email: str = "") -> Event | None:
    """The calendar event for a meeting: same Meet code within 12 hours if we know it,
    else the event with a Meet link (or attendees) that overlaps `when`."""
    when = when or datetime.now(UTC).astimezone()
    candidates: list[Event] = []
    for url in urls:
        try:
            events, _owner = _events(_fetch(url), when, timedelta(hours=12), me_email)
        except Exception as exc:  # noqa: BLE001 - a broken feed must not break the meeting
            log.warning("calendar %s…: %s", url[:40], exc)
            continue
        candidates += events
    if code:
        same = [e for e in candidates if e.meet_code == code]
        if same:
            return min(same, key=lambda e: abs((e.start - when).total_seconds()))
    slack = timedelta(minutes=10)
    now = [e for e in candidates if e.start - slack <= when <= e.end + slack and (e.end - e.start) < timedelta(hours=12)]
    for preferred in ([e for e in now if e.meet_code], [e for e in now if e.attendees]):
        if preferred:
            return min(preferred, key=lambda e: abs((e.start - when).total_seconds()))
    return None


def upcoming(urls: list[str], hours: float = 24, me_email: str = "") -> list[Event]:
    """Events from now on (for `chttr meeting calendar` to show that the feed works)."""
    now = datetime.now(UTC).astimezone()
    out: list[Event] = []
    for url in urls:
        events, _ = _events(_fetch(url, max_age=0), now + timedelta(hours=hours / 2), timedelta(hours=hours / 2),
                            me_email)
        out += [e for e in events if e.end >= now]
    return sorted(out, key=lambda e: e.start)
