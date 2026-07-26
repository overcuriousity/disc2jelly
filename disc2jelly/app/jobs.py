"""Job model, queue, and pipeline runner for Disc2Jelly.

Owns the SACRED event bus: ``subscribe() -> queue.Queue`` / ``publish(event)``.
Sibling modules (makemkv, handbrake, metadata, webdav, config) are imported
lazily inside ``_default_stages`` so this module — and its tests — work even
when those modules do not exist yet. Tests inject fake stage callables via
``JobManager(cfg_getter, stages=StageFuncs(...))``.
"""
from __future__ import annotations

import queue
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

EmitFn = Callable[[dict], None]


class Stage(Enum):
    DETECT = "DETECT"
    IDENTIFY = "IDENTIFY"
    RIP = "RIP"
    ENCODE = "ENCODE"
    UPLOAD = "UPLOAD"
    CLEANUP = "CLEANUP"
    DONE = "DONE"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


TERMINAL_STAGES = {Stage.DONE, Stage.ERROR, Stage.CANCELLED}


class CancelledError(Exception):
    """Raised internally when a job's cancel event is set between stages."""


@dataclass
class Job:
    """In-memory job per SPEC §Pipeline & job model."""

    id: str
    disc_name: str
    drive: str
    title_indices: list[int]
    tmdb_id: int | None
    movie_title: str
    year: int | None
    profile: str  # "hevc" | "h264"
    status: Stage
    created: float
    error: str | None = None
    cancel_event: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )

    def serialize(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "disc_name": self.disc_name,
            "drive": self.drive,
            "title_indices": list(self.title_indices),
            "tmdb_id": self.tmdb_id,
            "movie_title": self.movie_title,
            "year": self.year,
            "profile": self.profile,
            "status": self.status.value,
            "created": self.created,
            "error": self.error,
        }


@dataclass
class StageFuncs:
    """Injectable pipeline stage callables.

    Signatures (bound forms — binary paths / WebDAV credentials are already
    resolved by the JobManager from the current Config):
      rip(drive_id: str, title: int, out_dir: Path, emit, cancel) -> Path
      encode(src: Path, dst: Path, profile: str, quality: int, emit, cancel) -> Path
      upload(local: Path, rel_dest: str, emit, cancel, job_id: str) -> None
      relpath(title: str, year: int | None, tmdb_id: int | None) -> Path
    """

    rip: Callable[[str, int, Path, EmitFn, threading.Event], Path]
    encode: Callable[[Path, Path, str, int, EmitFn, threading.Event], Path]
    upload: Callable[[Path, str, EmitFn, threading.Event, str], None]
    relpath: Callable[[str, int | None, int | None], Path]


def _resolve_binary(name: str, configured: str) -> str:
    """Resolve a binary path via config.find_binary, with graceful fallbacks.

    Candidate paths come from config.py — the single source of truth for
    binary discovery (handles makemkvcon64.exe and env-var expansion).
    """
    try:
        from . import config as _config  # lazy: may not exist yet

        candidates = (_config.makemkv_candidates() if "makemkv" in name.lower()
                      else _config.handbrake_candidates())
        found = _config.find_binary(name, configured, candidates)
        if found:
            return found
    except Exception:
        pass
    if configured:
        return configured
    return shutil.which(name) or name


def _default_stages(cfg: Any) -> StageFuncs:
    """Build the real pipeline stages from the current config (lazy imports)."""
    from . import handbrake, makemkv, metadata, webdav  # lazy by design

    mk_path = _resolve_binary("makemkvcon", getattr(cfg, "makemkv_path", "") or "")
    hb_path = _resolve_binary("HandBrakeCLI", getattr(cfg, "handbrake_path", "") or "")
    min_title_seconds = getattr(cfg, "min_title_seconds", 600)
    client = webdav.WebDAVClient(
        getattr(cfg, "webdav_url", "") or "",
        getattr(cfg, "webdav_user", "") or "",
        getattr(cfg, "webdav_password", "") or "",
    )

    def rip(drive_id: str, title: int, out_dir: Path, emit: EmitFn,
            cancel: threading.Event) -> Path:
        return makemkv.rip(mk_path, drive_id, title, out_dir, emit, cancel,
                           minlength=min_title_seconds)

    def encode(src: Path, dst: Path, profile: str, quality: int, emit: EmitFn,
               cancel: threading.Event) -> Path:
        return handbrake.encode(hb_path, src, dst, profile, quality, emit, cancel)

    def upload(local: Path, rel_dest: str, emit: EmitFn,
               cancel: threading.Event, job_id: str) -> None:
        client.upload(local, rel_dest, emit, cancel, job_id)

    return StageFuncs(rip=rip, encode=encode, upload=upload,
                      relpath=metadata.jellyfin_movie_relpath)


