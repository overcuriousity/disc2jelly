"""Tests for disc2jelly.app.handbrake (pure parser + wrapper with mocked Popen)."""

from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

from app import handbrake

FIXTURES = Path(__file__).parent / "fixtures"


# --- parse_progress ----------------------------------------------------------


def test_parse_progress_with_stats() -> None:
    p = handbrake.parse_progress(
        "Encoding: task 1 of 2, 68.13 % (59.39 fps, avg 65.74 fps, ETA 00h00m02s)"
    )
    assert p == {
        "task": "Encoding",
        "n": 1,
        "total": 2,
        "percent": 68.13,
        "fps": 59.39,
        "avg_fps": 65.74,
        "eta": "00h00m02s",
    }


def test_parse_progress_early_line_without_stats() -> None:
    p = handbrake.parse_progress("Encoding: task 1 of 2, 5.84 %")
    assert p is not None
    assert p["percent"] == 5.84
    assert p["fps"] is None and p["avg_fps"] is None and p["eta"] is None


def test_parse_progress_muxing() -> None:
    p = handbrake.parse_progress("Muxing: task 1 of 1, 100.00 %")
    assert p is not None
    assert p["task"] == "Muxing" and p["percent"] == 100.0


def test_parse_progress_rejects_other_lines() -> None:
    assert handbrake.parse_progress("Scanning title 1 of 1, preview 2, 20.00 %") is None
    assert handbrake.parse_progress("[18:41:57] libhb: scan thread found 1 valid title(s)") is None
    assert handbrake.parse_progress("Encode done!") is None
    assert handbrake.parse_progress("") is None


def test_parse_progress_embedded_in_buffer_chunk() -> None:
    # search (not match) so trailing junk on the same segment still parses
    p = handbrake.parse_progress("Encoding: task 1 of 1, 12.40 % (71.22 fps, avg 69.10 fps, ETA 00h11m42s)\x00")
    assert p is not None and p["eta"] == "00h11m42s"


# --- encode() with mocked Popen ----------------------------------------------


class FakePopen:
    def __init__(self, args, stdout_text: str, returncode: int = 0, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.stdout = io.StringIO(stdout_text)
        self._returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):  # noqa: ANN201
        return self._returncode if (self.terminated or self.killed) else None

    def wait(self, timeout=None):  # noqa: ANN201
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def _patch_popen(monkeypatch: pytest.MonkeyPatch, text: str, rc: int = 0) -> list[FakePopen]:
    made: list[FakePopen] = []

    def factory(args, **kwargs):  # noqa: ANN001, ANN202
        proc = FakePopen(args, stdout_text=text, returncode=rc, **kwargs)
        made.append(proc)
        return proc

    monkeypatch.setattr(handbrake.subprocess, "Popen", factory)
    return made


def _fixture_text() -> str:
    return (FIXTURES / "handbrake_encode_output.txt").read_text(encoding="utf-8")


def test_encode_success_newline_separated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    made = _patch_popen(monkeypatch, _fixture_text())
    src = tmp_path / "rip.mkv"
    src.write_bytes(b"in")
    dst = tmp_path / "out" / "Movie (2020).mkv"
    dst.parent.mkdir()
    dst.write_bytes(b"out")

    events: list[dict] = []
    result = handbrake.encode(
        "HandBrakeCLI", src, dst, "hevc", 22, events.append, threading.Event()
    )
    assert result == dst

    args = made[0].args
    assert args[0] == "HandBrakeCLI"
    assert "--encoder" in args and args[args.index("--encoder") + 1] == "x265"
    assert args[args.index("--quality") + 1] == "22"
    assert "--format" in args and args[args.index("--format") + 1] == "av_mkv"
    for flag in ("--all-audio", "--all-subtitles", "--markers"):
        assert flag in args
    assert made[0].kwargs["env"]["LC_ALL"] == "C"

    # scan phase produced indeterminate "Analyzing source" events
    assert any(
        e["percent"] is None and e["detail"] == "Analyzing source" for e in events
    )
    # encoding events carry percent; stats present only when the line had them
    enc = [e for e in events if e["detail"] == "Encoding"]
    assert any(abs(e["percent"] - 68.13) < 0.001 for e in enc)
    with_stats = [e for e in enc if "fps" in e]
    assert with_stats and with_stats[0]["fps"] == 71.22
    assert with_stats[0]["eta"] == "00h11m42s"
    without_stats = [e for e in enc if "fps" not in e]
    assert any(abs(e["percent"] - 5.84) < 0.001 for e in without_stats)
    # muxing -> Finalizing, >= 99%
    fin = [e for e in events if e["detail"] == "Finalizing"]
    assert fin and fin[-1]["percent"] >= 99.0
    assert events[-1]["status"] == "done" and events[-1]["percent"] == 100.0


