"""Tests for disc2jelly.app.makemkv (pure parser + wrapper with mocked Popen)."""

from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

from app import makemkv

FIXTURES = Path(__file__).parent / "fixtures"


# --- parse_robot_line / split_fields ----------------------------------------


def test_split_fields_respects_quotes() -> None:
    assert makemkv.split_fields('0,30,0,"a, b , c"') == ["0", "30", "0", "a, b , c"]
    assert makemkv.split_fields('1,2,""') == ["1", "2", ""]
    assert makemkv.split_fields("0,0,65536") == ["0", "0", "65536"]


def test_parse_prgv_uses_field1_overall() -> None:
    p = makemkv.parse_robot_line("PRGV:857,858,65536")
    assert p == {
        "token": "PRGV",
        "current": 857,
        "total": 858,
        "max": 65536,
        "raw": "PRGV:857,858,65536",
    }


def test_parse_prgv_non_monotonic_field0() -> None:
    # field 0 resets as sub-tasks change; parser must just report values
    seq = ["PRGV:0,0,65536", "PRGV:730,0,65536", "PRGV:0,3276,65536"]
    vals = [makemkv.parse_robot_line(s) for s in seq]
    assert [v["current"] for v in vals] == [0, 730, 0]
    assert [v["total"] for v in vals] == [0, 0, 3276]
    assert all(v["max"] == 65536 for v in vals)


def test_parse_msg_5036_success() -> None:
    p = makemkv.parse_robot_line(
        'MSG:5036,260,1,"Copy complete. 1 titles saved.","Copy complete. %1 titles saved.","1"'
    )
    assert p["token"] == "MSG"
    assert p["code"] == 5036
    assert p["params"] == ["1"]


def test_parse_msg_5037_partial() -> None:
    p = makemkv.parse_robot_line(
        'MSG:5037,516,2,"Copy complete. 0 titles saved, 1 failed.",'
        '"Copy complete. %1 titles saved, %2 failed.","0","1"'
    )
    assert p["code"] == 5037
    assert p["flags"] == 516
    assert p["params"] == ["0", "1"]


def test_parse_msg_with_commas_inside_quotes() -> None:
    p = makemkv.parse_robot_line(
        'MSG:3025,0,3,"Title #00384.m2ts has length of 5 seconds which is less than '
        'minimum title length of 120 seconds and was therefore skipped",'
        '"Title #%1 has length of %2 seconds which is less than minimum title length '
        'of %3 seconds and was therefore skipped","00384.m2ts","5","120"'
    )
    assert p["code"] == 3025
    assert p["params"] == ["00384.m2ts", "5", "120"]


def test_parse_tinfo_quoted_comma_value() -> None:
    p = makemkv.parse_robot_line(
        'TINFO:0,30,0,"Breaking Bad: Season 1: Disc 1 - 7 chapter(s) , 12.5 GB"'
    )
    assert p["token"] == "TINFO"
    assert p["title"] == 0
    assert p["id"] == 30
    assert p["value"] == "Breaking Bad: Season 1: Disc 1 - 7 chapter(s) , 12.5 GB"


def test_parse_drv_and_cinfo_and_sinfo() -> None:
    drv = makemkv.parse_robot_line(
        'DRV:1,2,999,1,"BD-RE HL-DT-ST BD-RE  WH16NS60 1.03 M63IBOA5100","BUILD","/dev/sr0"'
    )
    assert drv["index"] == 1 and drv["status"] == 2 and drv["flags"] == 1
    assert drv["disc_name"] == "BUILD" and drv["device"] == "/dev/sr0"
    cinfo = makemkv.parse_robot_line('CINFO:2,0,"Breaking Bad: Season 1: Disc 1"')
    assert cinfo["id"] == 2 and cinfo["value"] == "Breaking Bad: Season 1: Disc 1"
    sinfo = makemkv.parse_robot_line('SINFO:3,0,21,0,"25"')
    assert sinfo["title"] == 3 and sinfo["stream"] == 0 and sinfo["id"] == 21


def test_parse_prgc_prgt() -> None:
    p = makemkv.parse_robot_line('PRGC:5015,1,"Saving 1 titles into directory /tmp/work"')
    assert p["token"] == "PRGC" and p["name"].startswith("Saving 1 titles")
    p = makemkv.parse_robot_line('PRGT:5018,0,"Saving all titles to MKV files"')
    assert p["token"] == "PRGT"


def test_parse_garbage_returns_none() -> None:
    assert makemkv.parse_robot_line("") is None
    assert makemkv.parse_robot_line("   \n") is None
    assert makemkv.parse_robot_line("no colon here") is None


# --- rip() with mocked Popen -------------------------------------------------


class FakePopen:
    """Minimal Popen stand-in: streams fixture text, fixed exit code."""

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
        if self.terminated or self.killed:
            return self._returncode
        # drain not needed; StringIO reader exhausts itself
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

    monkeypatch.setattr(makemkv.subprocess, "Popen", factory)
    return made


