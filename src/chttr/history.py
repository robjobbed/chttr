"""Dictation history (~/.local/share/chttr/history.jsonl): picker, stats and what the Home page shows."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from . import config as C

TYPING_WPM = 40          # an average typist, for "time saved"


def load() -> list[dict]:
    if not C.HISTORY_FILE.exists():
        return []
    entries = []
    for line in C.HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("text") or e.get("raw"):
            entries.append(e)
    return entries


def _local(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts).astimezone()
    except (TypeError, ValueError):
        return None


def _ago(when: datetime | None) -> str:
    if when is None:
        return ""
    secs = (datetime.now().astimezone() - when).total_seconds()
    if secs < 3600:
        return f"{max(1, int(secs // 60))}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{when:%b} {when.day}"


def pick(entries: list[dict], limit: int = 50) -> str | None:
    """Show the newest dictations in the desktop's menu; return the chosen text."""
    from . import desktop as DT

    recent = list(reversed(entries[-limit:]))
    options = []
    for i, e in enumerate(recent):
        text = " ".join(str(e.get("text") or e.get("raw")).split())
        label = text if len(text) <= 90 else text[:89] + "…"
        sub = " · ".join(x for x in (f"#{i + 1}", _ago(_local(e.get("ts", ""))), e.get("app", "")) if x)
        # Omarchy's picker reads "<icon>\t<label>\t<subtext>"; without the icon column it
        # would draw the whole dictation in the icon slot, over the label.
        options.append(f"{'󰆓' if e.get('output') == 'clipboard' else '󰌌'}\t{label}\t{sub}")
    try:
        choice = DT.menu("Dictation history", options)
    except FileNotFoundError as exc:
        raise SystemExit(f"{exc}; or use: chttr history --print") from None
    if not choice:
        return None
    try:
        idx = int(choice.rsplit("\t", 1)[1].split(" ", 1)[0].lstrip("#")) - 1
    except (IndexError, ValueError):
        return None
    e = recent[idx]
    return str(e.get("text") or e.get("raw"))


def _words(e: dict) -> int:
    return len(str(e.get("text") or e.get("raw")).split())


def _saved(words: int, secs: float) -> float:
    """Minutes saved against typing the same words; negative when speaking was slower."""
    return words / TYPING_WPM - secs / 60


def _streak(days_used: set, today: date) -> int:
    """Consecutive days with a dictation, ending today, or yesterday when today hasn't started yet."""
    day = today if today in days_used else today - timedelta(1)
    n = 0
    while day in days_used:
        n += 1
        day -= timedelta(1)
    return n


def summary(entries: list[dict], days: int = 30, today: date | None = None) -> dict:
    """The numbers behind `chttr stats` and the Home page."""
    today = today or datetime.now().astimezone().date()
    words_on: Counter[date] = Counter()
    count_on: Counter[date] = Counter()
    apps: Counter[str] = Counter()
    words = 0
    secs = 0.0
    longest: dict | None = None
    for e in entries:
        n = _words(e)
        s = float(e.get("seconds") or 0)
        words += n
        secs += s
        when = _local(e.get("ts", ""))
        if when:
            words_on[when.date()] += n
            count_on[when.date()] += 1
        apps[e.get("app") or "unknown"] += n
        if s > 0 and (longest is None or s > longest["seconds"]):
            longest = {"seconds": round(s, 1), "words": n, "text": str(e.get("text") or e.get("raw"))[:120]}
    best = max(words_on.items(), key=lambda kv: (kv[1], kv[0]), default=None)
    return {
        "dictations": len(entries),
        "words": words,
        "seconds": round(secs, 1),
        "wpm": round(words / (secs / 60)) if secs else 0,
        "saved_minutes": round(max(0.0, _saved(words, secs)), 2),
        "per_day": [{"date": d.isoformat(), "words": words_on.get(d, 0), "count": count_on.get(d, 0)}
                    for d in (today - timedelta(i) for i in reversed(range(days)))],
        "top_apps": [{"app": app, "words": n} for app, n in apps.most_common(5)],
        "streak_days": _streak(set(count_on), today),
        "best_day": {"date": best[0].isoformat(), "words": best[1]} if best and best[1] else None,
        "longest_dictation": longest,
    }


def stats(entries: list[dict], days: int = 7) -> str:
    if not entries:
        return "no dictations in history yet"
    s = summary(entries, days)
    saved = _saved(s["words"], sum(float(e.get("seconds") or 0) for e in entries))
    if saved >= 1:
        saved_line = f"time saved   {saved:.0f} min"
    elif round(saved * 60) > 0:
        saved_line = f"time saved   {saved * 60:.0f} s"
    else:                        # slower than typing so far: say so instead of a negative number
        saved_line = "time saved   0 s (no time saved yet)"
    lines = [
        f"dictations   {s['dictations']}",
        f"words        {s['words']}",
        f"speaking     {s['wpm']} wpm (vs ~{TYPING_WPM} typing)",
        saved_line,
        "",
        f"last {days} days:",
    ]
    peak = max((d["words"] for d in s["per_day"]), default=0) or 1
    for d in s["per_day"]:
        day, n = date.fromisoformat(d["date"]), d["words"]
        bar = "█" * round(24 * n / peak) if n else ""
        lines.append(f"  {day.strftime('%a %b %d')}  {n:5}  {bar}")
    lines += ["", "top apps:"]
    for a in s["top_apps"]:
        lines.append(f"  {a['app'][:24]:24} {a['words']:5} words")
    return "\n".join(lines)


def entries_with_ids(entries: list[dict]) -> list[dict]:
    """Each entry with a stable `id`: its position in the history (the file is only ever appended to)."""
    return [{**e, "id": str(i)} for i, e in enumerate(entries)]


def _norm(word: str) -> str:
    return re.sub(r"[^\w']+", "", word.casefold()) or word


def word_diff(raw: str, text: str) -> list[tuple[str, str]]:
    """What chttr heard against what it typed, word by word: [("equal" | "del" | "ins", "words")].
    Capitals and punctuation alone don't count as a change; equal runs carry the typed words."""
    a, b = str(raw or "").split(), str(text or "").split()
    out: list[tuple[str, str]] = []

    def add(kind: str, words: list[str]) -> None:
        if words:
            out.append((kind, " ".join(words)))

    matcher = SequenceMatcher(None, [_norm(w) for w in a], [_norm(w) for w in b], autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            add("equal", b[j1:j2])
        else:                    # replace = the heard words out, the typed words in
            add("del", a[i1:i2])
            add("ins", b[j1:j2])
    return out
