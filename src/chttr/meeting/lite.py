"""On-device meeting notes, for when there is no Claude key and no Ollama: a summary, the
questions asked, numbers and dates, commitments and action items, found with patterns in
the transcript. Same shape as notes.write_notes. Pure: no I/O, no models."""

from __future__ import annotations

import re

from .analysis import _fmt_ts, _turns
from .store import Utterance

STYLE = "lite"
STYLE_NAME = "On-device notes"
MAX_QUESTIONS, MAX_NUMBERS, MAX_COMMITMENTS = 12, 10, 12

_DAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_MONTHS = "January|February|March|April|June|July|August|September|October|November|December"
_SMALL = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|" \
         "sixty|seventy|eighty|ninety|a hundred|a thousand|hundred"
_NUM = r"\d[\d,]*(?:\.\d+)?"
_UNITS = r"seconds?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|quarters?|sprints?|users?|customers?|" \
         r"people|seats?|licen[cs]es?|accounts?|deals?|units?|tickets?|calls?|meetings?|pages?|gb|tb|mb|ms|km|miles?|kg"

# One alternation, so overlapping spellings ("$50k", "50k") give one match, leftmost first
NUMBERS = re.compile(
    rf"[$€£¥]\s?{_NUM}(?:\s?(?i:k|m|bn|million|billion|thousand)\b)?"
    rf"|(?i:{_NUM}\s?(?:k|m|bn)?\s?(?:dollars|euros|pounds|bucks|usd|eur|gbp)\b)"
    rf"|(?i:\b(?:{_SMALL})(?:[- ](?:{_SMALL}))?\s(?:hundred|thousand|million|billion|percent)(?:\s(?:dollars|euros|pounds))?\b)"
    rf"|(?i:{_NUM}\s?(?:%|percent\b|per cent\b))"
    rf"|(?i:\b{_NUM}\s?(?:k|thousand|million|billion)?\s?(?:{_UNITS})\b)"
    rf"|(?i:\b{_NUM}\s?(?:k|thousand|million|billion)\b)"
    rf"|(?i:\b\d{{1,2}}(?::\d\d)?\s?(?:am|pm|a\.m\.|p\.m\.))"
    rf"|\b(?:{_MONTHS})(?:\s\d{{1,2}}(?:st|nd|rd|th)?)?(?:,?\s20\d\d)?\b|\bMay\s\d{{1,2}}(?:st|nd|rd|th)?\b"
    rf"|(?i:\b\d{{1,2}}(?:st|nd|rd|th)\sof\s(?:{_MONTHS}|May))"
    rf"|(?i:\b(?:next|this|last)?\s?(?:{_DAYS})\b)"
    rf"|(?i:\b(?:tomorrow|tonight|next (?:week|month|quarter|year)|end of (?:the )?(?:day|week|month|quarter|year))\b)"
    rf"|\bQ[1-4](?:\s20\d\d)?\b|\b20[2-9]\d\b"
)
DUE = re.compile(
    rf"(?i:\b(?:{_DAYS})\b|\btomorrow\b|\btonight\b|\btoday\b|\bnext (?:week|month)\b"
    rf"|\bend of (?:the )?(?:day|week|month|quarter)\b|\beo[dw]\b)"
    rf"|\b(?:{_MONTHS}|May)\s\d{{1,2}}(?:st|nd|rd|th)?\b"
)
COMMITMENT = re.compile(
    rf"\b(?:I will|I'll|we'll|we will|let me|I can(?!'t|not)|I'm going to|we're going to"
    rf"|by (?:{_DAYS}|tomorrow|next week|end of (?:the )?(?:day|week)))\b", re.IGNORECASE)
NOT_A_COMMITMENT = re.compile(
    r"\b(?:share my screen|let me see|let me think|let me know|let me ask|I can see|I can hear|I can't)\b", re.IGNORECASE)
QUESTION_WORDS = {"who", "what", "when", "where", "why", "how", "can", "could", "should", "do", "does", "did", "is",
                  "are", "will"}
SMALL_TALK = re.compile(r"\b(?:can you (?:hear|see) (?:me|my screen)|am I (?:muted|audible|on mute)|how are you)\b",
                        re.IGNORECASE)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.replace("’", "'")) if s.strip()]


def _clip(text: str, n: int = 140) -> str:
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _join(names: list[str]) -> str:
    if len(names) > 4:
        names = [*names[:3], f"{len(names) - 3} others"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _is_question(s: str) -> bool:
    words = re.findall(r"[\w']+", s.lower())
    if len(words) < (2 if s.endswith("?") else 3) or SMALL_TALK.search(s):    # not "Right?" or "Okay?"
        return False
    # A recognizer that punctuates marks questions; without punctuation, the first word tells
    return s.endswith("?") or (words[0] in QUESTION_WORDS and not s.endswith((".", "!")))


def _is_commitment(s: str) -> bool:
    return (len(s.split()) >= 3 and not s.endswith("?") and bool(COMMITMENT.search(s))
            and not NOT_A_COMMITMENT.search(s))


def _keep(found: list[tuple[float, int, str]], limit: int) -> list[int]:
    """Indexes of the `limit` best of (start, rank, item) (higher rank, then longer item
    first), back in time order."""
    best = sorted(range(len(found)), key=lambda i: (-found[i][1], -len(found[i][2])))[:limit]
    return sorted(best, key=lambda i: found[i][0])


def _due(s: str) -> str:
    """The deadline a commitment mentions ("Friday", "end of week", "Oct 3"), or ""."""
    m = DUE.search(s)
    if not m:
        return ""
    due = m.group(0)
    if due.lower() in ("eod", "eow"):
        return due.upper()
    return due.capitalize() if re.fullmatch(_DAYS, due, re.IGNORECASE) else due


def _talk(result: dict, utts: list[Utterance]) -> dict[str, float]:
    """Seconds spoken per speaker label."""
    secs = {k: float(v.get("seconds") or 0) for k, v in result.get("speakers", {}).items()}
    if not any(secs.values()):
        secs = {}
        for u in utts:
            secs[u.speaker] = secs.get(u.speaker, 0.0) + (u.end - u.start)
    return {k: v for k, v in secs.items() if v > 0}


def notes_lite(result: dict) -> dict:
    """Notes from the transcript alone, in the shape notes.write_notes returns."""
    meta, event = result.get("meta") or {}, result.get("event") or {}
    utts = [Utterance(**u) for u in result.get("utterances", [])]
    turns = _turns(utts)
    title = event.get("title") or meta.get("title") or "Meeting"

    talk = _talk(result, utts)
    total = sum(talk.values())
    mins = max(1, round(float(result.get("duration") or 0) / 60))
    me = {k for k, v in result.get("speakers", {}).items() if v.get("source") == "me"}
    others = [k for k in talk if k not in me] or list(talk)
    summary = []
    if turns and talk:
        who = max(talk, key=lambda k: talk[k])
        length = f"{mins} minute{'s' if mins != 1 else ''}"
        if len(talk) == 1:
            line = f"{length}; only {who} spoke."
        else:
            line = f"{length} with {_join(others)}; {who} spoke most ({round(100 * talk[who] / total)}%)."
        first, last = turns[0], turns[-1]
        line += f" It started with {first[0]}: “{_clip(first[2])}”"
        if len(turns) > 1:
            line += f" and ended with {last[0]}: “{_clip(last[2])}”"
        summary.append(line if line[-2] in ".!?…" else line + ".")
    elif not turns:
        summary.append("No speech was transcribed.")

    questions: list[tuple[float, int, str]] = []
    numbers: list[tuple[float, int, str]] = []
    commitments: list[tuple[float, int, str]] = []
    actions: list[dict] = []
    seen_q: set[str] = set()
    seen_n: set[str] = set()
    for label, start, text in turns:
        at = f"{label} ({_fmt_ts(start)})"
        for s in _sentences(text):
            if _is_question(s) and s.lower() not in seen_q:
                seen_q.add(s.lower())
                questions.append((start, 0, f"{at}: {_clip(s, 200)}"))
            new = []
            for m in NUMBERS.finditer(s):
                found = " ".join(m.group(0).split())
                key = found.lower().removeprefix("next ").removeprefix("this ")
                if key not in seen_n:
                    seen_n.add(key)
                    new.append(found)
            if new:     # money and percentages first, then other figures, then dates
                rank = 2 if re.search(r"[$€£¥%]|percent|dollars|euros|pounds", " ".join(new), re.IGNORECASE) else \
                    1 if re.search(r"\d", " ".join(new)) else 0
                numbers.append((start, rank, f"{', '.join(new)} · {at}: {_clip(s, 160)}"))
            if _is_commitment(s):
                due = _due(s)
                commitments.append((start, 1 if due else 0, f"{at}: {_clip(s, 200)}"))
                actions.append({"owner": label, "task": _clip(s, 200), "due": due})

    kept = _keep(commitments, MAX_COMMITMENTS)
    return {
        "style": STYLE, "style_name": STYLE_NAME, "title": title,
        "sections": [
            {"title": "Summary", "kind": "paragraph", "items": summary},
            {"title": "Questions asked", "kind": "bullets",
             "items": [questions[i][2] for i in _keep(questions, MAX_QUESTIONS)]},
            {"title": "Numbers and dates", "kind": "bullets",
             "items": [numbers[i][2] for i in _keep(numbers, MAX_NUMBERS)]},
            {"title": "Commitments", "kind": "bullets", "items": [commitments[i][2] for i in kept]},
        ],
        "actions_heading": "Action items",
        "action_items": [actions[i] for i in kept],
        "speakers": [], "extra": "",
    }
