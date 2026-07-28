# SPEC.md — Disc2Jelly

**One-click DVD → Jellyfin ingest app, films and TV series.** Local Python app (FastAPI + vanilla-JS web UI), runs on Windows 10/11 and Linux. Detects an inserted disc, has HandBrake read and encode it in a single pass (HEVC default), names output per the Jellyfin movie/show spec via TMDb (manual fallback), writes to a local folder or WebDAV, cleans up temp files. Wife-proof UI with accurate per-stage progress bars.

On Windows it ships as one Inno Setup installer that bundles Python and HandBrakeCLI. libdvdcss is the sole exception — it has no official binary distribution, so the user supplies it and the app explains how.

## Non-goals
- **No Blu-ray.** AACS is an actively maintained revocation scheme plus the BD+ VM; no FOSS path works reliably and every working tool is proprietary and paid. DVD's CSS is cryptographically broken, so libdvdcss handles it with no key database. Supporting Blu-ray would reintroduce exactly the dependency this design exists to remove.
- No user auth, no multi-user, no remote access (localhost only).
- No music CDs, no transcoding of existing file libraries.
- No Docker (native install; installer on Windows, start script on Linux).

## Tech stack
- Python ≥ 3.11. Dependencies (pinned in requirements.txt): `fastapi`, `uvicorn[standard]`, `requests`, `pytest` (dev). **No other deps.** Stdlib for everything else (subprocess, threading, queue, json, re, pathlib, urllib).
- Frontend: single `static/index.html` + `static/app.js` + `static/style.css`. Vanilla JS, EventSource for SSE. No build step, no CDN.
- External binaries: `HandBrakeCLI` (≥ 1.6) only — bundled by the Windows installer, distro package on Linux. Located via `config.handbrake_candidates()`, bundled copy first.
- `libdvdcss` for CSS decryption: user-supplied DLL beside HandBrakeCLI (Windows) or a distro package (Linux). Detected, never installed — there is no official binary release, and not redistributing it also keeps distribution (legally distinct from use) off the table.
- Build tooling (Windows only): PyInstaller (onedir) + Inno Setup 6.

## Repo layout
```
disc2jelly/
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI app, routes, SSE          [Coder C]
│   ├── jobs.py        # Job model, queue, pipeline runner  [Coder C]
│   ├── config.py      # settings load/save/validate        [Coder A]
│   ├── drives.py      # optical drive detection (OS-level, no binary)
│   ├── scan.py        # title enumeration via HandBrake --scan --json
│   ├── handbrake.py   # HandBrakeCLI wrapper + parser
│   ├── dvdcss.py      # libdvdcss detection + setup guidance
│   ├── metadata.py    # TMDb movie + TV client, Jellyfin naming
│   ├── destination.py # local folder / WebDAV output targets
│   ├── webdav.py      # WebDAV upload (behind destination.py)
│   └── _baked.py      # build-time defaults (generated, gitignored)
├── static/
│   ├── index.html     # [Coder C]
│   ├── app.js         # [Coder C]
│   └── style.css      # [Coder C]
├── tests/
│   ├── fixtures/      # recorded CLI outputs               [Coder A/B]
│   └── test_*.py
├── requirements.txt
├── run_app.py         # PyInstaller entry point (app/main.py cannot be one:
│                      # an entry script is __main__, so its relative imports fail)
├── start_linux.sh
└── start_windows.bat  # source-run fallback; end users get the installer
build/
├── gen_baked.py       # writes app/_baked.py from build_config.toml
├── fetch_deps.py      # downloads + checksum-pins HandBrakeCLI
├── disc2jelly.spec    # PyInstaller onedir
├── disc2jelly.iss     # Inno Setup wizard (collects destination config)
└── build_windows.ps1  # orchestrates the five build steps
```

## Pipeline & job model

Stages (enum `Stage`): `DETECT → IDENTIFY → ENCODE → UPLOAD → CLEANUP → DONE` (+ `ERROR`, `CANCELLED`).
`IDENTIFY` = TMDb match confirmed by user in UI before job starts; the job is only created after confirmation, so the running pipeline is ENCODE→UPLOAD→CLEANUP.

