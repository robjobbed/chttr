"""Command mode without a model: everyday edits on the selected text, done locally.

    "upper case" / "all caps"        -> UPPER CASE
    "lower case", "title case", "sentence case"
    "join lines" / "one line"        -> the lines joined with spaces
    "sort lines" (also "reverse" / "descending"), "reverse lines"
    "remove blank lines", "remove duplicates", "trim whitespace"
    "wrap in quotes" | "double quotes" | "single quotes" | "backticks" | "parentheses" | "brackets" | "braces"
    "bullet list", "numbered list", "remove bullets"
    "delete that" / "clear"          -> the selection is deleted

Anything else (and anything without a selection: writing new text) needs the
cleanup model. Instructions are matched loosely: "um, can you make it all caps
please." works, and without a model so do small mis-hearings ("upper cases").
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable

_FILLERS = re.compile(r"\b(um+|uh+|er+|please|can you|could you|would you|make it|make this|make them|"
                      r"turn it into|turn this into|turn them into|convert it to|convert this to|convert to|"
                      r"change it to|change this to|put it in|put this in|the selection|selection|the text|"
                      r"this|these|those|it|them|an?|the|to|into|okay|ok|so)\b")
_BULLET = re.compile(r"^(\s*)(?:[-*•–]|\d+[.)])\s+")


def _lines(text: str) -> list[str]:
    return text.split("\n")


def _items(text: str) -> list[str]:
    """What a list is made of: the lines; or, for one line, its sentences or its
    comma-separated items ("apples, pears and plums")."""
    lines = [_BULLET.sub("", line).strip() for line in _lines(text) if line.strip()]
    if len(lines) != 1:
        return lines
    line = lines[0]
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", line) if s]
    if len(sentences) > 1:
        return sentences
    parts = [p.strip() for p in line.rstrip(".").split(",") if p.strip()]
    if len(parts) < 2:
        return [line]
    last = re.sub(r"^(and|or)\s+", "", parts[-1])
    if last == parts[-1] and re.search(r"\s(and|or)\s", last):        # no Oxford comma
        parts[-1:] = re.split(r"\s+(?:and|or)\s+", last, maxsplit=1)
    else:
        parts[-1] = last
    return parts


def _title(text: str) -> str:
    small = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "with"}

    def word(m: re.Match) -> str:
        w = m.group(0)
        if m.start() and w.lower() in small and not text[:m.start()].rstrip().endswith((":", "\n")):
            return w.lower()
        return w[:1].upper() + w[1:]

    return re.sub(r"[^\W_][\w'’-]*", word, text)


def _sentence(text: str) -> str:
    text = text.lower()
    text = re.sub(r"(^\s*|[.!?]\s+|\n\s*)([^\W\d_])", lambda m: m.group(1) + m.group(2).upper(), text)
    return re.sub(r"\bi\b(?=['’]?\w*)", "I", text)


def _sort(text: str, reverse: bool = False) -> str:
    return "\n".join(sorted(_lines(text.strip("\n")), key=str.casefold, reverse=reverse))


def _dedupe(text: str) -> str:
    seen: set[str] = set()
    out = []
    for line in _lines(text):
        key = line.strip().casefold()
        if key and key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out)


def _trim(text: str) -> str:
    return "\n".join(line.strip() for line in _lines(text.strip()))


def _wrap(left: str, right: str | None = None) -> Callable[[str], str]:
    return lambda text: left + text + (left if right is None else right)


def _bullets(text: str) -> str:
    return "\n".join(f"- {item}" for item in _items(text))


def _numbered(text: str) -> str:
    return "\n".join(f"{n}. {item}" for n, item in enumerate(_items(text), 1))


TRANSFORMS: dict[str, Callable[[str], str]] = {
    "upper case": str.upper, "uppercase": str.upper, "all caps": str.upper, "capitals": str.upper,
    "all capitals": str.upper, "caps": str.upper,
    "lower case": str.lower, "lowercase": str.lower, "all lower case": str.lower,
    "title case": _title,
    "sentence case": _sentence,
    "join lines": lambda t: " ".join(line.strip() for line in _lines(t) if line.strip()),
    "join the lines": lambda t: " ".join(line.strip() for line in _lines(t) if line.strip()),
    "one line": lambda t: " ".join(line.strip() for line in _lines(t) if line.strip()),
    "single line": lambda t: " ".join(line.strip() for line in _lines(t) if line.strip()),
    "sort lines": _sort, "sort": _sort, "sort alphabetically": _sort, "sort lines alphabetically": _sort,
    "sort lines reverse": lambda t: _sort(t, reverse=True),
    "reverse sort lines": lambda t: _sort(t, reverse=True),
    "reverse sort": lambda t: _sort(t, reverse=True),
    "sort lines descending": lambda t: _sort(t, reverse=True),
    "sort descending": lambda t: _sort(t, reverse=True),
    "sort lines in reverse": lambda t: _sort(t, reverse=True),
    "reverse lines": lambda t: "\n".join(reversed(_lines(t.strip("\n")))),
    "reverse order": lambda t: "\n".join(reversed(_lines(t.strip("\n")))),
    "remove blank lines": lambda t: "\n".join(line for line in _lines(t) if line.strip()),
    "remove empty lines": lambda t: "\n".join(line for line in _lines(t) if line.strip()),
    "delete blank lines": lambda t: "\n".join(line for line in _lines(t) if line.strip()),
    "remove duplicates": _dedupe, "remove duplicate lines": _dedupe, "dedupe": _dedupe,
    "delete duplicates": _dedupe,
    "trim": _trim, "trim whitespace": _trim, "strip whitespace": _trim, "remove whitespace": _trim,
    "wrap in quotes": _wrap('"'), "quotes": _wrap('"'), "in quotes": _wrap('"'), "quote": _wrap('"'),
    "wrap in double quotes": _wrap('"'), "double quotes": _wrap('"'), "in double quotes": _wrap('"'),
    "wrap in single quotes": _wrap("'"), "single quotes": _wrap("'"), "in single quotes": _wrap("'"),
    "wrap in backticks": _wrap("`"), "backticks": _wrap("`"), "in backticks": _wrap("`"),
    "wrap in parentheses": _wrap("(", ")"), "parentheses": _wrap("(", ")"), "in parentheses": _wrap("(", ")"),
    "wrap in brackets": _wrap("[", "]"), "brackets": _wrap("[", "]"), "in brackets": _wrap("[", "]"),
    "wrap in square brackets": _wrap("[", "]"), "square brackets": _wrap("[", "]"),
    "wrap in braces": _wrap("{", "}"), "braces": _wrap("{", "}"), "wrap in curly braces": _wrap("{", "}"),
    "curly braces": _wrap("{", "}"),
    "bullet list": _bullets, "bullets": _bullets, "bulleted list": _bullets, "bullet points": _bullets,
    "list": _bullets,
    "numbered list": _numbered, "numbers": _numbered, "number lines": _numbered, "number list": _numbered,
    "remove bullets": lambda t: "\n".join(_BULLET.sub(r"\1", line) for line in _lines(t)),
    "remove bullet points": lambda t: "\n".join(_BULLET.sub(r"\1", line) for line in _lines(t)),
    "remove numbering": lambda t: "\n".join(_BULLET.sub(r"\1", line) for line in _lines(t)),
    "remove numbers": lambda t: "\n".join(_BULLET.sub(r"\1", line) for line in _lines(t)),
    "delete": lambda t: "", "delete that": lambda t: "", "clear": lambda t: "", "clear that": lambda t: "",
    "remove": lambda t: "", "remove that": lambda t: "", "erase": lambda t: "", "erase that": lambda t: "",
    "scratch that": lambda t: "",
}

def normalize(instruction: str) -> str:
    """ "Um, can you make it UPPER CASE, please." -> "upper case" """
    text = instruction.lower().replace("-", " ")
    text = re.sub(r"[^\w\s']", " ", text)
    return " ".join(_FILLERS.sub(" ", text).split())


def _key(phrase: str) -> str:
    return " ".join(_FILLERS.sub(" ", phrase).split())


# The table as instructions look once their fillers are gone ("join the lines" -> "join lines")
_TABLE: dict[str, Callable[[str], str]] = {}
for _phrase, _fn in TRANSFORMS.items():
    _TABLE.setdefault(_key(_phrase) or _phrase, _fn)
# Short phrases only match exactly: "short" is not "sort"
_FUZZY = [k for k in _TABLE if len(k) >= 8]


def _near(said: str, phrase: str) -> bool:
    """A mis-hearing, not a different edit: as many words, each one nearly the same
    ("upper cases" is "upper case"; "remove spaces" is not "remove whitespace")."""
    a, b = said.split(), phrase.split()
    return len(a) == len(b) and all(difflib.SequenceMatcher(None, x, y).ratio() >= 0.75 for x, y in zip(a, b, strict=True))


def match(instruction: str, fuzzy: bool = True) -> str | None:
    """The table phrase an instruction means, or None. `fuzzy` also accepts small
    mis-hearings; with a model at hand chttr only takes exact phrases and leaves
    the rest to it."""
    said = normalize(instruction)
    if not said:
        return None
    if said in _TABLE:
        return said
    if not fuzzy:
        return None
    close = [c for c in difflib.get_close_matches(said, _FUZZY, n=3, cutoff=0.8) if _near(said, c)]
    return close[0] if close else None


def local_command(instruction: str, selection: str, fuzzy: bool = True) -> str | None:
    """The selection transformed as instructed, "" to delete it, or None when the
    instruction needs a model (or there is no selection: writing new text does).
    Pass `fuzzy=False` when a model is configured: a near miss goes to the model
    instead of becoming the wrong edit."""
    if not selection:
        return None
    phrase = match(instruction, fuzzy)
    if phrase is None:
        return None
    # the edit applies to the text; the whitespace around it (a selected line's newline) stays
    edit = _TABLE[phrase]
    if edit is _trim:
        return _trim(selection)
    m = re.fullmatch(r"(\s*)(.*?)(\s*)", selection, re.DOTALL)
    out = edit(m.group(2))
    return m.group(1) + out + m.group(3) if out else ""
