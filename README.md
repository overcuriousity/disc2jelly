# Disc2Jelly

One-click DVD & Blu-ray → Jellyfin ingest. Insert disc → confirm the detected movie → press **RIP & UPLOAD**. The app rips with MakeMKV, encodes with HandBrake (HEVC by default), names everything exactly the way Jellyfin wants it, and uploads to your WebDAV share. Local web UI — identical on Windows and Linux.

```
DVD/Blu-ray → MakeMKV (decrypt+rip) → HandBrake (HEVC/H.264 MKV)
            → Jellyfin naming (TMDb auto / manual) → WebDAV upload → temp cleanup
```

## Requirements

| Component | Windows | Linux |
|---|---|---|
| Python | ≥ 3.11 from python.org (check "Add to PATH") | `python3` + `python3-venv` ( distro pkg ) |
| MakeMKV | https://www.makemkv.com/download/ (installs `makemkvcon`) | distro pkg or makemkv.com Linux build |
| HandBrakeCLI | https://handbrake.fr/downloads2.php (CLI zip, extract e.g. to `C:\Program Files\HandBrake\`) | `handbrake-cli` (Debian/Ubuntu) or Flatpak |
| WebDAV | any WebDAV endpoint (Nextcloud, NAS, …) reachable from both PCs | same |
| TMDb key | free: themoviedb.org account → Settings → API (v3 key or v4 Read Access Token) | same |

MakeMKV is shareware; Blu-ray ripping requires a valid (free beta or paid) key. DVD-only works indefinitely.

## Install & start

**Linux:**
```bash
cd disc2jelly
./start_linux.sh        # creates venv, installs deps, starts app, opens browser
```

**Windows:** double-click `start_windows.bat` (first run creates a venv and installs deps). The app opens at http://127.0.0.1:8642.

## First-time setup (2 minutes, once per PC)

1. Click the **gear icon** (Settings).
2. **WebDAV**: URL pointing *into your files tree*, e.g. `https://nas.example/remote.php/dav/files/yourname/movies-inbox`, user + app password. Click **Test WebDAV** → green dot.
   - Point your Jellyfin "Movies" library at the same folder (mounted on the Jellyfin host). Disc2Jelly creates `Movies/<Title> (<Year>)/` underneath.
3. **TMDb API key**: paste v3 key or v4 token → enables automatic movie detection from the disc label.
4. Leave **temp dir** default unless your system drive is small (a Blu-ray needs ~50 GB temp).
5. Save. All four status dots should be green.

## Daily use (the wife workflow)

1. Insert disc. The disc panel updates automatically (or click **Rescan**).
2. The app shows the main title (longest) and its best TMDb guess: **"We think this is: Movie (Year)"**.
   - Wrong guess? Click **Search other** or type the title manually.
3. Choose profile: **Smaller file (HEVC)** (default) or **Maximum compatibility (H.264)**.
4. Press **RIP & UPLOAD**. Watch the three bars (Rip → Encode → Upload). Done — the movie appears in Jellyfin after its next library scan (or trigger a scan in Jellyfin).

Extras/bonus titles: pick additional titles in the dropdown before starting. Series discs: name episodes manually for now (movie workflow is the primary target).

## Encoding defaults

- Container: MKV. Video: x265 RF 22 (HEVC) or x264 RF 20. Audio: all tracks passthrough. Subtitles: all, none burned in. Chapter markers kept.
- RF 22 HEVC typically lands a DVD main feature at ~1–2 GB and a Blu-ray at ~4–8 GB with visually transparent quality.
- Change quality in Settings (`hevc_quality` / `h264_quality`, lower = better/bigger).

## Troubleshooting

- **Red MakeMKV dot**: install MakeMKV or set the binary path manually in Settings (`makemkv_path`). Linux: check the user can access the optical drive (`sg` group / udev rules).
- **"Registration expired"**: MakeMKV beta key lapsed — update key from the MakeMKV forum.
- **Upload fails with quota/auth errors**: check Settings → Test WebDAV; use an app password, not your main password.
- **Progress bar stalls on "Analyzing source"**: HandBrake scan of a big disc, normal for 1–3 min.
- Logs: open the log pane at the bottom of the page; every raw tool line is there.

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests -q
python -m app.main
```
SPEC.md = architecture contract. info.md = verified CLI/API format notes.
