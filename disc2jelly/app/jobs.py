"""Job model, queue, and pipeline runner for Disc2Jelly.

Owns the SACRED event bus: ``subscribe() -> queue.Queue`` / ``publish(event)``.
Sibling modules (handbrake, destination, config) are imported lazily inside
``_default_stages``. Tests inject fake stage callables via
``JobManager(cfg_getter, stages=StageFuncs(...))``.

There is no rip stage: HandBrake reads the DVD directly, so each title is a
single encode pass straight from the drive. A job carries a list of
``TitleTarget`` — one per output file — which makes a movie (one target) and a
season disc (one target per episode) the same shape.
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
class TitleTarget:
    """One output file: which disc title to encode, and where it belongs.

    The relpath is resolved once at job creation, where the TMDb data is
    already in hand, so the pipeline never has to know about movies vs series.
    """

    title_index: int
    relpath: str  # posix, relative to the destination root

    def serialize(self) -> dict[str, Any]:
        return {"title_index": self.title_index, "relpath": self.relpath}


@dataclass
class Job:
    """In-memory job per SPEC §Pipeline & job model."""

    id: str
    disc_name: str
    drive: str
    targets: list[TitleTarget]
    tmdb_id: int | None
    display_title: str  # movie title or series name, for the UI
    year: int | None
    profile: str  # "hevc" | "h264"
    status: Stage
    created: float
    error: str | None = None
    cancel_event: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )

    @property
    def title_indices(self) -> list[int]:
        return [t.title_index for t in self.targets]

    def serialize(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "disc_name": self.disc_name,
            "drive": self.drive,
            "targets": [t.serialize() for t in self.targets],
            "tmdb_id": self.tmdb_id,
            "display_title": self.display_title,
            "year": self.year,
            "profile": self.profile,
            "status": self.status.value,
            "created": self.created,
            "error": self.error,
        }


@dataclass
class StageFuncs:
    """Injectable pipeline stage callables.

    Signatures (bound forms — the HandBrake path and destination credentials
    are already resolved by the JobManager from the current Config):
      encode(source: str, title_index: int, dst: Path, profile: str,
             quality: int, emit, cancel) -> Path
      send(local: Path, rel_dest: str, emit, cancel, job_id: str) -> None
    """

    encode: Callable[[str, int, Path, str, int, EmitFn, threading.Event], Path]
    send: Callable[[Path, str, EmitFn, threading.Event, str], None]


def _resolve_handbrake(cfg: Any) -> str:
    """HandBrakeCLI path via config.resolve_binaries, with graceful fallbacks."""
    name = "HandBrakeCLI"
    try:
        from . import config as _config  # lazy by design

        found = _config.resolve_binaries(cfg)
        if found:
            return found
    except Exception:
        pass
    configured = (getattr(cfg, "handbrake_path", "") or "").strip()
    return configured or shutil.which(name) or name


def _default_stages(cfg: Any) -> StageFuncs:
    """Build the real pipeline stages from the current config (lazy imports)."""
    from . import destination, handbrake  # lazy by design

    hb_path = _resolve_handbrake(cfg)
    target = destination.for_config(cfg)

    def encode(source: str, title_index: int, dst: Path, profile: str,
               quality: int, emit: EmitFn, cancel: threading.Event) -> Path:
        return handbrake.encode(hb_path, source, title_index, dst, profile,
                                quality, emit, cancel)

    def send(local: Path, rel_dest: str, emit: EmitFn,
             cancel: threading.Event, job_id: str) -> None:
        target.send(local, rel_dest, emit, cancel, job_id)

    return StageFuncs(encode=encode, send=send)


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

    def create_job(self, drive: str, targets: list[TitleTarget],
                   tmdb_id: int | None, display_title: str, year: int | None,
                   profile: str, disc_name: str = "") -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            disc_name=disc_name,
            drive=drive,
            targets=list(targets),
            tmdb_id=tmdb_id,
            display_title=display_title,
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
            "detail": f"Queued: {display_title}",
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
        quality = (getattr(cfg, "hevc_quality", 22) if job.profile == "hevc"
                   else getattr(cfg, "h264_quality", 20))

        self._check_cancel(job)
        n = len(job.targets)

        try:
            # Encode and send one title at a time: a season disc never stacks
            # several finished files on the temp drive at once.
            for i, target in enumerate(job.targets, start=1):
                rel_dest = Path(target.relpath)

                # ---- ENCODE (straight off the disc, no rip stage) ----
                dst = work_dir / rel_dest.name
                self._set_running(job, Stage.ENCODE,
                                  f"Ripping and shrinking file {i} of {n}")
                enc_emit = self._emit_for(job, Stage.ENCODE)
                out = Path(stages.encode(job.drive, target.title_index, dst,
                                         job.profile, quality, enc_emit,
                                         job.cancel_event))
                self._check_cancel(job)
                enc_emit({"status": "done", "percent": 100.0,
                          "detail": f"File {i} of {n} complete"})

                # ---- SEND ----
                self._set_running(job, Stage.UPLOAD, f"Saving {i} of {n}")
                up_emit = self._emit_for(job, Stage.UPLOAD)
                stages.send(out, rel_dest.as_posix(), up_emit,
                            job.cancel_event, job.id)
                self._check_cancel(job)
                up_emit({"status": "done", "percent": 100.0,
                         "detail": f"Saved {i} of {n}"})
                out.unlink(missing_ok=True)

            # ---- CLEANUP ----
            self._set_running(job, Stage.CLEANUP, "Cleaning up temporary files",
                              percent=None)
            self._cleanup(work_dir)
            self._finish(job, Stage.DONE,
                         f"Done — {job.display_title} is on the server")
        except BaseException:
            try:
                self._cleanup(work_dir)
            except Exception:
                pass
            raise

    @staticmethod
    def _cleanup(work_dir: Path) -> None:
        """Delete the job's temp dir. Only encode outputs ever live here."""
        if not work_dir.exists():
            return
        shutil.rmtree(work_dir, ignore_errors=True)
