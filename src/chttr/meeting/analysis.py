"""Pure helpers for the meeting pipeline: excluded intervals, echo detection, caption
votes, attendee matching and transcript turns. No I/O, no models."""

from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict

import numpy as np

from .store import Utterance

# How Meet labels your own captions, in the languages it's most often used in
SELF_CAPTION_NAMES = {"you", "du", "vous", "tú", "tu", "você", "jij", "ty", "あなた", "你"}
CAPTION_LAG = (0.1, 1.8)         # caption text appears this long after the words are spoken
ECHO_OVERLAP = 0.6               # share of mic words also heard on the speakers => echo, not you


def _intervals(events: list[dict], started: float) -> list[tuple[float, float]]:
    """Stretches of the recording your mic should not count: muted in Meet, or dictating."""
    out = []
    muted_since = None
    for e in events:
        t = float(e.get("t", 0)) - started
        if e.get("type") == "mute":
            if e.get("muted") and muted_since is None:
                muted_since = t
            elif not e.get("muted") and muted_since is not None:
                out.append((muted_since, t))
                muted_since = None
        elif e.get("type") == "dictation":
            out.append((float(e["start"]) - started, float(e["end"]) - started))
    if muted_since is not None:
        out.append((muted_since, float("inf")))
    return out


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def _is_echo(mic: Utterance, them: list[Utterance]) -> bool:
    """Your speakers' sound reaching your mic transcribes to the same words as the
    them channel at the same moment."""
    near = [u for u in them if _overlap(mic.start - 1, mic.end + 1, u.start, u.end) > 0]
    if not near:
        return False
    mw = _words(mic.text)
    tw = _words(" ".join(u.text for u in near))
    if not mw:          # "…" or "-" while they talk is noise
        return True
    if len(mw) < 2:
        # "Yes." right after their question is an answer. A lone word is echo only when
        # it is heard while they talk and they said that word.
        during = any(_overlap(mic.start, mic.end, u.start, u.end) > 0 for u in near)
        return during and mw[0] in tw
    matched = sum(b.size for b in difflib.SequenceMatcher(None, mw, tw, autojunk=False).get_matching_blocks())
    return matched / len(mw) >= ECHO_OVERLAP


def _caption_votes(them: list[Utterance], events: list[dict], started: float,
                   self_names: set[str]) -> tuple[dict[int, Counter], list[Counter]]:
    speaking = sorted((float(e["t"]) - started, str(e["name"]).strip())
                      for e in events if e.get("type") == "speaking" and e.get("name"))
    speaking = [(t, n) for t, n in speaking if n.lower() not in self_names]
    times = np.array([t for t, _ in speaking])
    per_utt: list[Counter] = []
    per_cluster: dict[int, Counter] = defaultdict(Counter)
    for u in them:
        votes: Counter = Counter()
        if len(times):
            lo, hi = np.searchsorted(times, [u.start + CAPTION_LAG[0], u.end + CAPTION_LAG[1]])
            votes.update(n for _, n in speaking[lo:hi])
        per_utt.append(votes)
        per_cluster[u.cluster].update(votes)
    return per_cluster, per_utt


def _norm(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", name.lower()).split())


def _match_attendee(name: str, attendees: list[dict]) -> dict | None:
    """The invitee a Meet display name belongs to: same name, else a unique first-name
    match, else a unique email local-part match."""
    n = _norm(name)
    if not n:
        return None
    for a in attendees:
        if a["name"] and _norm(a["name"]) == n:
            return a
    first = n.split()[0]
    by_first = [a for a in attendees if a["name"] and _norm(a["name"]).split()[0] == first]
    if len(by_first) == 1:
        return by_first[0]
    if len(by_first) > 1:
        return None             # two invitees share the first name: don't guess
    by_email = [a for a in attendees if first in re.sub(r"[^a-z]", " ", a["email"].split("@")[0].lower()).split()
                or a["email"].split("@")[0].lower().startswith(first)]
    return by_email[0] if len(by_email) == 1 else None


def _fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _turns(utts: list[Utterance], gap: float = 3.0) -> list[tuple[str, float, str]]:
    """Consecutive utterances by the same speaker joined into turns: (label, start, text)."""
    turns: list[list] = []      # [label, start, text, end]
    for u in sorted(utts, key=lambda u: u.start):
        if not u.text:
            continue
        if turns and turns[-1][0] == u.speaker and u.start - turns[-1][3] < gap:
            turns[-1][2] += " " + u.text
            turns[-1][3] = max(turns[-1][3], u.end)
        else:
            turns.append([u.speaker, u.start, u.text, u.end])
    return [(label, start, text) for label, start, text, _ in turns]

