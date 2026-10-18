"""The meeting library: a local full-text index over every meeting and dictation.

~/.local/share/chttr/library.sqlite (0600) is rebuilt incrementally from the
meetings' result.json files and the dictation history, so it can be deleted any
time. SQLite's FTS5 does the searching (BM25 ranking, snippets); `chttr ask`,
`chttr mcp` and `chttr ui` all read through this module.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config as C

DB_FILE = C.DATA_DIR / "library.sqlite"
SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY, title TEXT, started REAL, duration REAL, markdown TEXT, style TEXT,
    speakers TEXT, summary TEXT, mtime REAL
);
CREATE TABLE IF NOT EXISTS lines (
    meeting_id TEXT, start REAL, speaker TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS lines_meeting ON lines (meeting_id, start);
CREATE VIRTUAL TABLE IF NOT EXISTS lines_fts USING fts5 (
    text, speaker, meeting_id UNINDEXED, start UNINDEXED, tokenize = 'porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5 (
    title, body, meeting_id UNINDEXED, tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS actions (
    meeting_id TEXT, owner TEXT, task TEXT, due TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS dictations_fts USING fts5 (
    text, app UNINDEXED, ts UNINDEXED, tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class Hit:
    kind: str               # line | notes | dictation
    meeting_id: str
    title: str
    when: str               # ISO date-time of the meeting (or dictation)
    start: float            # seconds into the meeting (lines)
    speaker: str
    snippet: str
    score: float

    def to_dict(self) -> dict:
        return asdict(self)


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = path or DB_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    conn = sqlite3.connect(path)
    if new and os.name == "posix":
        os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _notes_text(notes: dict) -> str:
    parts = []
    for sec in notes.get("sections", []):
        parts.append(sec.get("title", ""))
        parts += [str(x) for x in sec.get("items", [])]
    for a in notes.get("action_items", []):
        parts.append(f"{a.get('owner', '')}: {a.get('task', '')} {a.get('due', '')}")
    return "\n".join(p for p in parts if p)


def _summary(notes: dict) -> str:
    for sec in notes.get("sections", []):
        items = [str(x) for x in sec.get("items", []) if str(x).strip()]
        if items:
            return " ".join(items[:3])[:600]
    return ""


def index_meeting(conn: sqlite3.Connection, directory: Path, result: dict) -> None:
    mid = directory.name
    meta, notes = result.get("meta", {}), result.get("notes") or {}
    title = (result.get("event") or {}).get("title") or notes.get("title") or meta.get("title") or "Meeting"
    for table in ("lines", "actions"):
        conn.execute(f"DELETE FROM {table} WHERE meeting_id = ?", (mid,))
    for table in ("lines_fts", "notes_fts"):
        conn.execute(f"DELETE FROM {table} WHERE meeting_id = ?", (mid,))
    speakers = [k for k, v in result.get("speakers", {}).items() if v.get("seconds")]
    conn.execute("INSERT OR REPLACE INTO meetings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (mid, title, float(meta.get("started", 0)), float(result.get("duration", 0)),
                  result.get("markdown", ""), notes.get("style", ""), json.dumps(speakers), _summary(notes),
                  (directory / "result.json").stat().st_mtime))
    rows = [(mid, float(u["start"]), u.get("speaker", ""), u.get("text", "")) for u in result.get("utterances", [])
            if u.get("text")]
    conn.executemany("INSERT INTO lines VALUES (?, ?, ?, ?)", rows)
    conn.executemany("INSERT INTO lines_fts (text, speaker, meeting_id, start) VALUES (?, ?, ?, ?)",
                     [(t, s, m, st) for m, st, s, t in rows])
    conn.execute("INSERT INTO notes_fts (title, body, meeting_id) VALUES (?, ?, ?)", (title, _notes_text(notes), mid))
    conn.executemany("INSERT INTO actions VALUES (?, ?, ?, ?)",
                     [(mid, a.get("owner", ""), a.get("task", ""), a.get("due", ""))
                      for a in notes.get("action_items", [])])


def sync(conn: sqlite3.Connection | None = None) -> int:
    """Bring the index up to date with the meetings on disk and the dictation history.
    Returns how many meetings were (re)indexed."""
    if conn is None:
        with connect() as c:
            return sync(c)
    from .meeting import meeting_dirs, read_json

    known = {r["id"]: r["mtime"] for r in conn.execute("SELECT id, mtime FROM meetings")}
    on_disk = set()
    changed = 0
    for d in meeting_dirs():
        f = d / "result.json"
        if not f.exists():
            continue
        on_disk.add(d.name)
        if known.get(d.name) == f.stat().st_mtime:
            continue
        result = read_json(f)
        if result:
            index_meeting(conn, d, result)
            changed += 1
    for gone in set(known) - on_disk:
        for table in ("meetings", "lines", "actions"):
            conn.execute(f"DELETE FROM {table} WHERE {'id' if table == 'meetings' else 'meeting_id'} = ?", (gone,))
        for table in ("lines_fts", "notes_fts"):
            conn.execute(f"DELETE FROM {table} WHERE meeting_id = ?", (gone,))
    _sync_dictations(conn)
    return changed


def _sync_dictations(conn: sqlite3.Connection) -> None:
    if not C.HISTORY_FILE.exists():
        return
    size = str(C.HISTORY_FILE.stat().st_size)
    row = conn.execute("SELECT value FROM state WHERE key = 'history_size'").fetchone()
    if row and row["value"] == size:
        return
    from .history import load

    conn.execute("DELETE FROM dictations_fts")
    conn.executemany("INSERT INTO dictations_fts (text, app, ts) VALUES (?, ?, ?)",
                     [(e.get("text") or e.get("raw") or "", e.get("app", ""), e.get("ts", "")) for e in load()])
    conn.execute("INSERT OR REPLACE INTO state VALUES ('history_size', ?)", (size,))


def fts_query(text: str, any_word: bool = False, prefix_last: bool = False) -> str:
    """A safe FTS5 query from free text: every word quoted (no operator injection),
    all required by default, any of them with any_word. With prefix_last the last
    word also matches longer words ("contr" finds "contract"), for search as you type;
    only from 3 characters, so "co" doesn't match half the transcript."""
    words = [w for w in re.findall(r"[\w'’-]+", text.lower()) if len(w) > 1 or w.isdigit()]
    parts = [f'"{w}"' for w in words]
    if prefix_last and parts and len(words[-1]) >= 3:
        parts[-1] += "*"
    return (" OR " if any_word else " ").join(parts)


