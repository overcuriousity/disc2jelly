"""FastAPI app for Disc2Jelly — routes + SSE per SPEC §HTTP API.

Sibling modules (config, disc, metadata, webdav) are imported lazily inside
handlers so ``from app.main import app`` never crashes while those modules are
still being built by other coders.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import queue
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .jobs import JobManager

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
HOST = "127.0.0.1"
PORT = 8642
MASKED = "********"

# ------------------------------------------------------------------ config
# Lazy accessors — config.py is Coder A's module; imported per call so the app
# object itself imports cleanly even without it.


def _config_module():
    from . import config  # lazy by design

    return config


def _load_config():
    return _config_module().load()


manager = JobManager(cfg_getter=_load_config)


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager.start()
    yield


app = FastAPI(title="Disc2Jelly", lifespan=lifespan)


# ------------------------------------------------------------------ helpers


def _err(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _find_handbrake(cfg: Any) -> str | None:
    """Delegate to config.py — the single source of truth for binary discovery."""
    return _config_module().resolve_binaries(cfg)


def _tmdb_key(cfg: Any) -> str:
    """Configured TMDb credential, falling back to the build-time baked one."""
    from . import metadata  # lazy

    return metadata.resolve_api_key(getattr(cfg, "tmdb_api_key", "") or "")


def _serialize_dc(obj: Any) -> dict:
    return dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else dict(obj)


# ------------------------------------------------------------------- models


class EpisodeAssignment(BaseModel):
    title_index: int
    season: int
    episode: int
    name: str = ""


class JobCreate(BaseModel):
    drive: str
    kind: str = "movie"  # "movie" | "series"
    tmdb_id: int | None = None
    title: str = Field(min_length=1)
    year: int | None = None
    profile: str = "hevc"
    disc_name: str = ""
    titles: list[int] = Field(default_factory=list)          # movie
    episodes: list[EpisodeAssignment] = Field(default_factory=list)  # series


# ------------------------------------------------------------------- routes


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    try:
        cfg = _load_config()
    except Exception as exc:
        return {"ok": False, "binaries": {"handbrake": False},
                "destination_ok": None, "tmdb_key_set": False,
                "config_ok": False, "error": str(exc)}
    try:
        hb = _find_handbrake(cfg)
    except Exception:
        hb = None
    destination_ok = _check_destination(cfg)
    tmdb_key_set = bool(_tmdb_key(cfg))
    dvdcss_ok, dvdcss_hint = _dvdcss_state()
    ok = bool(hb) and dvdcss_ok and destination_ok is not False
    return {"ok": ok,
            "binaries": {"handbrake": bool(hb)},
            "destination_ok": destination_ok, "tmdb_key_set": tmdb_key_set,
            "dvdcss_ok": dvdcss_ok, "dvdcss_hint": dvdcss_hint,
            "config_ok": True}


def _dvdcss_state() -> tuple[bool, str]:
    """Is CSS decryption available, and if not, what should the user do?

    There is no install path: VideoLAN ships libdvdcss as source only, so the
    app can report and explain but never fetch. See app/dvdcss.py.
    """
    try:
        from . import dvdcss  # lazy

        bundled = _config_module().bundled_dir()
        if dvdcss.is_available(bundled):
            return True, ""
        return False, dvdcss.hint(bundled)
    except Exception as exc:
        return False, f"Could not check for libdvdcss: {exc}"


def _check_destination(cfg: Any) -> bool | None:
    """True/False for a reachable destination, None when nothing is set up."""
    kind = (getattr(cfg, "destination_kind", "") or "local").lower()
    if kind == "webdav":
        if not getattr(cfg, "webdav_url", ""):
            return None
        try:
            ok, _msg = _test_webdav(cfg)
            return ok
        except Exception:
            return False
    try:
        from . import destination  # lazy

        destination.for_config(cfg)
        return True
    except Exception:
        return False


@app.get("/api/drives")
def drives():
    """Enumerate optical drives via OS facilities — no external binary."""
    try:
        from . import drives as drives_mod  # lazy

        return [_serialize_dc(d) for d in drives_mod.list_drives()]
    except Exception as exc:
        return _err(f"Drive scan failed: {exc}", 500)


@app.get("/api/titles")
def titles(device: str = Query(min_length=1)):
    """Scan one optical device for titles. `device` is "/dev/sr0" or "D:\\"."""
    try:
        cfg = _load_config()
        hb = _find_handbrake(cfg)
        if not hb:
            return _err("HandBrakeCLI not found", 503)
        from . import scan  # lazy

        found = scan.scan_titles(hb, device,
                                 getattr(cfg, "min_title_seconds", 600))
        return [_serialize_dc(t) for t in found]
    except Exception as exc:
        return _err(f"Reading titles failed: {exc}", 500)


@app.get("/api/disc/hint")
def disc_hint(label: str = ""):
    """Classify a disc label as movie or series; drives the UI's default mode."""
    from . import metadata  # lazy

    return _serialize_dc(metadata.parse_disc_label(label))


@app.get("/api/tmdb/search")
def tmdb_search(q: str = Query(min_length=1), kind: str = "movie"):
    try:
        cfg = _load_config()
        key = _tmdb_key(cfg)
        if not key:
            return _err("TMDb API key not configured — open Settings", 400)
        from . import metadata  # lazy

        # clean_query turns raw disc labels ("THE_MATRIX_16X9") into a useful
        # search string; harmless for already-clean manual queries.
        try:
            query = metadata.clean_query(q) or q
        except Exception:
            query = q
        if kind == "tv":
            return [_serialize_dc(s) for s in metadata.search_shows(key, query)]
        return [_serialize_dc(m) for m in metadata.search_movies(key, query)]
    except Exception as exc:
        return _err(f"TMDb search failed: {exc}", 502)


@app.get("/api/tmdb/tv/{tmdb_id}/season/{season}")
def tmdb_season(tmdb_id: int, season: int):
    try:
        cfg = _load_config()
        key = _tmdb_key(cfg)
        if not key:
            return _err("TMDb API key not configured — open Settings", 400)
        from . import metadata  # lazy

        return [_serialize_dc(e) for e in metadata.season_episodes(key, tmdb_id, season)]
    except Exception as exc:
        return _err(f"TMDb season lookup failed: {exc}", 502)


@app.get("/api/jobs")
def jobs_list():
    return manager.list_jobs()


def _build_targets(body: JobCreate) -> list:
    """Turn the request into TitleTargets, resolving Jellyfin paths here.

    Naming stays server-side so the client never dictates a filesystem path.
    """
    from . import metadata  # lazy
    from .jobs import TitleTarget

    title = body.title.strip()
    targets = []
    if body.kind == "series":
        # Episode numbers come from editable inputs in the UI, so two rows can
        # carry the same one. That yields one relpath for two encodes and the
        # second silently overwrites the first — refuse instead. Duplicate
        # title_index is fine: the same disc title can legitimately be sent to
        # two episode slots.
        seen: set[tuple[int, int]] = set()
        for ep in body.episodes:
            slot = (ep.season, ep.episode)
            if slot in seen:
                raise ValueError(
                    f"Season {ep.season} episode {ep.episode} is assigned twice; "
                    "give each ticked title its own episode number"
                )
            seen.add(slot)
            targets.append(TitleTarget(
                title_index=ep.title_index,
                relpath=metadata.jellyfin_episode_relpath(
                    series=title, year=body.year, tmdb_id=body.tmdb_id,
                    season=ep.season, episode=ep.episode, ep_title=ep.name,
                ).as_posix(),
            ))
    else:
        base = metadata.jellyfin_movie_relpath(title, body.year, body.tmdb_id)
        for i, index in enumerate(body.titles, start=1):
            # Extra titles (bonus features) share the movie folder under a
            # disambiguated name so Jellyfin still matches the main feature.
            rel = base if i == 1 else base.parent / f"{base.stem} - Title {i}{base.suffix}"
            targets.append(TitleTarget(title_index=index, relpath=rel.as_posix()))

    # Belt and braces: whatever the naming rules do, two targets must never
    # resolve to the same file.
    paths = [t.relpath for t in targets]
    if len(set(paths)) != len(paths):
        raise ValueError("Two titles would be written to the same file")
    return targets


@app.post("/api/jobs", status_code=201)
def jobs_create(body: JobCreate):
    if body.profile not in ("hevc", "h264"):
        return _err("profile must be 'hevc' or 'h264'", 400)
    if body.kind not in ("movie", "series"):
        return _err("kind must be 'movie' or 'series'", 400)
    if body.kind == "series" and not body.episodes:
        return _err("A series job needs at least one episode assignment", 400)
    if body.kind == "movie" and not body.titles:
        return _err("A movie job needs at least one title", 400)

    try:
        targets = _build_targets(body)
    except Exception as exc:
        return _err(f"Could not build destination paths: {exc}", 400)

    if manager.has_active_duplicate(body.drive, [t.title_index for t in targets]):
        return _err("This disc is already being ripped", 409)
    try:
        job = manager.create_job(
            drive=body.drive,
            targets=targets,
            tmdb_id=body.tmdb_id,
            display_title=body.title.strip(),
            year=body.year,
            profile=body.profile,
            disc_name=body.disc_name,
        )
    except Exception as exc:
        return _err(f"Could not create job: {exc}", 500)
    return job.serialize()


@app.post("/api/jobs/{job_id}/cancel")
def jobs_cancel(job_id: str):
    if manager.get_job(job_id) is None:
        return _err("Job not found", 404)
    return {"ok": manager.cancel(job_id)}


@app.get("/api/events")
async def events(request: Request):
    """SSE stream: replay last event per job, then live events, 15s heartbeat."""
    q = manager.subscribe()

    async def stream():
        try:
            for ev in manager.last_events():
                yield f"data: {json.dumps(ev)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    # Offloaded so client disconnects (which close this
                    # generator) do not strand a blocking worker thread.
                    ev = await asyncio.to_thread(q.get, True, 15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            manager.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------ config


@app.get("/api/config")
def config_get():
    try:
        cfg = _load_config()
    except Exception as exc:
        return _err(f"Could not load settings: {exc}", 500)
    data = _serialize_dc(cfg)
    if data.get("webdav_password"):
        data["webdav_password"] = MASKED
    return data


@app.put("/api/config")
async def config_put(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON body", 400)
    try:
        current = _load_config()
        fields = {f.name for f in dataclasses.fields(current)}
        values = _serialize_dc(current)
        for key, value in body.items():
            if key in fields:
                values[key] = value
        if body.get("webdav_password") == MASKED:
            values["webdav_password"] = getattr(current, "webdav_password", "")
        # Coerce int-like strings ("22" -> 22) so a JSON-stringified form
        # value does not get saved as a string into the typed Config.
        for key in ("hevc_quality", "h264_quality", "min_title_seconds"):
            value = values.get(key)
            if isinstance(value, str):
                try:
                    values[key] = int(value.strip())
                except ValueError:
                    pass  # _validate_config flags non-numeric input
    except Exception as exc:
        return _err(f"Could not read settings: {exc}", 500)

    errors = _validate_config(values)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    try:
        cfg = dataclasses.replace(current, **{
            k: v for k, v in values.items() if k in
            {f.name for f in dataclasses.fields(current)}})
        _config_module().save(cfg)
    except Exception as exc:
        return _err(f"Could not save settings: {exc}", 500)
    return {"ok": True, "errors": []}


def _validate_config(values: dict) -> list[str]:
    errors: list[str] = []
    if values.get("encoder") not in ("hevc", "h264"):
        errors.append("encoder must be 'hevc' or 'h264'")
    for key in ("hevc_quality", "h264_quality"):
        try:
            q = int(values.get(key, 0))
            if not 0 <= q <= 63:
                errors.append(f"{key} must be between 0 and 63")
        except (TypeError, ValueError):
            errors.append(f"{key} must be a number")
    try:
        if int(values.get("min_title_seconds", 0)) < 0:
            errors.append("min_title_seconds must be ≥ 0")
    except (TypeError, ValueError):
        errors.append("min_title_seconds must be a number")
    url = (values.get("webdav_url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        errors.append("webdav_url must start with http:// or https://")
    return errors


def _test_webdav(cfg: Any) -> tuple[bool, str]:
    from . import webdav  # lazy

    client = webdav.WebDAVClient(
        getattr(cfg, "webdav_url", "") or "",
        getattr(cfg, "webdav_user", "") or "",
        getattr(cfg, "webdav_password", "") or "",
    )
    return client.test_connection()


@app.post("/api/config/test-webdav")
def config_test_webdav():
    try:
        cfg = _load_config()
    except Exception as exc:
        return _err(f"Could not load settings: {exc}", 500)
    if not getattr(cfg, "webdav_url", ""):
        return _err("WebDAV server address is empty — fill it in and Save first", 400)
    try:
        ok, message = _test_webdav(cfg)
        return {"ok": bool(ok), "message": message}
    except Exception as exc:
        return _err(f"WebDAV test failed: {exc}", 502)


# ------------------------------------------------------------------- static

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------- run


def main() -> None:
    import uvicorn

    manager.start()
    threading.Timer(
        1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")
    ).start()
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
