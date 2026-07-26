# SPEC.md — Disc2Jelly

**One-click DVD/Blu-ray → Jellyfin ingest app.** Local Python app (FastAPI + vanilla-JS web UI), runs on Windows 10/11 and Linux. Detects an inserted disc, rips it with MakeMKV, encodes with HandBrake (HEVC default), names output per Jellyfin movie spec via TMDb (manual fallback), uploads to a WebDAV folder, cleans up temp files. Wife-proof UI with accurate per-stage progress bars.

## Non-goals
- No user auth, no multi-user, no remote access (localhost only).
- No music CDs, no transcoding of existing file libraries.
- No Docker (native install; simple start script per OS).

## Tech stack
- Python ≥ 3.11. Dependencies (pinned in requirements.txt): `fastapi`, `uvicorn[standard]`, `requests`, `pytest` (dev). **No other deps.** Stdlib for everything else (subprocess, threading, queue, json, re, pathlib, urllib).
- Frontend: single `static/index.html` + `static/app.js` + `static/style.css`. Vanilla JS, EventSource for SSE. No build step, no CDN.
- External binaries (user installs, app locates + validates): `makemkvcon` (MakeMKV ≥ 1.17), `HandBrakeCLI` (≥ 1.6).

## Repo layout
```
disc2jelly/
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI app, routes, SSE          [Coder C]
│   ├── jobs.py        # Job model, queue, pipeline runner  [Coder C]
│   ├── config.py      # settings load/save/validate        [Coder A]
│   ├── disc.py        # drive + disc detection             [Coder A]
│   ├── makemkv.py     # makemkvcon wrapper + parser        [Coder A]
│   ├── handbrake.py   # HandBrakeCLI wrapper + parser      [Coder A]
│   ├── metadata.py    # TMDb client + Jellyfin naming      [Coder B]
│   └── webdav.py      # WebDAV upload                      [Coder B]
├── static/
│   ├── index.html     # [Coder C]
│   ├── app.js         # [Coder C]
│   └── style.css      # [Coder C]
├── tests/
│   ├── fixtures/      # recorded CLI outputs               [Coder A/B]
│   ├── test_makemkv.py  [Coder A]
│   ├── test_handbrake.py[Coder A]
│   ├── test_config.py   [Coder A]
│   ├── test_metadata.py [Coder B]
│   └── test_webdav.py   [Coder B]
├── requirements.txt   # [Orchestrator provides]
├── README.md          # [Orchestrator, stage 4]
├── start_linux.sh     # [Coder C]
└── start_windows.bat  # [Coder C]
```

## Pipeline & job model

Stages (enum `Stage`): `DETECT → IDENTIFY → RIP → ENCODE → UPLOAD → CLEANUP → DONE` (+ `ERROR`, `CANCELLED`).
`IDENTIFY` = TMDb match confirmed by user in UI before job starts; the job is only created after confirmation, so the running pipeline is RIP→ENCODE→UPLOAD→CLEANUP.

Job (in-memory, dataclass `Job` in jobs.py):
```
id: str (uuid4 hex), disc_name: str, drive: str, title_indices: list[int],
tmdb_id: int|None, movie_title: str, year: int|None, profile: str ("hevc"|"h264"),
status: Stage, created: float, error: str|None
```
One job = one movie (main feature). Multi-title discs: user picks the main title (largest) in the UI; extra titles selectable. Multiple jobs queue sequentially (single optical drive anyway).

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
- `percent` semantics: RIP uses MakeMKV PRGV total; ENCODE uses HandBrake task %; UPLOAD uses bytes-sent %.
- jobs.py owns the event bus: `subscribe() -> queue.Queue`, `publish(event)`; main.py streams to clients. Last event per job is retained so late-joining clients see state.

