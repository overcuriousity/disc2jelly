# Verified Technical Specs: MakeMKV + HandBrake + TMDb + WebDAV Rip Pipeline

Compiled from authoritative sources (makemkv.com developer docs, HandBrake issue tracker/docs,
jellyfin.org docs, developer.themoviedb.org, docs.nextcloud.com). Real captured output lines are
quoted verbatim from forum posts / GitHub issues where available.

---

## 1. makemkvcon robot mode (`-r` / `--robot`)

Primary source: official developer doc <https://www.makemkv.com/developers/usage.txt>
(mirrored in the Linux man page, e.g. bluray.beandog.org/makemkv/man/makemkvcon.html).

With `-r`, ALL messages go to stdout, one message per line, in machine-readable `TOKEN:field,field,...`
form. String values are always double-quoted. Line tokens: `MSG`, `DRV`, `TCOUNT`, `CINFO`, `TINFO`,
`SINFO`, `PRGT`, `PRGC`, `PRGV`.

### 1.1 Drive/disc scan

List drives: `makemkvcon -r --cache=1 info disc:9999` (disc:9999 = enumerate drives, don't open a disc).

Format: `DRV:index,visible,enabled,flags,drive name,disc name[,drive path]`

Real captured output (gist pjobson / docker-makemkv issues, `info disc:9999`):

```
MSG:1005,0,1,"MakeMKV v1.18.1 linux(x64-release) started","%1 started","MakeMKV v1.18.1 linux(x64-release)"
DRV:0,0,999,0,"BD-RE PIONEER BD-RW   BDR-X13 1.04 DEDL301260UC","","/dev/sr2"
DRV:1,2,999,1,"BD-RE HL-DT-ST BD-RE  WH16NS60 1.03 M63IBOA5100","BUILD","/dev/sr0"
DRV:2,0,999,0,"DVD+R-DL HL-DT-ST DVDRAM GUE0N T.02 KWAJ7AC1833","","/dev/sr1"
DRV:3,256,999,0,"","",""
...
MSG:5010,0,0,"Failed to open disc","Failed to open disc"
TCOUNT:0
```

DRV field semantics (per ARM wiki / community observation):
- field 1 (index): drive index, use as `disc:N` in later commands
- field 2 (visible/status): 0 = empty, 1 = tray open, 2 = disc inserted/ready, 3 = loading, 256 = drive absent
- field 3: always `999` in practice
- field 4 (flags/media): 0 = empty/none, 1 = DVD present, 12/28 = Blu-ray present (see AP_DskFsFlagXXX)
- field 5: drive name, field 6: disc label, field 7: device path (Linux; often absent on Windows)

Note: `TCOUNT:0` + `MSG:5010 "Failed to open disc"` is NORMAL at the end of `info disc:9999`
(disc:9999 is pseudo, nothing to open). Do not treat it as an error during drive enumeration.

### 1.2 Disc info (`makemkvcon -r info disc:0`)

Formats (official usage.txt):

```
TCOUNT:count                 # number of titles found
CINFO:id,code,value          # disc attribute
TINFO:id,code,value          # title attribute  (first field = title index)
SINFO:id,code,value          # stream attribute (TINFO idx, then stream idx, then attr)
```

- `id` = attribute id, see `AP_ItemAttributeId` in `apdefs.h`
- `code` = message code if value is a constant/localized string (0 when plain)
- `value` = always a quoted string, even for numbers

Attribute IDs (from AP_ItemAttributeId, confirmed by community enum + real output):

| id | name (enum) | used for |
|----|-------------|----------|
| 1  | Type | disc type, e.g. `CINFO:1,6209,"Blu-ray disc"` |
| 2  | Name | **disc name / title name**: `CINFO:2,0,"Breaking Bad: Season 1: Disc 1"`, `TINFO:0,2,0,"..."` |
| 5/6/7 | CodecId/CodecShort/CodecLong | stream codec (`SINFO:...,6,0,"Mpeg2"`) |
| 8  | ChapterCount | **chapter count**: `TINFO:0,8,0,"7"` |
| 9  | Duration | **duration as H:MM:SS string**: `TINFO:0,9,0,"0:58:06"` (NOT seconds) |
| 10 | DiskSize | human size: `TINFO:0,10,0,"12.5 GB"` |
| 11 | DiskSizeBytes | size in bytes: `TINFO:0,11,0,"13472686080"` |
| 16 | SourceFileName | e.g. `TINFO:0,16,0,"00763.mpls"` |
| 20 | VideoAspectRatio (SINFO) | `SINFO:3,0,20,0,"4:3"` |
| 21 | VideoFrameRate (SINFO) | `SINFO:3,0,21,0,"25"` |
| 26 | SegmentsMap | segment list |
| 27 | OutputFileName | **output MKV name**: `TINFO:0,27,0,"Breaking_Bad_Season_1_Disc_1_t00.mkv"` |
| 28/29 | MetadataLanguageCode/Name | `CINFO:28,0,"eng"` / `CINFO:29,0,"English"` |
| 32 | VolumeName | **disc volume label**: `CINFO:32,0,"BREAKINGBADS1"` |

Real captured example (ARM wiki / Autorippr notes):

```
TCOUNT:7
CINFO:1,6209,"Blu-ray disc"
CINFO:2,0,"Breaking Bad: Season 1: Disc 1"
CINFO:28,0,"eng"
CINFO:29,0,"English"
CINFO:30,0,"Breaking Bad: Season 1: Disc 1"
CINFO:31,6119,"<b>Source information</b><br>"
CINFO:32,0,"BREAKINGBADS1"
CINFO:33,0,"0"
TINFO:0,2,0,"Breaking Bad: Season 1: Disc 1"
TINFO:0,8,0,"7"
TINFO:0,9,0,"0:58:06"
TINFO:0,10,0,"12.5 GB"
TINFO:0,11,0,"13472686080"
TINFO:0,16,0,"00763.mpls"
TINFO:0,25,0,"1"
TINFO:0,26,0,"262"
TINFO:0,27,0,"Breaking_Bad_Season_1_Disc_1_t00.mkv"
TINFO:0,28,0,"eng"
TINFO:0,30,0,"Breaking Bad: Season 1: Disc 1 - 7 chapter(s) , 12.5 GB"
SINFO:3,0,1,6201,"Video"
SINFO:3,0,6,0,"Mpeg2"
SINFO:3,0,21,0,"25"
```

Note TINFO/SINFO have an EXTRA leading field (title index / title index + stream index) compared
to CINFO. SINFO layout: `SINFO:<title_idx>,<stream_idx>,<attr_id>,<code>,<value>`.

### 1.3 Ripping

```
makemkvcon -r mkv disc:0 all /output/dir        # all titles
makemkvcon -r mkv disc:0 3 /output/dir          # single title index 3
```

During ripping, output is MSG lines + progress lines. Real MSG examples (makemkv.com forum t=35384,
ARM issue #720):

```
MSG:3307,16777216,2,"File 00381.m2ts was added as title #18","File %1 was added as title #%2","00381.m2ts","18"
MSG:3025,0,3,"Title #00384.m2ts has length of 5 seconds which is less than minimum title length of 120 seconds and was therefore skipped","Title #%1 has length of %2 seconds which is less than minimum title length of %3 seconds and was therefore skipped","00384.m2ts","5","120"
MSG:5037,516,2,"Copy complete. 0 titles saved, 1 failed.","Copy complete. %1 titles saved, %2 failed.","0","1"
```

Key MSG codes (from apdefs.h via community PowerShell enum + ARM):
- 1005 program started; 3007 "Using direct disc access mode"; 3307 title added; 5010 "Failed to open disc"
- 5005/5004 save (part) successful; **5036 = "Copy complete. %1 titles saved." (full success)**;
  **5037 = "Copy complete. %1 titles saved, %2 failed." (partial/failure)**; 5015 saving titles

### 1.4 Progress reporting

Formats (official usage.txt):

```
PRGT:code,id,name            # total progress bar title, e.g. PRGT:5018,0,"Saving all titles to MKV files"
PRGC:code,id,name            # current progress bar title (sub-task caption)
PRGV:current,total,max       # progress bar values; max is CONSTANT 65536
```

Real captured sequence (makemkv.com forum t=35384):

```
PRGV:0,0,65536
PRGV:0,0,65536
PRGV:730,0,65536
PRGV:730,730,65536
PRGV:857,730,65536
PRGV:857,858,65536
```

PRGV fields: `PRGV:current,total,max`
- index 0 = current sub-task progress (current title/file)
- index 1 = total progress (all titles)
- index 2 = max, always 65536
- Percent = `field / 65536 * 100`. Use field 1 for overall job progress, field 0 for current file.
  (The middle-field purpose confused even experienced devs; both 0 and 1 oscillate as sub-tasks start/stop.)

Useful options: `--progress=-same` redirects progress to same stream as messages;
`--messages=-stdout` / `--messages=-none` control MSG routing; `--minlength=120` filter short titles;
`--cache=16` MB read buffer; `--noscan` skip initial disc scan.

### 1.5 Binary names/paths

- Linux: `/usr/bin/makemkvcon` (docker images often `/opt/makemkv/bin/makemkvcon`)
- Windows: `makemkvcon64.exe` (64-bit) or `makemkvcon.exe`, in `%ProgramFiles(x86)%\MakeMKV` or `%ProgramFiles%\MakeMKV`
- Exit code: 0 on success. WARNING: community rippers (ARM) treat exit code 0 alone as insufficient —
  makemkvcon can exit 0 after `MSG:5037` (partial rip, N failed). Parse final MSG 5036 vs 5037 and
  verify output files exist. Exit code 1 = fatal error (bad args, registration expired, etc.).

---

## 2. HandBrakeCLI progress output

### 2.1 Encode progress line (stdout)

Exact format (real capture, HandBrake issue #4745, v1.4.0):

```
Encoding: task 1 of 2, 5.84 %
Encoding: task 1 of 2, 68.13 % (59.39 fps, avg 65.74 fps, ETA 00h00m02s)
```

Pattern: `Encoding: task <n> of <total>, <pct> %` with OPTIONAL suffix
` (<fps> fps, avg <avgfps> fps, ETA <HH>h<MM>m<SS>s)` — early in a pass the parenthetical stats are
absent (no fps measured yet). `task n of total` counts queue jobs/passes, e.g. "task 1 of 2" with
2-pass encoding. Percent always 2 decimals.

Suggested regex (used in the wild):
`/(Encoding|Muxing): task (\d+) of (\d+), (\d+\.?\d*) %( \(([\d.]+) fps, avg ([\d.]+) fps, ETA ([\dhms]+)\))?/`

### 2.2 Scan phase

When scanning input (`-i` without `-t 0` skip, or during job start):

```
Scanning title 1 of 1, preview 2, 20.00 %
Scanning title 1 of 1, preview 10, 100.00 %
[18:41:57] scan: 10 previews, 1440x2498, 30.714 fps, autocrop = 0/0/0/0, aspect 1:1.73, PAR 1:1, color profile: 5-6-5
[18:41:57] libhb: scan thread found 1 valid title(s)
```

Structured alternative: `HandBrakeCLI -i src -t 0 --scan --json` returns scan results as JSON
(much easier than parsing text; highly recommended for title enumeration).

### 2.3 Muxing phase

Task word changes from `Encoding` to `Muxing` (handbrake-js documents task = "Encoding" | "Muxing");
muxing progress lines follow the same `Muxing: task n of n, NN.NN %` pattern. Also look for
`Muxing: this may take awhile...` and the final `Encode done!` / libhb "work: average encoding speed
for job is NNN fps" lines; exit code 0 = success.

### 2.4 Pipe vs tty behavior

- On an interactive console (tty), progress updates are emitted with `\r` (carriage return) so each
  update overwrites the same line; Python readers must split on `\r` as well as `\n`.
- When stdout is redirected to a file/pipe, each progress update is terminated with a newline
  (verified: HandBrake issue #3513 — a redirected log file contains one `Encoding:` line per update).
  So line-based reading works when piped, BUT do not rely on this; split on `\r` and `\n`.
- Windows gotcha (issue #3513): on Windows, most of the libhb log (scan details, job config) is
  written directly to the console and is NOT captured by `>` redirection or pipes; only the
  `Encoding:` progress lines reliably reach redirected stdout. On Linux the full log goes to
  stdout/stderr normally. Use `--json` scan + progress lines for parsing; treat the human log as best-effort.
- Timestamps on log lines: `[HH:MM:SS] message`.

### 2.5 Binary names/paths

- Windows: `HandBrakeCLI.exe`, default `C:\Program Files\HandBrake\HandBrakeCLI.exe`
- Linux: `HandBrakeCLI` (Debian/Ubuntu package `handbrake-cli`, `/usr/bin/HandBrakeCLI`;
  Flatpak: `flatpak run --command=HandBrakeCLI fr.handbrake.ghb`)
- macOS (for reference): `/Applications/HandBrake.app/Contents/MacOS/HandBrakeCLI`

---

## 3. Jellyfin movie naming rules

Source: <https://jellyfin.org/docs/general/server/media/movies> (read in full).

- Library type "Movies". Supported: mp4, mkv, `VIDEO_TS`/`BDMV` folders (no multi-version/parts/
  external subs on those). `.iso` works but is unsupported — remux to mkv.
- **One folder per movie.** Folder name: `Movie Name (year) [providerid]` — year and provider id
  optional but recommended. Provider id syntax: `[imdbid-tt12801262]` (also tmdbid).
- **Video file must exactly match the folder name**: `Movie Name (year).mkv`, optionally with
  suffix tags. Examples:
  - `Jellyfin Documentary.mkv`
  - `Jellyfin Documentary (2030).mkv`
  - `Jellyfin Documentary (2030) [imdbid-tt00000000].mkv`
- Reserved characters that WILL cause problems: `< > : " / \ | ? *` (strip/replace in generated names!).
- **Multiple versions**: files in one movie folder, each name = folder name (character-for-character,
  including year/ids) + ` - label` (space-hyphen-space REQUIRED; brackets around label optional):
  `Movie (2021) - 1080p.mp4`, `Movie (2021) - [Directors Cut].mp4`. Without the exact prefix +
  ` - ` they are treated as separate movies.
- **Multiple parts** (stacked): `Movie Name-cd1.mkv`, `Movie Name-cd2.mkv`; part types
  `cd|dvd|part|pt|disc|disk`, separators ` .-_` (optional), number 1..n or a-d. Not combinable with multi-version.
- **Extras**: subfolders named e.g. `extras`, `behind the scenes`, `deleted scenes`, `trailers`,
  `featurettes`, `shorts`, `interviews`, `scenes`, `clips`, `samples`, `other`; or filename suffixes
  `-trailer`, `.trailer`, `-sample`, `-scene`, `-clip`, `-featurette`, `-short`, `-behindthescenes`,
  `-deletedscene`, `-interview`, `-extra`, `-other` (note: most suffixes contain NO spaces,
  exceptions: ` trailer`, ` sample`). Single trailer can be just `trailer.mp4` next to the movie.
- External subs/audio: `Film.en.srt`, `Film.default.en.forced.ass`, flags `default|forced|sdh|cc|hi`.
- **TV episodes (one-liner for series discs)**: `Shows/Series Name (year)/Season 1/Series Name S01E01 Optional Title.mkv`
  — core pattern is `SxxEyy` (optionally `S01E01-E02` for multi-episode); season folders `Season 01` etc.

---

## 4. TMDb search API (`/search/movie`)

Source: developer.themoviedb.org docs + themoviedb.org/talk.

- Endpoint: `GET https://api.themoviedb.org/3/search/movie`
- **Auth (two equivalent options for v3 read calls):**
  1. Header: `Authorization: Bearer <API Read Access Token>` (v4 token; works for v3 AND v4 — recommended)
  2. Query param: `?api_key=<v3 API key>` (v3 only)
  Both are issued FREE from https://www.themoviedb.org/settings/api after creating a free
  themoviedb.org account (requires account signup; "API Key" (v3) and "API Read Access Token" (v4)
  are two DIFFERENT values on that page — don't confuse them).
- **Params:** `query` (required, URL-encoded title text); optional: `include_adult` (bool),
  `language` (e.g. `en-US`), `primary_release_year` (yyyy), `year`, `region`, `page`.
- **Response:** `{ "page": 1, "results": [...], "total_pages": N, "total_results": M }`.
  Each result object includes: `id`, `title`, `original_title`, `original_language`,
  `release_date` ("YYYY-MM-DD", may be empty string), `overview`, `poster_path`, `backdrop_path`,
  `genre_ids`, `popularity`, `vote_average`, `vote_count`, `adult`, `video`.
  Year for Jellyfin naming = first 4 chars of `release_date`. Follow up with
  `GET /3/movie/{id}` for full details (adds `imdb_id`, `runtime`, etc.).
- Errors: 401 (invalid key/token), 404 with `{"status_code":34,"status_message":"The resource you
  requested could not be found.","success":false}` for missing IDs.
- Rate limits: historically ~40 requests / 10 s per IP; treat 429 by backing off
  (respect `Retry-After`).

---

## 5. WebDAV upload (plain HTTP, Nextcloud/ownCloud focus)

Sources: docs.nextcloud.com developer manual (basic.html, chunking.html), Nextcloud community forum.

### 5.1 Minimal ops

Base URL pattern: `https://server/remote.php/dav/files/<userid>/<path>` (Nextcloud/ownCloud).
Auth: HTTP Basic with username + **app password** (recommended over real password).

- **Create nested folders**: `MKCOL /remote.php/dav/files/user/path/to/new/folder` — one MKCOL per
  path segment; MKCOL is NOT recursive (creating `a/b/c` when `a/b` doesn't exist returns
  409 Conflict). Existing folder returns 405 Method Not Allowed — treat 405 as "already exists".
- **Upload file**: `PUT /remote.php/dav/files/user/path/to/file.mkv` with the raw bytes as request
  body. Overwrites any existing file silently. Send `Content-Length` (avoid chunked
  `Transfer-Encoding` on some servers) — in Python `requests`, pass an open file object as `data=`.
- `MOVE`/`COPY` use a full-URL `Destination` header; `Overwrite: T|F` header controls overwrite.
- Useful check: `PROPFIND` (Depth: 0) to test existence; `HEAD`/`GET` to verify after upload.
  Nextcloud also accepts `X-OC-Mtime: <unixtime>` to preserve mtime.

### 5.2 Gotchas

- URL-encode every path segment (spaces, `#`, `?`, non-ASCII break naive URLs).
- 401 = bad credentials; 403 = no permission; 404 = parent missing on PUT; 409 = intermediate
  collection missing on MKCOL; 405 = MKCOL target exists; 507 = quota exceeded.
- Nextcloud ignores/limits PROPPATCH on files for arbitrary properties — don't rely on PROPPATCH to
  set metadata; only a small set (e.g. favorite) is writable. Not needed for plain upload.
- **Large files**: plain single PUT of multi-GB rips frequently fails on Nextcloud/ownCloud due to
  PHP timeouts, reverse-proxy limits (`client_max_body_size`), 2 GB boundary bugs and interrupted
  connections (whole upload lost). Community guidance: third-party clients SHOULD implement chunked
  upload for big files.

### 5.3 Nextcloud chunked upload v2 (recommended for large files)

Docs: docs.nextcloud.com .../client_apis/WebDAV/chunking.html.

1. `MKCOL /remote.php/dav/uploads/<user>/<unique-uuid-dir>` — every request (incl. this one) should
   carry header `Destination: <full url of final file>`.
2. `PUT /remote.php/dav/uploads/<user>/<dir>/<chunkname>` for each chunk. Rules:
   - chunk names must be numbers 1–10000 (assembly order = name order, zero-pad!)
   - chunk size between 5 MB and 5 GB (last chunk may be smaller)
   - header `OC-Total-Length: <total file size>` enables quota pre-check (else 507 surfaces only at MOVE)
3. Assemble: `MOVE /remote.php/dav/uploads/<user>/<dir>/.file` with
   `Destination: https://server/remote.php/dav/files/<user>/dest/file.mkv`.
4. Abort: `DELETE` the upload dir. Upload dirs expire after 24 h of inactivity.

ownCloud legacy chunking (v1) differs: chunk names `<filename>-chunking-<transferid>-<count>-<n>`
PUT directly into the files tree. Detect server type and pick the right scheme.

---

## DEV NOTES (parsing-relevant gotchas)

- **makemkvcon numbers are quoted strings**: `TINFO:0,11,0,"13472686080"` — strip quotes before int().
  Values may contain commas INSIDE quotes (`"12.5 GB"` won't, but `TINFO:0,30,0,"... - 7 chapter(s) , 12.5 GB"`
  does) — use a CSV parser or split respecting quotes, never a naive `line.split(',')`.
- **makemkvcon duration is `H:MM:SS` text** (`TINFO:x,9,0,"0:58:06"`), not seconds. Chapter count is id 8.
- **Disc name**: prefer `CINFO:2` (clean name); `CINFO:32` (volume label, e.g. `BREAKINGBADS1`) is a fallback.
  DRV field 6 also carries the volume label during scan.
- **PRGV max is always 65536**; fields are `current,total,max` — middle field (index 1) is overall/total
  progress. Fields can reset to 0 as sub-tasks change; don't expect monotonic field 0.
- **makemkvcon exit codes**: 0 = OK, 1 = error, BUT exit 0 can still mean a partial rip. Treat the job as
  successful only on `MSG:5036` ("Copy complete. N titles saved."); `MSG:5037` means N saved / M failed;
  `MSG:5010` = failed to open disc (also appears harmlessly at end of `info disc:9999` with `TCOUNT:0`).
- **MSG line layout**: `MSG:code,flags,count,message,format,param0,...` — parse by `code` (locale-stable),
  never by message text; the `format` string is localized and can change. High `flags` values
  (e.g. 16777216 = 0x1000000) indicate error/alert class messages.
- **HandBrake progress stats suffix is optional**: `Encoding: task 1 of 2, 5.84 %` appears early in a pass
  with no `(fps, avg, ETA)` parenthetical — make that whole group optional in regex.
- **HandBrake line endings**: `\r` on tty, newline when piped — split buffers on both `\r` and `\n`.
  On Windows, non-progress log lines may never reach a pipe at all (issue #3513).
- **Locale/decimal separator**: HandBrakeCLI's progress output is locale-independent in practice (always
  `.` decimal — generated by libhb's own snprintf with C locale), but x264's own `[info]` stat lines and
  other tools can be locale-sensitive; force `LC_ALL=C` (Linux) when spawning subprocesses to be safe,
  and never parse localized message text from either tool.
- **HandBrake `--json`**: use `-t 0 --scan --json` for machine-readable scan instead of regexing
  "Scanning title" text; progress lines themselves have no JSON mode on stdout.
- **Jellyfin**: strip `< > : " / \ | ? *` from generated folder/file names; file must match folder name
  EXACTLY (char-for-char) for multi-version grouping; multi-version separator is exactly `" - "`.
- **TMDb**: `release_date` can be `""` (unknown) — guard year extraction; two different credentials
  (v3 `api_key` vs v4 Bearer "API Read Access Token"); Bearer works everywhere, api_key is v3-only.
- **WebDAV MKCOL is not recursive** — iterate path segments, ignoring 405; expect 409 if parent missing.
  URL-encode each segment. Nextcloud plain PUTs of >~2 GB files are unreliable — use chunking v2
  (5 MB–5 GB chunks, numeric names 1–10000, `.file` MOVE to assemble, 24 h upload-dir expiry).