_FILTER = re.compile(r'(?<!\S)(speaker|from|after|before|on):(?:"([^"]*)"?|(\S*))', re.IGNORECASE)


def _filter_date(value: str) -> str:
    """YYYY-MM-DD (or today / yesterday) -> ISO date; "" for anything else, so a date
    that is still being typed is ignored rather than searched for as words."""
    value = value.strip().lower()
    if value in ("today", "yesterday"):
        day = datetime.now().astimezone().date()
        return (day - timedelta(days=1) if value == "yesterday" else day).isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def parse_query(text: str) -> tuple[str, str, str, str]:
    """Search box text -> (words, speaker, after, before).
    `speaker:dana` (or `from:`, `speaker:"Dana Lee"`), `after:2026-10-01`, `before:…` and
    `on:…` (that day) are filters; everything else is the words to search for.
    Mirrored by parseQuery in ui_static/lib.js."""
    speaker = after = before = ""
    for m in _FILTER.finditer(text):
        key, value = m.group(1).lower(), (m.group(2) if m.group(2) is not None else m.group(3)).strip()
        if key in ("speaker", "from"):
            speaker = value
        elif key == "on":
            after = before = _filter_date(value)
        elif key == "after":
            after = _filter_date(value)
        else:
            before = _filter_date(value)
    words = " ".join(_FILTER.sub(" ", text).split())
    return words, speaker, after, before


def _when(started: float) -> str:
    return datetime.fromtimestamp(started).astimezone().isoformat(timespec="minutes") if started else ""


def search(query: str, limit: int = 20, speaker: str = "", after: str = "", before: str = "",
           include_dictations: bool = False, conn: sqlite3.Connection | None = None,
           prefix_last: bool = False, meeting_id: str = "") -> list[Hit]:
    """Ranked transcript lines and notes matching `query` (all words, else any word).
    `after` / `before` are ISO dates (YYYY-MM-DD). Without explicit filters, `query` is
    read as search box text: `speaker:dana budget after:2026-10-01` (see parse_query)."""
    return search_with_facets(query, limit, speaker, after, before, include_dictations, conn, prefix_last,
                              meeting_id, facets_too=False)[0]


def search_with_facets(query: str, limit: int = 20, speaker: str = "", after: str = "", before: str = "",
                       include_dictations: bool = False, conn: sqlite3.Connection | None = None,
                       prefix_last: bool = False, meeting_id: str = "",
                       facets_too: bool = True) -> tuple[list[Hit], list[dict]]:
    """`search`, plus how many lines matched in each meeting (for narrowing a search)."""
    if conn is None:
        with connect() as c:
            sync(c)
            return search_with_facets(query, limit, speaker, after, before, include_dictations, c, prefix_last,
                                      meeting_id, facets_too)
    if not (speaker or after or before):
        query, speaker, after, before = parse_query(query)
    lo, hi = _date_bounds(after, before)
    if not fts_query(query):
        if not (speaker or after or before or meeting_id):
            return [], []
        return _browse(conn, limit, speaker, lo, hi, meeting_id), []
    for any_word in (False, True):
        q = fts_query(query, any_word, prefix_last)
        hits = _search(conn, q, limit, speaker, lo, hi, include_dictations, meeting_id)
        if hits:
            return hits, facets(conn, q, lo, hi, speaker) if facets_too else []
    return [], []