def _work_root(cfg: Any) -> Path:
    """Temp work root: cfg.temp_dir, else <config dir>/work, else ~/.disc2jelly/work."""
    temp = (getattr(cfg, "temp_dir", "") or "").strip()
    if temp:
        return Path(temp).expanduser()
    try:
        from . import config as _config  # lazy

        return _config.config_path().parent / "work"
    except Exception:
        return Path.home() / ".disc2jelly" / "work"


class JobManager:
    """Sequential job queue + worker thread + event bus (SACRED contract)."""

    def __init__(self, cfg_getter: Callable[[], Any],
                 stages: StageFuncs | None = None) -> None:
        self._cfg_getter = cfg_getter
        self._stages = stages  # None → built per job from current cfg
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._pending: queue.Queue[str] = queue.Queue()
        self._subscribers: list[queue.Queue] = []
        self._last_events: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    # ------------------------------------------------------------------ API

    def has_active_duplicate(self, drive: str,
                             title_indices: list[int]) -> bool:
        """True if a non-terminal job already targets this (drive, titles)."""
        wanted = frozenset(title_indices)
        with self._lock:
            return any(
                existing.status not in TERMINAL_STAGES
                and existing.drive == drive
                and frozenset(existing.title_indices) == wanted
                for existing in self._jobs.values()
            )

    def create_job(self, drive: str, title_indices: list[int],
                   tmdb_id: int | None, movie_title: str, year: int | None,
                   profile: str, disc_name: str = "") -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            disc_name=disc_name,
            drive=drive,
            title_indices=list(title_indices),
            tmdb_id=tmdb_id,
            movie_title=movie_title,
            year=year,
            profile=profile,
            status=Stage.DETECT,  # queued, waiting for the worker
            created=time.time(),
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        self.publish({
            "job_id": job.id,
            "stage": "APP",
            "status": "running",
            "percent": None,
            "detail": f"Queued: {movie_title}",
        })
        self._pending.put(job.id)
        return job

    def list_jobs(self) -> list[dict]:
        """Serialized jobs with their last event attached."""
        with self._lock:
            jobs = [self._jobs[jid] for jid in self._order]
            return [
                {**job.serialize(), "last_event": self._last_events.get(job.id)}
                for job in jobs
            ]

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.status in TERMINAL_STAGES:
            return False
        job.cancel_event.set()
        if job.status in (Stage.DETECT, Stage.IDENTIFY):
            # Still queued (worker has not picked it up) → cancel immediately.
            self._finish(job, Stage.CANCELLED, "Cancelled before start")
        else:
            self.publish({
                "job_id": job.id,
                "stage": job.status.value,
                "status": "cancelled",
                "percent": None,
                "detail": "Cancelling…",
            })
        return True

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: dict) -> None:
        """Fan an event out to all subscribers; retain last event per job."""
        ev = dict(event)
        ev.setdefault("ts", time.time())
        job_id = ev.get("job_id")
        with self._lock:
            if job_id:
                self._last_events[job_id] = ev
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass  # slow consumer drops; SSE replay/list_jobs heals state

    def last_events(self) -> list[dict]:
        with self._lock:
            return list(self._last_events.values())

    def start(self) -> None:
        """Start the worker thread (idempotent)."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop, name="disc2jelly-worker", daemon=True
            )
            self._worker.start()

    # -------------------------------------------------------------- worker

    def _worker_loop(self) -> None:
        while True:
            job_id = self._pending.get()
            job = self.get_job(job_id)
            if job is None or job.status in TERMINAL_STAGES:
                continue
            try:
                self._run_pipeline(job)
            except CancelledError:
                self._finish(job, Stage.CANCELLED, "Cancelled")
            except Exception as exc:  # queue continues no matter what
                if job.cancel_event.is_set():
                    self._finish(job, Stage.CANCELLED, "Cancelled")
                else:
                    self._finish(job, Stage.ERROR, str(exc) or exc.__class__.__name__)

    def _emit_for(self, job: Job, stage: Stage) -> EmitFn:
        def emit(event: dict) -> None:
            ev = dict(event)
            ev.setdefault("stage", stage.value)
            ev["job_id"] = job.id
            self.publish(ev)

        return emit

    def _set_running(self, job: Job, stage: Stage, detail: str,
                     percent: float | None = 0.0) -> None:
        job.status = stage
        self.publish({
            "job_id": job.id,
            "stage": stage.value,
            "status": "running",
            "percent": percent,
            "detail": detail,
        })

    def _finish(self, job: Job, stage: Stage, detail: str) -> None:
        job.status = stage
        if stage is Stage.ERROR:
            job.error = detail
        status_word = {Stage.DONE: "done", Stage.ERROR: "error",
                       Stage.CANCELLED: "cancelled"}.get(stage, "running")
        self.publish({
            "job_id": job.id,
            "stage": stage.value,
            "status": status_word,
            "percent": 100.0 if stage is Stage.DONE else None,
            "detail": detail,
        })

    def _check_cancel(self, job: Job) -> None:
        if job.cancel_event.is_set():
            raise CancelledError()

    # ------------------------------------------------------------ pipeline

    def _run_pipeline(self, job: Job) -> None:
        cfg = self._cfg_getter()  # re-read per job: settings edits apply to new jobs
        stages = self._stages or _default_stages(cfg)
        work_dir = _work_root(cfg) / job.id
        work_dir.mkdir(parents=True, exist_ok=True)
        keep_mkv = bool(getattr(cfg, "keep_mkv", False))
        quality = (getattr(cfg, "hevc_quality", 22) if job.profile == "hevc"
                   else getattr(cfg, "h264_quality", 20))

        self._check_cancel(job)
        n = len(job.title_indices)
        encoded: list[tuple[Path, str]] = []  # (encoded file, upload rel path)

        try:
            for i, title in enumerate(job.title_indices, start=1):
                # ---- RIP ----
                self._set_running(job, Stage.RIP,
                                  f"Ripping disc title {i} of {n}")
                rip_emit = self._emit_for(job, Stage.RIP)
                rip_dir = work_dir / f"title{i}"
                rip_dir.mkdir(parents=True, exist_ok=True)
                mkv = stages.rip(job.drive, title, rip_dir, rip_emit,
                                 job.cancel_event)
                self._check_cancel(job)
                rip_emit({"status": "done", "percent": 100.0,
                          "detail": f"Rip of title {i} complete"})

                # ---- ENCODE ----
                rel = Path(stages.relpath(job.movie_title, job.year, job.tmdb_id))
                if i == 1:
                    rel_dest = rel
                else:  # extra titles share the movie folder, disambiguated name
                    rel_dest = rel.parent / f"{rel.stem} - Title {i}{rel.suffix}"
                dst = work_dir / rel_dest.name
                self._set_running(job, Stage.ENCODE,
                                  f"Shrinking movie file {i} of {n}")
                enc_emit = self._emit_for(job, Stage.ENCODE)
                out = Path(stages.encode(Path(mkv), dst, job.profile, quality,
                                         enc_emit, job.cancel_event))
                self._check_cancel(job)
                enc_emit({"status": "done", "percent": 100.0,
                          "detail": f"Encoding of file {i} complete"})
                encoded.append((out, rel_dest.as_posix()))

            # ---- UPLOAD ----
            m = len(encoded)
            for j, (local, rel_dest) in enumerate(encoded, start=1):
                self._set_running(job, Stage.UPLOAD,
                                  f"Saving to server {j} of {m}")
                up_emit = self._emit_for(job, Stage.UPLOAD)
                stages.upload(local, rel_dest, up_emit, job.cancel_event, job.id)
                self._check_cancel(job)
                up_emit({"status": "done", "percent": 100.0,
                         "detail": f"Upload {j} of {m} complete"})

            # ---- CLEANUP ----
            self._set_running(job, Stage.CLEANUP, "Cleaning up temporary files",
                              percent=None)
            self._cleanup(work_dir, keep_mkv=keep_mkv)
            self._finish(job, Stage.DONE, f"Done — {job.movie_title} is on the server")
        except BaseException:
            # Best-effort cleanup of the encoded leftovers on failure too.
            try:
                if not keep_mkv:
                    self._cleanup(work_dir, keep_mkv=False)
            except Exception:
                pass
            raise

    @staticmethod
    def _cleanup(work_dir: Path, keep_mkv: bool) -> None:
        """Delete temp files. keep_mkv keeps the intermediate MakeMKV rips."""
        if not work_dir.exists():
            return
        # Encoded files live at the job work-dir root; rips in title<i>/ subdirs.
        for child in work_dir.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
        if not keep_mkv:
            for child in work_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
        try:
            if not any(work_dir.iterdir()):
                work_dir.rmdir()
        except OSError:
            pass
