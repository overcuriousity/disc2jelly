"""MakeMKV (makemkvcon) robot-mode wrapper + line parser.

Verified formats (see info.md §1):
  PRGT:code,id,"name"            total progress bar title
  PRGC:code,id,"name"            current sub-task caption
  PRGV:current,total,max         max is CONSTANT 65536; field 1 = overall progress
  MSG:code,flags,count,"msg","fmt",params...   match by code, never text
  MSG:5036 = full success, MSG:5037 = partial (N saved / M failed)

Success requires: exit code 0 AND MSG:5036 seen AND output .mkv exists.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

PRGV_MAX = 65536
MSG_COPY_COMPLETE_OK = 5036
MSG_COPY_COMPLETE_PARTIAL = 5037

_TOKEN_RE = re.compile(r"^([A-Z]+):(.*)$")


class RipError(Exception):
    """Raised when a MakeMKV rip fails or is cancelled."""


def split_fields(body: str) -> list[str]:
    """Split a robot-mode field list on commas, respecting double quotes.

    Values may contain commas inside quotes; quotes are stripped.
    """
    out: list[str] = []
    cur: list[str] = []
    in_quotes = False
    for ch in body:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _to_int(s: str, default: int = 0) -> int:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return default


def parse_robot_line(line: str) -> dict | None:
    """Parse one makemkvcon robot-mode line.

    Returns None for blank/unrecognized lines. Known tokens get typed
    fields; unknown tokens return {"token": ..., "fields": [...]}.
    """
    text = line.strip("\r\n")
    if not text:
        return None
    m = _TOKEN_RE.match(text)
    if not m:
        return None
    token, body = m.group(1), m.group(2)
    f = split_fields(body)

    if token == "PRGV" and len(f) >= 3:
        return {
            "token": "PRGV",
            "current": _to_int(f[0]),
            "total": _to_int(f[1]),
            "max": _to_int(f[2], PRGV_MAX),
            "raw": text,
        }
    if token in ("PRGT", "PRGC") and len(f) >= 3:
        return {
            "token": token,
            "code": _to_int(f[0]),
            "id": _to_int(f[1]),
            "name": f[2],
            "raw": text,
        }
    if token == "MSG" and len(f) >= 3:
        return {
            "token": "MSG",
            "code": _to_int(f[0]),
            "flags": _to_int(f[1]),
            "count": _to_int(f[2]),
            "message": f[3] if len(f) > 3 else "",
            "format": f[4] if len(f) > 4 else "",
            "params": f[5:] if len(f) > 5 else [],
            "raw": text,
        }
    if token == "DRV" and len(f) >= 6:
        return {
            "token": "DRV",
            "index": _to_int(f[0]),
            "status": _to_int(f[1]),
            "enabled": _to_int(f[2]),
            "flags": _to_int(f[3]),
            "drive_name": f[4],
            "disc_name": f[5],
            "device": f[6] if len(f) > 6 and f[6] else None,
            "raw": text,
        }
    if token == "TCOUNT" and len(f) >= 1:
        return {"token": "TCOUNT", "count": _to_int(f[0]), "raw": text}
    if token == "CINFO" and len(f) >= 3:
        return {
            "token": "CINFO",
            "id": _to_int(f[0]),
            "code": _to_int(f[1]),
            "value": f[2],
            "raw": text,
        }
    if token == "TINFO" and len(f) >= 4:
        return {
            "token": "TINFO",
            "title": _to_int(f[0]),
            "id": _to_int(f[1]),
            "code": _to_int(f[2]),
            "value": f[3],
            "raw": text,
        }
    if token == "SINFO" and len(f) >= 5:
        return {
            "token": "SINFO",
            "title": _to_int(f[0]),
            "stream": _to_int(f[1]),
            "id": _to_int(f[2]),
            "code": _to_int(f[3]),
            "value": f[4],
            "raw": text,
        }
    return {"token": token, "fields": f, "raw": text}


def _clean_env() -> dict:
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    return env


def _terminate(proc: subprocess.Popen, grace_s: float = 5.0) -> None:
    """Cross-platform terminate with kill fallback (no fcntl/signals)."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            pass


