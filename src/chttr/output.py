"""Deliver text to the focused window (through the desktop layer in desktop.py)."""

from __future__ import annotations

import functools
import logging
import re
import subprocess
import threading
import time

from . import desktop as DT
from .config import OutputCfg

log = logging.getLogger("chttr.output")

_FAILURES = (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError)


@functools.cache
def current_desktop() -> DT.Desktop:
    desk = DT.detect()
    log.info("desktop: %s", desk.describe())
    return desk


def focused_window() -> tuple[str, str]:
    """(class, title) of the focused window, or empty strings."""
    return DT.focused_window(current_desktop())


def grab_selection(terminal: bool) -> str:
    """The text selected in the focused window, or "".

    Terminals: the primary selection (Ctrl+C would interrupt the shell). Elsewhere:
    copy with Ctrl+C into an emptied clipboard and read it back, so a stale
    selection is never mistaken for a current one. A text clipboard is restored
    afterwards; an image on the clipboard is lost.
    """
    desk = current_desktop()
    if terminal:
        return DT.clipboard_get(desk, primary=True)
    saved = DT.clipboard_get(desk) if DT.clipboard_has_text(desk) else None
    DT.clipboard_clear(desk)
    selection = ""
    try:
        DT.send_shortcut("ctrl+c", desk)
        for _ in range(8):           # the app answers asynchronously
            time.sleep(0.05)
            selection = DT.clipboard_get(desk)
            if selection:
                break
    except _FAILURES as exc:
        log.warning("could not copy the selection: %s", exc)
    finally:
        if saved:
            copy_to_clipboard(saved)
    return selection


def copy_to_clipboard(text: str) -> None:
    DT.clipboard_set(text, current_desktop())


def paste_shortcut() -> None:
    DT.send_shortcut("ctrl+v", current_desktop())


def paste_preserving_clipboard(text: str, restore_after: float = 0.6) -> None:
    """Paste `text` with Ctrl+V, then put the clipboard back the way it was."""
    desk = current_desktop()
    saved = DT.clipboard_get(desk) if DT.clipboard_has_text(desk) else None
    copy_to_clipboard(text)
    paste_shortcut()
    if saved is not None:
        timer = threading.Timer(restore_after, copy_to_clipboard, args=(saved,))
        timer.daemon = True
        timer.start()


def press_enter(delay: float = 0.0) -> None:
    """Press Enter in the focused window ("run it" at the end of a terminal dictation).
    `delay` lets a paste land first: the app reads the clipboard asynchronously."""
    if delay:
        time.sleep(delay)
    DT.send_shortcut("return", current_desktop())


def delete_selection() -> None:
    """Delete the selected text ("delete that" in command mode)."""
    DT.send_shortcut("backspace", current_desktop())


def deliver(text: str, cfg: OutputCfg, mode: str = "", trailing_space: bool | None = None,
            allow_paste: bool = True, newline: str = "enter") -> str:
    """Send text to the user. `mode`/`trailing_space` override cfg (per-app modes).
    `newline` is how line breaks are typed: "enter", or "shift+enter" for chat apps,
    where Enter sends the message. Returns the method actually used: "type", "paste"
    or "clipboard"."""
    mode = mode or cfg.mode
    if not text:                    # e.g. a dictation that was only "run it"
        return "clipboard" if mode == "clipboard" else "type"
    if (cfg.trailing_space if trailing_space is None else trailing_space) and not text.endswith(("\n", " ")):
        text += " "
    # Line breaks as Shift+Enter only work when typed: never paste those around it
    allow_paste = allow_paste and ("\n" not in text or newline == "enter")
    if mode == "type" and allow_paste and cfg.paste_over and len(text) > cfg.paste_over:
        try:
            paste_preserving_clipboard(text)
            return "paste"
        except _FAILURES as exc:
            log.warning("pasting failed (%s); typing instead", exc)
    if mode == "clipboard":
        copy_to_clipboard(text)
        return "clipboard"
    if mode == "paste":
        copy_to_clipboard(text)
        paste_shortcut()
        return "paste"
    try:
        DT.type_text(text, current_desktop(), cfg.typer, newline)
        return "type"
    except _FAILURES as exc:
        detail = (getattr(exc, "stderr", b"") or b"")
        detail = detail.decode(errors="replace") if isinstance(detail, bytes) else str(detail)
        log.warning("typing failed (%s)", detail.strip() or exc)
        if allow_paste:
            try:
                paste_preserving_clipboard(text)
                return "paste"
            except _FAILURES as exc2:
                log.warning("pasting failed too (%s)", exc2)
        if not cfg.fallback_to_clipboard:
            raise
        copy_to_clipboard(text)
        return "clipboard"


# ---- joining the text around the cursor ----------------------------------------

