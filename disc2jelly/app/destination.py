"""Where finished files go: a local/UNC folder, or a WebDAV share.

The local target is the zero-config default — she picks a folder (including a
mapped network drive or \\\\nas\\share) and no URL, username or password is ever
needed. WebDAV stays available for remote setups and keeps webdav.py's chunked
upload logic untouched behind the same interface.

Both targets emit the same UPLOAD-stage events the frontend already renders.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Callable, Protocol

EmitFn = Callable[[dict], None]

COPY_CHUNK = 4 * 1024 * 1024            # 4 MiB reads
PROGRESS_GRANULARITY = 8 * 1024 * 1024  # at most one event per 8 MiB, as in webdav.py

DEFAULT_LOCAL_ROOT = Path.home() / "Videos" / "Disc2Jelly"


class DestinationError(Exception):
    """Raised on write failures, an unusable root, or cancellation."""


class Destination(Protocol):
    def send(
        self,
        src: Path,
        rel_dest: str,
        emit: EmitFn,
        cancel: threading.Event,
        job_id: str,
    ) -> None:
        ...


# ---------------------------------------------------------------------------
# Local folder
# ---------------------------------------------------------------------------

class LocalDestination:
    def __init__(self, root: Path | str) -> None:
        if not str(root or "").strip():
            raise DestinationError("No destination folder configured")
        self.root = Path(root)

    def _target(self, rel_dest: str) -> Path:
        root = self.root.resolve()
        target = (root / rel_dest).resolve()
        # A relpath is built from TMDb data; refuse anything that climbs out.
        if root != target and root not in target.parents:
            raise DestinationError(f"Refusing to write outside the destination: {rel_dest}")
        return target

    def send(
        self,
        src: Path,
        rel_dest: str,
        emit: EmitFn,
        cancel: threading.Event,
        job_id: str,
    ) -> None:
        src = Path(src)
        target = self._target(rel_dest)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DestinationError(f"Cannot create {target.parent}: {exc}") from exc

        total = src.stat().st_size
        copied = 0
        last_event = 0

        try:
            with src.open("rb") as fh_in, target.open("wb") as fh_out:
                while True:
                    if cancel.is_set():
                        raise DestinationError("Cancelled")
                    chunk = fh_in.read(COPY_CHUNK)
                    if not chunk:
                        break
                    fh_out.write(chunk)
                    copied += len(chunk)
                    if copied - last_event >= PROGRESS_GRANULARITY:
                        last_event = copied
                        emit(_event(copied, total, rel_dest))
        except DestinationError:
            target.unlink(missing_ok=True)
            raise
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise DestinationError(f"Write failed: {exc}") from exc

        emit(_event(total, total, rel_dest))


def _event(done: int, total: int, rel_dest: str) -> dict:
    percent = 100.0 if total <= 0 else min(100.0, done * 100.0 / total)
    return {
        "stage": "UPLOAD",
        "status": "running" if percent < 100.0 else "done",
        "percent": round(percent, 2),
        "detail": f"Saving {rel_dest}",
    }


# ---------------------------------------------------------------------------
# WebDAV
# ---------------------------------------------------------------------------

class WebDavDestination:
    def __init__(self, client: object) -> None:
        self.client = client

    def send(
        self,
        src: Path,
        rel_dest: str,
        emit: EmitFn,
        cancel: threading.Event,
        job_id: str,
    ) -> None:
        self.client.upload(src, rel_dest, emit, cancel, job_id)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def for_config(cfg: object) -> Destination:
    """Build the destination the config asks for; local is the default."""
    kind = (getattr(cfg, "destination_kind", "") or "local").strip().lower()
    if kind == "webdav":
        from .webdav import WebDAVClient  # lazy: keeps requests off the local path

        return WebDavDestination(
            WebDAVClient(
                getattr(cfg, "webdav_url", "") or "",
                getattr(cfg, "webdav_user", "") or "",
                getattr(cfg, "webdav_password", "") or "",
            )
        )
    root = (getattr(cfg, "local_path", "") or "").strip() or DEFAULT_LOCAL_ROOT
    return LocalDestination(root)
