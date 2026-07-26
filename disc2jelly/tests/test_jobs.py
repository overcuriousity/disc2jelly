"""Tests for app.jobs.JobManager using injected FAKE pipeline stages.

No real makemkv/handbrake/metadata/webdav modules are needed — the manager's
stage callables are injected (JobManager(cfg_getter, stages=...)).
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.jobs import JobManager, Stage, StageFuncs


# ------------------------------------------------------------------ fakes


class FakeStages:
    """Configurable fake pipeline stages."""

    def __init__(self, fail_encode_calls: set[int] | None = None,
                 rip_waits_for_cancel: bool = False) -> None:
        import threading

        self.uploads: list[tuple[str, str, str]] = []  # (file name, rel dest, job id)
        self.rip_calls = 0
        self.encode_calls = 0
        self.fail_encode_calls = fail_encode_calls or set()
        self.rip_waits_for_cancel = rip_waits_for_cancel
        self.rip_started = threading.Event()

    def funcs(self) -> StageFuncs:
        return StageFuncs(rip=self.rip, encode=self.encode,
                          upload=self.upload, relpath=self.relpath)

    # signature mirrors the bound makemkv.rip
    def rip(self, drive_id: str, title: int, out_dir: Path, emit, cancel) -> Path:
        import threading  # noqa: F401  (kept for parity with real signature)

        self.rip_calls += 1
        self.rip_started.set()
        emit({"status": "running", "percent": 50.0, "detail": "Saving title"})
        if self.rip_waits_for_cancel:
            cancel.wait(timeout=5.0)
            raise RuntimeError("cancelled by user")
        out_dir.mkdir(parents=True, exist_ok=True)
        produced = out_dir / f"title{title}.mkv"
        produced.write_bytes(b"FAKE-MKV")
        return produced

    # signature mirrors the bound handbrake.encode
    def encode(self, src: Path, dst: Path, profile: str, quality: int,
               emit, cancel) -> Path:
        self.encode_calls += 1
        if self.encode_calls in self.fail_encode_calls:
            raise RuntimeError("encoder exploded")
        emit({"status": "running", "percent": 42.0, "fps": 98.2,
              "eta": "00:12:31", "detail": "Encoding"})
        dst.write_bytes(b"FAKE-ENCODED")
        return dst

    # signature mirrors WebDAVClient.upload
    def upload(self, local: Path, rel_dest: str, emit, cancel, job_id: str) -> None:
        self.uploads.append((Path(local).name, rel_dest, job_id))
        emit({"status": "running", "percent": 100.0, "detail": "Uploading"})

    # signature mirrors metadata.jellyfin_movie_relpath
    def relpath(self, title: str, year: int | None, tmdb_id: int | None) -> Path:
        name = title
        if year:
            name += f" ({year})"
        if tmdb_id:
            name += f" [tmdbid-{tmdb_id}]"
        return Path("Movies") / name / f"{name}.mkv"


def make_cfg(tmp_path: Path, keep_mkv: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        temp_dir=str(tmp_path / "work"),
        keep_mkv=keep_mkv,
        hevc_quality=22,
        h264_quality=20,
        webdav_url="https://nas.example/dav",
        webdav_user="u",
        webdav_password="p",
        makemkv_path="",
        handbrake_path="",
    )


def make_manager(tmp_path: Path, fake: FakeStages, **cfg_kw) -> JobManager:
    cfg = make_cfg(tmp_path, **cfg_kw)
    mgr = JobManager(cfg_getter=lambda: cfg, stages=fake.funcs())
    mgr.start()
    return mgr


def wait_for_status(mgr: JobManager, job_id: str,
                    statuses: set[Stage], timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = mgr.get_job(job_id)
        if job is not None and job.status in statuses:
            return job
        time.sleep(0.01)
    job = mgr.get_job(job_id)
    raise AssertionError(
        f"timed out waiting for {statuses}; status={job and job.status}, "
        f"error={job and job.error}")


TERMINAL = {Stage.DONE, Stage.ERROR, Stage.CANCELLED}


# ------------------------------------------------------------------ tests


def test_happy_path_event_sequence(tmp_path):
    fake = FakeStages()
    mgr = make_manager(tmp_path, fake)
    sub = mgr.subscribe()

    job = mgr.create_job(drive="disc:0", title_indices=[1], tmdb_id=42,
                         movie_title="Fake Movie", year=2020, profile="hevc")
    done = wait_for_status(mgr, job.id, TERMINAL)

    assert done.status is Stage.DONE
    assert done.error is None

    # Drain the subscriber queue and check the event sequence for this job.
    events = []
    while not sub.empty():
        events.append(sub.get_nowait())
    stages = [e["stage"] for e in events if e.get("job_id") == job.id]
    # Collapse consecutive duplicates (progress updates repeat a stage) and
    # check the pipeline order: APP → RIP → ENCODE → UPLOAD → CLEANUP → DONE.
    run = [s for i, s in enumerate(stages) if i == 0 or s != stages[i - 1]]
    assert run == ["APP", "RIP", "ENCODE", "UPLOAD", "CLEANUP", "DONE"]
    assert stages.count("RIP") >= 2  # start + progress + done events flowed
    # Every event carries the sacred fields.
    for e in events:
        if e.get("job_id") == job.id:
            assert e["status"] in ("running", "done", "error", "cancelled")
            assert "ts" in e

    # Upload destination follows the Jellyfin movie relpath.
    assert len(fake.uploads) == 1
    _name, rel_dest, up_job = fake.uploads[0]
    assert rel_dest == ("Movies/Fake Movie (2020) [tmdbid-42]/"
                        "Fake Movie (2020) [tmdbid-42].mkv")
    assert up_job == job.id

    # Temp dir cleaned up (keep_mkv=False).
    assert not (tmp_path / "work" / job.id).exists()

    # list_jobs serializes with last event attached.
    listed = mgr.list_jobs()
    assert listed[0]["id"] == job.id
    assert listed[0]["status"] == "DONE"
    assert listed[0]["last_event"]["stage"] == "DONE"


def test_multi_title_uploads_share_movie_folder(tmp_path):
    fake = FakeStages()
    mgr = make_manager(tmp_path, fake)
    job = mgr.create_job(drive="disc:0", title_indices=[1, 2], tmdb_id=None,
                         movie_title="Two Part", year=None, profile="h264")
    done = wait_for_status(mgr, job.id, TERMINAL)
    assert done.status is Stage.DONE
    dests = [d for _n, d, _j in fake.uploads]
    assert dests == [
        "Movies/Two Part/Two Part.mkv",
        "Movies/Two Part/Two Part - Title 2.mkv",
    ]


def test_encode_error_marks_job_and_queue_continues(tmp_path):
    fake = FakeStages(fail_encode_calls={1})  # first encode (job 1) blows up
    mgr = make_manager(tmp_path, fake)

    job1 = mgr.create_job(drive="disc:0", title_indices=[1], tmdb_id=None,
                          movie_title="Broken", year=None, profile="hevc")
    job2 = mgr.create_job(drive="disc:0", title_indices=[1], tmdb_id=None,
                          movie_title="Fine", year=2001, profile="hevc")

    done1 = wait_for_status(mgr, job1.id, TERMINAL)
    done2 = wait_for_status(mgr, job2.id, TERMINAL)

    assert done1.status is Stage.ERROR
    assert "encoder exploded" in (done1.error or "")
    assert done2.status is Stage.DONE  # queue kept going

    # Only the healthy job uploaded anything.
    assert [j for _n, _d, j in fake.uploads] == [job2.id]

    # An error event went out on the bus for job 1.
    assert mgr._last_events[job1.id]["stage"] == "ERROR"
    assert mgr._last_events[job1.id]["status"] == "error"


def test_cancel_during_rip(tmp_path):
    fake = FakeStages(rip_waits_for_cancel=True)
    mgr = make_manager(tmp_path, fake)

    job = mgr.create_job(drive="disc:0", title_indices=[1], tmdb_id=None,
                         movie_title="Long Rip", year=None, profile="hevc")
    assert fake.rip_started.wait(timeout=5.0), "rip never started"

    assert mgr.cancel(job.id) is True
    done = wait_for_status(mgr, job.id, TERMINAL)
    assert done.status is Stage.CANCELLED
    assert mgr._last_events[job.id]["status"] == "cancelled"

    # Cancelling again / unknown ids is harmless.
    assert mgr.cancel(job.id) is False
    assert mgr.cancel("does-not-exist") is False


def test_cancel_queued_job_before_start(tmp_path):
    fake = FakeStages()
    cfg = make_cfg(tmp_path)
    mgr = JobManager(cfg_getter=lambda: cfg, stages=fake.funcs())
    mgr.start()

    # Block the worker with a long rip so job 2 stays queued.
    fake.rip_waits_for_cancel = True
    job1 = mgr.create_job(drive="disc:0", title_indices=[1], tmdb_id=None,
                          movie_title="First", year=None, profile="hevc")
    assert fake.rip_started.wait(timeout=5.0)
    job2 = mgr.create_job(drive="disc:0", title_indices=[1], tmdb_id=None,
                          movie_title="Second", year=None, profile="hevc")

    assert mgr.cancel(job2.id) is True
    assert mgr.get_job(job2.id).status is Stage.CANCELLED

    # Unblock job 1 and let everything settle; job 2 must stay CANCELLED.
    mgr.cancel(job1.id)
    done1 = wait_for_status(mgr, job1.id, TERMINAL)
    assert done1.status is Stage.CANCELLED
    time.sleep(0.2)  # worker pops job2 and must skip it
    assert mgr.get_job(job2.id).status is Stage.CANCELLED
    assert fake.uploads == []


def test_subscriber_replay_last_events(tmp_path):
    fake = FakeStages()
    mgr = make_manager(tmp_path, fake)
    job = mgr.create_job(drive="disc:0", title_indices=[1], tmdb_id=7,
                         movie_title="Replay Me", year=1999, profile="hevc")
    wait_for_status(mgr, job.id, TERMINAL)

    # A late-joining client replays retained state: exactly one last event
    # per job, and it is the terminal DONE event.
    replay = mgr.last_events()
    by_job = {e["job_id"]: e for e in replay}
    assert by_job[job.id]["stage"] == "DONE"
    assert by_job[job.id]["status"] == "done"
    assert by_job[job.id]["percent"] == 100.0


def test_keep_mkv_preserves_rip_but_removes_encode(tmp_path):
    fake = FakeStages()
    mgr = make_manager(tmp_path, fake, keep_mkv=True)
    job = mgr.create_job(drive="disc:0", title_indices=[3], tmdb_id=None,
                         movie_title="Keeper", year=None, profile="hevc")
    done = wait_for_status(mgr, job.id, TERMINAL)
    assert done.status is Stage.DONE
    work = tmp_path / "work" / job.id
    assert (work / "title1" / "title3.mkv").exists()  # intermediate rip kept
    assert not list(work.glob("*.mkv"))  # encoded file removed after upload


# ------------------------------------------------------------------ review fixes


def test_resolve_binary_delegates_to_config_candidates(monkeypatch):
    """jobs._resolve_binary must use config.py candidates (one source of truth)."""
    from app import config, jobs

    seen = {}

    def fake_find_binary(name, configured, candidates):
        seen["name"] = name
        seen["configured"] = configured
        seen["candidates"] = candidates
        return "/found/makemkvcon"

    monkeypatch.setattr(config, "find_binary", fake_find_binary)
    monkeypatch.setattr(config, "makemkv_candidates", lambda: ["C1", "C2"])
    monkeypatch.setattr(config, "handbrake_candidates", lambda: ["H1"])

    assert jobs._resolve_binary("makemkvcon", "") == "/found/makemkvcon"
    assert seen["candidates"] == ["C1", "C2"]

    assert jobs._resolve_binary("HandBrakeCLI", "") == "/found/makemkvcon"
    assert seen["candidates"] == ["H1"]


def test_default_stages_pass_min_title_seconds(monkeypatch, tmp_path):
    """cfg.min_title_seconds must reach makemkv.rip as minlength=."""
    import threading

    from app import jobs, makemkv

    captured = {}

    def fake_rip(mk_path, drive_id, title, out_dir, emit, cancel,
                 minlength=None):
        captured["minlength"] = minlength
        return tmp_path / "out.mkv"

    monkeypatch.setattr(makemkv, "rip", fake_rip)
    monkeypatch.setattr(jobs, "_resolve_binary",
                        lambda name, configured: f"/fake/{name}")

    cfg = SimpleNamespace(
        makemkv_path="", handbrake_path="",
        webdav_url="https://nas.example/dav/files/u/movies",
        webdav_user="u", webdav_password="p",
        min_title_seconds=321,
    )
    stages = jobs._default_stages(cfg)
    stages.rip("disc:0", 1, tmp_path, lambda ev: None, threading.Event())
    assert captured["minlength"] == 321


def test_has_active_duplicate(tmp_path):
    """Double-submit guard: same drive + titles while non-terminal."""
    fake = FakeStages()
    cfg = make_cfg(tmp_path)
    mgr = JobManager(cfg_getter=lambda: cfg, stages=fake.funcs())
    # Worker NOT started: the job stays queued (non-terminal) forever.
    job = mgr.create_job(drive="disc:0", title_indices=[1, 3], tmdb_id=None,
                         movie_title="Dup", year=None, profile="hevc")

    assert mgr.has_active_duplicate("disc:0", [3, 1]) is True   # order-free
    assert mgr.has_active_duplicate("disc:0", [1]) is False     # different set
    assert mgr.has_active_duplicate("disc:1", [1, 3]) is False  # different drive

    mgr.cancel(job.id)  # -> CANCELLED (terminal)
    assert mgr.has_active_duplicate("disc:0", [1, 3]) is False