### Config (`config.py`) — SACRED CONTRACT
JSON at platform path: Linux `~/.config/disc2jelly/config.json`, Windows `%APPDATA%/disc2jelly/config.json` (use `os.environ.get("APPDATA")`). API:
```python
@dataclass
class Config:
    webdav_url: str = ""          # e.g. https://nas.example/remote.php/dav/files/me/movies-inbox
    webdav_user: str = ""
    webdav_password: str = ""
    tmdb_api_key: str = ""        # v3 api_key
    temp_dir: str = ""            # default: <config dir>/work
    keep_mkv: bool = False        # keep intermediate MakeMKV file
    encoder: str = "hevc"         # "hevc" | "h264"
    hevc_quality: int = 22        # RF/CRF
    h264_quality: int = 20
    makemkv_path: str = ""        # empty = auto-detect
    handbrake_path: str = ""      # empty = auto-detect
    min_title_seconds: int = 600  # filter junk titles

def load() -> Config
def save(cfg: Config) -> None
def config_path() -> Path
def find_binary(name: str, configured: str, os_candidates: list[str]) -> str|None
```
`find_binary`: if configured path exists → it; else shutil.which; else probe candidate absolute paths; else None.

### Disc detection (`disc.py`)
```python
@dataclass
class Drive:  id: str; label: str; device: str   # id: "disc:0" style for makemkv
def list_drives(makemkv_path: str) -> list[Drive]      # parse `makemkvcon -r info disc:9999` drive scan lines (DRV:index,visible,enabled,flags "drive name" "disc name")
def disc_info(makemkv_path: str, drive_id: str) -> dict  # raw parsed CINFO/TINFO/SINFO tree
@dataclass
class Title: index: int; name: str; duration_s: int; chapters: int; size_bytes: int|None
def list_titles(makemkv_path: str, drive_id: str, min_seconds: int) -> list[Title]
```
Timeouts: every subprocess call has `timeout=` and returns []/{} on failure rather than raising (caller emits error event).
- Drive scan: `makemkvcon -r --cache=1 info disc:9999`; parse `DRV:index,status,999,flags,"drive name","disc label"[,"/dev/srN"]`. status: 2 = disc ready, 0 = empty, 1 = tray open, 3 = loading, 256 = absent. flags: 1 = DVD, 12/28 = Blu-ray. `TCOUNT:0` + `MSG:5010` at end of enumeration is NORMAL, not an error.
- Title fields from `info disc:N`: name=TINFO:x,2 ; chapters=TINFO:x,8 ; duration=TINFO:x,9 as `H:MM:SS` text (parse to seconds); size=TINFO:x,11 (bytes, quoted). Disc name prefer CINFO:2, fallback CINFO:32 (volume label) / DRV field 6.