**There is no RIP stage.** HandBrake reads the disc device directly, so decrypt+rip+encode is one pass. This removes the ~50 GB intermediate scratch requirement and the `keep_mkv` option. Trade-off: a read failure mid-encode loses the job, with no raw intermediate to retry from.

Job (in-memory, dataclass `Job` in jobs.py):
```
id: str (uuid4 hex), disc_name: str, drive: str, targets: list[TitleTarget],
tmdb_id: int|None, display_title: str, year: int|None, profile: str ("hevc"|"h264"),
status: Stage, created: float, error: str|None

TitleTarget: title_index: int, relpath: str   # posix, relative to the destination root
```
`drive` is a device path (`/dev/sr0`, `D:\\`), not a MakeMKV handle.

One `TitleTarget` = one output file. A film is one target (plus one per selected bonus title); a season disc is one target per episode. Relpaths are resolved **server-side at job creation**, where the TMDb data is in hand — the client never dictates a filesystem path. This is what makes movies and series the same shape to the pipeline.

Encode and send run per target, so a season disc never stacks several finished files on the temp drive. Multiple jobs queue sequentially (single optical drive anyway).

### Event schema (SSE) — SACRED CONTRACT
All modules report progress only via callback `emit(event: dict)`. Event dict:
```json
{
  "job_id": "abc123",
  "stage": "RIP",               // Stage enum value; "APP" for app-level msgs
  "status": "running",          // running|done|error|cancelled
  "percent": 42.5,              // float 0..100 or null if indeterminate
  "detail": "Saving title 1",   // human-readable short text
  "fps": 98.2,                  // optional, encode stage
  "eta": "00:12:31",            // optional string
  "log": "PRGV:...",            // optional raw line for log pane
  "ts": 1712345678.9
}
```
- `percent` semantics: ENCODE uses HandBrake task %; UPLOAD uses bytes-sent %.
- jobs.py owns the event bus: `subscribe() -> queue.Queue`, `publish(event)`; main.py streams to clients. Last event per job is retained so late-joining clients see state.

### Config (`config.py`) — SACRED CONTRACT
JSON at platform path: Linux `~/.config/disc2jelly/config.json`, Windows `%APPDATA%/disc2jelly/config.json` (use `os.environ.get("APPDATA")`). API:
```python
@dataclass
class Config:
    destination_kind: str = "local"  # "local" | "webdav"
    local_path: str = ""          # empty = destination.DEFAULT_LOCAL_ROOT
    webdav_url: str = ""          # e.g. https://nas.example/remote.php/dav/files/me/movies-inbox
    webdav_user: str = ""
    webdav_password: str = ""
    tmdb_api_key: str = ""        # v3 api_key; empty = _baked.TMDB_API_KEY
    temp_dir: str = ""            # default: <config dir>/work
    encoder: str = "hevc"         # "hevc" | "h264"
    hevc_quality: int = 22        # RF/CRF
    h264_quality: int = 20
    handbrake_path: str = ""      # empty = auto-detect
    min_title_seconds: int = 600  # filter junk titles

def load() -> Config
def save(cfg: Config) -> None
def config_path() -> Path
def bundled_dir() -> Path          # next to the frozen exe, else ./vendor
def find_binary(name: str, configured: str, os_candidates: list[str]) -> str|None
def handbrake_candidates() -> list[str]   # bundled copy first
def resolve_binaries(cfg: Config) -> str|None   # HandBrakeCLI path, or None
```
`find_binary`: if configured path exists → it; else shutil.which; else probe candidate absolute paths; else None.

Build-time defaults live in generated `app/_baked.py` (TMDb key, destination kind, local path, WebDAV URL/user). Imported with a try/except fallback so a source checkout works with none of it. **The WebDAV password is not baked** unless `build_windows.ps1 -BakePassword` is used: PyInstaller does not obfuscate and any compiled-in string is recoverable with `strings`. The Inno Setup wizard writes the password to `%APPDATA%\disc2jelly\config.json` on the target machine instead.

### Drive detection (`drives.py`)
```python
@dataclass
class Drive: device: str; label: str; has_disc: bool   # device: "/dev/sr0" | "D:\\"
def list_drives() -> list[Drive]
```
No external binary and no subprocess. Windows: kernel32 `GetLogicalDrives` / `GetDriveTypeW` (DRIVE_CDROM = 5) / `GetVolumeInformationW` — the volume label doubles as the disc label and is what feeds TMDb. Linux: `/dev/sr*`, media presence from `/sys/block/<n>/size` (0 sectors = empty tray), label from the `/dev/disk/by-label` symlinks. Returns [] rather than raising.