def test_rip_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text = (FIXTURES / "makemkv_rip_output.txt").read_text(encoding="utf-8")
    made = _patch_popen(monkeypatch, text)
    big = tmp_path / "title_t00.mkv"
    big.write_bytes(b"\x00" * 4096)
    small = tmp_path / "extra_t01.mkv"
    small.write_bytes(b"\x00" * 10)

    events: list[dict] = []
    result = makemkv.rip(
        "makemkvcon", "disc:0", 0, tmp_path, events.append, threading.Event()
    )
    assert result == big  # largest .mkv wins

    args = made[0].args
    assert args[:2] == ["makemkvcon", "-r"]
    assert "--progress=-same" in args
    assert any(a.startswith("--minlength=") for a in args)
    assert args[-3:] == ["disc:0", "0", str(tmp_path)]
    assert made[0].kwargs["env"]["LC_ALL"] == "C"

    # progress events: stage RIP, percent from PRGV field 1
    prgv_events = [e for e in events if e.get("log", "").startswith("PRGV")]
    assert prgv_events, "expected PRGV progress events"
    assert all(e["stage"] == "RIP" and e["status"] == "running" for e in prgv_events)
    # PRGV:857,858,65536 -> 858/65536*100 = 1.31%
    assert any(abs(e["percent"] - 1.31) < 0.01 for e in prgv_events)
    # final PRGV total=65536 -> 100%
    assert prgv_events[-1]["percent"] == 100.0
    # caption comes from PRGC
    assert any("Saving 1 titles" in (e["detail"] or "") for e in events)
    assert events[-1]["status"] == "done"


def test_rip_partial_failure_msg5037(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text = (FIXTURES / "makemkv_rip_partial_output.txt").read_text(encoding="utf-8")
    _patch_popen(monkeypatch, text, rc=0)  # exit 0 is NOT sufficient
    (tmp_path / "stray.mkv").write_bytes(b"x")
    with pytest.raises(makemkv.RipError, match="copy incomplete"):
        makemkv.rip("makemkvcon", "disc:0", "all", tmp_path, lambda e: None, threading.Event())


def test_rip_no_success_message_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text = 'PRGV:0,65536,65536\nMSG:5005,0,0,"1 titles saved","%1 titles saved","1"\n'
    _patch_popen(monkeypatch, text, rc=0)
    (tmp_path / "t.mkv").write_bytes(b"x")
    with pytest.raises(makemkv.RipError, match="5036"):
        makemkv.rip("makemkvcon", "disc:0", 0, tmp_path, lambda e: None, threading.Event())


def test_rip_no_output_file_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text = (FIXTURES / "makemkv_rip_output.txt").read_text(encoding="utf-8")
    _patch_popen(monkeypatch, text, rc=0)
    with pytest.raises(makemkv.RipError, match="no .mkv"):
        makemkv.rip("makemkvcon", "disc:0", 0, tmp_path, lambda e: None, threading.Event())


def test_rip_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text = 'MSG:5010,0,0,"Failed to open disc","Failed to open disc"\n'
    _patch_popen(monkeypatch, text, rc=1)
    with pytest.raises(makemkv.RipError, match="exited with code 1"):
        makemkv.rip("makemkvcon", "disc:0", 0, tmp_path, lambda e: None, threading.Event())


def test_rip_cancel_terminates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text = (FIXTURES / "makemkv_rip_output.txt").read_text(encoding="utf-8")
    made = _patch_popen(monkeypatch, text)
    cancel = threading.Event()
    cancel.set()  # already cancelled before start
    with pytest.raises(makemkv.RipError, match="cancelled"):
        makemkv.rip("makemkvcon", "disc:0", 0, tmp_path, lambda e: None, cancel)
    assert made[0].terminated or made[0].killed


def test_rip_missing_binary_raises_riperror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("file not found")

    monkeypatch.setattr(makemkv.subprocess, "Popen", boom)
    with pytest.raises(makemkv.RipError, match="failed to start"):
        makemkv.rip("/nope", "disc:0", 0, tmp_path, lambda e: None, threading.Event())


def test_event_schema_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text = (FIXTURES / "makemkv_rip_output.txt").read_text(encoding="utf-8")
    _patch_popen(monkeypatch, text)
    (tmp_path / "t.mkv").write_bytes(b"x")
    events: list[dict] = []
    makemkv.rip("makemkvcon", "disc:0", 0, tmp_path, events.append, threading.Event())
    allowed = {"job_id", "stage", "status", "percent", "detail", "fps", "eta", "log", "ts"}
    for e in events:
        assert set(e) <= allowed
        assert {"stage", "status", "percent", "detail", "ts"} <= set(e)
        assert isinstance(e["ts"], float)
