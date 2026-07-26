"""Runs the DOM-less node logic test for app.js badgeForEvent (fix #1).

Skipped when node is not installed.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_js_badge_logic_node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = Path(__file__).parent / "js_badge_logic.test.mjs"
    res = subprocess.run([node, str(script)], capture_output=True, text=True,
                         timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr
