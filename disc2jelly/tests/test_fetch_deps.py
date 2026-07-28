"""Tests for build/fetch_deps.py — the HandBrakeCLI vendoring script.

The checksum pin is the integrity guarantee for a binary we redistribute, so
the local-archive escape hatch must be verified exactly like a download, and
TLS verification must never be silently skipped.
"""

from __future__ import annotations

import hashlib
import importlib.util
import ssl
import zipfile
from pathlib import Path

import pytest

BUILD = Path(__file__).resolve().parent.parent.parent / "build"


def _load():
    spec = importlib.util.spec_from_file_location("fetch_deps", BUILD / "fetch_deps.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_deps = _load()


def make_archive(tmp_path: Path, exe=b"MZ fake", licence=True) -> Path:
    path = tmp_path / "HandBrakeCLI.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("HandBrakeCLI.exe", exe)
        if licence:
            zf.writestr("doc/COPYING", "GPLv2 text")
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_local_archive_skips_download(monkeypatch, tmp_path):
    archive = make_archive(tmp_path)
    monkeypatch.setattr(fetch_deps, "HANDBRAKE_SHA256", digest(archive))
    monkeypatch.setattr(fetch_deps, "fetch",
                        lambda url: pytest.fail("no download expected"))
    vendor = tmp_path / "vendor"

    assert fetch_deps.main(
        ["--archive", str(archive), "--vendor-dir", str(vendor)]) == 0
    assert (vendor / "HandBrakeCLI.exe").read_bytes() == b"MZ fake"
    assert (vendor / "COPYING").exists()


def test_local_archive_still_checksum_verified(monkeypatch, tmp_path):
    archive = make_archive(tmp_path, exe=b"tampered")
    monkeypatch.setattr(fetch_deps, "HANDBRAKE_SHA256", "0" * 64)
    with pytest.raises(SystemExit, match="checksum mismatch"):
        fetch_deps.main(
            ["--archive", str(archive), "--vendor-dir", str(tmp_path / "v")])


def test_missing_local_archive_is_an_error(tmp_path):
    with pytest.raises(SystemExit, match="archive not found"):
        fetch_deps.main(["--archive", str(tmp_path / "nope.zip")])


def test_ssl_context_verifies(monkeypatch):
    """certifi or not, the context must check hostname and certificates."""
    ctx = fetch_deps._ssl_context()
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_cert_failure_names_the_fix(monkeypatch):
    def boom(*a, **k):
        raise fetch_deps.urllib.error.URLError(
            ssl.SSLCertVerificationError("unable to get local issuer certificate"))

    monkeypatch.setattr(fetch_deps.urllib.request, "urlopen", boom)
    with pytest.raises(SystemExit, match="--archive"):
        fetch_deps.fetch("https://example.invalid/x.zip")