### Title enumeration (`scan.py`)
```python
@dataclass
class Title: index: int; name: str; duration_s: int; chapters: int
              size_bytes: int|None; duplicate_of: int|None
def scan_titles(handbrake_path: str, device: str, min_seconds: int) -> list[Title]
def main_feature(titles: list[Title]) -> Title|None
```
Runs `HandBrakeCLI -i <device> --title 0 --scan --json` and decodes the `JSON Title Set:` block out of the surrounding libhb log. Title indices are **1-based**; there is no per-title byte size, so `size_bytes` is always None. Every call has a timeout and returns [] on failure.

Titles with identical `(duration_s, chapters)` are **flagged via `duplicate_of`, not dropped** — DVDs routinely expose the main feature more than once, but on a season disc two genuine episodes could collide and losing an episode is worse than showing a greyed-out row. `main_feature` ignores flagged duplicates.

### libdvdcss (`dvdcss.py`)
```python
def is_available(bundled_dir=None) -> bool
def hint(bundled_dir=None) -> str
def require(bundled_dir=None) -> None        # raises DvdCssError with the hint
```
Detection only — this module never installs anything. Windows: `libdvdcss-2.dll` beside HandBrakeCLI. Linux: the system library via `ctypes.util.find_library`. When it is missing, `hint()` returns platform-specific instructions naming the exact target folder, which `/api/health` passes to the UI banner.

**There is no acquisition path, by necessity, not just by policy.** VideoLAN publishes libdvdcss as source tarballs only; there is no `win64/` directory and never has been, and VLC's Windows build links libdvdcss statically into `libdvdread_plugin.dll` rather than shipping a standalone DLL. The user supplies the file. Not redistributing it also keeps the original legal position intact — distributing a circumvention library is legally distinct from using one.

### Destinations (`destination.py`)
```python
class Destination(Protocol):
    def send(self, src: Path, rel_dest: str, emit, cancel, job_id: str) -> None
class LocalDestination:  root: Path    # DEFAULT_LOCAL_ROOT = ~/Videos/Disc2Jelly
class WebDavDestination: client        # wraps webdav.WebDAVClient unchanged
def for_config(cfg) -> Destination
```
`LocalDestination` copies in 4 MiB chunks with the same 8 MiB progress granularity as the WebDAV path, honours the cancel event, removes a partial file on failure, and **refuses any relpath that resolves outside the root**.

### HandBrake wrapper (`handbrake.py`)
```python
def encode(handbrake_path: str, src: Path, dst: Path, profile: str,
           quality: int, emit, cancel: threading.Event) -> Path
```
- HEVC: `--encoder x265 --quality <q> --all-audio --all-subtitles --markers`; H.264: `--encoder x264 --quality <q> --all-audio --all-subtitles --markers`. Always `--format av_mkv`. No burn-in. Env: `LC_ALL=C`.
- Progress lines (verified, info.md §2): `Encoding: task <n> of <total>, <pct> %` with OPTIONAL suffix ` (<fps> fps, avg <avg> fps, ETA <HH>h<MM>m<SS>s)`; task word may also be `Muxing:` (treat as ≥99% encode, emit stage "ENCODE" detail "Finalizing"). Regex (stats group optional): `(Encoding|Muxing): task (\d+) of (\d+), (\d+\.?\d*) %( \(([\d.]+) fps, avg ([\d.]+) fps, ETA ([\dhms]+)\))?`
- **Split stdout buffer on BOTH `\r` and `\n`** (tty uses `\r`; pipes newlines). On Windows non-progress log lines may not reach the pipe — never depend on them.
- Scan phase lines (`Scanning title ...`) → emit stage "ENCODE", percent null, detail "Analyzing source".
- Pure parser `parse_progress(line: str) -> dict|None` exposed for tests.
- Raises `EncodeError` on non-zero exit/cancel; success confirmed by exit 0 AND dst file existing.

