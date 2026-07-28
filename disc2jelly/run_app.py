#!/usr/bin/env python3
"""Entry point for the frozen Windows build (and `python run_app.py`).

`app/main.py` cannot be the PyInstaller entry script: an entry script runs as
`__main__` with no parent package, so its `from .jobs import JobManager` fails
with "attempted relative import with no known parent package". This launcher
imports the package properly instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Frozen: PyInstaller puts the bundle root on sys.path already. Source run:
# make `app` importable no matter which directory the script is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import main  # noqa: E402  (after the sys.path fix, by design)

if __name__ == "__main__":
    main()
