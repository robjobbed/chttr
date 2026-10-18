"""Spoken code: identifier casing and file references, done locally before cleanup.

Anywhere:
    "camel case user id"        -> userId
    "snake case user id"        -> user_id
    "kebab case user id"        -> user-id
    "pascal case user id"       -> UserId
    "constant case max retries" -> MAX_RETRIES   (also "screaming snake case")

In code editors and terminals (and other apps whose mode sets `code = true`):
    "at main dot py"            -> @main.py     (Claude Code, Cursor, Aider file mentions)
    "at src slash app dot ts"   -> @src/app.ts
    "main dot py"               -> main.py      (known extensions only)
    "dash dash verbose"         -> --verbose    ("dash dash dry dash run" -> --dry-run)
    "dash m"                    -> -m
    "tilde slash notes"         -> ~/notes      (also "dot slash")
    "pipe", "double colon", "arrow", "fat arrow", "ampersand", "backtick ... backtick",
    "open paren" / "close paren" (bracket, brace)  -> | :: -> => & `...` ( ) [ ] { }
    "..., run it"               -> typed, then Enter is pressed (see strip_enter)

A cased phrase runs until punctuation, a filler or a short connecting word
("to", "and", "in", ...) or four words, whichever comes first.
"""

from __future__ import annotations

import re

CASES = {
    "camel case": "camel", "camelcase": "camel",
    "snake case": "snake", "snakecase": "snake",
    "kebab case": "kebab", "dash case": "kebab",
    "pascal case": "pascal", "title case": "pascal",
    "constant case": "constant", "screaming snake case": "constant", "all caps": "constant",
}
STOP_WORDS = {"to", "and", "or", "in", "on", "with", "from", "for", "then", "the", "a", "an", "into", "as", "of",
              "is", "equals", "plus", "minus"}
MAX_WORDS = 4
EXTENSIONS = {"py", "ts", "tsx", "js", "jsx", "json", "md", "toml", "yaml", "yml", "rs", "go", "rb", "java", "kt",
              "c", "h", "cpp", "hpp", "cs", "swift", "sh", "lua", "html", "css", "scss", "sql", "txt", "lock", "env",
              "conf", "qml", "vue", "svelte", "php", "ex", "exs", "zig", "nix", "dart"}

_CASE_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(k) for k in CASES), key=len, reverse=True)) + r")\s+",
                      re.IGNORECASE)


def _join(words: list[str], case: str) -> str:
    words = [w.lower() for w in words]
    if case == "camel":
        return words[0] + "".join(w.capitalize() for w in words[1:])
    if case == "pascal":
        return "".join(w.capitalize() for w in words)
    if case == "snake":
        return "_".join(words)
    if case == "kebab":
        return "-".join(words)
    return "_".join(words).upper()


_HYPHENATED = re.compile(r"\b(camel|snake|kebab|pascal|constant|dash|title)[-\s]?case\b", re.IGNORECASE)


def apply_casing(text: str) -> str:
    """Replace "<case name> word word ..." with the cased identifier."""
    text = _HYPHENATED.sub(lambda m: m.group(1) + " case", text)   # the recognizer may write "camel-case"
    out, pos = [], 0
    for m in _CASE_RE.finditer(text):
        if m.start() < pos:
            continue
        case = CASES[m.group(1).lower()]
        words, end = [], m.end()
        for w in re.finditer(r"[A-Za-z0-9]+|[^\sA-Za-z0-9]", text[m.end():]):
            token = w.group(0)
            if not token[0].isalnum() or token.lower() in STOP_WORDS or len(words) == MAX_WORDS:
                break
            words.append(token)
            end = m.end() + w.end()
        if not words:
            continue
        out.append(text[pos:m.start()])
        out.append(_join(words, case))
        pos = end
    out.append(text[pos:])
    return "".join(out)