### Metadata (`metadata.py`)
```python
@dataclass
class MovieMatch: tmdb_id: int; title: str; original_title: str; year: int|None; overview: str
def search_movies(api_key: str, query: str) -> list[MovieMatch]   # TMDb /3/search/movie, requests, timeout 10
def clean_query(disc_label: str) -> str   # "THE_MATRIX_16X9" → "The Matrix"-ish heuristic (strip 16X9/4X3/WS/FS/DISC\d/SEASON markers, underscores→spaces, title-case)
def jellyfin_movie_relpath(title: str, year: int|None, tmdb_id: int|None = None) -> Path    # SACRED, see below
```
- TMDb auth: accept EITHER credential from config — if the stored value looks like a v4 token (length > 100, contains dots) send `Authorization: Bearer <token>`, else send v3 `?api_key=<key>`. Year = first 4 chars of `release_date`, guard empty string. Raise `MetadataError` on 401.
- `jellyfin_movie_relpath` must follow https://jellyfin.org/docs/general/server/media/movies exactly (verified info.md §3):
  - Folder `Movies/<Title> (<Year>)[ tmdbid-<id>]/`, file `<same name>.mkv` — file base name **char-for-char identical** to folder name.
  - Year omitted if None; tmdb id tag `[tmdbid-123]` appended (space-separated) if provided.
  - Strip reserved chars `< > : " / \ | ? *` from title before composing (replace with nothing, collapse double spaces); do NOT touch umlauts/unicode otherwise.

### WebDAV (`webdav.py`)
```python
class WebDAVClient:
    def __init__(self, base_url: str, user: str, password: str): ...
    def ensure_dirs(self, rel_dir: str) -> None      # iterative MKCOL per segment; 405=exists ok, 409=parent missing (create parents first)
    def upload(self, local: Path, rel_dest: str, emit, cancel, job_id: str) -> None  # percent events stage "UPLOAD"
    def test_connection(self) -> tuple[bool, str]    # PROPFIND Depth:0 on base_url; returns (ok, message)
```
- **URL-encode every path segment** (spaces, non-ASCII). Auth: HTTP Basic (recommend app passwords in README).
- **Large files (≥ 256 MiB) on Nextcloud/ownCloud** (detected by `base_url` containing `/dav/files/<user>`, the shape the uploads root is derived from): use Nextcloud chunking v2 (info.md §5.3): MKCOL `<uploads-root>/<uuid>/` (uploads root derived from base_url by replacing `/dav/files/<user>` with `/dav/uploads/<user>`; every chunk request carries `Destination: <final url>` header) → PUT numeric zero-padded chunks (64 MiB each, names 000001..; header `OC-Total-Length: <size>`) → `MOVE <dir>/.file` with `Destination` final URL. On any chunking error, abort (DELETE upload dir) and raise — do NOT silently fall back to plain PUT for multi-GB files.
- Small files — and **files of any size on plain WebDAV servers** (rclone, nginx `dav_methods`, Apache `mod_dav`), which cannot speak chunking v2: plain streamed PUT (file object as data, 8 MiB read chunks for progress). Cancel → abort request, cleanup remote temp if chunked.
- Errors raise `WebDAVError` with HTTP status + server text. Handle: 401/403 auth, 404 parent missing, 507 quota.

### Jobs/queue (`jobs.py`) — SACRED CONTRACT
```python
class JobManager:
    def __init__(self, cfg_getter: Callable[[], Config]): ...
    def create_job(self, drive: str, title_indices: list[int], tmdb_id, movie_title, year, profile) -> Job
    def list_jobs(self) -> list[dict]         # serialized jobs w/ last event
    def cancel(self, job_id: str) -> bool
    def subscribe(self) -> queue.Queue        # event bus for SSE
    def start(self) -> None                   # starts worker thread
```
Pipeline per job (worker thread, sequential): RIP each selected title → ENCODE to `<Title> (<Year>).mkv` in temp → UPLOAD to `webdav_url + jellyfin_movie_relpath(...)` → CLEANUP (delete temp unless keep_mkv). Any exception → status ERROR w/ message; continue queue. Cancel sets threading.Event checked by wrappers.

