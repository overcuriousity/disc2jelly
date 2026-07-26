"""Tests for app.main review fixes: SSE lifecycle, config coercion, 503/409."""
from __future__ import annotations

import asyncio
import json
import queue

import pytest
from fastapi.testclient import TestClient

from app import config, jobs as jobs_mod, main


# ------------------------------------------------------------------ SSE (#5)


class _FakeRequest:
    """Scripted is_disconnected(): returns False `false_count` times, then True."""

    def __init__(self, false_count: int) -> None:
        self._false_count = false_count

    async def is_disconnected(self) -> bool:
        if self._false_count > 0:
            self._false_count -= 1
            return False
        return True


def _fresh_manager(monkeypatch) -> jobs_mod.JobManager:
    mgr = jobs_mod.JobManager(cfg_getter=lambda: None)
    monkeypatch.setattr(main, "manager", mgr)
    return mgr


def test_sse_replay_live_then_disconnect_unsubscribes(monkeypatch):
    mgr = _fresh_manager(monkeypatch)
    mgr.publish({"job_id": "j1", "stage": "RIP", "status": "running",
                 "percent": 5.0, "detail": "ripping"})

    async def collect():
        resp = await main.events(_FakeRequest(false_count=1))
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
            if len(chunks) == 1:
                # Live event after the replay chunk went out.
                mgr.publish({"job_id": "j1", "stage": "ENCODE",
                             "status": "running", "percent": 10.0,
                             "detail": "encoding"})
        return chunks

    chunks = asyncio.run(collect())

    assert len(chunks) == 2
    first = json.loads(chunks[0].removeprefix("data: "))
    second = json.loads(chunks[1].removeprefix("data: "))
    assert first["stage"] == "RIP"      # replayed last-event
    assert second["stage"] == "ENCODE"  # live event
    assert mgr._subscribers == []       # promptly unsubscribed on disconnect


def test_sse_heartbeat_when_idle_then_disconnect(monkeypatch):
    mgr = _fresh_manager(monkeypatch)

    class EmptyQueue:
        def get(self, block=True, timeout=None):
            raise queue.Empty

    monkeypatch.setattr(mgr, "subscribe", lambda: EmptyQueue())

    async def collect():
        resp = await main.events(_FakeRequest(false_count=1))
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect())
    assert chunks == [": heartbeat\n\n"]


# ----------------------------------------------------------- config PUT (#12)


def test_config_put_coerces_int_like_strings(monkeypatch):
    saved = {}
    monkeypatch.setattr(main, "_load_config", lambda: config.Config())
    monkeypatch.setattr(config, "save",
                        lambda cfg, path=None: saved.update(cfg=cfg))
    client = TestClient(main.app)

    resp = client.put("/api/config", json={
        "hevc_quality": "25",
        "h264_quality": "21",
        "min_title_seconds": "700",
    })
    assert resp.status_code == 200, resp.text
    cfg = saved["cfg"]
    assert cfg.hevc_quality == 25 and type(cfg.hevc_quality) is int
    assert cfg.h264_quality == 21 and type(cfg.h264_quality) is int
    assert cfg.min_title_seconds == 700 and type(cfg.min_title_seconds) is int


def test_config_put_rejects_non_numeric_quality(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda: config.Config())
    client = TestClient(main.app)
    resp = client.put("/api/config", json={"hevc_quality": "banana"})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


# ------------------------------------------------------- duplicate 409 (#15)


def test_duplicate_job_rejected_409(monkeypatch):
    mgr = _fresh_manager(monkeypatch)
    monkeypatch.setattr(mgr, "has_active_duplicate", lambda d, t: True)
    client = TestClient(main.app)
    resp = client.post("/api/jobs", json={
        "drive": "disc:0", "titles": [1], "title": "Some Movie",
    })
    assert resp.status_code == 409
    assert resp.json()["error"] == "This disc is already being ripped"


# ------------------------------------------------------- DVD-only API surface


def _cfg(**kw):
    return config.Config(**kw)


def test_drives_needs_no_makemkv(monkeypatch):
    """Drive enumeration is OS-level now; HandBrake is not even required."""
    from app import drives as drives_mod

    monkeypatch.setattr(main, "_load_config", lambda: _cfg())
    monkeypatch.setattr(
        drives_mod, "list_drives",
        lambda: [drives_mod.Drive(device="/dev/sr0", label="THE_MATRIX", has_disc=True)],
    )
    resp = TestClient(main.app).get("/api/drives")
    assert resp.status_code == 200
    assert resp.json() == [
        {"device": "/dev/sr0", "label": "THE_MATRIX", "has_disc": True}
    ]


