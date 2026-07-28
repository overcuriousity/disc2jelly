# Disc2Jelly

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Windows%20&#124;%20Linux-lightgrey.svg)](#requirements)
[![Dependencies](https://img.shields.io/badge/windows%20deps-none-brightgreen.svg)](#requirements)

One-click DVD → Jellyfin ingest, for films **and** TV series. Insert disc, confirm what it is, press **RIP & UPLOAD**. HandBrake reads the DVD directly and encodes in one pass (HEVC by default), everything is named exactly the way Jellyfin wants it, and the result lands in a folder or on your WebDAV share. Local web UI — identical on Windows and Linux.

```
DVD → HandBrake (decrypt via libdvdcss + encode, single pass, HEVC/H.264 MKV)
    → Jellyfin naming (TMDb film or series lookup) → folder or WebDAV → cleanup
```

**On Windows one `setup.exe` brings its own Python and HandBrake.** The one thing it cannot bring is libdvdcss — see below.

### Why DVD only

Blu-ray is deliberately out of scope. AACS is an actively maintained broadcast-encryption scheme with key revocation, stacked with the BD+ virtual machine — there is no FOSS path that works reliably, and every tool that does (MakeMKV, AnyDVD HD, DVDFab) is proprietary, paid, and needs a key refreshed every couple of months. DVD's CSS, by contrast, has been cryptographically dead since 1999: libdvdcss breaks it with no key database, on every disc, forever. That difference is what makes a genuinely dependency-free build possible.

## Requirements

**Windows:** the installer covers everything except libdvdcss, which is required to read
encrypted DVDs — that is, essentially every commercial disc. Put `libdvdcss-2.dll` next to
`Disc2Jelly.exe` in the install folder and restart; the app shows the exact path when it is
missing.

It is not bundled, and cannot be downloaded automatically, because there is no official
build to download: VideoLAN publishes libdvdcss as [source releases](https://download.videolan.org/pub/libdvdcss/)
only, and VLC's own Windows build links it statically rather than shipping a standalone DLL.
Build it from source, or copy the file from a player installation that already has one.

**Linux** (runs from source):

| Component | Package |
|---|---|
| Python ≥ 3.11 | `python3`, `python3-venv` |
| HandBrakeCLI | `handbrake-cli` (Debian/Ubuntu), `HandBrake-cli` (Fedora/RPM Fusion) |
| libdvdcss | `libdvdcss2` via `libdvd-pkg` (Debian/Ubuntu), `libdvdcss` (Fedora/RPM Fusion) |

A TMDb API key is optional — without one, type film and series names by hand.

## Install & start

**Windows:** run `Disc2Jelly-Setup-*.exe`. It asks once where finished files should go, then puts a shortcut on the desktop.

> The installer is unsigned, so Windows SmartScreen shows "Windows protected your PC" the first time. Click **More info** → **Run anyway**. Avoiding that warning needs a paid code-signing certificate.

**Linux:**
```bash
cd disc2jelly
./start_linux.sh        # creates venv, installs deps, starts app, opens browser
```

The app opens at http://127.0.0.1:8642.

## Building the Windows installer

Must be built **on Windows** — PyInstaller cannot cross-compile.

```powershell
copy build_config.example.toml build_config.toml   # fill in TMDb key, destination
powershell -ExecutionPolicy Bypass -File build\build_windows.ps1
```

Needs [Inno Setup 6](https://jrsoftware.org/isdl.php). Output lands in `dist\`.

If the HandBrakeCLI download fails with `CERTIFICATE_VERIFY_FAILED` (a
TLS-inspecting proxy or antivirus, or a Windows cert store missing the chain),
download the zip in a browser and hand it over — the pinned checksum is still
enforced:

```powershell
powershell -ExecutionPolicy Bypass -File build\build_windows.ps1 -HandBrakeArchive C:\path\HandBrakeCLI-1.9.2-win-x86_64.zip
```

HandBrakeCLI is pinned to a SHA-256 in `build/fetch_deps.py`, which refuses to build if the
download does not match. To move to a new HandBrake release, bump `HANDBRAKE_VERSION`, run
`python build\fetch_deps.py --print-hashes`, and paste the printed digest into
`HANDBRAKE_SHA256`.

### A note on credentials

`build_windows.ps1` bakes the TMDb key, WebDAV URL and username in — none of those are secret. Every value in `build_config.toml` also pre-fills the installer wizard, and **each wizard page is skipped once nothing on it is unanswered**: fill in `destination_kind = "webdav"` plus `webdav_url`, `webdav_user` and `webdav_password` and the installer asks the end user nothing at all.

That last one is the trade-off to understand. A `webdav_password` in `build_config.toml` is compiled into `Setup.exe` and recoverable from it with `strings` — nothing obfuscates it. Leave it empty (the default) and the wizard collects the password on the target machine instead, which is the only version safe to hand to anyone else. For a private build, use an app password scoped to the destination share and keep the installer to yourself. `-BakePassword` additionally compiles it into `Disc2Jelly.exe`; with the wizard path above it is rarely needed.

## First-time setup (2 minutes, once per PC)

On Windows the installer already did this. Otherwise:

1. Click the **gear icon** (Settings).
2. **Destination**: either a folder (including a mapped drive or `\\nas\media`) or a WebDAV URL with user + app password.
   - Point your Jellyfin libraries at the same place. Disc2Jelly creates `Movies/…` and `Shows/…` underneath.
   - Nextcloud/ownCloud works out of the box. For a plain WebDAV server on your Jellyfin box (rclone behind nginx, ~10 minutes), see [docs/webdav-server.md](docs/webdav-server.md).
3. **TMDb API key**: paste a v3 key or v4 token — enables automatic film and series detection from the disc label.
4. Save. All four status dots should be green.

Temp space is small now: the single-pass pipeline never writes a raw disc image, only the finished file, one at a time.

## Daily use (the wife workflow)

1. Insert disc. The disc panel updates automatically (or click **Rescan**).
2. The app shows the main title (longest) and its best TMDb guess: **"We think this is: Movie (Year)"**.
   - Wrong guess? Click **Search other** or type the title manually.
3. Choose profile: **Smaller file (HEVC)** (default) or **Maximum compatibility (H.264)**.
4. Press **RIP & UPLOAD**. Watch the three bars (Rip → Encode → Upload). Done — the movie appears in Jellyfin after its next library scan (or trigger a scan in Jellyfin).

Extras/bonus titles: tick additional titles before starting; they land in the film's folder under a disambiguated name.

### TV series

Switch the disc panel to **TV episodes**. Disc2Jelly reads `SEASON`/`S01`/`DISC` markers off the volume label and preselects the mode and season number, then:

1. Confirm the series (TMDb TV search).
2. Set the season and the first episode number on this disc — press **Number them** to fill the rest in disc order.
3. Episode titles from TMDb appear beside each row, so a wrong ordering is visible before anything is encoded. Adjust any number by hand.
4. Press **RIP & UPLOAD**.

Files land as `Shows/<Series> (<Year>) [tmdbid-<id>]/Season 01/<Series> S01E01 - <Episode>.mkv`.

Titles the scanner detects as duplicates (DVDs routinely expose the main feature more than once) are flagged and start unticked, rather than being dropped — on a season disc, losing a real episode is worse than showing an extra row.

## Encoding defaults

- Container: MKV. Video: x265 RF 22 (HEVC) or x264 RF 20. Audio: all tracks passthrough. Subtitles: all, none burned in. Chapter markers kept.
- RF 22 HEVC typically lands a DVD main feature at ~1–2 GB with visually transparent quality.
- Change quality in Settings (`hevc_quality` / `h264_quality`, lower = better/bigger).

## Troubleshooting

- **Red "Disc reader" dot**: libdvdcss is missing, and the banner names the folder it belongs in. Windows: put `libdvdcss-2.dll` there and restart. Linux: install your distro's `libdvdcss` package.
- **Red "Movie shrinker" dot**: HandBrakeCLI not found. Linux: install `handbrake-cli`, or set the path in Settings (`handbrake_path`).
- **No disc found**: Linux — check the user can access the optical drive (`cdrom` group / udev rules).
- **Upload fails with quota/auth errors**: check Settings → Test server connection; use an app password, not your main password.
- **Progress bar stalls on "Analyzing source"**: HandBrake is scanning the disc, normal for 1–3 min.
- **A scratched disc fails partway**: the single-pass pipeline has no intermediate file to retry from. HandBrake pushes through most read errors with artefacts, but a badly damaged disc will lose the job.
- Logs: open the log pane at the bottom of the page; every raw tool line is there.

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests -q
python -m app.main
```
SPEC.md = architecture contract. info.md = verified CLI/API format notes.
