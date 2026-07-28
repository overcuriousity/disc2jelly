"""Guards for the frozen Windows build.

These failures only surface after a full PyInstaller + Inno Setup run on a
Windows machine, which is far too slow a feedback loop — so pin the two
things that broke: the entry script, and the wizard defaults plumbing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
BUILD = REPO / "build"
SPEC = (BUILD / "disc2jelly.spec").read_text(encoding="utf-8")
ISS = (BUILD / "disc2jelly.iss").read_text(encoding="utf-8")
PS1 = (BUILD / "build_windows.ps1").read_text(encoding="utf-8")


def _load_run_app():
    path = REPO / "disc2jelly" / "run_app.py"
    spec = importlib.util.spec_from_file_location("run_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_app_exposes_main():
    assert callable(_load_run_app().main)


def test_missing_std_streams_are_replaced(monkeypatch):
    """console=False builds start with sys.stdout/stderr None (isatty crash)."""
    run_app = _load_run_app()
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    run_app._ensure_std_streams()

    try:
        assert sys.stdout.isatty() is False
        assert sys.stderr.isatty() is False
        print("writable")  # what a bare print in the frozen app would do
    finally:
        for stream in (sys.stdout, sys.stderr):
            stream.close()


def test_entry_script_is_the_launcher_not_the_package_module():
    """app/main.py as entry script runs as __main__: its relative imports die."""
    assert '"run_app.py"' in SPEC
    assert '"app" / "main.py"' not in SPEC


def test_lazily_imported_app_modules_are_hidden_imports():
    for module in ("app.config", "app.jobs", "app.metadata", "app.webdav",
                   "app.destination", "app.drives", "app.dvdcss",
                   "app.handbrake", "app.scan"):
        assert f'"{module}"' in SPEC, f"{module} missing from hiddenimports"


def test_uvicorn_is_handed_the_app_object():
    """An import string re-imports the module, orphaning the started manager."""
    main_py = (REPO / "disc2jelly" / "app" / "main.py").read_text(encoding="utf-8")
    assert "uvicorn.run(app," in main_py
    assert 'uvicorn.run("' not in main_py


def test_installer_honours_the_configured_destination_kind():
    assert "DefaultDestinationKind" in ISS
    assert "/DDefaultDestinationKind=" in PS1


def test_missing_build_config_is_reported():
    assert "No build_config.toml" in PS1


def test_installer_omits_empty_values_from_install_defaults():
    """A blank wizard box must not overwrite a baked-in default with ""."""
    assert "procedure AddPair" in ISS
    assert "if Value = '' then" in ISS
    # the old unconditional JSON literal, which always emitted every key
    assert '\'  "webdav_url": "\'' not in ISS


def test_wizard_skips_webdav_page_when_everything_is_baked():
    assert "HasBakedPassword" in ISS
    assert "CredentialsAreComplete" in ISS
    assert "/DHasBakedPassword=1" in PS1
