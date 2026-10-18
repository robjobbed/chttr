"""The browser side of `chttr ui`: syntax of every script, the node unit tests of the pure
helpers (tests/js), and the contract other scripts rely on. Skipped without node."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/chttr/ui_static"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
@pytest.mark.parametrize("script", sorted(STATIC.glob("*.js")), ids=lambda p: p.name)
def test_scripts_parse(script):
    out = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


@needs_node
def test_node_unit_tests_pass():
    files = [str(ROOT / "tests/js/route.test.js"), str(ROOT / "tests/js/lib.test.js")]
    out = subprocess.run([NODE, "--test", *files], capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr


def test_app_exposes_its_api_before_announcing_it_is_ready():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    exposed = app.index("window.chttr = {")
    api = app[exposed:app.index("};", exposed)]
    for name in ("api", "el", "toast", "ts", "when", "state", "navigate", "registerView", "openMeeting", "setView"):
        assert re.search(rf"\b{name}\b", api), name
    # chttr:ready is sent from start(), and start() only runs once window.chttr is set and every
    # script has loaded (DOMContentLoaded), so other views can register first
    start = app.index("async function start()")
    start_body = app[start:app.index("\n  }\n", start)]
    assert app.count('new CustomEvent("chttr:ready")') == 1 and 'new CustomEvent("chttr:ready")' in start_body
    assert start_body.index("loadMeetings()") < start_body.index("chttr:ready")
    launch = app.index('addEventListener("DOMContentLoaded", start)')
    assert exposed < launch and "start()" not in app[:exposed].replace("async function start()", "")


def test_searches_and_meeting_opens_drop_stale_answers():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for fn in ("runSearch", "loadMeeting"):
        body = app[app.index(f"async function {fn}("):]
        body = body[:body.index("\n  }\n")]
        assert "nextSeq(" in body and "isLatest(" in body, fn
        assert body.index("nextSeq(") < body.index("await"), fn


def test_no_inner_html_and_no_prompt_dialogs():
    for script in STATIC.glob("*.js"):
        text = script.read_text(encoding="utf-8")
        assert ".innerHTML" not in text and "insertAdjacentHTML" not in text, script.name
        assert not re.search(r"\b(prompt|confirm|alert)\(", text), script.name


def test_css_declares_both_schemes():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert re.search(r":root\s*{\s*color-scheme: light dark;", css)
    assert ':root:not([data-theme="light"])' in css and ':root[data-theme="dark"]' in css
    assert "prefers-reduced-motion: reduce" in css and ".sr-only" in css and ":focus-visible" in css
    for i in range(1, 9):
        assert f"--c{i}:" in css
