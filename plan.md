# Plan: DVD/Blu-ray → Jellyfin Ingest App ("DiscRipper" working title)

## Goal
Cross-platform (Windows + Linux) local app, wife-proof: detect disc → rip (MakeMKV) → encode (HandBrake, HEVC default) → TMDb auto-naming per Jellyfin movie spec (manual fallback) → upload to WebDAV directory consumed by Jellyfin → cleanup. Accurate live progress bars per stage.

## Architecture (orchestrator-designed)
- Python 3.11+, FastAPI backend, single-page vanilla JS frontend (no build step), SSE for progress.
- Modules with clean interfaces so coders work in parallel:
  - `app/config.py` — settings (WebDAV, TMDb key, encode profile, temp dir), JSON file persistence
  - `app/disc.py` — disc detection (Windows: WMI/drive letters; Linux: /dev/sr*, udev; plus `makemkvcon -r info disc:N`)
  - `app/makemkv.py` — wrapper for `makemkvcon -r` (robot mode), parse PRGV/PRGT/PRGC/MSG → progress events
  - `app/handbrake.py` — wrapper for `HandBrakeCLI`, parse stdout progress ("Encoding: task X of Y, NN.NN % ... fps") → events
  - `app/metadata.py` — TMDb search + Jellyfin naming sanitizer (Movies/Name (Year)/Name (Year).mkv)
  - `app/webdav.py` — WebDAV upload with resume/progress (requests), MKCOL for folders
  - `app/jobs.py` — job queue, pipeline orchestration, stage events, cancellation
  - `app/main.py` — FastAPI routes + SSE stream
  - `static/` — index.html/app.js/style.css (disc status, title confirm, queue, progress bars, logs)
  - `tests/` — unit tests for all parsers (recorded CLI output fixtures) and naming logic

## Stage 1 — Verify external specs (explore subagent)
- makemkvcon robot-mode progress output format (exact tokens)
- HandBrakeCLI progress stdout format
- Jellyfin movie naming rules from https://jellyfin.org/docs/general/server/media/movies
- TMDb search API essentials
Output: verified spec notes file → feeds coders.

## Stage 2 — Implementation (vibecoding-general-swarm; parallel coder subagents)
- Coder A: disc.py, makemkv.py, handbrake.py, config.py + parser tests
- Coder B: metadata.py, webdav.py + tests
- Coder C: jobs.py, main.py, static frontend
Interfaces fixed by orchestrator contract in prompts (event dict schema, function signatures).

## Stage 3 — Integration & test
- Install deps, run full test suite, fix failures (redelegate fixes to coders).
- Reviewer subagent: code review pass (robustness, Windows/Linux paths, error handling, cancellation).

## Stage 4 — Packaging & delivery
- README with install/usage for Windows (wife) + Linux (user), dependency table (MakeMKV, HandBrakeCLI, Python), systemd/autostart hints.
- Deliver project folder + zip under /mnt/agents/output/.
