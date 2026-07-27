"""Tests for disc2jelly.app.dvdcss (libdvdcss detection and guidance).

There is no acquisition path to test: VideoLAN publishes libdvdcss as source
tarballs only, so the module detects and explains but never installs. An
earlier version downloaded from a win64/ URL that has never existed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import dvdcss
from app.dvdcss import DvdCssError


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the Windows path from any host platform."""
    monkeypatch.setattr(dvdcss.sys, "platform", "win32")


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dvdcss.sys, "platform", "linux")


# --- Windows ----------------------------------------------------------------


def test_windows_dll_present_in_the_bundled_dir(windows, tmp_path: Path) -> None:
    (tmp_path / dvdcss.DLL_NAME).write_bytes(b"MZ")
    assert dvdcss.is_available(tmp_path) is True
    dvdcss.require(tmp_path)  # must not raise


def test_windows_dll_absent(windows, tmp_path: Path) -> None:
    assert dvdcss.is_available(tmp_path) is False


def test_windows_hint_names_the_exact_folder(windows, tmp_path: Path) -> None:
    hint = dvdcss.hint(tmp_path)
    assert str(tmp_path) in hint
    assert dvdcss.DLL_NAME in hint


def test_windows_without_a_bundled_dir_is_unavailable(windows) -> None:
    assert dvdcss.is_available(None) is False
    assert "Disc2Jelly folder" in dvdcss.hint(None)


def test_windows_require_raises_with_the_guidance(windows, tmp_path: Path) -> None:
    with pytest.raises(DvdCssError) as excinfo:
        dvdcss.require(tmp_path)
    assert str(tmp_path) in str(excinfo.value)


def test_windows_does_not_consult_the_system_loader(
        windows, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """find_library("dvdcss") is meaningless for the bundled Windows layout."""
    monkeypatch.setattr(dvdcss, "_find_system_library", lambda: "/usr/lib/libdvdcss.so.2")
    assert dvdcss.is_available(tmp_path) is False


# --- Linux ------------------------------------------------------------------


def test_linux_uses_the_system_library(
        linux, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dvdcss, "_find_system_library", lambda: "libdvdcss.so.2")
    assert dvdcss.is_available() is True
    dvdcss.require()  # must not raise


def test_linux_missing_library_gives_the_package_hint(
        linux, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dvdcss, "_find_system_library", lambda: None)
    assert dvdcss.is_available() is False
    with pytest.raises(DvdCssError) as excinfo:
        dvdcss.require()
    assert "libdvd-pkg" in str(excinfo.value)
    assert "RPM Fusion" in str(excinfo.value)


def test_linux_ignores_the_bundled_dir(
        linux, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A stray DLL in vendor/ must not make Linux think it is set up."""
    monkeypatch.setattr(dvdcss, "_find_system_library", lambda: None)
    (tmp_path / dvdcss.DLL_NAME).write_bytes(b"MZ")
    assert dvdcss.is_available(tmp_path) is False


# --- contract ---------------------------------------------------------------


def test_is_available_never_raises(
        windows, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("broken")

    monkeypatch.setattr(dvdcss, "Path", boom)
    assert dvdcss.is_available("anywhere") is False


def test_no_download_surface_remains() -> None:
    """Guards against reintroducing the 404 URL."""
    for gone in ("DOWNLOAD_URL", "EXPECTED_SHA256", "ensure_libdvdcss",
                 "_https_fetch", "LIBDVDCSS_VERSION"):
        assert not hasattr(dvdcss, gone), f"{gone} should be gone"