### HTTP API (`main.py`)
```
GET  /                       → static/index.html
GET  /api/health             → {ok, binaries:{handbrake}, destination_ok, dvdcss_ok,
                                dvdcss_hint, tmdb_key_set, config_ok}
GET  /api/drives             → list[Drive] (rescans every call)
GET  /api/titles?device=...  → list[Title]  (503 if HandBrakeCLI missing)
GET  /api/disc/hint?label=   → DiscHint {kind, title, season, disc}
GET  /api/tmdb/search?q=&kind=movie|tv → list[MovieMatch|ShowMatch]
GET  /api/tmdb/tv/{id}/season/{n}      → list[EpisodeInfo]
GET  /api/jobs               → list of jobs + last events
POST /api/jobs               → body {drive, kind, tmdb_id?, title, year?, profile,
                                titles:[int]                       # kind=movie
                                episodes:[{title_index, season, episode, name}]  # kind=series
                              } → Job. Relpaths are built server-side.
POST /api/jobs/{id}/cancel   → {ok}
GET  /api/events             → SSE stream (text/event-stream, replay of last events on connect)
GET  /api/config             → Config as JSON (password masked)
PUT  /api/config             → save; returns {ok, errors}
POST /api/config/test-webdav → {ok, message}
```
Run: `python -m app.main` starts uvicorn on 127.0.0.1:8642 and opens browser.

### Frontend (static/) — wife-proof
Sections top→bottom:
1. **Status bar**: app health dots (libdvdcss ✓/✗, HandBrake ✓/✗, Destination ✓/✗, TMDb ✓/✗) + gear icon opening Settings modal. A red libdvdcss dot also shows a banner with `dvdcss_hint`, naming the folder the DLL belongs in.
2. **Disc panel**: "Rescan" button; when disc found: disc label, a **film / TV episodes** toggle preselected from `/api/disc/hint`, then either the film title dropdown + extras, or the series episode table (one row per title: tick box, episode number, TMDb episode name; "Number them" fills sequentially from a starting number). TMDb suggestion card ("We think this is: **Title (Year)** — [Use] / [Search other]" with manual search box + fully manual title/year fields as fallback), profile select (HEVC default), big **"RIP & UPLOAD"** button.
3. **Queue panel**: per job a card with movie name and 3 stacked progress bars (Rip / Encode / Upload) with % and fps/ETA on encode, status badge, cancel button, error text if failed.
4. **Log pane** (collapsible): last 200 raw log lines.
Design: warm, low-saturation (cream background #faf7f2, dark slate text, muted teal accent #3f7f77, soft red for errors), generous whitespace, system font stack, big click targets (≥44px), no jargon (labels: "Disc", "Movie", "Save to server", not "transcode/HEVC" — advanced settings behind a "Advanced" details element).

## Cross-platform rules
- All paths via pathlib; never assume `/`; subprocess lists (no shell=True); binary candidates: the bundled `HandBrakeCLI.exe` beside the executable first, then Windows `C:/Program Files/HandBrake/HandBrakeCLI.exe`; Linux `/usr/bin/HandBrakeCLI`, plus PATH lookup.
- Process termination cross-platform (Popen.terminate then kill fallback).
- No fcntl, no signal handlers beyond what works on Windows.

## Testing
- pytest. Parser tests use fixtures in tests/fixtures (HandBrakeCLI `--scan --json` output, HandBrakeCLI stdout incl. `\r` updates). Naming tests cover umlauts, colons, year-missing, and Jellyfin episode paths. Drive detection is tested with a fake kernel32 object (Windows) and a temp-dir fake of `/dev` + `/sys/block` + `/dev/disk/by-label` (Linux), so both platforms are covered from either host. Target: all pure functions covered; wrappers tested with mocked subprocess.
- `tests/js_badge_logic.test.mjs` runs app.js in a node vm sandbox (skipped when node is absent).

## Git workflow (per vibecoding-general-swarm)
Shared repo: /mnt/agents/output/project. Each coder: `git worktree add $HOME/work-<branch> <branch>`, implement, run their tests, commit on branch. Orchestrator merges all into main, runs full suite, fixes integration.
Branches: `feat/pipeline` (Coder A), `feat/meta-webdav` (Coder B), `feat/api-ui` (Coder C).
**Interface contracts in this SPEC are sacred — deviations require orchestrator sign-off.**
