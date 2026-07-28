#!/usr/bin/env python3
"""Entry point for the frozen Windows build (and `python run_app.py`).

`app/main.py` cannot be the PyInstaller entry script: an entry script runs as
`__main__` with no parent package, so its `from .jobs import JobManager` fails
with "attempted relative import with no known parent package". This launcher
imports the package properly instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Frozen: PyInstaller puts the bundle root on sys.path already. Source run:
# make `app` importable no matter which directory the script is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _ensure_std_streams() -> None:
    """Give sys.stdout/stderr real file objects in the windowed build.

    A PyInstaller build with console=False starts with both set to None, so
    anything that touches them dies: uvicorn's log formatter calls
    sys.stdout.isatty() and the whole app fell over with "'NoneType' object
    has no attribute 'isatty'" before serving a single request. A plain
    print() anywhere would fail the same way. There is no console to write
    to, so the output goes to the null device.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


_ensure_std_streams()

from app.main import main  # noqa: E402  (after the sys.path fix, by design)

if __name__ == "__main__":
    main()
