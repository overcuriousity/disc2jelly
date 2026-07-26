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


# ------------------------------------------------------- drives 503 (#11)


def test_drives_503_when_makemkv_missing(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda: config.Config())
    monkeypatch.setattr(main, "_find_binary", lambda name, configured: None)
    client = TestClient(main.app)
    resp = client.get("/api/drives")
    assert resp.status_code == 503
    assert "error" in resp.json()


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