def rip(
    makemkv_path: str,
    drive_id: str,
    title: str | int,
    out_dir: Path,
    emit: Callable[[dict], None],
    cancel: threading.Event,
    minlength: int = 120,
) -> Path:
    """Rip one title from a disc to MKV. Returns path of produced .mkv.

    Emits stage "RIP" events (percent from PRGV field 1, detail from the
    latest PRGC caption, raw line in "log"). Raises RipError on failure
    or cancel.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    args = [
        makemkv_path,
        "-r",
        "--progress=-same",
        f"--minlength={minlength}",
        "mkv",
        drive_id,
        str(title),
        str(out_dir),
    ]
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_clean_env(),
        )
    except OSError as exc:
        raise RipError(f"failed to start makemkvcon: {exc}") from exc

    lines: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for ln in proc.stdout:
                lines.put(ln)
        finally:
            lines.put(None)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    def _event(status: str, percent: float | None, detail: str, log: str | None = None) -> dict:
        ev: dict = {
            "stage": "RIP",
            "status": status,
            "percent": percent,
            "detail": detail,
            "ts": time.time(),
        }
        if log is not None:
            ev["log"] = log
        return ev

    saw_success = False
    fail_msg: str | None = None
    caption = "Ripping"
    last_percent: float | None = 0.0
    cancelled = False

    emit(_event("running", 0.0, caption))
    while True:
        if cancel.is_set():
            cancelled = True
            _terminate(proc, grace_s=5.0)
            break
        try:
            ln = lines.get(timeout=0.1)
        except queue.Empty:
            if proc.poll() is not None and not reader.is_alive():
                break
            continue
        if ln is None:
            break
        text = ln.strip("\r\n")
        parsed = parse_robot_line(text)
        if parsed is None:
            continue
        tok = parsed["token"]
        if tok == "PRGC":
            caption = parsed["name"] or caption
            emit(_event("running", last_percent, caption, log=text))
        elif tok == "PRGT":
            emit(_event("running", last_percent, caption, log=text))
        elif tok == "PRGV":
            maxv = parsed["max"] or PRGV_MAX
            last_percent = round(parsed["total"] / maxv * 100.0, 2)
            emit(_event("running", last_percent, caption, log=text))
        elif tok == "MSG":
            if parsed["code"] == MSG_COPY_COMPLETE_OK:
                saw_success = True
            elif parsed["code"] == MSG_COPY_COMPLETE_PARTIAL:
                params = parsed["params"]
                saved = params[0] if len(params) > 0 else "0"
                failed = params[1] if len(params) > 1 else "?"
                fail_msg = f"copy incomplete: {saved} titles saved, {failed} failed"
            emit(_event("running", last_percent, caption, log=text))
    reader.join(timeout=5.0)
    rc = proc.wait()

    if cancelled:
        emit(_event("cancelled", None, "Rip cancelled"))
        raise RipError("rip cancelled")
    if rc != 0:
        emit(_event("error", None, f"makemkvcon exited with code {rc}"))
        raise RipError(f"makemkvcon exited with code {rc}")
    if fail_msg is not None:
        emit(_event("error", None, fail_msg))
        raise RipError(fail_msg)
    if not saw_success:
        emit(_event("error", None, "makemkvcon did not report copy complete"))
        raise RipError("no MSG:5036 copy-complete message seen")

    mkvs = sorted(out_dir.glob("*.mkv"), key=lambda p: p.stat().st_size, reverse=True)
    if not mkvs:
        emit(_event("error", None, "no .mkv produced"))
        raise RipError("rip reported success but no .mkv file was produced")
    emit(_event("done", 100.0, f"Saved {mkvs[0].name}"))
    return mkvs[0]
