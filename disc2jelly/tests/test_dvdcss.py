"""Tests for disc2jelly.app.dvdcss (first-run libdvdcss acquisition)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app import dvdcss
from app.dvdcss import DvdCssError, ensure_libdvdcss

PAYLOAD = b"MZ fake libdvdcss-2.dll payload"
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()


def _fake_fetch(payload: bytes = PAYLOAD):
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return payload

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def _collect() -> tuple[list[dict], object]:
    events: list[dict] = []
    return events, events.append


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the Windows download path from any host platform."""
    monkeypatch.setattr(dvdcss.sys, "platform", "win32")


# --- already present --------------------------------------------------------


def test_returns_the_existing_dll_without_downloading(windows, tmp_path: Path) -> None:
    existing = tmp_path / dvdcss.DLL_NAME
    existing.write_bytes(PAYLOAD)
    fetch = _fake_fetch()
    _, emit = _collect()

    result = ensure_libdvdcss(tmp_path, emit, fetch=fetch)

    assert result == existing
    assert fetch.calls == []


# --- download ---------------------------------------------------------------


def test_downloads_and_installs_when_missing(windows, tmp_path: Path) -> None:
    fetch = _fake_fetch()
    _, emit = _collect()

    result = ensure_libdvdcss(tmp_path, emit, fetch=fetch)

    assert result == tmp_path / dvdcss.DLL_NAME
    assert result.read_bytes() == PAYLOAD
    assert fetch.calls == [dvdcss.DOWNLOAD_URL]
    assert dvdcss.DOWNLOAD_URL.startswith("https://")


def test_creates_the_destination_directory(windows, tmp_path: Path) -> None:
    dest = tmp_path / "vendor" / "nested"
    _, emit = _collect()
    ensure_libdvdcss(dest, emit, fetch=_fake_fetch())
    assert (dest / dvdcss.DLL_NAME).is_file()


def test_emits_a_visible_notice_before_downloading(windows, tmp_path: Path) -> None:
    events, emit = _collect()
    ensure_libdvdcss(tmp_path, emit, fetch=_fake_fetch())
    assert events, "the download must not be silent"
    assert any("libdvdcss" in e["detail"] for e in events)
    assert events[-1]["status"] == "done"


def test_network_failure_raises(windows, tmp_path: Path) -> None:
    def boom(url: str) -> bytes:
        raise OSError("dns failure")

    _, emit = _collect()
    with pytest.raises(DvdCssError, match="dns failure"):
        ensure_libdvdcss(tmp_path, emit, fetch=boom)
    assert not (tmp_path / dvdcss.DLL_NAME).exists()


# --- checksum pinning -------------------------------------------------------


def test_pinned_checksum_match_installs(
    windows, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(dvdcss, "EXPECTED_SHA256", PAYLOAD_SHA)
    _, emit = _collect()
    assert ensure_libdvdcss(tmp_path, emit, fetch=_fake_fetch()).is_file()


def test_pinned_checksum_mismatch_refuses_and_leaves_nothing(
    windows, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(dvdcss, "EXPECTED_SHA256", "0" * 64)
    _, emit = _collect()
    with pytest.raises(DvdCssError, match="checksum"):
        ensure_libdvdcss(tmp_path, emit, fetch=_fake_fetch())
    assert not (tmp_path / dvdcss.DLL_NAME).exists()


def test_unpinned_download_warns(
    windows, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no pinned hash the download is HTTPS-trust only — say so."""
    monkeypatch.setattr(dvdcss, "EXPECTED_SHA256", "")
    events, emit = _collect()
    ensure_libdvdcss(tmp_path, emit, fetch=_fake_fetch())
    assert any("not pinned" in e["detail"].lower() for e in events)


# --- Linux ------------------------------------------------------------------


def test_linux_reports_the_system_library_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dvdcss.sys, "platform", "linux")
    monkeypatch.setattr(dvdcss, "_find_system_library", lambda: "libdvdcss.so.2")
    _, emit = _collect()
    assert ensure_libdvdcss(None, emit) is None


def test_linux_without_the_library_raises_with_an_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dvdcss.sys, "platform", "linux")
    monkeypatch.setattr(dvdcss, "_find_system_library", lambda: None)
    _, emit = _collect()
    with pytest.raises(DvdCssError, match="libdvdcss"):
        ensure_libdvdcss(None, emit)
