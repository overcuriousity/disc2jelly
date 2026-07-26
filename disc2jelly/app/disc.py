"""Drive + disc detection via makemkvcon robot mode.

Verified formats (info.md §1.1/§1.2):
  DRV:index,status,999,flags,"drive name","disc label"[,"/dev/srN"]
    status: 2 = disc ready, 0 = empty, 1 = tray open, 3 = loading, 256 = absent
    flags:  1 = DVD, 12/28 = Blu-ray
  TCOUNT:0 + MSG:5010 at end of `info disc:9999` is NORMAL, not an error.
  TINFO:x,2  = title name; TINFO:x,8 = chapters; TINFO:x,9 = duration
    (H:MM:SS text); TINFO:x,11 = size in bytes (quoted).
  Disc name: prefer CINFO:2, fallback CINFO:32 (volume label) / DRV field 6.

Every subprocess call has a timeout; failures return []/{} rather than
raising (the caller emits the error event).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from .makemkv import parse_robot_line

SCAN_TIMEOUT_S = 30
INFO_TIMEOUT_S = 120

# DRV status codes
STATUS_EMPTY = 0
STATUS_TRAY_OPEN = 1
STATUS_READY = 2
STATUS_LOADING = 3
STATUS_ABSENT = 256

# Attribute ids (AP_ItemAttributeId)
ATTR_NAME = 2
ATTR_CHAPTERS = 8
ATTR_DURATION = 9
ATTR_SIZE_BYTES = 11
ATTR_VOLUME_NAME = 32


@dataclass
class Drive:
    id: str       # "disc:0" style, for makemkv commands
    label: str    # disc label if inserted, else drive name
    device: str   # e.g. "/dev/sr0" (may be empty on Windows)


@dataclass
class Title:
    index: int
    name: str
    duration_s: int
    chapters: int
    size_bytes: int | None


def _run_robot(makemkv_path: str, args: list[str], timeout: int) -> list[dict]:
    """Run makemkvcon robot mode; return parsed lines ([] on any failure)."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        proc = subprocess.run(
            [makemkv_path, "-r", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    out: list[dict] = []
    for line in proc.stdout.splitlines():
        parsed = parse_robot_line(line)
        if parsed is not None:
            out.append(parsed)
    return out


def list_drives(makemkv_path: str) -> list[Drive]:
    """Enumerate optical drives via `makemkvcon -r --cache=1 info disc:9999`."""
    lines = _run_robot(makemkv_path, ["--cache=1", "info", "disc:9999"], SCAN_TIMEOUT_S)
    drives: list[Drive] = []
    for p in lines:
        if p["token"] != "DRV":
            continue
        if p["status"] == STATUS_ABSENT:
            continue
        label = p["disc_name"] or p["drive_name"] or f"Drive {p['index']}"
        drives.append(
            Drive(id=f"disc:{p['index']}", label=label, device=p["device"] or "")
        )
    return drives


def disc_info(makemkv_path: str, drive_id: str) -> dict:
    """Raw parsed CINFO/TINFO/SINFO tree for `info <drive_id>`.

    Returns {} on failure. Structure:
      {"tcount": int, "name": str,
       "disc":   {attr_id: value},
       "titles": {title_idx: {attr_id: value}},
       "streams": {title_idx: {stream_idx: {attr_id: value}}}}
    """
    lines = _run_robot(makemkv_path, ["info", drive_id], INFO_TIMEOUT_S)
    if not lines:
        return {}
    info: dict = {"tcount": 0, "name": "", "disc": {}, "titles": {}, "streams": {}}
    for p in lines:
        tok = p["token"]
        if tok == "TCOUNT":
            info["tcount"] = p["count"]
        elif tok == "CINFO":
            info["disc"][p["id"]] = p["value"]
        elif tok == "TINFO":
            info["titles"].setdefault(p["title"], {})[p["id"]] = p["value"]
        elif tok == "SINFO":
            info["streams"].setdefault(p["title"], {}).setdefault(p["stream"], {})[
                p["id"]
            ] = p["value"]
    disc = info["disc"]
    info["name"] = disc.get(ATTR_NAME) or disc.get(ATTR_VOLUME_NAME) or ""
    return info


def _parse_duration(text: str) -> int:
    """Parse makemkvcon H:MM:SS duration text to seconds (0 on garbage)."""
    parts = text.strip().split(":")
    if len(parts) != 3:
        return 0
    try:
        h, m, s = (int(x) for x in parts)
    except ValueError:
        return 0
    return h * 3600 + m * 60 + s


def _to_int(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def list_titles(makemkv_path: str, drive_id: str, min_seconds: int) -> list[Title]:
    """List titles on a disc, filtered to duration >= min_seconds."""
    info = disc_info(makemkv_path, drive_id)
    if not info:
        return []
    titles: list[Title] = []
    for idx in sorted(info["titles"]):
        attrs = info["titles"][idx]
        duration = _parse_duration(attrs.get(ATTR_DURATION, ""))
        if duration < min_seconds:
            continue
        name = attrs.get(ATTR_NAME) or f"Title {idx}"
        chapters = _to_int(attrs.get(ATTR_CHAPTERS)) or 0
        titles.append(
            Title(
                index=idx,
                name=name,
                duration_s=duration,
                chapters=chapters,
                size_bytes=_to_int(attrs.get(ATTR_SIZE_BYTES)),
            )
        )
    return titles
