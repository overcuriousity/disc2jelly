"""Tests for build/gen_wizard_defaults.py — the installer's pre-filled values.

The generated .isi is compiled into Setup.exe, so a value that escapes its
string literal would break the build (or worse, alter the script). Quoting is
the thing worth pinning.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

BUILD = Path(__file__).resolve().parent.parent.parent / "build"


def _load():
    spec = importlib.util.spec_from_file_location(
        "gen_wizard_defaults", BUILD / "gen_wizard_defaults.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load()


def test_every_wizard_field_is_defined_even_when_config_is_empty():
    out = gen.render({})
    for define in ("DefaultDestinationKind", "DefaultLocalPath",
                   "DefaultWebdavUrl", "DefaultWebdavUser",
                   "DefaultWebdavPassword"):
        assert f"#define {define} " in out


def test_values_are_emitted_as_quoted_ispp_strings():
    out = gen.render({
        "destination_kind": "webdav",
        "webdav_url": "https://streaming.example/dav",
        "webdav_user": "disc2jelly",
        "webdav_password": "s3cret",
    })
    assert '#define DefaultWebdavUrl "https://streaming.example/dav"' in out
    assert '#define DefaultWebdavPassword "s3cret"' in out
    assert '#define DefaultDestinationKind "webdav"' in out


def test_embedded_quote_is_doubled_not_left_to_break_the_literal():
    out = gen.render({"webdav_password": 'a"b'})
    assert '#define DefaultWebdavPassword "a""b"' in out


def test_default_destination_kind_is_local():
    assert '#define DefaultDestinationKind "local"' in gen.render({})


def test_output_is_written_with_a_bom_for_inno_unicode(tmp_path):
    cfg = tmp_path / "build_config.toml"
    cfg.write_text('webdav_user = "kimi"\n', encoding="utf-8")
    out = tmp_path / "wizard_defaults.isi"

    assert gen.main(["--config", str(cfg), "--output", str(out)]) == 0
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")
    assert '#define DefaultWebdavUser "kimi"' in out.read_text(encoding="utf-8-sig")


def test_missing_config_still_writes_empty_defaults(tmp_path):
    out = tmp_path / "wizard_defaults.isi"
    assert gen.main(["--config", str(tmp_path / "nope.toml"),
                     "--output", str(out)]) == 0
    assert '#define DefaultWebdavUrl ""' in out.read_text(encoding="utf-8-sig")
