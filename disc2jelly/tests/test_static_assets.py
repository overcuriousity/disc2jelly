"""Guards for the static UI assets that only break in a real browser.

These are cheap string checks, not a rendering test — but they pin the two
mistakes that actually shipped: a modal that could not be closed, and a save
payload that sent NaN.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def test_hidden_attribute_beats_display_rules():
    """`element.hidden = true` must actually hide.

    [hidden] { display: none } lives in the browser's default stylesheet, so
    any class of ours that sets `display` outranks it. The settings modal is
    .modal-backdrop { display: flex }, which is why its Close button did
    nothing. Only an !important rule of our own restores the invariant.
    """
    # Strip /* comments */ first: prose about the rule is not the rule.
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    rule = re.search(r"\[hidden\]\s*\{([^}]*)\}", css)
    assert rule, "style.css needs a [hidden] rule"
    body = rule.group(1)
    assert "display" in body and "none" in body and "!important" in body


def test_every_js_hidden_target_exists_in_the_markup():
    """A typo in an id makes $(id) null and throws mid-function."""
    ids = set(re.findall(r'\$\("([a-z0-9-]+)"\)', JS))
    missing = sorted(i for i in ids if f'id="{i}"' not in HTML)
    assert not missing, f"app.js references ids absent from index.html: {missing}"


def test_js_settings_numbers_node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = Path(__file__).parent / "js_settings_numbers.test.mjs"
    res = subprocess.run([node, str(script)], capture_output=True, text=True,
                         timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr
