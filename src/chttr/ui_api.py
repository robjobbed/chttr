"""The Home page and the Dictations tab of `chttr ui`: read-only JSON over the history and the library.

`route()` answers a GET path it knows with (status, body, content type) and returns None for
anything else, so `ui.Handler.do_GET` can try it before its own routes. The handler has already
checked the token and the Host header.

    /api/dashboard                  dictation totals, the last 30 days, meetings at a glance
    /api/dictations?q=&app=&limit=  newest first, filtered by a plain substring
    /api/dictation/<id>             one dictation and what chttr heard against what it typed
    /api/dictation/<id>/audio       its WAV, when [history] keep_audio_days kept it
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from . import config as C
from . import history as H
from . import library as L

log = logging.getLogger("chttr.ui")
_FILE = re.compile(r"^[\w.-]+$")
_ID = re.compile(r"^\d{1,9}$")
_UNNAMED = re.compile(r"^(Speaker|In the room)\s*\d*$", re.IGNORECASE)
JSON = "application/json; charset=utf-8"
MAX_LIMIT = 2000


def _json(data, status: int = 200) -> tuple[int, bytes, str]:
    return status, json.dumps(data, ensure_ascii=False).encode(), JSON


def _error(status: int, message: str) -> tuple[int, bytes, str]:
    return _json({"error": message}, status)


def route(path: str, q: dict, cfg: C.Config | None = None) -> tuple[int, bytes, str] | None:
    if path == "/api/dashboard":
        return _json(dashboard())
    if path == "/api/dictations":
        try:
            limit = max(1, min(MAX_LIMIT, int(q.get("limit") or 200)))
        except ValueError:
            return _error(400, "limit must be a number")
        return _json(dictations(q.get("q", ""), q.get("app", ""), limit))
    m = re.match(r"^/api/dictation/([^/]+)(/audio)?$", path)
    if not m:
        return None
    entry = _entry(m.group(1))
    if entry is None:
        return _error(404, "no such dictation")
    if m.group(2):
        f = _audio_file(entry)
        if f is None:
            return _error(404, "no audio kept for this dictation")
        return 200, f.read_bytes(), "audio/wav"
    row = _row(entry)
    # the diff only means something for a dictation chttr typed: a voice command's raw is the
    # instruction, and a failed one stored no text
    diff = H.word_diff(row["raw"], row["text"]) if row["typed"] and row["kind"] != "command" else []
    return _json({**row, "diff": diff, "words": len(row["text"].split()),
                  "keep_audio_days": _keep_audio_days(cfg)})


def _keep_audio_days(cfg: C.Config | None) -> int:
    try:
        return int((cfg or C.load_config()).history.keep_audio_days)
    except (SystemExit, Exception):  # noqa: BLE001 - a broken config only costs the replay hint
        return 0


# ---- dictations ---------------------------------------------------------------------------
def _audio_file(entry: dict):
    """The kept WAV for a dictation, only ever from inside HISTORY_AUDIO_DIR."""
    name = str(entry.get("audio") or "")
    if not name or not _FILE.match(name) or name.strip(".") == "":
        return None
    root = C.HISTORY_AUDIO_DIR.resolve()
    f = (C.HISTORY_AUDIO_DIR / name).resolve()
    if root not in f.parents or not f.is_file():
        return None
    return f


def _typed(e: dict) -> bool:
    """Whether chttr typed, pasted or copied anything. A command or cleanup that failed still lands in
    the history (with no text), and so does a dictation it couldn't deliver (with no output)."""
    return bool(str(e.get("text") or "").strip()) and e.get("output", "type") != ""


def _row(e: dict) -> dict:
    return {"id": e["id"], "ts": e.get("ts", ""), "app": e.get("app", ""), "mode": e.get("mode", ""),
            "kind": e.get("kind", "dictate"), "seconds": float(e.get("seconds") or 0), "output": e.get("output", ""),
            "raw": str(e.get("raw") or ""), "text": str(e.get("text") or e.get("raw") or ""),
            "typed": _typed(e), "has_audio": _audio_file(e) is not None}


def _entry(raw_id: str) -> dict | None:
    if not _ID.match(raw_id):
        return None                 # ids are line positions; anything else (../, names) is not ours
    entries = H.load()
    i = int(raw_id)
    return {**entries[i], "id": str(i)} if i < len(entries) else None


def dictations(query: str = "", app: str = "", limit: int = 200) -> list[dict]:
    needle, app = " ".join(query.split()).casefold(), app.strip().casefold()
    out = []
    for e in reversed(H.entries_with_ids(H.load())):
        if app and str(e.get("app") or "").casefold() != app:
            continue
        if needle and needle not in " ".join(f"{e.get('text') or ''} {e.get('raw') or ''}".split()).casefold():
            continue
        out.append(_row(e))
        if len(out) >= limit:
            break
    return out


# ---- dashboard ----------------------------------------------------------------------------
def _meetings_overview() -> dict:
    meetings = L.meetings(limit=1000)
    week_ago = datetime.now().astimezone() - timedelta(days=7)

    def recent(m: dict) -> bool:
        try:
            return datetime.fromisoformat(m["when"]) >= week_ago
        except (KeyError, TypeError, ValueError):
            return False

    named = {s for m in meetings for s in m.get("speakers", []) if s and not _UNNAMED.match(s)}
    return {"meetings_total": len(meetings), "meetings_last_7d": sum(1 for m in meetings if recent(m)),
            "minutes_total": sum(int(m.get("minutes") or 0) for m in meetings), "speakers_named": len(named),
            "open_actions": len(L.action_items()),
            "recent": [{k: m.get(k) for k in ("id", "title", "when", "minutes", "speakers", "summary")}
                       for m in meetings[:3]]}


def dashboard() -> dict:
    try:
        meetings = _meetings_overview()
    except Exception as exc:  # noqa: BLE001 - the dictation numbers still show without the library
        log.warning("dashboard: meeting library unavailable: %s", exc)
        meetings = {"meetings_total": 0, "meetings_last_7d": 0, "minutes_total": 0, "speakers_named": 0,
                    "open_actions": 0, "recent": [], "error": str(exc)}
    return {"history": H.summary(H.load(), 30), "typing_wpm": H.TYPING_WPM, "meetings": meetings}
