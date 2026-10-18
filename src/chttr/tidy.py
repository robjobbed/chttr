"""Local cleanup: the part of dictation cleanup that needs no model.

Used when no Claude key or Ollama is set up, while the model is unreachable, and
as a quick pass before the model sees a dictation. Rules, in order:

    a) spoken formatting becomes symbols (table below)
    b) "scratch that" removes the phrase before it (back to , . ! ? ; or a new line)
       when a pause, the start or the end sets it off on at least one side;
       "delete that" / "strike that" only when set off on both sides. Never after
       "I", "you", "to", "please", "don't", ...: "Did you delete that?" is a question
    c) fillers go: um, uh, erm, hmm anywhere; "like", "you know", "I mean" only
       when set off by commas. Stutters ("the the") collapse for words of up to
       four letters ("had had" and "that that" stay)
    d) spacing and capitals: no space before , ; (or . : ! ? outside code, where
       "if ! grep" and "sudo !!" mean something), one space between words, a capital
       at the start, after . ! ? and on each new line ("user_id" and "x86" stay as
       spoken), "i" -> "I"; a sentence of three words or more with no closing
       punctuation gets a period
    e) per app: code (terminals, editors) keeps your capitals, gets no added period
       and loses one trailing period; chat adds no period and, when the app has no
       safe way to type a line break (fold_newlines), folds new lines into spaces
       unless you dictated a list

Spoken formatting (case-insensitive; never after "a", "the", "my", ...):

    comma, colon, semicolon         , : ;       after a word ("the Oxford comma" and
                                                "Colon cancer ..." stay)
    period, full stop               .           only before the end, punctuation, a
    question mark                   ?           capital or another command, so "a period
    exclamation mark / point        !           of time" and "the trial period" stay;
                                                never "period" after a time unit, a
                                                hyphenated word or an adjective ("a 14 day
                                                period", "a two-year period", "a difficult
                                                period"); "at 5 period" is a full stop
    dash                            " - "       between words, not in code ("dash m"
                                                is a flag: dev mode's business)
    open quote, close quote         "
    new line, new paragraph         \\n, \\n\\n    not "a brand new line of shoes"
    bullet point                    \\n-          not "the next bullet point"

Self-corrections ("Tuesday, no, Friday") are left to the model: deciding what
"no" takes back needs to understand the sentence.

`level`: "light" runs c-e only (your words and spoken commands stay); "medium" and
"high" run everything; "none" returns the text unchanged.
"""

from __future__ import annotations

import re

# ---- what a model would have to act on (polish.quick_path) -----------------

# Fillers, self-corrections, spoken formatting, snippets' trigger verbs.
NEEDS_MODEL = re.compile(
    r"\b(um+|uh+|erm|hmm+|like|you know|i mean|sort of|kind of|actually|scratch that|delete that|no wait|"
    r"wait no|comma|period|full stop|question mark|exclamation (?:mark|point)|new ?line|new paragraph|"
    r"bullet(?: point)?|open quote|close quote|colon|semicolon|dash|hyphen|insert|paste)\b",
    re.IGNORECASE,
)
STUTTER = re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE)


def needs_model(text: str) -> bool:
    """True when the text has something only cleanup would fix."""
    return bool(NEEDS_MODEL.search(text) or STUTTER.search(text))


# ---- a) spoken formatting -----------------------------------------------------

# spoken words -> (symbol, kind). Kinds: "loose" converts anywhere; "strict" only at
# the end of a clause; "break" is a line break; "dash"/"open"/"close" have their own spacing.
SPOKEN = {
    "comma": (",", "loose"),
    "colon": (":", "loose"),
    "semicolon": (";", "loose"),
    "semi colon": (";", "loose"),
    "period": (".", "strict"),
    "full stop": (".", "strict"),
    "question mark": ("?", "strict"),
    "exclamation mark": ("!", "strict"),
    "exclamation point": ("!", "strict"),
    "dash": (" - ", "dash"),
    "open quote": ('"', "open"),
    "close quote": ('"', "close"),
    "end quote": ('"', "close"),
    "new line": ("\n", "break"),
    "newline": ("\n", "break"),
    "new paragraph": ("\n\n", "break"),
    "bullet point": ("\n- ", "break"),
}
_SPOKEN_RE = re.compile(
    r"(?<![\w'-])(" + "|".join(k.replace(" ", r"[ \t]+") for k in sorted(SPOKEN, key=len, reverse=True))
    + r")(?![\w'-])",
    re.IGNORECASE,
)
# "a comma", "the new line", "my colon": the word itself, not the command
_DETERMINERS = frozenset({
    "a", "an", "the", "this", "that", "no", "your", "my", "his", "her", "its", "our",
    "their", "each", "every", "any", "some", "what", "which", "another"})
