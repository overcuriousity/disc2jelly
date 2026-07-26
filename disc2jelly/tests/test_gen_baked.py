"""Tests for build/gen_baked.py — the installer-defaults code generator.

The generated app/_baked.py must never carry a WebDAV password unless the
maintainer explicitly asks for it: PyInstaller does not obfuscate, so anything
in here is recoverable from the shipped binary with `strings`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

BUILD = Path(__file__).resolve().parent.parent.parent / "build"


def _load():
    spec = importlib.util.spec_from_file_location("gen_baked", BUILD / "gen_baked.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen_baked = _load()


FULL_CONFIG = {
    "tmdb_api_key": "abc123",
    "destination_kind": "webdav",
    "webdav_url": "https://nas.example/dav/files/me/media",
    "webdav_user": "ripper",
    "webdav_password": "s3cret-app-password",
    "local_path": r"\\nas\media",
}


def test_generated_module_is_valid_python_defining_the_expected_names() -> None:
    src = gen_baked.render_baked(FULL_CONFIG, include_password=False)
    namespace: dict = {}
    exec(compile(src, "_baked.py", "exec"), namespace)  # noqa: S102 - that is the point
    assert namespace["TMDB_API_KEY"] == "abc123"
    assert namespace["WEBDAV_URL"] == "https://nas.example/dav/files/me/media"
    assert namespace["WEBDAV_USER"] == "ripper"
    assert namespace["DESTINATION_KIND"] == "webdav"
    assert namespace["LOCAL_PATH"] == r"\\nas\media"


def test_password_is_omitted_by_default() -> None:
    src = gen_baked.render_baked(FULL_CONFIG, include_password=False)
    assert "s3cret-app-password" not in src
    namespace: dict = {}
    exec(compile(src, "_baked.py", "exec"), namespace)  # noqa: S102
    assert namespace["WEBDAV_PASSWORD"] == ""


def test_password_included_only_when_explicitly_requested() -> None:
    src = gen_baked.render_baked(FULL_CONFIG, include_password=True)
    namespace: dict = {}
    exec(compile(src, "_baked.py", "exec"), namespace)  # noqa: S102
    assert namespace["WEBDAV_PASSWORD"] == "s3cret-app-password"


def test_baking_a_password_emits_a_warning_comment() -> None:
    src = gen_baked.render_baked(FULL_CONFIG, include_password=True)
    assert "WARNING" in src
    assert "strings" in src.lower()


def test_missing_keys_become_empty_strings() -> None:
    src = gen_baked.render_baked({}, include_password=False)
    namespace: dict = {}
    exec(compile(src, "_baked.py", "exec"), namespace)  # noqa: S102
    assert namespace["TMDB_API_KEY"] == ""
    assert namespace["DESTINATION_KIND"] == "local"


def test_values_are_repr_quoted_so_backslashes_survive() -> None:
    src = gen_baked.render_baked({"local_path": r"C:\Media\Films"}, include_password=False)
    namespace: dict = {}
    exec(compile(src, "_baked.py", "exec"), namespace)  # noqa: S102
    assert namespace["LOCAL_PATH"] == r"C:\Media\Films"


def test_injection_via_a_config_value_cannot_execute() -> None:
    hostile = {"tmdb_api_key": '"\nimport os; os.system("boom")\nX = "'}
    src = gen_baked.render_baked(hostile, include_password=False)
    namespace: dict = {}
    exec(compile(src, "_baked.py", "exec"), namespace)  # noqa: S102
    assert namespace["TMDB_API_KEY"] == hostile["tmdb_api_key"]
    assert "X" not in namespace
