# PyInstaller spec for Disc2Jelly — onedir, not onefile.
#
# onefile self-extracts the whole bundle to %TEMP% on every launch (slow with
# HandBrakeCLI inside) and unsigned onefile binaries draw antivirus false
# positives far more often. The Inno Setup installer hides the folder anyway,
# so the user still just double-clicks one shortcut.
#
# Build:  pyinstaller build/disc2jelly.spec --noconfirm

import os
from pathlib import Path

SPEC_DIR = Path(os.path.abspath(SPECPATH))
APP_ROOT = SPEC_DIR.parent / "disc2jelly"

datas = [
    (str(APP_ROOT / "static"), "static"),
]

# HandBrakeCLI.exe lands next to the executable so config.bundled_dir() finds
# it first; fetch_deps.py puts it in disc2jelly/vendor/ before the build.
vendor = APP_ROOT / "vendor"
binaries = []
if (vendor / "HandBrakeCLI.exe").is_file():
    binaries.append((str(vendor / "HandBrakeCLI.exe"), "."))
for extra in ("COPYING", "COPYING.txt", "LICENSE"):
    if (vendor / extra).is_file():
        datas.append((str(vendor / extra), "."))

# run_app.py, not app/main.py: an entry script runs as __main__ with no parent
# package, so main.py's `from .jobs import ...` would fail at startup.
a = Analysis(
    [str(APP_ROOT / "run_app.py")],
    pathex=[str(APP_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        # main.py imports these lazily inside handlers (see its docstring);
        # spell them out so a missed one cannot ship as a runtime ImportError.
        "app.config",
        "app.destination",
        "app.drives",
        "app.dvdcss",
        "app.handbrake",
        "app.jobs",
        "app.main",
        "app.metadata",
        "app.scan",
        "app.webdav",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Disc2Jelly",
    console=False,       # no console window; the UI is the browser
    icon=None,
    debug=False,
    strip=False,
    upx=False,           # UPX compression is a common AV false-positive trigger
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Disc2Jelly",
)