def test_titles_503_when_handbrake_missing(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda: _cfg())
    monkeypatch.setattr(main, "_find_handbrake", lambda cfg: None)
    resp = TestClient(main.app).get("/api/titles", params={"device": "/dev/sr0"})
    assert resp.status_code == 503
    assert "HandBrake" in resp.json()["error"]


def test_titles_scans_the_named_device(monkeypatch):
    from app import scan as scan_mod

    seen = {}

    def fake_scan(hb, device, min_seconds):
        seen["device"] = device
        seen["min_seconds"] = min_seconds
        return [scan_mod.Title(1, "T", 3486, 7, None, None)]

    monkeypatch.setattr(main, "_load_config", lambda: _cfg(min_title_seconds=300))
    monkeypatch.setattr(main, "_find_handbrake", lambda cfg: "/usr/bin/HandBrakeCLI")
    monkeypatch.setattr(scan_mod, "scan_titles", fake_scan)

    resp = TestClient(main.app).get("/api/titles", params={"device": "/dev/sr0"})
    assert resp.status_code == 200
    assert seen == {"device": "/dev/sr0", "min_seconds": 300}
    assert resp.json()[0]["duration_s"] == 3486


def test_disc_hint_classifies_a_series_label(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda: _cfg())
    resp = TestClient(main.app).get(
        "/api/disc/hint", params={"label": "Breaking Bad: Season 1: Disc 1"}
    )
    assert resp.json() == {
        "kind": "series", "title": "Breaking Bad", "season": 1, "disc": 1
    }


def test_tmdb_search_tv(monkeypatch):
    from app import metadata as md

    monkeypatch.setattr(main, "_load_config", lambda: _cfg(tmdb_api_key="k"))
    monkeypatch.setattr(
        md, "search_shows",
        lambda key, q: [md.ShowMatch(1396, "Breaking Bad", "Breaking Bad", 2008, "")],
    )
    resp = TestClient(main.app).get("/api/tmdb/search", params={"q": "bb", "kind": "tv"})
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "Breaking Bad"


def test_tmdb_season_episodes(monkeypatch):
    from app import metadata as md

    monkeypatch.setattr(main, "_load_config", lambda: _cfg(tmdb_api_key="k"))
    monkeypatch.setattr(
        md, "season_episodes",
        lambda key, tid, season: [md.EpisodeInfo(1, 1, "Pilot")],
    )
    resp = TestClient(main.app).get("/api/tmdb/tv/1396/season/1")
    assert resp.status_code == 200
    assert resp.json() == [{"season": 1, "episode": 1, "name": "Pilot"}]


def test_tmdb_uses_the_baked_key_when_none_is_configured(monkeypatch):
    from app import metadata as md

    monkeypatch.setattr(main, "_load_config", lambda: _cfg(tmdb_api_key=""))
    monkeypatch.setattr(md, "DEFAULT_TMDB_API_KEY", "baked")
    monkeypatch.setattr(md, "search_movies", lambda key, q: [] if key == "baked" else 1 / 0)
    resp = TestClient(main.app).get("/api/tmdb/search", params={"q": "x"})
    assert resp.status_code == 200


def test_movie_job_builds_a_movie_relpath(monkeypatch):
    mgr = _fresh_manager(monkeypatch)
    monkeypatch.setattr(main, "_load_config", lambda: _cfg())
    resp = TestClient(main.app).post("/api/jobs", json={
        "drive": "/dev/sr0", "kind": "movie", "titles": [1],
        "title": "The Matrix", "year": 1999, "tmdb_id": 603,
    })
    assert resp.status_code == 201
    assert resp.json()["targets"] == [{
        "title_index": 1,
        "relpath": "Movies/The Matrix (1999) [tmdbid-603]/"
                   "The Matrix (1999) [tmdbid-603].mkv",
    }]