_PATH_WORD = r"[A-Za-z0-9_-]+"
_FILE_RE = re.compile(
    rf"\b(?P<at>at\s+)?(?P<path>{_PATH_WORD}(?:\s+slash\s+{_PATH_WORD})*)\s+dot\s+(?P<ext>[A-Za-z0-9]+)\b",
    re.IGNORECASE,
)


def apply_files(text: str) -> str:
    """ "at src slash app dot ts" -> "@src/app.ts"; "main dot py" -> "main.py"."""

    def sub(m: re.Match) -> str:
        ext = m.group("ext").lower()
        if ext not in EXTENSIONS:
            return m.group(0)
        path = re.sub(r"\s+slash\s+", "/", m.group("path"), flags=re.IGNORECASE)
        return ("@" if m.group("at") else "") + path + "." + ext

    text = _FILE_RE.sub(sub, text)
    # the recognizer sometimes writes the file name itself: "at main.py"
    return _WRITTEN_RE.sub(lambda m: "@" + m.group(1) if m.group(2).lower() in EXTENSIONS else m.group(0), text)


_WRITTEN_RE = re.compile(r"\bat\s+([\w/-]+\.([A-Za-z0-9]+))\b", re.IGNORECASE)


_W = r"[A-Za-z0-9_]+"
_FLAG_RE = re.compile(rf"\bdash[\s-]+dash\s+({_W}(?:\s+dash\s+{_W})*)", re.IGNORECASE)
_SHORT_FLAG_RE = re.compile(r"\bdash\s+([A-Za-z])\b(?!['\u2019])")
_HOME_RE = re.compile(rf"\b(tilde|dot)\s+slash\b(?:\s+({_W}(?:\s+slash\s+{_W})*))?", re.IGNORECASE)
_BACKTICKS_RE = re.compile(r"\bback[\s-]?tick\s+(.+?)\s+back[\s-]?tick\b", re.IGNORECASE)
_PAIRS = {"paren": "()", "parenthesis": "()", "bracket": "[]", "square bracket": "[]", "brace": "{}",
          "curly brace": "{}", "curly": "{}"}
_OPEN_RE = re.compile(r"(\s*)\b(?:open|left)\s+(paren|parenthesis|square bracket|bracket|curly brace|brace|curly)\b\s*",
                      re.IGNORECASE)
_CLOSE_RE = re.compile(r"\s*\b(close|closed|right)\s+(paren|parenthesis|square bracket|bracket|curly brace|brace|curly)\b",
                       re.IGNORECASE)
# Words that make "pipe" and "arrow" ordinary words: "pipe the output to jq", "the arrow keys"
_PROSE_BEFORE = {"a", "an", "the", "this", "that", "my", "your", "data", "named", "pipe"}
_PROSE_AFTER = {"it", "the", "that", "this", "them", "into", "to", "through", "output", "everything", "all",
                "function", "functions", "key", "keys", "icon", "button", "up", "down", "left", "right", "symbol"}
_SYMBOLS = [   # (spoken, typed); the spaces around them stay
    (r"\b(?:double\s+ampersand|ampersand\s+ampersand)\b", "&&"),
    (r"\b(?:double\s+pipe|pipe\s+pipe)\b", "||"),
    (r"\s*\bdouble\s+colon\b\s*", "::"),
    (r"\bfat\s+arrow\b", "=>"),
    (r"(?<=\S) arrow\b", " ->"),
    (r"(?<=\S) pipe (?=\S)", " | "),
    (r"\bampersand\b", "&"),
    (r"\bback[\s-]?tick\b", "`"),
]
_SYMBOL_RES = [(re.compile(p, re.IGNORECASE), typed) for p, typed in _SYMBOLS]


def _symbol(typed: str, text: str):
    def sub(m: re.Match) -> str:
        if typed.strip() in ("->", "|"):
            before = re.findall(r"[\w']+", text[:m.start()])
            after = re.findall(r"[\w']+", text[m.end():])
            if (before and before[-1].lower() in _PROSE_BEFORE) or (after and after[0].lower() in _PROSE_AFTER):
                return m.group(0)
        return typed
    return sub


