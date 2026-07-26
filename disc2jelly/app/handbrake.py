"""HandBrakeCLI wrapper + progress parser.

Verified formats (info.md §2):
  Encoding: task 1 of 2, 5.84 %
  Encoding: task 1 of 2, 68.13 % (59.39 fps, avg 65.74 fps, ETA 00h00m02s)
  Muxing:   task 2 of 2, 100.00 %          (treat as finalizing, >= 99%)
  Scanning title 1 of 1, preview 2, 20.00 %  (scan phase, indeterminate)

Progress updates arrive separated by \r on a tty and by \n when piped —
split the stdout buffer on BOTH. Stats suffix (fps/avg/ETA) is optional.
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

PROGRESS_RE = re.compile(
    r"(Encoding|Muxing): task (\d+) of (\d+), (\d+\.?\d*) %"
    r"( \(([\d.]+) fps, avg ([\d.]+) fps, ETA ([\dhms]+)\))?"
)
SCAN_RE = re.compile(r"Scanning title \d+ of \d+")


class EncodeError(Exception):
    """Raised when a HandBrake encode fails or is cancelled."""


def parse_progress(line: str) -> dict | None:
    """Parse one HandBrake progress line; None if it isn't one.

    Returns {"task": "Encoding"|"Muxing", "n", "total", "percent",
             "fps", "avg_fps", "eta"} with stats keys None when absent.
    """
    m = PROGRESS_RE.search(line)
    if not m:
        return None
    return {
        "task": m.group(1),
        "n": int(m.group(2)),
        "total": int(m.group(3)),
        "percent": float(m.group(4)),
        "fps": float(m.group(6)) if m.group(6) is not None else None,
        "avg_fps": float(m.group(7)) if m.group(7) is not None else None,
        "eta": m.group(8) if m.group(8) is not None else None,
    }


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


def encode(
    handbrake_path: str,
    src: Path,
    dst: Path,
    profile: str,
    quality: int,
    emit: Callable[[dict], None],
    cancel: threading.Event,
) -> Path:
    """Encode src → dst (MKV). Returns dst on success.

    profile: "hevc" (x265) | "h264" (x264). Emits stage "ENCODE" events:
    indeterminate "Analyzing source" during scan, then percent/fps/eta
    from progress lines, "Finalizing" for Muxing. Raises EncodeError on
    non-zero exit, missing output, or cancel.
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    encoder = "x264" if profile == "h264" else "x265"
    args = [
        handbrake_path,
        "-i",
        str(src),
        "-o",
        str(dst),
        "--format",
        "av_mkv",
        "--encoder",
        encoder,
        "--quality",
        str(quality),
        "--all-audio",
        "--all-subtitles",
        "--markers",
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
        raise EncodeError(f"failed to start HandBrakeCLI: {exc}") from exc

    segments: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        """Push stdout segments split on BOTH \\r and \\n."""
        buf = ""
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(1024)
                if not chunk:
                    break
                buf += chunk
                # Split on \r and \n; keep trailing partial segment buffered.
                parts = re.split(r"[\r\n]", buf)
                buf = parts.pop()
                for part in parts:
                    if part:
                        segments.put(part)
        finally:
            if buf.strip():
                segments.put(buf.strip())
            segments.put(None)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    def _event(
        status: str,
        percent: float | None,
        detail: str,
        log: str | None = None,
        fps: float | None = None,
        eta: str | None = None,
    ) -> dict:
        ev: dict = {
            "stage": "ENCODE",
            "status": status,
            "percent": percent,
            "detail": detail,
            "ts": time.time(),
        }
        if log is not None:
            ev["log"] = log
        if fps is not None:
            ev["fps"] = fps
        if eta is not None:
            ev["eta"] = eta
        return ev

    cancelled = False
    emit(_event("running", None, "Analyzing source"))
    while True:
        if cancel.is_set():
            cancelled = True
            _terminate(proc, grace_s=5.0)
            break
        try:
            seg = segments.get(timeout=0.1)
        except queue.Empty:
            if proc.poll() is not None and not reader.is_alive():
                break
            continue
        if seg is None:
            break
        prog = parse_progress(seg)
        if prog is not None:
            if prog["task"] == "Muxing":
                percent = max(99.0, prog["percent"])
                emit(_event("running", percent, "Finalizing", log=seg))
            else:
                emit(
                    _event(
                        "running",
                        prog["percent"],
                        "Encoding",
                        log=seg,
                        fps=prog["fps"],
                        eta=prog["eta"],
                    )
                )
        elif SCAN_RE.search(seg):
            emit(_event("running", None, "Analyzing source", log=seg))
    reader.join(timeout=5.0)
    rc = proc.wait()

    if cancelled:
        emit(_event("cancelled", None, "Encode cancelled"))
        raise EncodeError("encode cancelled")
    if rc != 0:
        emit(_event("error", None, f"HandBrakeCLI exited with code {rc}"))
        raise EncodeError(f"HandBrakeCLI exited with code {rc}")
    if not dst.is_file():
        emit(_event("error", None, "no output file produced"))
        raise EncodeError("encode exited 0 but output file is missing")
    emit(_event("done", 100.0, f"Encoded {dst.name}"))
    return dst
