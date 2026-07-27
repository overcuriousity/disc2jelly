"""Title enumeration via `HandBrakeCLI -i <device> --title 0 --scan --json`.

Replaces the MakeMKV ``TINFO`` parsing that used to live in disc.py. info.md
§2.2 recommends exactly this call for structured title data.

HandBrake emits libhb log lines around a single ``JSON Title Set: {...}`` block,
so the parser locates the marker and decodes from the following brace, ignoring
whatever trails it.

Two differences from the MakeMKV data we replaced:
  - Title indices are 1-based (MakeMKV's were 0-based).
  - There is no per-title byte size, so ``size_bytes`` is always None.

Failures return [] rather than raising, matching the old disc.py contract.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

SCAN_TIMEOUT_S = 300  # a slow DVD drive spins up and reads previews for minutes
JSON_MARKER = "JSON Title Set:"


@dataclass
class Title:
    index: int
    name: str
    duration_s: int
    chapters: int
    size_bytes: int | None
    # Index of an earlier title with identical duration and chapter count.
    # DVDs routinely expose the main feature more than once. These are flagged
    # rather than dropped: on a season disc two genuine episodes could collide,
    # and losing an episode is worse than showing a greyed-out row.
    duplicate_of: int | None = None


def _duration_seconds(duration: object) -> int:
    if not isinstance(duration, dict):
        return 0
    try:
        return (
            int(duration.get("Hours", 0)) * 3600
            + int(duration.get("Minutes", 0)) * 60
            + int(duration.get("Seconds", 0))
        )
    except (TypeError, ValueError):
        return 0


def _extract_json(stdout: str) -> dict | None:
    """Pull the `JSON Title Set:` object out of HandBrake's mixed log output."""
    marker = stdout.find(JSON_MARKER)
    if marker < 0:
        return None
    start = stdout.find("{", marker)
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(stdout[start:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _mark_duplicates(titles: list[Title]) -> None:
    seen: dict[tuple[int, int], int] = {}
    for title in titles:
        key = (title.duration_s, title.chapters)
        if key in seen:
            title.duplicate_of = seen[key]
        else:
            seen[key] = title.index


def scan_titles(handbrake_path: str, device: str, min_seconds: int) -> list[Title]:
    """List titles on the disc in `device`, filtered to duration >= min_seconds."""
    cmd = [handbrake_path, "-i", device, "--title", "0", "--scan", "--json"]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SCAN_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    data = _extract_json(proc.stdout)
    if data is None:
        return []

    titles: list[Title] = []
    for entry in data.get("TitleList") or []:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry["Index"])
        except (KeyError, TypeError, ValueError):
            continue
        duration = _duration_seconds(entry.get("Duration"))
        if duration < min_seconds:
            continue
        chapters = len(entry.get("ChapterList") or [])
        titles.append(
            Title(
                index=index,
                name=entry.get("Name") or f"Title {index}",
                duration_s=duration,
                chapters=chapters,
                size_bytes=None,
            )
        )

    _mark_duplicates(titles)
    return titles


def main_feature(titles: list[Title]) -> Title | None:
    """Longest non-duplicate title, or None for an empty list."""
    candidates = [t for t in titles if t.duplicate_of is None] or titles
    if not candidates:
        return None
    return max(candidates, key=lambda t: t.duration_s)
