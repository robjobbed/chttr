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
