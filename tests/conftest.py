"""Point every chttr path at a throwaway directory *before* chttr is imported:
config.py resolves its paths at import time."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

REAL_MODELS = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "chttr" / "models"
os.environ["CHTTR_TEST_REAL_MODELS"] = str(REAL_MODELS)
_ROOT = Path(tempfile.mkdtemp(prefix="chttr-tests-"))
for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
    os.environ[var] = str(_ROOT / var.lower())
    Path(os.environ[var]).mkdir(parents=True, exist_ok=True)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("CHTTR_CONFIG", None)

import pytest  # noqa: E402

from chttr import dictionary  # noqa: E402

FIXTURES = Path(__file__).with_name("fixtures")


@pytest.fixture(autouse=True)
def _no_system_word_list(monkeypatch):
    """Sound-alike correction consults /usr/share/dict; make it the same everywhere."""
    words = frozenset({"sorry", "sorrow", "serbia", "rob", "robe", "so", "the", "and", "send", "it", "to",
                       "call", "with", "about", "meeting", "tomorrow", "budget", "price", "deal"})
    monkeypatch.setattr(dictionary, "english_words", lambda: words)


@pytest.fixture(autouse=True)
def _real_models_for_model_tests(request, monkeypatch):
    """`-m model` tests use the models you downloaded; everything else stays sandboxed."""
    if not request.node.get_closest_marker("model"):
        return
    from chttr import config, diarize, transcriber

    parakeet = [Path.home() / ".local/share/voxtype/models/parakeet-tdt-0.6b-v3-int8",
                REAL_MODELS / "parakeet-tdt-0.6b-v3-int8"]
    for module in (config, transcriber):
        monkeypatch.setattr(module, "PARAKEET_SEARCH_DIRS", parakeet)
    monkeypatch.setattr(config, "MODELS_DIR", REAL_MODELS)
    monkeypatch.setattr(diarize, "MODELS_DIR", REAL_MODELS)


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture(autouse=True)
def _no_real_key_presses(monkeypatch):
    """The daemon presses Enter ("run it") and Backspace ("delete that") straight
    through the desktop; tests must never reach the real one."""
    from chttr import daemon

    monkeypatch.setattr(daemon, "press_enter", lambda delay=0.0: None)
    monkeypatch.setattr(daemon, "delete_selection", lambda: None)


@pytest.fixture(autouse=True)
def _no_timers_outliving_the_test(monkeypatch):
    """The daemon checks your edits 8 s after typing (a threading.Timer). One left
    running would fire during a later test, after the fakes are gone, and read the
    real focused text field; cancel them when the test ends."""
    import threading

    timers: list[threading.Timer] = []

    class Timer(threading.Timer):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            timers.append(self)

    monkeypatch.setattr(threading, "Timer", Timer)
    yield
    for t in timers:
        t.cancel()


def _sandboxed_calendar() -> None:
    """[meeting] calendar_file defaults to ~/.config/chttr/calendar-urls, which the XDG
    variables above don't move: no test, and no daemon thread a test leaves behind,
    reads your real calendar. Patched for the whole session, not per test."""
    from chttr import gcal

    real, private = gcal.read_urls, (Path.home() / ".config" / "chttr").resolve()

    def read_urls(path: str) -> list[str]:
        return [] if Path(path).expanduser().resolve().is_relative_to(private) else real(path)

    gcal.read_urls = read_urls


_sandboxed_calendar()