def test_encode_success_carriage_return_separated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # tty mode: progress updates separated by \r, no \n
    text = (
        "Scanning title 1 of 1, preview 10, 100.00 %\r"
        "Encoding: task 1 of 1, 0.00 %\r"
        "Encoding: task 1 of 1, 5.84 %\r"
        "Encoding: task 1 of 1, 68.13 % (59.39 fps, avg 65.74 fps, ETA 00h00m02s)\r"
        "Muxing: task 1 of 1, 100.00 %\r"
    )
    _patch_popen(monkeypatch, text)
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    dst = tmp_path / "out.mkv"
    dst.write_bytes(b"y")
    events: list[dict] = []
    handbrake.encode("HandBrakeCLI", src, dst, "h264", 20, events.append, threading.Event())
    enc = [e for e in events if e["detail"] == "Encoding"]
    assert [e["percent"] for e in enc] == [0.0, 5.84, 68.13]
    assert any(e["detail"] == "Finalizing" and e["percent"] >= 99.0 for e in events)


def test_encode_h264_profile_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    made = _patch_popen(monkeypatch, "Encoding: task 1 of 1, 100.00 %\n")
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    dst = tmp_path / "out.mkv"
    dst.write_bytes(b"y")
    handbrake.encode("HandBrakeCLI", src, dst, "h264", 20, lambda e: None, threading.Event())
    args = made[0].args
    assert args[args.index("--encoder") + 1] == "x264"
    assert args[args.index("--quality") + 1] == "20"


def test_encode_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_popen(monkeypatch, "Encoding: task 1 of 1, 10.00 %\n", rc=1)
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    with pytest.raises(handbrake.EncodeError, match="exited with code 1"):
        handbrake.encode(
            "HandBrakeCLI", src, tmp_path / "out.mkv", "hevc", 22,
            lambda e: None, threading.Event(),
        )


def test_encode_missing_output_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_popen(monkeypatch, "Encode done!\n", rc=0)
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    with pytest.raises(handbrake.EncodeError, match="output file is missing"):
        handbrake.encode(
            "HandBrakeCLI", src, tmp_path / "out.mkv", "hevc", 22,
            lambda e: None, threading.Event(),
        )


def test_encode_cancel_terminates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    made = _patch_popen(monkeypatch, _fixture_text())
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(handbrake.EncodeError, match="cancelled"):
        handbrake.encode(
            "HandBrakeCLI", src, tmp_path / "out.mkv", "hevc", 22,
            lambda e: None, cancel,
        )
    assert made[0].terminated or made[0].killed


def test_encode_missing_binary_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("file not found")

    monkeypatch.setattr(handbrake.subprocess, "Popen", boom)
    with pytest.raises(handbrake.EncodeError, match="failed to start"):
        handbrake.encode(
            "/nope", tmp_path / "in.mkv", tmp_path / "out.mkv", "hevc", 22,
            lambda e: None, threading.Event(),
        )


def test_event_schema_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_popen(monkeypatch, _fixture_text())
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    dst = tmp_path / "out.mkv"
    dst.write_bytes(b"y")
    events: list[dict] = []
    handbrake.encode("HandBrakeCLI", src, dst, "hevc", 22, events.append, threading.Event())
    allowed = {"job_id", "stage", "status", "percent", "detail", "fps", "eta", "log", "ts"}
    for e in events:
        assert set(e) <= allowed
        assert e["stage"] == "ENCODE"
        assert {"stage", "status", "percent", "detail", "ts"} <= set(e)
