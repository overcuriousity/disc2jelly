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


# --- layering: baked < install_defaults.json < config.json ------------------


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_install_defaults_supply_values_absent_from_config(tmp_path: Path) -> None:
    _write(tmp_path / "install_defaults.json",
           {"destination_kind": "local", "local_path": r"\\nas\media"})
    _write(tmp_path / "config.json", {"encoder": "h264"})
    cfg = config.load(path=tmp_path / "config.json")
    assert cfg.local_path == r"\\nas\media"
    assert cfg.encoder == "h264"


def test_user_config_wins_over_install_defaults(tmp_path: Path) -> None:
    _write(tmp_path / "install_defaults.json", {"local_path": "/installer/choice"})
    _write(tmp_path / "config.json", {"local_path": "/user/choice"})
    assert config.load(path=tmp_path / "config.json").local_path == "/user/choice"


def test_install_defaults_apply_when_user_config_is_absent(tmp_path: Path) -> None:
    _write(tmp_path / "install_defaults.json",
           {"destination_kind": "webdav", "webdav_url": "https://nas.example/dav"})
    cfg = config.load(path=tmp_path / "config.json")
    assert cfg.destination_kind == "webdav"
    assert cfg.webdav_url == "https://nas.example/dav"


def test_corrupt_install_defaults_degrade_to_field_defaults(tmp_path: Path) -> None:
    (tmp_path / "install_defaults.json").write_text("{not json", encoding="utf-8")
    _write(tmp_path / "config.json", {"encoder": "h264"})
    cfg = config.load(path=tmp_path / "config.json")
    assert cfg.destination_kind == "local"
    assert cfg.encoder == "h264"


def test_install_defaults_are_type_checked_too(tmp_path: Path) -> None:
    """The default_factory fields must not bypass the coercion rules."""
    _write(tmp_path / "install_defaults.json",
           {"local_path": 42, "hevc_quality": "nope", "webdav_url": "https://x"})
    cfg = config.load(path=tmp_path / "config.json")
    assert cfg.local_path == ""       # wrong type -> dropped
    assert cfg.hevc_quality == 22     # wrong type -> dropped
    assert cfg.webdav_url == "https://x"


def test_ascii_escaped_install_defaults_decode_to_the_original_text(
        tmp_path: Path) -> None:
    """The contract build/disc2jelly.iss's JsonEscape has to satisfy.

    SaveStringToFile writes the system ANSI codepage; this file is read as
    UTF-8. Escaping every non-ASCII character as \\uXXXX makes the two agree,
    and load() must recover the original umlauts. Before the fix the file was
    written raw and the resulting UnicodeDecodeError -- a ValueError subclass
    -- was swallowed, silently discarding every installer-collected setting.
    """
    (tmp_path / "install_defaults.json").write_text(
        '{\n'
        '  "destination_kind": "local",\n'
        '  "local_path": "D:\\\\Filme\\\\J\\u00f6rg"\n'
        '}\n',
        encoding="ascii",
    )
    cfg = config.load(path=tmp_path / "config.json")
    assert cfg.local_path == "D:\\Filme\\Jörg"


def test_raw_non_utf8_install_defaults_do_not_take_the_app_down(
        tmp_path: Path) -> None:
    """An ANSI-encoded file still degrades to defaults rather than raising."""
    (tmp_path / "install_defaults.json").write_bytes(
        b'{"local_path": "D:\\\\Filme\\\\J\xf6rg"}')  # cp1252 umlaut
    cfg = config.load(path=tmp_path / "config.json")
    assert cfg.local_path == ""


def test_install_defaults_path_sits_beside_config(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    if sys.platform.startswith("win"):
        pytest.skip("linux-only assertion")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.install_defaults_path() == (
        tmp_path / "disc2jelly" / "install_defaults.json")


def test_save_never_writes_install_defaults(tmp_path: Path) -> None:
    """The app owns config.json; the installer owns install_defaults.json."""
    config.save(config.Config(local_path="/x"), path=tmp_path / "config.json")
    assert not (tmp_path / "install_defaults.json").exists()


def test_baked_default_falls_back_when_nothing_is_baked() -> None:
    assert config.baked_default("NO_SUCH_CONSTANT", "fallback") == "fallback"
    assert config.baked_default("NO_SUCH_CONSTANT") == ""


def test_baked_values_seed_the_field_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """A build with baked values works before any settings file exists."""
    baked = type(sys)("_baked")
    baked.DESTINATION_KIND = "webdav"          # type: ignore[attr-defined]
    baked.WEBDAV_URL = "https://baked.example" # type: ignore[attr-defined]
    monkeypatch.setattr(config, "_baked", baked)
    cfg = config.Config()
    assert cfg.destination_kind == "webdav"
    assert cfg.webdav_url == "https://baked.example"
    assert cfg.local_path == ""  # not baked -> fallback


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


def test_find_binary_prefers_a_candidate_over_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bundled binary is the known-good one; a stray PATH entry is not."""
    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/bin/HandBrakeCLI")
    bundled = tmp_path / "HandBrakeCLI"
    bundled.write_text("x", encoding="utf-8")
    assert config.find_binary("HandBrakeCLI", "", [str(bundled)]) == str(bundled)


def test_handbrake_candidates_lead_with_the_bundled_copy() -> None:
    assert config.handbrake_candidates()[0].startswith(str(config.bundled_dir()))


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