# "the trial period", "the Oxford comma", "an em dash"
_COMPOUNDS = {
    "period": frozenset({
        "trial", "grace", "waiting", "notice", "cooling", "probation", "probationary", "billing",
        "reporting", "review", "transition", "time", "test", "testing", "study", "sample",
        "school", "class", "lunch", "holiday", "rest", "honeymoon", "introductory", "free",
        "launch", "warranty", "refund", "retention", "lockout", "cooldown", "payment", "pay",
        "accounting", "fiscal"}),
    "comma": frozenset({"oxford", "serial", "inverted"}),
    "dash": frozenset({"em", "en"}),
    "new line": frozenset({"brand", "product", "whole", "entire", "clothing", "fashion", "production"}),
    "bullet point": frozenset({
        "next", "first", "last", "second", "third", "fourth", "fifth", "final", "main", "key", "top",
        "brand", "product", "whole", "one", "two", "three", "each", "other", "previous"}),
}
_COMPOUNDS["newline"] = _COMPOUNDS["new line"]
# "a difficult period", "a two-year period", "a 14 day period": the noun, at the end of
# a sentence where the recognizer has put its own stop
_PERIOD_AFTER = frozenset({
    "day", "days", "week", "weeks", "month", "months", "year", "years", "quarter", "quarters", "hour",
    "hours", "minute", "minutes", "decade", "season", "term", "semester", "night", "weekend",
    "short", "long", "same", "given", "whole", "entire", "brief", "difficult", "strong", "weak", "busy",
    "quiet", "extended", "prolonged", "lengthy", "hard", "tough", "rough",
    "slow", "critical", "initial", "early", "late", "recent", "current", "previous", "next", "last",
    "certain", "specific", "limited", "set", "fixed", "transitional", "peak", "summer", "winter",
    "spring", "autumn", "historical", "medieval", "victorian", "postwar", "creative",
    "productive", "stressful", "challenging", "formative", "comment", "consultation", "lock", "tax"})
_PUNCT = ",.;:!?"


def _prev_word(before: str) -> str:
    m = re.search(r"([A-Za-z'-]+)[ \t,]*$", before)
    return m.group(1).lower() if m else ""


def _a_noun_period(before: str) -> bool:
    """"a difficult period", "a two-year period", "a 14 day period": not a full stop.
    A bare number is not enough: "call me at 5 period" is a full stop."""
    token = before.rstrip(" \t,").rsplit(None, 1)[-1:]
    if not token:
        return False
    word = token[0].lower()
    return "-" in word.strip("-") or word in _PERIOD_AFTER


def _clause_ends(after: str) -> bool:
    """What follows a strict word: the end, punctuation, a new line, a capital, another command."""
    rest = after.lstrip(" \t")
    return (not rest or rest[0] in _PUNCT + "\n\"')" or rest[0].isupper()
            or bool(_SPOKEN_RE.match(rest)))


def _convert(text: str, m: re.Match, code: bool) -> tuple[str, str] | None:
    """(text up to and including the symbol, the rest), or None to leave the word alone."""
    word = re.sub(r"\s+", " ", m.group(1).lower())
    sym, kind = SPOKEN[word]
    before, after = text[:m.start()], text[m.end():]
    prev = _prev_word(before)
    if prev in _DETERMINERS or prev in _COMPOUNDS.get(word, ()):
        return None
    if kind in ("loose", "strict") and not before.strip():
        return None                                 # nothing to punctuate: "Colon cancer ..."
    if kind == "strict" and not _clause_ends(after):
        return None
    if word == "period" and _a_noun_period(before):
        return None
    if word in ("new line", "newline") and re.match(r"[ \t]+of\b", after, re.IGNORECASE):
        return None                                 # "a new line of products"
    b, a = before.rstrip(" \t"), after.lstrip(" \t")
    if kind in ("loose", "strict"):
        b = b.rstrip(_PUNCT)                        # the recognizer's own pause or stop is replaced
        a = re.sub(r"^[,.;:!?]+[ \t]*", "", a)
        return b + sym, (" " + a if a and a[0] != "\n" else a)
    if kind == "break":
        a = re.sub(r"^[,.;:]+[ \t]*", "", a)
        return b.rstrip(",") + sym, a
    if kind == "dash":
        if code or not b or not a or not before.endswith(" ") or not after.startswith(" "):
            return None
        return b.rstrip(",") + sym, a.lstrip(", ")
    if kind == "open":
        a = re.sub(r"^[,:]+[ \t]*", "", a)
        return b + (" " if b and b[-1] not in "\n(\"" else "") + sym, a
    b = b.rstrip(",")                               # close quote
    return b + sym, (" " + a if a and a[0].isalnum() else a)


