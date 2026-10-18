"""The Home and Dictations scripts: syntax, their node tests, and that they plug into app.js's registry."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "chttr" / "ui_static"
SCRIPTS = ("home.js", "dictations.js")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_parse(name):
    r = subprocess.run([NODE, "--check", str(STATIC / name)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


@needs_node
def test_node_tests_pass():
    r = subprocess.run([NODE, "--test", str(ROOT / "tests" / "js" / "home.test.js")], capture_output=True, text=True,
                       timeout=60, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_wait_for_the_app_and_register_a_view(name):
    src = (STATIC / name).read_text(encoding="utf-8")
    assert "registerView(" in src
    assert '"chttr:ready"' in src
    assert "innerHTML" not in src                        # every string from history goes in as text
