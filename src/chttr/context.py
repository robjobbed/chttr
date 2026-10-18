"""What's on screen around the cursor, and learning from your corrections.

Both read the focused text field through AT-SPI (atspi_helper.py, run under the
system Python). Apps that don't expose accessibility (some Electron apps,
Chromium without --force-renderer-accessibility) simply contribute nothing.

- Names near the cursor ("Reply to Priyanka about...") become extra spellings
  for sound-alike correction, so a name that is on screen comes out right.
- A few seconds after chttr types, it looks at the field again. If you replaced
  a word it typed with a name-like word that sounds similar ("Sorov" ->
  "Sohrab"), it offers to remember that spelling.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import dictionary as D

log = logging.getLogger("chttr.context")

HELPER = Path(__file__).with_name("atspi_helper.py")
_SENTENCE_START = re.compile(r"(^|[.!?:]\s+|\n\s*)$")


@dataclass
class Field:
    app: str = ""
    before: str = ""
    after: str = ""
    caret: int = 0
    count: int = 0

    @property
    def text(self) -> str:
        return self.before + self.after


def read_field(python: str, timeout: float = 0.6, before: int = 600, after: int = 200) -> Field | None:
    """The focused text field, or None (no AT-SPI, not a text field, too slow)."""
    try:
        out = subprocess.run([python, "-I", str(HELPER), "--timeout", str(timeout), "--before", str(before),
                              "--after", str(after)], capture_output=True, text=True, timeout=timeout + 1.5)
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        log.debug("AT-SPI read failed: %s", exc)
        return None
    if not data:
        return None
    return Field(str(data.get("app", "")), str(data.get("before", "")), str(data.get("after", "")),
                 int(data.get("caret", 0)), int(data.get("count", 0)))


def probe(python: str, timeout: float = 2.0) -> dict:
    """What `atspi_helper.py --probe` says: {"gi": bool, "bus": bool, "apps": N},
    or {} if the helper couldn't run."""
    try:
        out = subprocess.run([python, "-I", str(HELPER), "--probe"], capture_output=True, text=True,
                             timeout=timeout)
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        log.debug("AT-SPI probe failed: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    try:
        apps = int(data.get("apps") or 0)
    except (TypeError, ValueError):
        apps = 0
    return {"gi": bool(data.get("gi")), "bus": bool(data.get("bus")), "apps": apps}


def names_in(text: str, limit: int = 40) -> list[str]:
    """Name-like words: capitalized mid-sentence, or mixed case (iPhone, McAllister),
    and not ordinary English words."""
    common = D.english_words()
    seen: dict[str, None] = {}
    for m in re.finditer(r"\b[A-Za-z][A-Za-z'’-]{2,}\b", text):
        word = m.group(0).strip("'’-")
        mixed = any(c.isupper() for c in word[1:]) and any(c.islower() for c in word)
        capital = word[0].isupper() and not _SENTENCE_START.search(text[:m.start()])
        if (mixed or capital) and word.lower() not in common and not word.isupper():
            seen.setdefault(word, None)
        if len(seen) >= limit:
            break
    return list(seen)


@dataclass
class Correction:
    heard: str          # what chttr typed
    right: str          # what you changed it to


def corrections(typed: str, field_text: str) -> list[Correction]:
    """Words you changed after chttr typed `typed`: one-for-one replacements of a word
    by a name-like word that sounds alike. Empty unless most of the dictation is
    still recognisably in the field."""
    tw = re.findall(r"[\w'’-]+", typed)
    fw = re.findall(r"[\w'’-]+", field_text)
    if len(tw) < 2 or not fw:
        return []
    sm = difflib.SequenceMatcher(None, [w.lower() for w in tw], [w.lower() for w in fw], autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    if matched < 0.6 * len(tw):
        return []
    common = D.english_words()
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or (i2 - i1) != (j2 - j1) or (i2 - i1) > 2:
            continue
        for heard, right in zip(tw[i1:i2], fw[j1:j2], strict=True):
            if right.lower() in common or not right[0].isupper() or heard.lower() == right.lower():
                continue
            close = difflib.SequenceMatcher(None, heard.lower(), right.lower()).ratio()
            if D.sound_key(heard) == D.sound_key(right) or close >= 0.5:
                out.append(Correction(heard, right))
    return out