def spoken_formatting(text: str, code: bool = False) -> str:
    pos = 0
    while m := _SPOKEN_RE.search(text, pos):
        done = _convert(text, m, code)
        if done is None:
            pos = m.end()
            continue
        head, tail = done
        text, pos = head + tail, len(head)
    return text


# ---- b) scratch that ------------------------------------------------------------

_SCRATCH_RE = re.compile(r"(?<![\w'-])(scratch that|delete that|strike that)(?![\w'-])", re.IGNORECASE)
_BOUNDARY_RE = re.compile(r"[,.;!?](?=\s)|\n")
# "Did you delete that?", "I'll scratch that off", "I had to delete that": the words
# are part of the sentence when what comes right before them can't stand alone.
_SCRATCH_SUBJECTS = frozenset({
    "i", "i'll", "i'd", "i've", "you", "you'll", "you'd", "we", "we'll", "we'd", "they", "they'll", "to",
    "can", "could", "will", "would", "should", "must", "might", "may", "shall", "please", "don't", "didn't",
    "won't", "wouldn't", "can't", "shouldn't", "do", "did", "does", "let's", "just", "not", "never", "gonna",
    "wanna", "who", "why", "how"})


def _is_command(before: str, after: str, phrase: str) -> bool:
    """Whether "scratch that" / "delete that" stands on its own, or is in a sentence."""
    stripped = before.rstrip(" \t")
    prev, nxt = stripped[-1:], after.lstrip(" \t")[:1]
    left, right = prev in ("", *_PUNCT, "\n"), nxt in ("", *_PUNCT, "\n")
    if not left and _prev_word(stripped) in _SCRATCH_SUBJECTS:
        return False
    if phrase.startswith("scratch"):
        return left or right            # "..., scratch that okay" / "Send it Tuesday scratch that."
    return left and right               # "Send it Tuesday. Delete that." but not "Did you delete that?"


def scratch_that(text: str) -> str:
    pos = 0
    while m := _SCRATCH_RE.search(text, pos):
        before, after = text[:m.start()], text[m.end():]
        if not _is_command(before, after, m.group(1).lower()):
            pos = m.end()
            continue
        kept = before.rstrip().rstrip(_PUNCT).rstrip()
        cut = [b.end() for b in _BOUNDARY_RE.finditer(kept)]
        kept = kept[:cut[-1]] if cut else ""
        rest = re.sub(r"^[,.;:!?]*\s*", "", after)
        if not rest:
            kept = kept.rstrip(" ,;:")
        sep = " " if kept and rest and not kept.endswith("\n") else ""
        text, pos = kept + sep + rest, len(kept)
    return text


# ---- c) fillers and stutters --------------------------------------------------

_HESITATION_RE = re.compile(r"(?P<pre>,[ \t]*)?(?<![\w'-])(?:um+|uhm+|uh+|erm+|hmm+)(?![\w'-])(?P<post>[ \t]*,)?",
                            re.IGNORECASE)
_ASIDE = r"(?:like|you know|i mean)"
_ASIDE_BETWEEN_RE = re.compile(r",[ \t]*" + _ASIDE + r"[ \t]*,", re.IGNORECASE)
_ASIDE_START_RE = re.compile(r"(^|[.!?\n][ \t]*)(?:you know|i mean),[ \t]*", re.IGNORECASE)
_ASIDE_END_RE = re.compile(r",[ \t]*(?:you know|i mean)(?=[ \t]*(?:[.!?\n]|$))", re.IGNORECASE)
_REPEAT_RE = re.compile(r"(?<![\w'-])([A-Za-z']+)([ \t]+\1)+(?![\w'-])", re.IGNORECASE)
_KEEP_DOUBLED = frozenset({"had", "that", "bye", "ha", "no"})