### MakeMKV wrapper (`makemkv.py`)
```python
def rip(makemkv_path: str, drive_id: str, title: str|int, out_dir: Path,
        emit: Callable[[dict], None], cancel: threading.Event) -> Path
```
- Runs `makemkvcon -r --progress=-same --minlength=<cfg.min_title_seconds> mkv <drive_id> <title> <out_dir>`. Env: `LC_ALL=C`.
- Robot line formats (verified, see /mnt/agents/output/info.md §1): `PRGT:code,id,"name"`, `PRGC:code,id,"name"`, `PRGV:current,total,max` — **max is constant 65536**, field index 1 = overall job progress, field 0 = current sub-task (NOT monotonic). percent = field1/65536*100. Emit stage "RIP" with detail from PRGC caption.
- **Parse respecting quotes** (values can contain commas) — use a small CSV-aware splitter, never `line.split(',')`.
- **Success detection**: exit code 0 is NOT sufficient. Only `MSG:5036` = full success; `MSG:5037` = partial (N saved / M failed → treat as error); verify output file exists. MSG layout `MSG:code,flags,count,"msg","fmt",params...` — match by code, never by text.
- Returns path of produced .mkv (largest .mkv in out_dir after run). Raises `RipError` on failure or cancel (cancel → terminate process, kill fallback after 5s).
- Pure parser function `parse_robot_line(line: str) -> dict|None` exposed for tests (returns {"token": "PRGV"|"MSG"|..., fields parsed}).

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
- **Large files (≥ 256 MiB)**: use Nextcloud chunking v2 (info.md §5.3): MKCOL `<uploads-root>/<uuid>/` (uploads root derived from base_url by replacing `/dav/files/<user>` with `/dav/uploads/<user>`; every chunk request carries `Destination: <final url>` header) → PUT numeric zero-padded chunks (64 MiB each, names 000001..; header `OC-Total-Length: <size>`) → `MOVE <dir>/.file` with `Destination` final URL. On any chunking error, abort (DELETE upload dir) and raise — do NOT silently fall back to plain PUT for multi-GB files.
- Small files: plain streamed PUT (file object as data, 8 MiB read chunks for progress). Cancel → abort request, cleanup remote temp if chunked.
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
GET  /api/health             → {ok, binaries: {makemkv, handbrake}, config_ok}
GET  /api/drives             → list[Drive] (rescans every call, 20s timeout)
GET  /api/drives/{id}/titles → list[Title]
GET  /api/tmdb/search?q=...  → list[MovieMatch] (400 if no api key)
GET  /api/jobs               → list of jobs + last events
POST /api/jobs               → body {drive, titles:[int], tmdb_id?, title, year?, profile} → Job
POST /api/jobs/{id}/cancel   → {ok}
GET  /api/events             → SSE stream (text/event-stream, replay of last events on connect)
GET  /api/config             → Config as JSON (password masked)
PUT  /api/config             → save; returns {ok, errors}
POST /api/config/test-webdav → {ok, message}
```
Run: `python -m app.main` starts uvicorn on 127.0.0.1:8642 and opens browser.

### Frontend (static/) — wife-proof
Sections top→bottom:
1. **Status bar**: app health dots (MakeMKV ✓/✗, HandBrake ✓/✗, WebDAV ✓/✗, TMDb ✓/✗) + gear icon opening Settings modal (all config fields, "Test WebDAV" button, Save).
2. **Disc panel**: "Rescan" button; when disc found: disc label, title dropdown (duration+chapters shown, main title preselected), TMDb suggestion card ("We think this is: **Title (Year)** — [Use] / [Search other]" with manual search box + fully manual title/year fields as fallback), profile select (HEVC default), big **"RIP & UPLOAD"** button.
3. **Queue panel**: per job a card with movie name and 3 stacked progress bars (Rip / Encode / Upload) with % and fps/ETA on encode, status badge, cancel button, error text if failed.
4. **Log pane** (collapsible): last 200 raw log lines.
Design: warm, low-saturation (cream background #faf7f2, dark slate text, muted teal accent #3f7f77, soft red for errors), generous whitespace, system font stack, big click targets (≥44px), no jargon (labels: "Disc", "Movie", "Save to server", not "transcode/HEVC" — advanced settings behind a "Advanced" details element).

## Cross-platform rules
- All paths via pathlib; never assume `/`; subprocess lists (no shell=True); binary candidates: Windows `C:/Program Files (x86)/MakeMKV/makemkvcon.exe`, `C:/Program Files/HandBrake/HandBrakeCLI.exe`; Linux `/usr/bin/makemkvcon`, `/usr/bin/HandBrakeCLI`, plus PATH lookup.
- Process termination cross-platform (Popen.terminate then kill fallback).
- No fcntl, no signal handlers beyond what works on Windows.

## Testing
- pytest. Parser tests use fixtures in tests/fixtures (recorded makemkvcon robot output, HandBrakeCLI stdout incl. `\r` updates). Naming tests cover umlauts, colons, year-missing. WebDAV tests mock requests. Target: all pure functions covered; wrappers tested with mocked subprocess.

## Git workflow (per vibecoding-general-swarm)
Shared repo: /mnt/agents/output/project. Each coder: `git worktree add $HOME/work-<branch> <branch>`, implement, run their tests, commit on branch. Orchestrator merges all into main, runs full suite, fixes integration.
Branches: `feat/pipeline` (Coder A), `feat/meta-webdav` (Coder B), `feat/api-ui` (Coder C).
**Interface contracts in this SPEC are sacred — deviations require orchestrator sign-off.**