def _date_bounds(after: str, before: str) -> tuple[float, float]:
    lo = datetime.fromisoformat(after).astimezone().timestamp() if after else 0.0
    hi = datetime.fromisoformat(before).astimezone().timestamp() + 86400 if before else 1e12
    return lo, hi


def _search(conn: sqlite3.Connection, q: str, limit: int, speaker: str, lo: float, hi: float,
            include_dictations: bool, meeting_id: str = "") -> list[Hit]:
    hits = []
    rows = conn.execute(
        "SELECT f.meeting_id, f.start, f.speaker, snippet(lines_fts, 0, '**', '**', '…', 24) AS snip, "
        "bm25(lines_fts) AS score, m.title, m.started FROM lines_fts f JOIN meetings m ON m.id = f.meeting_id "
        "WHERE lines_fts MATCH ? AND m.started BETWEEN ? AND ? AND (? = '' OR f.speaker LIKE ?) "
        "AND (? = '' OR f.meeting_id = ?) ORDER BY score LIMIT ?",
        (q, lo, hi, speaker, f"%{speaker}%", meeting_id, meeting_id, limit)).fetchall()
    hits += [Hit("line", r["meeting_id"], r["title"], _when(r["started"]), float(r["start"]), r["speaker"],
                 r["snip"], float(r["score"])) for r in rows]
    if not speaker:
        rows = conn.execute(
            "SELECT f.meeting_id, snippet(notes_fts, 1, '**', '**', '…', 24) AS snip, bm25(notes_fts) AS score, "
            "m.title, m.started FROM notes_fts f JOIN meetings m ON m.id = f.meeting_id "
            "WHERE notes_fts MATCH ? AND m.started BETWEEN ? AND ? AND (? = '' OR f.meeting_id = ?) "
            "ORDER BY score LIMIT ?",
            (q, lo, hi, meeting_id, meeting_id, max(3, limit // 4))).fetchall()
        hits += [Hit("notes", r["meeting_id"], r["title"], _when(r["started"]), 0.0, "", r["snip"],
                     float(r["score"]) - 0.5) for r in rows]
    if include_dictations and not meeting_id:
        rows = conn.execute("SELECT text, app, ts, bm25(dictations_fts) AS score FROM dictations_fts "
                            "WHERE dictations_fts MATCH ? ORDER BY score LIMIT ?", (q, limit)).fetchall()
        hits += [Hit("dictation", "", r["app"], r["ts"], 0.0, "", r["text"][:300], float(r["score"]))
                 for r in rows]
    return sorted(hits, key=lambda h: h.score)[:limit]


def facets(conn: sqlite3.Connection, q: str, lo: float, hi: float, speaker: str = "") -> list[dict]:
    """Matching transcript lines per meeting, most first: [{meeting_id, title, when, count}]."""
    rows = conn.execute(
        "SELECT f.meeting_id, m.title, m.started, count(*) AS n FROM lines_fts f JOIN meetings m ON m.id = f.meeting_id "
        "WHERE lines_fts MATCH ? AND m.started BETWEEN ? AND ? AND (? = '' OR f.speaker LIKE ?) "
        "GROUP BY f.meeting_id ORDER BY n DESC, m.started DESC", (q, lo, hi, speaker, f"%{speaker}%")).fetchall()
    return [{"meeting_id": r["meeting_id"], "title": r["title"], "when": _when(r["started"]), "count": r["n"]}
            for r in rows]


def _browse(conn: sqlite3.Connection, limit: int, speaker: str, lo: float, hi: float, meeting_id: str) -> list[Hit]:
    """Filters without words (`speaker:dana`, `on:2026-10-01`): that speaker's lines, newest
    meeting first; or, for dates alone, the meetings in that range."""
    if not speaker and not meeting_id:
        rows = conn.execute("SELECT id, title, started, summary FROM meetings WHERE started BETWEEN ? AND ? "
                            "ORDER BY started DESC LIMIT ?", (lo, hi, limit)).fetchall()
        return [Hit("notes", r["id"], r["title"], _when(r["started"]), 0.0, "", r["summary"] or "", 0.0)
                for r in rows]
    rows = conn.execute(
        "SELECT l.meeting_id, l.start, l.speaker, l.text, m.title, m.started FROM lines l "
        "JOIN meetings m ON m.id = l.meeting_id WHERE m.started BETWEEN ? AND ? AND (? = '' OR l.speaker LIKE ?) "
        "AND (? = '' OR l.meeting_id = ?) ORDER BY m.started DESC, l.start LIMIT ?",
        (lo, hi, speaker, f"%{speaker}%", meeting_id, meeting_id, limit)).fetchall()
    return [Hit("line", r["meeting_id"], r["title"], _when(r["started"]), float(r["start"]), r["speaker"],
                r["text"][:300], 0.0) for r in rows]


def turns(transcript_lines: list[dict], gap: float = 3.0) -> list[dict]:
    """Consecutive lines by the same speaker, less than `gap` seconds apart, read as one turn:
    [{speaker, start, end, text, lines: [{start, end, text}]}]. Mirrored by turnsFromLines in
    ui_static/lib.js."""
    out: list[dict] = []
    for u in transcript_lines:
        start = float(u.get("start", 0))
        end = float(u.get("end") or start)
        line = {"start": start, "end": max(end, start), "text": str(u.get("text", ""))}
        last = out[-1] if out else None
        if last and last["speaker"] == u.get("speaker", "") and start - last["end"] < gap:
            last["lines"].append(line)
            last["end"] = max(last["end"], line["end"])
            last["text"] += " " + line["text"]
        else:
            out.append({"speaker": u.get("speaker", ""), "start": start, "end": line["end"], "text": line["text"],
                        "lines": [line]})
    return out


def meetings(limit: int = 50, conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        with connect() as c:
            sync(c)
            return meetings(limit, c)
    rows = conn.execute("SELECT * FROM meetings ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r["id"], "title": r["title"], "when": _when(r["started"]), "minutes": round(r["duration"] / 60),
             "speakers": json.loads(r["speakers"] or "[]"), "style": r["style"], "summary": r["summary"],
             "markdown": r["markdown"]} for r in rows]


def meeting(meeting_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """One meeting: metadata, notes and the transcript (from result.json)."""
    from .meeting import read_json, resolve

    try:
        directory = resolve(meeting_id)
    except SystemExit:
        return None
    result = read_json(directory / "result.json")
    if not result:
        return None
    notes = result.get("notes") or {}
    title = (result.get("event") or {}).get("title") or notes.get("title") or result["meta"].get("title") or "Meeting"
    transcript = [{"start": u["start"], "end": u.get("end", u["start"]), "speaker": u.get("speaker", ""),
                   "text": u.get("text", "")} for u in result.get("utterances", []) if u.get("text")]
    return {"id": directory.name, "title": title, "when": _when(float(result["meta"].get("started", 0))),
            "minutes": round(result.get("duration", 0) / 60), "notes": notes,
            "speakers": {k: {"source": v.get("source"), "email": v.get("email", ""),
                             "minutes": round(v.get("seconds", 0) / 60, 1)}
                         for k, v in result.get("speakers", {}).items() if v.get("seconds")},
            "talk_seconds": {k: round(float(v.get("seconds", 0)), 1)
                             for k, v in result.get("speakers", {}).items() if v.get("seconds")},
            "transcript": transcript, "turns": turns(transcript),
            "markdown": result.get("markdown", ""), "event": result.get("event")}


def action_items(owner: str = "", since: str = "", conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        with connect() as c:
            sync(c)
            return action_items(owner, since, c)
    lo, _ = _date_bounds(since, "")
    rows = conn.execute(
        "SELECT a.*, m.title, m.started FROM actions a JOIN meetings m ON m.id = a.meeting_id "
        "WHERE m.started >= ? AND (? = '' OR a.owner LIKE ?) ORDER BY m.started DESC",
        (lo, owner, f"%{owner}%")).fetchall()
    return [{"meeting_id": r["meeting_id"], "meeting": r["title"], "when": _when(r["started"]), "owner": r["owner"],
             "task": r["task"], "due": r["due"]} for r in rows]


def context_around(meeting_id: str, start: float, seconds: float = 90,
                   conn: sqlite3.Connection | None = None) -> list[dict]:
    """Transcript lines within `seconds` of `start`, for reading a hit in context."""
    if conn is None:
        with connect() as c:
            return context_around(meeting_id, start, seconds, c)
    rows = conn.execute("SELECT start, speaker, text FROM lines WHERE meeting_id = ? AND start BETWEEN ? AND ? "
                        "ORDER BY start", (meeting_id, start - seconds, start + seconds)).fetchall()
    return [dict(r) for r in rows]