def _collapse(m: re.Match) -> str:
    word = m.group(1)
    if len(word) > 4 or word.lower() in _KEEP_DOUBLED:
        return m.group(0)
    return word


def fillers(text: str) -> str:
    # "send it, um, Friday" keeps one comma; "Um, so" and "it's um fine" keep none
    text = _HESITATION_RE.sub(lambda m: "," if m.group("pre") and m.group("post") else "", text)
    text = _ASIDE_BETWEEN_RE.sub("", text)
    text = _ASIDE_START_RE.sub(r"\1", text)
    text = _ASIDE_END_RE.sub("", text)
    return _REPEAT_RE.sub(_collapse, text)


# ---- d) spacing and capitals ----------------------------------------------------

_ABBREVIATIONS = frozenset({"e.g.", "i.e.", "vs.", "a.m.", "p.m.", "mr.", "mrs.", "ms.", "dr.", "st.", "approx."})
_CAPITAL_RE = re.compile(r"(^(?:- )?|[.!?][\"')]*[ \t]+|\n(?:- )?)([a-z][\w']*)")
_LONE_I_RE = re.compile(r"(?<![\w'-])i(?=(?:'(?:m|d|ll|ve|s|re))?(?![\w'-])(?!\.\w))")


def _capitalize(text: str) -> str:
    def up(m: re.Match) -> str:
        word, tail = m.group(2), text[m.end():]
        if any(c.isupper() or c.isdigit() or c == "_" for c in word) or re.match(r"[./:@_\\-]\w", tail):
            # iPhone, user_id, x86, example.com
            return m.group(0)
        prev = (text[:m.start()] + m.group(1)).split()
        if prev and m.group(1).strip() and (prev[-1].lower() in _ABBREVIATIONS or prev[-1].endswith("...")):
            return m.group(0)                                                 # "e.g. this", "and then... nothing"
        return m.group(1) + word[0].upper() + word[1:]

    text = _CAPITAL_RE.sub(up, text)
    return _LONE_I_RE.sub("I", text)


def spacing(text: str, code: bool = False) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # in code only before , and ; ("if ! grep", "sudo !!" and "cd ." mean something)
    text = re.sub(r" +([,;])" if code else r" +([,;!?.:])(?!\w)", r"\1", text)
    text = re.sub(r",+(?=[.!?;:])", "", text)
    text = re.sub(r",{2,}", ",", text)
    return re.sub(r"^[\s,;]+", "", text).rstrip()


def _closes_sentence(text: str) -> str:
    """A sentence of three words or more that doesn't end in punctuation gets a period."""
    last = text.rsplit("\n", 1)[-1]
    if last.startswith("- ") or len(last.split()) < 3 or not last[-1:].isalnum():
        return text
    return text + "."


# ---- e) per app ------------------------------------------------------------------

def for_app(text: str, code: bool = False, chat: bool = False, fold_newlines: bool | None = None) -> str:
    """The rules that depend on where the text goes: also the safety net after the model.
    `fold_newlines` (default: `chat`) is for apps where typing a line break would send
    the message; leave it off when the app's line breaks are typed as Shift+Enter."""
    if code and re.search(r"[\w)\"']\.$", text) and not text.endswith(".."):
        text = text[:-1]
    if (chat if fold_newlines is None else fold_newlines) and not re.search(r"(^|\n)- ", text):
        text = re.sub(r"(?<=[\w)])\s*\n\s*", ". ", text)     # a line break becomes a sentence break
        text = re.sub(r"\s*\n\s*", " ", text)
    return text


# ---- public ------------------------------------------------------------------------

def tidy(text: str, level: str = "medium", *, code: bool = False, chat: bool = False,
         fold_newlines: bool | None = None) -> str:
    """Clean a raw transcript with local rules only (see the module docstring)."""
    if level == "none" or not text:
        return text
    if level != "light":
        text = spoken_formatting(text, code)
        text = scratch_that(text)
    text = spacing(fillers(text), code)
    if not re.search(r"\w", text):
        return ""
    text = for_app(text, code, chat, fold_newlines)
    if not code:
        text = _capitalize(text)
        if not chat:
            text = _closes_sentence(text)
    return text
