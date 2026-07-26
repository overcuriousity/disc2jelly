"""Tests for disc2jelly.app.config."""

from __future__ import annotations

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
    assert cfg.min_title_seconds == 600
    assert cfg.keep_mkv is False
    assert cfg.makemkv_path == ""
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
        keep_mkv=True,
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
                "keep_mkv": 1,  # int not bool -> dropped
                "encoder": "h264",
            }
        ),
        encoding="utf-8",
    )
    cfg = config.load(path=p)
    assert cfg.webdav_url == "https://x"
    assert cfg.encoder == "h264"
    assert cfg.hevc_quality == 22  # default kept
    assert cfg.keep_mkv is False


# --- find_binary -----------------------------------------------------------


def test_find_binary_configured_exists(tmp_path: Path) -> None:
    exe = tmp_path / "makemkvcon"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    assert config.find_binary("makemkvcon", str(exe), ["/nope"]) == str(exe)


def test_find_binary_configured_missing_falls_to_which(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/bin/makemkvcon")
    assert (
        config.find_binary("makemkvcon", str(tmp_path / "missing"), [])
        == "/usr/bin/makemkvcon"
    )


def test_find_binary_probes_candidates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    cand = tmp_path / "opt" / "HandBrakeCLI"
    cand.parent.mkdir(parents=True)
    cand.write_text("x", encoding="utf-8")
    assert config.find_binary("HandBrakeCLI", "", ["/nonexistent", str(cand)]) == str(cand)


def test_find_binary_none_when_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    assert config.find_binary("makemkvcon", "", ["/nope/a", "/nope/b"]) is None


def test_candidates_cover_both_platforms() -> None:
    mkv = config.makemkv_candidates()
    hb = config.handbrake_candidates()
    assert mkv and hb
    if sys.platform.startswith("win"):
        assert any("makemkvcon" in c for c in mkv)
        assert any(c.endswith("HandBrakeCLI.exe") for c in hb)
    else:
        assert "/usr/bin/makemkvcon" in mkv
        assert "/usr/bin/HandBrakeCLI" in hb
