"""Tests for app.jobs.JobManager using injected FAKE pipeline stages.

The rip stage is gone: HandBrake reads the disc directly, so a job is a list
of TitleTargets (title index + destination relpath) that each get encoded and
sent. Stage callables are injected via JobManager(cfg_getter, stages=...).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.jobs import JobManager, Stage, StageFuncs, TitleTarget


# ------------------------------------------------------------------ fakes


class FakeStages:
    """Configurable fake pipeline stages."""

    def __init__(self, fail_encode_calls: set[int] | None = None,
                 encode_waits_for_cancel: bool = False) -> None:
        self.sends: list[tuple[str, str, str]] = []  # (file name, rel dest, job id)
        self.encodes: list[tuple[str, int, str]] = []  # (source, title, dst name)
        self.encode_calls = 0
        self.fail_encode_calls = fail_encode_calls or set()
        self.encode_waits_for_cancel = encode_waits_for_cancel
        self.encode_started = threading.Event()

    def funcs(self) -> StageFuncs:
        return StageFuncs(encode=self.encode, send=self.send)

    # signature mirrors the bound handbrake.encode
    def encode(self, source: str, title_index: int, dst: Path, profile: str,
               quality: int, emit, cancel) -> Path:
        self.encode_calls += 1
        self.encodes.append((str(source), title_index, Path(dst).name))
        self.encode_started.set()
        if self.encode_waits_for_cancel:
            cancel.wait(timeout=5.0)
            raise RuntimeError("cancelled by user")
        if self.encode_calls in self.fail_encode_calls:
            raise RuntimeError("encoder exploded")
        emit({"status": "running", "percent": 42.0, "fps": 98.2,
              "eta": "00:12:31", "detail": "Encoding"})
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"FAKE-ENCODED")
        return dst

    # signature mirrors destination.Destination.send
    def send(self, local: Path, rel_dest: str, emit, cancel, job_id: str) -> None:
        self.sends.append((Path(local).name, rel_dest, job_id))
        emit({"status": "running", "percent": 100.0, "detail": "Saving"})


def make_cfg(tmp_path: Path, **kw) -> SimpleNamespace:
    base = dict(
        temp_dir=str(tmp_path / "work"),
        hevc_quality=22,
        h264_quality=20,
        destination_kind="local",
        local_path=str(tmp_path / "out"),
        webdav_url="https://nas.example/dav",
        webdav_user="u",
        webdav_password="p",
        handbrake_path="",
        min_title_seconds=600,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def make_manager(tmp_path: Path, fake: FakeStages, **cfg_kw) -> JobManager:
    cfg = make_cfg(tmp_path, **cfg_kw)
    mgr = JobManager(cfg_getter=lambda: cfg, stages=fake.funcs())
    mgr.start()
    return mgr


def movie_target(index: int = 1, name: str = "Fake Movie (2020) [tmdbid-42]"):
    return TitleTarget(title_index=index, relpath=f"Movies/{name}/{name}.mkv")


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


def test_stage_enum_has_no_rip_stage() -> None:
    assert not hasattr(Stage, "RIP")


def test_happy_path_event_sequence(tmp_path):
    fake = FakeStages()
    mgr = make_manager(tmp_path, fake)
    sub = mgr.subscribe()

    job = mgr.create_job(drive="/dev/sr0", targets=[movie_target()], tmdb_id=42,
                         display_title="Fake Movie", year=2020, profile="hevc")
    done = wait_for_status(mgr, job.id, TERMINAL)

    assert done.status is Stage.DONE
    assert done.error is None

    events = []
    while not sub.empty():
        events.append(sub.get_nowait())
    stages = [e["stage"] for e in events if e.get("job_id") == job.id]
    # One encode pass now, no RIP: APP -> ENCODE -> UPLOAD -> CLEANUP -> DONE.
    run = [s for i, s in enumerate(stages) if i == 0 or s != stages[i - 1]]
    assert run == ["APP", "ENCODE", "UPLOAD", "CLEANUP", "DONE"]
    assert "RIP" not in stages
    for e in events:
        if e.get("job_id") == job.id:
            assert e["status"] in ("running", "done", "error", "cancelled")
            assert "ts" in e

    assert len(fake.sends) == 1
    _name, rel_dest, up_job = fake.sends[0]
    assert rel_dest == ("Movies/Fake Movie (2020) [tmdbid-42]/"
                        "Fake Movie (2020) [tmdbid-42].mkv")
    assert up_job == job.id

    assert not (tmp_path / "work" / job.id).exists()

    listed = mgr.list_jobs()
    assert listed[0]["id"] == job.id
    assert listed[0]["status"] == "DONE"
    assert listed[0]["last_event"]["stage"] == "DONE"


def test_encode_reads_the_drive_directly_with_the_title_index(tmp_path):
    fake = FakeStages()
    mgr = make_manager(tmp_path, fake)
    job = mgr.create_job(drive="/dev/sr0", targets=[movie_target(index=4)],
                         tmdb_id=None, display_title="X", year=None,
                         profile="hevc")
    wait_for_status(mgr, job.id, TERMINAL)
    source, title, _dst = fake.encodes[0]
    assert source == "/dev/sr0"
    assert title == 4


def test_each_target_carries_its_own_destination_path(tmp_path):
    """A season disc is N targets, each with a full episode relpath."""
    fake = FakeStages()
    mgr = make_manager(tmp_path, fake)
    targets = [
        TitleTarget(1, "Shows/Breaking Bad (2008)/Season 01/Breaking Bad S01E01 - Pilot.mkv"),
        TitleTarget(2, "Shows/Breaking Bad (2008)/Season 01/Breaking Bad S01E02 - Cat.mkv"),
    ]
    job = mgr.create_job(drive="/dev/sr0", targets=targets, tmdb_id=1396,
                         display_title="Breaking Bad", year=2008, profile="hevc")
    done = wait_for_status(mgr, job.id, TERMINAL)

    assert done.status is Stage.DONE
    assert [d for _n, d, _j in fake.sends] == [t.relpath for t in targets]
    assert [t for _s, t, _d in fake.encodes] == [1, 2]


def test_local_file_is_removed_after_each_send(tmp_path):
    """Encode/send run per title so a season disc never stacks up on disk."""
    fake = FakeStages()
    mgr = make_manager(tmp_path, fake)
    targets = [movie_target(1, "A"), movie_target(2, "B")]
    job = mgr.create_job(drive="/dev/sr0", targets=targets, tmdb_id=None,
                         display_title="Two", year=None, profile="h264")
    wait_for_status(mgr, job.id, TERMINAL)
    assert not (tmp_path / "work" / job.id).exists()


def test_encode_error_marks_job_and_queue_continues(tmp_path):
    fake = FakeStages(fail_encode_calls={1})
    mgr = make_manager(tmp_path, fake)

    job1 = mgr.create_job(drive="/dev/sr0", targets=[movie_target()], tmdb_id=None,
                          display_title="Broken", year=None, profile="hevc")
    job2 = mgr.create_job(drive="/dev/sr0", targets=[movie_target()], tmdb_id=None,
                          display_title="Fine", year=2001, profile="hevc")

    done1 = wait_for_status(mgr, job1.id, TERMINAL)
    done2 = wait_for_status(mgr, job2.id, TERMINAL)

    assert done1.status is Stage.ERROR
    assert "encoder exploded" in (done1.error or "")
    assert done2.status is Stage.DONE

    assert [j for _n, _d, j in fake.sends] == [job2.id]
    assert mgr._last_events[job1.id]["stage"] == "ERROR"
    assert mgr._last_events[job1.id]["status"] == "error"


def test_cancel_during_encode(tmp_path):
    fake = FakeStages(encode_waits_for_cancel=True)
    mgr = make_manager(tmp_path, fake)

    job = mgr.create_job(drive="/dev/sr0", targets=[movie_target()], tmdb_id=None,
                         display_title="Long Encode", year=None, profile="hevc")
    assert fake.encode_started.wait(timeout=5.0), "encode never started"

    assert mgr.cancel(job.id) is True
    done = wait_for_status(mgr, job.id, TERMINAL)
    assert done.status is Stage.CANCELLED
    assert mgr._last_events[job.id]["status"] == "cancelled"

    assert mgr.cancel(job.id) is False
    assert mgr.cancel("does-not-exist") is False


def test_cancel_queued_job_before_start(tmp_path):
    fake = FakeStages()
    cfg = make_cfg(tmp_path)
    mgr = JobManager(cfg_getter=lambda: cfg, stages=fake.funcs())
    mgr.start()

    fake.encode_waits_for_cancel = True
    job1 = mgr.create_job(drive="/dev/sr0", targets=[movie_target()], tmdb_id=None,
                          display_title="First", year=None, profile="hevc")
    assert fake.encode_started.wait(timeout=5.0)
    job2 = mgr.create_job(drive="/dev/sr0", targets=[movie_target()], tmdb_id=None,
                          display_title="Second", year=None, profile="hevc")

    assert mgr.cancel(job2.id) is True
    assert mgr.get_job(job2.id).status is Stage.CANCELLED

    mgr.cancel(job1.id)
    done1 = wait_for_status(mgr, job1.id, TERMINAL)
    assert done1.status is Stage.CANCELLED
    time.sleep(0.2)
    assert mgr.get_job(job2.id).status is Stage.CANCELLED
    assert fake.sends == []


def test_subscriber_replay_last_events(tmp_path):
    fake = FakeStages()
    mgr = make_manager(tmp_path, fake)
    job = mgr.create_job(drive="/dev/sr0", targets=[movie_target()], tmdb_id=7,
                         display_title="Replay Me", year=1999, profile="hevc")
    wait_for_status(mgr, job.id, TERMINAL)

    replay = mgr.last_events()
    by_job = {e["job_id"]: e for e in replay}
    assert by_job[job.id]["stage"] == "DONE"
    assert by_job[job.id]["status"] == "done"
    assert by_job[job.id]["percent"] == 100.0


def test_job_serializes_targets_and_display_title(tmp_path):
    fake = FakeStages()
    cfg = make_cfg(tmp_path)
    mgr = JobManager(cfg_getter=lambda: cfg, stages=fake.funcs())
    job = mgr.create_job(drive="D:\\", targets=[movie_target(2, "N")], tmdb_id=5,
                         display_title="Name", year=2001, profile="hevc")
    data = job.serialize()
    assert data["drive"] == "D:\\"
    assert data["display_title"] == "Name"
    assert data["targets"] == [{"title_index": 2, "relpath": "Movies/N/N.mkv"}]


# ------------------------------------------------------------------ wiring


def test_default_stages_bind_handbrake_and_the_destination(monkeypatch, tmp_path):
    """_default_stages must wire handbrake.encode and destination.for_config."""
    from app import destination, handbrake, jobs

    captured = {}

    def fake_encode(hb_path, source, title_index, dst, profile, quality, emit, cancel):
        captured["hb_path"] = hb_path
        captured["source"] = source
        captured["title_index"] = title_index
        return dst

    class FakeDest:
        def send(self, local, rel_dest, emit, cancel, job_id):
            captured["sent"] = rel_dest

    monkeypatch.setattr(handbrake, "encode", fake_encode)
    monkeypatch.setattr(destination, "for_config", lambda cfg: FakeDest())
    monkeypatch.setattr(jobs, "_resolve_handbrake", lambda cfg: "/fake/HandBrakeCLI")

    stages = jobs._default_stages(make_cfg(tmp_path))
    stages.encode("/dev/sr0", 3, tmp_path / "o.mkv", "hevc", 22,
                  lambda ev: None, threading.Event())
    stages.send(tmp_path / "o.mkv", "Movies/A/A.mkv", lambda ev: None,
                threading.Event(), "job1")

    assert captured["hb_path"] == "/fake/HandBrakeCLI"
    assert captured["source"] == "/dev/sr0"
    assert captured["title_index"] == 3
    assert captured["sent"] == "Movies/A/A.mkv"


def test_resolve_handbrake_delegates_to_config(monkeypatch, tmp_path):
    from app import config, jobs

    monkeypatch.setattr(config, "resolve_binaries", lambda cfg: "/found/HandBrakeCLI")
    assert jobs._resolve_handbrake(make_cfg(tmp_path)) == "/found/HandBrakeCLI"


def test_has_active_duplicate(tmp_path):
    """Double-submit guard: same drive + title set while non-terminal."""
    fake = FakeStages()
    cfg = make_cfg(tmp_path)
    mgr = JobManager(cfg_getter=lambda: cfg, stages=fake.funcs())
    targets = [movie_target(1, "A"), movie_target(3, "B")]
    mgr.create_job(drive="/dev/sr0", targets=targets, tmdb_id=None,
                   display_title="Dup", year=None, profile="hevc")

    assert mgr.has_active_duplicate("/dev/sr0", [3, 1]) is True
    assert mgr.has_active_duplicate("/dev/sr0", [1]) is False
    assert mgr.has_active_duplicate("/dev/sr1", [1, 3]) is False