_KEYWORDS = {"if", "for", "while", "switch", "return", "and", "or", "not", "in", "elif", "with", "catch", "match",
             "case", "when", "yield", "await", "is", "else", "equals", "plus", "minus", "times", "then", "to", "of"}


def _open(m: re.Match, text: str) -> str:
    """ "print open paren" -> "print(", but "if open paren" -> "if (" and "main() open brace" -> "main() {"."""
    bracket = _PAIRS[m.group(2).lower()][0]
    prev = re.findall(r"\w+$|\S$", text[:m.start()])
    attach = bracket != "{" and prev and prev[0][-1].isalnum() and prev[0].lower() not in _KEYWORDS
    return bracket if attach or not m.group(1) else " " + bracket


def apply_symbols(text: str) -> str:
    """Spoken flags, paths and symbols, for terminals and code editors:
    "ls dash l tilde slash notes pipe grep dash dash ignore dash case todo"
    -> "ls -l ~/notes | grep --ignore-case todo"."""
    text = _FLAG_RE.sub(lambda m: "--" + "-".join(re.split(r"\s+dash\s+", m.group(1), flags=re.IGNORECASE)), text)
    text = _SHORT_FLAG_RE.sub(lambda m: "-" + m.group(1), text)
    text = _HOME_RE.sub(lambda m: ("~/" if m.group(1).lower() == "tilde" else "./")
                        + re.sub(r"\s+slash\s+", "/", m.group(2) or "", flags=re.IGNORECASE), text)
    text = _BACKTICKS_RE.sub(lambda m: f"`{m.group(1)}`", text)
    text = _OPEN_RE.sub(lambda m: _open(m, text), text)
    text = _CLOSE_RE.sub(lambda m: _PAIRS[m.group(2).lower()][1], text)
    for pattern, typed in _SYMBOL_RES:
        text = pattern.sub(_symbol(typed, text), text)
    return text


_ENTER_RE = re.compile(r"(?P<sep>^|\s*[,;.:\u2014-]+\s*|\s+)(run it|press enter|hit enter|send it|and go)[\s.!]*$",
                       re.IGNORECASE)
# "fix the bug and run it", "Priyanka will send it": part of the sentence, not a key press
_CONNECTORS = {"and", "then", "to", "or", "but", "so", "will", "would", "can", "could", "should", "must", "might",
               "please", "i", "we", "you", "they", "he", "she", "i'll", "we'll", "you'll", "let's", "just", "also",
               "me", "us", "not", "don't", "gonna", "wanna", "ll", "d"}


def strip_enter(text: str) -> tuple[str, bool]:
    """ "git status, run it" -> ("git status", True): ending a dictation with "run it",
    "press enter", "hit enter", "send it" or "and go" presses Enter after typing.
    Only at the very end, and not when it is part of the sentence: "run it again
    later" and "fix the test and run it" are left alone."""
    m = _ENTER_RE.search(text)
    if not m:
        return text, False
    head = text[:m.start()]
    if m.group("sep").strip() == "" and head:
        words = re.findall(r"[\w'\u2019]+", head.lower().replace("\u2019", "'"))
        if words and words[-1] in _CONNECTORS:
            return text, False
    return head.rstrip(" ,;:\u2014-"), True


def transform(text: str, code: bool) -> str:
    if code:        # symbols first: "dash dash ignore dash case" is a flag, not kebab case
        return apply_files(apply_casing(apply_symbols(text)))
    return apply_casing(text)


def keep_list(original: str, transformed: str) -> list[str]:
    """Identifiers and paths the transform produced, for the cleanup model to keep verbatim."""
    before = set(original.split())
    return [w.strip(".,;:!?") for w in transformed.split() if w not in before and re.search(r"[_@./-]|[a-z][A-Z]", w)]