def test_extra_movie_titles_are_disambiguated(monkeypatch):
    _fresh_manager(monkeypatch)
    monkeypatch.setattr(main, "_load_config", lambda: _cfg())
    resp = TestClient(main.app).post("/api/jobs", json={
        "drive": "/dev/sr0", "kind": "movie", "titles": [1, 5],
        "title": "Two Part", "year": None,
    })
    rels = [t["relpath"] for t in resp.json()["targets"]]
    assert rels == [
        "Movies/Two Part/Two Part.mkv",
        "Movies/Two Part/Two Part - Title 2.mkv",
    ]


def test_series_job_builds_episode_relpaths(monkeypatch):
    _fresh_manager(monkeypatch)
    monkeypatch.setattr(main, "_load_config", lambda: _cfg())
    resp = TestClient(main.app).post("/api/jobs", json={
        "drive": "/dev/sr0", "kind": "series", "title": "Breaking Bad",
        "year": 2008, "tmdb_id": 1396,
        "episodes": [
            {"title_index": 1, "season": 1, "episode": 1, "name": "Pilot"},
            {"title_index": 2, "season": 1, "episode": 2, "name": "Cat's in the Bag..."},
        ],
    })
    assert resp.status_code == 201
    rels = [t["relpath"] for t in resp.json()["targets"]]
    assert rels == [
        "Shows/Breaking Bad (2008) [tmdbid-1396]/Season 01/Breaking Bad S01E01 - Pilot.mkv",
        "Shows/Breaking Bad (2008) [tmdbid-1396]/Season 01/"
        "Breaking Bad S01E02 - Cat's in the Bag....mkv",
    ]


def test_series_job_requires_episodes(monkeypatch):
    _fresh_manager(monkeypatch)
    monkeypatch.setattr(main, "_load_config", lambda: _cfg())
    resp = TestClient(main.app).post("/api/jobs", json={
        "drive": "/dev/sr0", "kind": "series", "title": "Breaking Bad",
        "episodes": [],
    })
    assert resp.status_code == 400


def test_health_reports_handbrake_and_destination(monkeypatch):
    monkeypatch.setattr(main, "_load_config",
                        lambda: _cfg(destination_kind="local", local_path="/tmp/x"))
    monkeypatch.setattr(main, "_find_handbrake", lambda cfg: "/usr/bin/HandBrakeCLI")
    body = TestClient(main.app).get("/api/health").json()
    assert body["binaries"] == {"handbrake": True}
    assert "makemkv" not in body["binaries"]
    assert body["destination_ok"] is True


# ------------------------------------------------------- libdvdcss first run


def test_health_reports_libdvdcss_presence(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda: _cfg())
    monkeypatch.setattr(main, "_find_handbrake", lambda cfg: "/usr/bin/HandBrakeCLI")
    monkeypatch.setattr(main, "_dvdcss_present", lambda: False)
    body = TestClient(main.app).get("/api/health").json()
    assert body["dvdcss_ok"] is False
    assert body["ok"] is False  # encrypted DVDs cannot be read without it


def test_install_libdvdcss_endpoint(monkeypatch):
    from app import dvdcss

    called = {}

    def fake_ensure(dest, emit, **kw):
        called["dest"] = dest
        emit({"stage": "APP", "status": "done", "detail": "installed", "ts": 0})
        return "C:/app/libdvdcss-2.dll"

    monkeypatch.setattr(main, "_load_config", lambda: _cfg())
    monkeypatch.setattr(dvdcss, "ensure_libdvdcss", fake_ensure)
    resp = TestClient(main.app).post("/api/setup/libdvdcss")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert called["dest"] is not None


def test_install_libdvdcss_reports_failure(monkeypatch):
    from app import dvdcss

    def boom(dest, emit, **kw):
        raise dvdcss.DvdCssError("no network")

    monkeypatch.setattr(main, "_load_config", lambda: _cfg())
    monkeypatch.setattr(dvdcss, "ensure_libdvdcss", boom)
    resp = TestClient(main.app).post("/api/setup/libdvdcss")
    assert resp.status_code == 502
    assert "no network" in resp.json()["error"]


def test_dvdcss_present_detects_the_system_library(monkeypatch):
    """Regression: _dvdcss_present must not swallow a NameError and report False."""
    from app import dvdcss

    monkeypatch.setattr(main.sys, "platform", "linux")
    monkeypatch.setattr(dvdcss, "_find_system_library", lambda: "libdvdcss.so.2")
    assert main._dvdcss_present() is True
