"""Tests for disc2jelly.app.scan (HandBrakeCLI --scan --json title enumeration)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import scan

FIXTURES = Path(__file__).parent / "fixtures"
SCAN_OUTPUT = (FIXTURES / "handbrake_scan_output.txt").read_text(encoding="utf-8")


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _mock_run(monkeypatch: pytest.MonkeyPatch, stdout: str) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):  # noqa: ANN001, ANN202
        calls.append(list(args))
        assert kwargs.get("timeout") is not None, "every call must have a timeout"
        return _Completed(stdout)

    monkeypatch.setattr(scan.subprocess, "run", fake_run)
    return calls


# --- command construction ---------------------------------------------------


def test_scan_invokes_handbrake_with_json_scan_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _mock_run(monkeypatch, SCAN_OUTPUT)
    scan.scan_titles("/usr/bin/HandBrakeCLI", "/dev/sr0", min_seconds=0)
    cmd = calls[0]
    assert cmd[0] == "/usr/bin/HandBrakeCLI"
    assert cmd[cmd.index("-i") + 1] == "/dev/sr0"
    assert cmd[cmd.index("--title") + 1] == "0"
    assert "--scan" in cmd
    assert "--json" in cmd


# --- parsing ----------------------------------------------------------------


def test_parses_all_titles_from_scan_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, SCAN_OUTPUT)
    titles = scan.scan_titles("HandBrakeCLI", "/dev/sr0", min_seconds=0)
    assert [t.index for t in titles] == [1, 2, 3, 4]


def test_duration_is_flattened_to_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, SCAN_OUTPUT)
    titles = scan.scan_titles("HandBrakeCLI", "/dev/sr0", min_seconds=0)
    assert titles[0].duration_s == 58 * 60 + 6
    assert titles[1].duration_s == 47 * 60 + 12
    assert titles[3].duration_s == 90


def test_chapter_count_comes_from_chapterlist_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_run(monkeypatch, SCAN_OUTPUT)
    titles = scan.scan_titles("HandBrakeCLI", "/dev/sr0", min_seconds=0)
    assert [t.chapters for t in titles] == [7, 5, 7, 1]


def test_size_bytes_is_none_handbrake_does_not_report_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_run(monkeypatch, SCAN_OUTPUT)
    titles = scan.scan_titles("HandBrakeCLI", "/dev/sr0", min_seconds=0)
    assert all(t.size_bytes is None for t in titles)


def test_title_name_falls_back_when_json_name_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_run(monkeypatch, SCAN_OUTPUT.replace('"Name": "BREAKINGBADS1"', '"Name": ""'))
    titles = scan.scan_titles("HandBrakeCLI", "/dev/sr0", min_seconds=0)
    assert titles[0].name == "Title 1"


# --- filtering --------------------------------------------------------------


def test_min_seconds_drops_short_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, SCAN_OUTPUT)
    titles = scan.scan_titles("HandBrakeCLI", "/dev/sr0", min_seconds=600)
    assert [t.index for t in titles] == [1, 2, 3]


# --- duplicate marking ------------------------------------------------------


def test_duplicate_titles_are_marked_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Title 3 repeats title 1. Dropping it would risk losing a real episode,
    so it is flagged and left in the list for the UI to grey out."""
    _mock_run(monkeypatch, SCAN_OUTPUT)
    titles = scan.scan_titles("HandBrakeCLI", "/dev/sr0", min_seconds=0)
    by_index = {t.index: t for t in titles}
    assert by_index[1].duplicate_of is None
    assert by_index[2].duplicate_of is None
    assert by_index[3].duplicate_of == 1
    assert by_index[4].duplicate_of is None


# --- main feature -----------------------------------------------------------


def test_main_feature_picks_the_longest_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_run(monkeypatch, SCAN_OUTPUT)
    titles = scan.scan_titles("HandBrakeCLI", "/dev/sr0", min_seconds=0)
    assert scan.main_feature(titles).index == 1


def test_main_feature_of_empty_list_is_none() -> None:
    assert scan.main_feature([]) is None


def test_main_feature_ignores_duplicates() -> None:
    titles = [
        scan.Title(1, "a", 100, 2, None, None),
        scan.Title(2, "b", 900, 5, None, None),
        scan.Title(3, "c", 900, 5, None, 2),
    ]
    assert scan.main_feature(titles).index == 2


# --- failure modes ----------------------------------------------------------


def test_missing_binary_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("no such binary")

    monkeypatch.setattr(scan.subprocess, "run", boom)
    assert scan.scan_titles("/missing/HandBrakeCLI", "/dev/sr0", 600) == []


def test_timeout_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise subprocess.TimeoutExpired(cmd="HandBrakeCLI", timeout=120)

    monkeypatch.setattr(scan.subprocess, "run", slow)
    assert scan.scan_titles("HandBrakeCLI", "/dev/sr0", 600) == []


def test_output_without_json_marker_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_run(monkeypatch, "Opening /dev/sr0...\nlibhb: scan thread found 0 valid title(s)\n")
    assert scan.scan_titles("HandBrakeCLI", "/dev/sr0", 600) == []


def test_malformed_json_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, "JSON Title Set: {not valid json\n")
    assert scan.scan_titles("HandBrakeCLI", "/dev/sr0", 600) == []


def test_trailing_log_lines_after_json_do_not_break_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The JSON block is followed by more libhb log output — see the fixture."""
    _mock_run(monkeypatch, SCAN_OUTPUT + "\n[12:04:25] hb_close: joining threads\n")
    titles = scan.scan_titles("HandBrakeCLI", "/dev/sr0", min_seconds=0)
    assert len(titles) == 4