_NO_SPACE_AFTER = "([{\"\u201c\u2018/"
_NO_SPACE_AFTER_CODE = "._@$`~"         # obj.method, @file, $VAR, ~/path in code
_NO_SPACE_BEFORE = ",.;:!?)]}"
_MID_SENTENCE = re.compile(r"[^\W\d_][\"'\u201d\u2019)]*[ \t]*$|[,;:][ \t]*$")
_SENTENCE_END = re.compile(r"[.!?][\"'\u201d\u2019)\]]*\s*$")
_QUOTES = "\"'\u201c\u201d\u2018\u2019`"
# Words the recognizer capitalises only because they start its sentence. Anything
# else keeps its capital mid-sentence: it may be a name (Monday, Priyanka, Will).
_STARTER_WORDS = """
a an the this that these those there here it its it's that's there's what's
and but or so then also plus yet nor because since though although while if unless until when whenever
where wherever which who whom whose what why how whether as than like
we you they he she me us them him her his hers my our your their mine ours yours theirs one
we're we'll we've we'd you're you'll you've you'd they're they'll they've they'd he's he'll she's she'll
let's let is are was were be been being am do does did don't doesn't didn't have has had having
can can't could couldn't would wouldn't should shouldn't must might won't isn't aren't wasn't weren't
not no yes yeah yep nope ok okay sure thanks thank please sorry hi hey hello well oh dear good great nice
now today tonight tomorrow yesterday soon later then again already still just only even maybe perhaps
probably definitely actually really very quite too either neither both all any some each every
everyone everything everybody someone something somebody anyone anything anybody nothing nobody none
more most less least much many few other another such same next last first second after before
to of in on at by for with from into onto about over under around through between without within
otherwise however therefore instead meanwhile anyway besides hopefully unfortunately luckily basically
"""
_STARTERS = frozenset(_STARTER_WORDS.split())


def _first_word(text: str) -> str:
    m = re.match(r"\s*([\w'\u2019-]+)", text)
    return m.group(1) if m else ""


def _in_vocabulary(text: str, vocabulary: list[str]) -> bool:
    """`text` starts with one of your words (spelled your way, whatever its case)."""
    lowered = text.lstrip().casefold()
    for v in vocabulary:
        v = v.strip().casefold()
        if v and lowered.startswith(v) and not lowered[len(v):len(v) + 1].isalnum():
            return True
    return False


def _keeps_capital(text: str, vocabulary: list[str]) -> bool:
    """A capital at the start of `text` that is not just the start of a sentence:
    anything but a common sentence opener ("The", "Tomorrow", "We"), so names, days
    and months (Priyanka, Monday) stay; and one of your words, an acronym or a quote."""
    word = _first_word(text)
    if not word or text.lstrip()[:1] in _QUOTES:
        return True
    if word.replace("\u2019", "'").lower() not in _STARTERS or sum(c.isupper() for c in word) >= 2:
        return True
    return _in_vocabulary(text, vocabulary)


def smart_join(before: str, after: str, text: str, vocabulary: list[str] | None = None,
               code: bool = False) -> tuple[str, bool | None]:
    """Fit `text` between the field's `before` and `after` the cursor.

    Returns the adjusted text and a trailing-space override (None = leave it to the
    config): a space in front when the cursor sits right after a word, a lowercase
    first word when it continues a sentence ("as we said" + "Tomorrow works" ->
    " tomorrow works"), a capital after a full stop or on an empty line, and no
    trailing space when the text after the cursor starts with a space or
    punctuation. `code` (terminals, editors): spacing only, letters keep their case.
    """
    if not text:
        return text, None
    vocabulary = vocabulary or []
    stripped = text.lstrip()
    if before and not before[-1].isspace() and before[-1] not in _NO_SPACE_AFTER \
            and not (code and before[-1] in _NO_SPACE_AFTER_CODE) \
            and stripped[:1] not in _NO_SPACE_BEFORE and not text[0].isspace():
        text = " " + text
    if not code:
        i = len(text) - len(text.lstrip(" \t" + _QUOTES + "(["))
        first = text[i:i + 1]
        word = _first_word(text[i:])
        if not before.strip(" \t") or before.endswith("\n") or _SENTENCE_END.search(before):
            # a capital to start a sentence, unless the word is spelled lower-case on purpose (iPhone)
            if first.islower() and not any(c.isupper() for c in word[1:]) \
                    and not _in_vocabulary(text[i:], vocabulary):
                text = text[:i] + first.upper() + text[i + 1:]
        elif _MID_SENTENCE.search(before) and first.isupper() and not _keeps_capital(text, vocabulary):
            text = text[:i] + first.lower() + text[i + 1:]
            # continuing a sentence that goes on after the cursor: no full stop in the middle of it
            rest = after.lstrip()
            if rest[:1].islower() and text.endswith(".") and not text.endswith(".."):
                text = text[:-1]
    if after and (after[0].isspace() or after[0] in _NO_SPACE_BEFORE):
        return text, False
    return text, None
