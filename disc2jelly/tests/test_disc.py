"""Tests for disc2jelly.app.disc (subprocess mocked via fixtures)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import disc

FIXTURES = Path(__file__).parent / "fixtures"


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _mock_run(monkeypatch: pytest.MonkeyPatch, stdout: str) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):  # noqa: ANN001, ANN202
        calls.append(list(args))
        assert kwargs.get("timeout") is not None, "every call must have a timeout"
        assert kwargs.get("env", {}).get("LC_ALL") == "C"
        return _Completed(stdout)

    monkeypatch.setattr(disc.subprocess, "run", fake_run)
    return calls


# --- list_drives -----------------------------------------------------------


def test_list_drives_parses_drv_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    out = (FIXTURES / "makemkv_drives_output.txt").read_text(encoding="utf-8")
    calls = _mock_run(monkeypatch, out)
    drives = disc.list_drives("/usr/bin/makemkvcon")
    assert calls[0][:2] == ["/usr/bin/makemkvcon", "-r"]
    assert "disc:9999" in calls[0]
    # status 256 (absent) drive is skipped
    assert [d.id for d in drives] == ["disc:0", "disc:1", "disc:2"]
    ready = drives[1]
    assert ready.label == "BUILD"  # disc label preferred
    assert ready.device == "/dev/sr0"
    # empty drive falls back to drive name
    assert drives[0].label.startswith("BD-RE PIONEER")
    assert drives[0].device == "/dev/sr2"


def test_list_drives_tcount0_msg5010_is_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    out = (FIXTURES / "makemkv_drives_output.txt").read_text(encoding="utf-8")
    _mock_run(monkeypatch, out)
    drives = disc.list_drives("makemkvcon")  # must not raise / must not be empty
    assert len(drives) == 3


def test_list_drives_failure_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("no such binary")

    monkeypatch.setattr(disc.subprocess, "run", boom)
    assert disc.list_drives("/missing/makemkvcon") == []


def test_list_drives_timeout_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise subprocess.TimeoutExpired(cmd="makemkvcon", timeout=30)

    monkeypatch.setattr(disc.subprocess, "run", slow)
    assert disc.list_drives("makemkvcon") == []


# --- disc_info / list_titles ------------------------------------------------


def test_disc_info_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    out = (FIXTURES / "makemkv_info_output.txt").read_text(encoding="utf-8")
    _mock_run(monkeypatch, out)
    info = disc.disc_info("makemkvcon", "disc:0")
    assert info["tcount"] == 7
    assert info["name"] == "Breaking Bad: Season 1: Disc 1"  # CINFO:2 preferred
    assert info["disc"][32] == "BREAKINGBADS1"
    assert info["titles"][0][9] == "0:58:06"
    # quoted value containing a comma must survive intact
    assert info["titles"][0][30] == "Breaking Bad: Season 1: Disc 1 - 7 chapter(s) , 12.5 GB"
    assert info["titles"][1][2] == "Pilot, Commentary"
    assert info["streams"][0][0][6] == "Mpeg2"


def test_disc_info_failure_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("nope")

    monkeypatch.setattr(disc.subprocess, "run", boom)
    assert disc.disc_info("makemkvcon", "disc:0") == {}


def test_list_titles_filters_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    out = (FIXTURES / "makemkv_info_output.txt").read_text(encoding="utf-8")
    _mock_run(monkeypatch, out)
    titles = disc.list_titles("makemkvcon", "disc:0", min_seconds=600)
    # title 0 = 58:06 (3486 s) kept; title 1 = 4:12 (252 s) and title 2 = 1:30 dropped
    assert [t.index for t in titles] == [0]
    t = titles[0]
    assert t.duration_s == 58 * 60 + 6
    assert t.chapters == 7
    assert t.size_bytes == 13472686080
    assert t.name == "Breaking Bad: Season 1: Disc 1"


def test_list_titles_low_threshold_keeps_short_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    out = (FIXTURES / "makemkv_info_output.txt").read_text(encoding="utf-8")
    _mock_run(monkeypatch, out)
    titles = disc.list_titles("makemkvcon", "disc:0", min_seconds=60)
    assert [t.index for t in titles] == [0, 1, 2]
    trailer = titles[2]
    assert trailer.duration_s == 90
    assert trailer.chapters == 1
    assert trailer.size_bytes is None  # TINFO:2 has no id-11 line in fixture


def test_list_titles_failure_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("nope")

    monkeypatch.setattr(disc.subprocess, "run", boom)
    assert disc.list_titles("makemkvcon", "disc:0", 600) == []


def test_parse_duration() -> None:
    assert disc._parse_duration("0:58:06") == 3486
    assert disc._parse_duration("2:05:00") == 7500
    assert disc._parse_duration("garbage") == 0
    assert disc._parse_duration("12:34") == 0
    assert disc._parse_duration("") == 0
