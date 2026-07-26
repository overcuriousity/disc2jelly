"""Tests for disc2jelly.app.config."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

from app import config


def test_defaults() -> None:
    cfg = config.Config()
    assert cfg.encoder == "hevc"
    assert cfg.hevc_quality == 22
    assert cfg.h264_quality == 20
    assert cfg.handbrake_path == ""
    assert cfg.temp_dir == ""


def test_config_path_linux_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if sys.platform.startswith("win"):
        pytest.skip("linux-only assertion")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.config_path() == tmp_path / "disc2jelly" / "config.json"


def test_config_path_linux_home_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if sys.platform.startswith("win"):
        pytest.skip("linux-only assertion")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert config.config_path() == tmp_path / ".config" / "disc2jelly" / "config.json"


def test_save_load_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "config.json"
    cfg = config.Config(
        webdav_url="https://nas.example/remote.php/dav/files/me/movies",
        webdav_user="me",
        webdav_password="secret",
        tmdb_api_key="abc123",
        encoder="h264",
        h264_quality=21,
        local_path="/srv/media",
        min_title_seconds=900,
    )
    config.save(cfg, path=p)
    assert p.is_file()
    loaded = config.load(path=p)
    assert loaded == cfg


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    assert config.load(path=tmp_path / "nope.json") == config.Config()


def test_load_corrupt_json_returns_defaults(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text("{not json", encoding="utf-8")
    assert config.load(path=p) == config.Config()


def test_load_ignores_unknown_keys_and_bad_types(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "webdav_url": "https://x",
                "bogus_key": 42,
                "hevc_quality": "twenty-two",  # wrong type -> dropped
                "min_title_seconds": "lots",  # wrong type -> dropped
                "encoder": "h264",
            }
        ),
        encoding="utf-8",
    )
    cfg = config.load(path=p)
    assert cfg.webdav_url == "https://x"
    assert cfg.encoder == "h264"
    assert cfg.hevc_quality == 22  # default kept
    assert cfg.min_title_seconds == 600  # default kept


# --- find_binary -----------------------------------------------------------


def test_find_binary_configured_exists(tmp_path: Path) -> None:
    exe = tmp_path / "HandBrakeCLI"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    assert config.find_binary("HandBrakeCLI", str(exe), ["/nope"]) == str(exe)


def test_find_binary_configured_missing_falls_to_which(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/bin/HandBrakeCLI")
    assert (
        config.find_binary("HandBrakeCLI", str(tmp_path / "missing"), [])
        == "/usr/bin/HandBrakeCLI"
    )


def test_find_binary_probes_candidates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    cand = tmp_path / "opt" / "HandBrakeCLI"
    cand.parent.mkdir(parents=True)
    cand.write_text("x", encoding="utf-8")
    assert config.find_binary("HandBrakeCLI", "", ["/nonexistent", str(cand)]) == str(cand)


def test_find_binary_none_when_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    assert config.find_binary("HandBrakeCLI", "", ["/nope/a", "/nope/b"]) is None


def test_candidates_cover_both_platforms() -> None:
    hb = config.handbrake_candidates()
    assert hb
    if sys.platform.startswith("win"):
        assert any(c.endswith("HandBrakeCLI.exe") for c in hb)
    else:
        assert "/usr/bin/HandBrakeCLI" in hb


# --- DVD-only config surface (MakeMKV removed) -------------------------------


def test_config_has_no_makemkv_or_keep_mkv_fields() -> None:
    names = {f.name for f in dataclasses.fields(config.Config)}
    assert "makemkv_path" not in names
    assert "keep_mkv" not in names


def test_destination_defaults_to_local_with_no_path() -> None:
    cfg = config.Config()
    assert cfg.destination_kind == "local"
    assert cfg.local_path == ""


def test_makemkv_candidates_is_gone() -> None:
    assert not hasattr(config, "makemkv_candidates")


def test_resolve_binaries_returns_handbrake_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = tmp_path / "HandBrakeCLI"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    assert config.resolve_binaries(config.Config(handbrake_path=str(exe))) == str(exe)


def test_resolve_binaries_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    monkeypatch.setattr(config, "handbrake_candidates", lambda: ["/nope/HandBrakeCLI"])
    assert config.resolve_binaries(config.Config()) is None


def test_bundled_handbrake_is_probed_before_a_system_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The installer ships HandBrakeCLI beside the app; prefer that copy."""
    monkeypatch.setattr(config, "bundled_dir", lambda: tmp_path)
    cands = config.handbrake_candidates()
    assert cands[0].startswith(str(tmp_path))


def test_bundled_dir_follows_the_frozen_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(tmp_path / "Disc2Jelly.exe"))
    assert config.bundled_dir() == tmp_path


def test_bundled_dir_falls_back_to_the_repo_vendor_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(config.sys, "frozen", raising=False)
    assert config.bundled_dir().name == "vendor"
