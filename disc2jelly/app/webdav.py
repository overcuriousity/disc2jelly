"""WebDAV upload client (Nextcloud/ownCloud focus).

SPEC contract:
    class WebDAVClient:
        def __init__(self, base_url: str, user: str, password: str)
        def ensure_dirs(self, rel_dir: str) -> None
        def upload(self, local: Path, rel_dest: str, emit, cancel, job_id: str) -> None
        def test_connection(self) -> tuple[bool, str]
    class WebDAVError(Exception)

Small files (< 256 MiB): single streamed PUT with progress from bytes sent.
Large files (>= 256 MiB): Nextcloud chunking v2 (info.md §5.3) — MKCOL transfer
dir under /dav/uploads/<user>/, zero-padded numeric 64 MiB chunks carrying
OC-Total-Length + Destination headers, MOVE of `.file` to assemble, DELETE of
the transfer dir to abort. No silent fallback to plain PUT for large files.

No imports from other disc2jelly app modules (owned by other coders).
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import requests

SMALL_FILE_LIMIT = 256 * 1024 * 1024  # >= 256 MiB -> chunked upload
CHUNK_SIZE = 64 * 1024 * 1024         # Nextcloud allows 5 MB..5 GB per chunk
PROGRESS_GRANULARITY = 8 * 1024 * 1024  # emit at most one progress event per 8 MiB
CHUNK_NAME_PAD = 6                     # "000001" style zero-padded numeric names
TIMEOUT = (15, 300)                    # (connect, read) seconds


class WebDAVError(Exception):
    """WebDAV operation failed; message carries HTTP status + server text."""


class _UploadCancelled(Exception):
    """Internal: cancel event tripped mid-transfer."""


def _encode_rel_path(rel: str) -> str:
    """URL-encode every path segment (spaces, non-ASCII); keeps '/' separators."""
    segments = [seg for seg in str(rel).replace("\\", "/").strip("/").split("/") if seg]
    return "/".join(quote(seg, safe="") for seg in segments)


def _parent_rel(rel: str) -> str:
    parts = [seg for seg in str(rel).replace("\\", "/").strip("/").split("/") if seg]
    return "/".join(parts[:-1])


def _human_size(n: int) -> str:
    gib = n / (1024 ** 3)
    if gib >= 1:
        return f"{gib:.1f} GiB"
    return f"{n / (1024 ** 2):.0f} MiB"


class _ProgressReader:
    """File-like wrapper requests can stream; tracks bytes sent for progress.

    http.client reads bodies in small (8 KiB) blocks; we coalesce progress
    callbacks to at most one per PROGRESS_GRANULARITY bytes (SPEC: 8 MiB).
    """

    def __init__(
        self,
        fh,
        total: int,
        cancel: threading.Event,
        on_progress: Callable[[int], None] | None,
    ):
        self._fh = fh
        self.total = total
        self._cancel = cancel
        self._on_progress = on_progress
        self.sent = 0
        self._next_report = PROGRESS_GRANULARITY

    def read(self, size: int = -1) -> bytes:
        if self._cancel.is_set():
            raise _UploadCancelled("Upload cancelled")
        data = self._fh.read(size)
        self.sent += len(data)
        if self._on_progress and (self.sent >= self._next_report or not data):
            self._next_report = self.sent + PROGRESS_GRANULARITY
            self._on_progress(self.sent)
        return data

    def __len__(self) -> int:  # lets requests send Content-Length, not chunked TE
        return self.total


class WebDAVClient:
    def __init__(self, base_url: str, user: str, password: str):
        base_url = (base_url or "").strip()
        if not base_url:
            raise WebDAVError("WebDAV URL is empty")
        self.base_url = base_url.rstrip("/")
        self.user = user or ""
        self.session = requests.Session()
        self.session.auth = (self.user, password or "")

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------
    def file_url(self, rel: str = "") -> str:
        """Absolute URL of a path inside the user's files tree."""
        enc = _encode_rel_path(rel)
        return f"{self.base_url}/{enc}" if enc else self.base_url

    def uploads_root(self) -> str:
        """Derive the Nextcloud chunking endpoint from base_url.

        /dav/files/<user>/... -> /dav/uploads/<user>. Anything else is a
        config error the user must fix (SPEC: raise, don't guess).
        """
        marker = f"/dav/files/{self.user}"
        idx = self.base_url.find(marker) if self.user else -1
        if idx != -1:
            # Transfer dirs live directly under /dav/uploads/<user>/ — the
            # library sub-path of base_url does NOT carry over.
            return self.base_url[:idx] + f"/dav/uploads/{self.user}"
        raise WebDAVError(
            f"Cannot derive the Nextcloud uploads URL from {self.base_url!r}: "
            f"the WebDAV URL must point into a files tree containing "
            f"'/dav/files/{self.user or '<user>'}' "
            f"(e.g. https://server/remote.php/dav/files/{self.user or '<user>'}/movies)."
        )

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------
    @staticmethod
    def _raise_for_status(resp: requests.Response, context: str) -> None:
        if resp.status_code < 400:
            return
        hint = {
            401: "authentication failed — check username/password (use an app password)",
            403: "permission denied — the user may not write to this folder",
            404: "not found — a parent folder is missing",
            507: "insufficient storage — the server quota is exceeded",
        }.get(resp.status_code, "")
        body = (resp.text or "").strip()
        if len(body) > 300:
            body = body[:300] + "…"
        msg = f"{context} failed: HTTP {resp.status_code}"
        if hint:
            msg += f" ({hint})"
        if body:
            msg += f": {body}"
        raise WebDAVError(msg)

    def _request(self, method: str, url: str, context: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", TIMEOUT)
        if method in ("PUT", "MOVE"):
            # A streamed upload body cannot be re-read, so following a
            # redirect (e.g. http -> https on the NAS) would corrupt the
            # upload. Refuse redirects and surface them as an error instead.
            kwargs.setdefault("allow_redirects", False)
        try:
            resp = self.session.request(method, url, **kwargs)
        except _UploadCancelled:
            raise
        except requests.RequestException as exc:
            raise WebDAVError(f"{context} failed: {exc}") from exc
        if method in ("PUT", "MOVE") and 300 <= resp.status_code < 400:
            location = (getattr(resp, "headers", None) or {}).get("Location", "")
            raise WebDAVError(
                f"{context} failed: HTTP {resp.status_code} redirect"
                + (f" to {location}" if location else "")
                + " — the server wants to redirect this request, but a "
                "streamed upload cannot be re-sent. Fix the WebDAV URL in "
                "Settings to the final address (e.g. https://…)."
            )
        return resp

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    @staticmethod
    def _emit(
        emit: Callable[[dict], None] | None,
        job_id: str,
        status: str,
        percent: float | None,
        detail: str,
    ) -> None:
        if emit is None:
            return
        emit({
            "job_id": job_id,
            "stage": "UPLOAD",
            "status": status,
            "percent": percent,
            "detail": detail,
            "ts": time.time(),
        })

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def ensure_dirs(self, rel_dir: str) -> None:
        """Create rel_dir iteratively (MKCOL is not recursive).

        405 = already exists (ok). 409 = parent missing -> create parents
        first, then retry the segment once.
        """
        segments = [
            seg
            for seg in str(rel_dir).replace("\\", "/").strip("/").split("/")
            if seg
        ]
        for depth in range(1, len(segments) + 1):
            self._mkcol_segment(segments, depth)

    def _mkcol_segment(self, segments: list[str], depth: int, _retry: bool = True) -> None:
        url = self.file_url("/".join(segments[:depth]))
        resp = self._request("MKCOL", url, f"MKCOL {url}")
        if resp.status_code == 405:
            return  # collection already exists
        if resp.status_code == 409:
            if not _retry:
                raise WebDAVError(
                    f"MKCOL {url} failed: HTTP 409 (parent folder missing; "
                    f"parent-first creation was already attempted)"
                )
            # Parent collection missing: ensure parents first, then retry.
            for d in range(1, depth):
                self._mkcol_segment(segments, d)
            self._mkcol_segment(segments, depth, _retry=False)
            return
        self._raise_for_status(resp, f"MKCOL {url}")

    def upload(
        self,
        local: Path,
        rel_dest: str,
        emit: Callable[[dict], None] | None,
        cancel: threading.Event,
        job_id: str,
    ) -> None:
        """Upload local to rel_dest (relative to base_url) with UPLOAD events."""
        local = Path(local)
        if not local.is_file():
            raise WebDAVError(f"Local file not found: {local}")
        size = local.stat().st_size
        name = Path(str(rel_dest).replace("\\", "/")).name or local.name

        self._emit(emit, job_id, "running", 0.0,
                   f"Uploading {name} ({_human_size(size)})")
        if cancel.is_set():
            self._emit(emit, job_id, "cancelled", 0.0, "Upload cancelled")
            raise WebDAVError("Upload cancelled before it started")

        chunked = size >= SMALL_FILE_LIMIT
        if chunked:
            # Validate the uploads-root derivation before any request goes out:
            # a bad base_url is a config error, not a runtime surprise.
            self.uploads_root()

        parent = _parent_rel(rel_dest)
        if parent:
            self.ensure_dirs(parent)

        try:
            if chunked:
                self._upload_chunked(local, rel_dest, size, name, emit, cancel, job_id)
            else:
                self._upload_plain(local, rel_dest, size, name, emit, cancel, job_id)
        except _UploadCancelled as exc:
            self._emit(emit, job_id, "cancelled", None, "Upload cancelled")
            raise WebDAVError("Upload cancelled") from exc

        self._emit(emit, job_id, "done", 100.0, f"Uploaded {name}")

    def test_connection(self) -> tuple[bool, str]:
        """PROPFIND Depth:0 on base_url; (ok, human-readable message)."""
        try:
            resp = self._request(
                "PROPFIND", self.base_url, "PROPFIND",
                headers={"Depth": "0"},
            )
        except WebDAVError as exc:
            return False, str(exc)
        if resp.status_code in (200, 207):
            return True, f"Connected to {self.base_url}"
        if resp.status_code == 401:
            return False, "Authentication failed (HTTP 401) — check username and app password"
        if resp.status_code == 403:
            return False, "Permission denied (HTTP 403) — check the user's access to this folder"
        if resp.status_code == 404:
            return False, "Folder not found (HTTP 404) — check the WebDAV URL path"
        return False, f"Unexpected response: HTTP {resp.status_code}"

    # ------------------------------------------------------------------
    # Small files: single streamed PUT
    # ------------------------------------------------------------------
    def _upload_plain(
        self,
        local: Path,
        rel_dest: str,
        size: int,
        name: str,
        emit,
        cancel: threading.Event,
        job_id: str,
    ) -> None:
        url = self.file_url(rel_dest)

        def on_progress(sent: int) -> None:
            pct = min(100.0, sent / size * 100) if size else 100.0
            self._emit(emit, job_id, "running", pct,
                       f"Uploading {name} — {_human_size(sent)} of {_human_size(size)}")

        with local.open("rb") as fh:
            reader = _ProgressReader(fh, size, cancel, on_progress)
            try:
                resp = self._request(
                    "PUT", url, f"PUT {url}",
                    data=reader,
                    headers={"Content-Length": str(size)},
                )
            except _UploadCancelled:
                # Mid-stream cancel: the server may hold a partial file.
                self._delete_quietly(url)
                raise
            except WebDAVError as exc:
                # urllib3 may wrap the reader's _UploadCancelled in a
                # ConnectionError; the cancel event is the source of truth.
                if cancel.is_set():
                    self._delete_quietly(url)
                    raise _UploadCancelled("Upload cancelled") from exc
                raise
        self._raise_for_status(resp, f"PUT {url}")

    def _delete_quietly(self, url: str) -> None:
        """Best-effort remote DELETE (partial-upload cleanup); never raises."""
        try:
            self.session.request("DELETE", url, timeout=TIMEOUT)
        except requests.RequestException:
            pass

    # ------------------------------------------------------------------
    # Large files: Nextcloud chunking v2 (info.md §5.3)
    # ------------------------------------------------------------------
    def _upload_chunked(
        self,
        local: Path,
        rel_dest: str,
        size: int,
        name: str,
        emit,
        cancel: threading.Event,
        job_id: str,
    ) -> None:
        dest_url = self.file_url(rel_dest)
        upload_dir = f"{self.uploads_root()}/{uuid.uuid4().hex}"
        total_chunks = max(1, -(-size // CHUNK_SIZE))  # ceil
        aborted = False

        def abort() -> None:
            nonlocal aborted
            if aborted:
                return
            aborted = True
            try:
                self.session.request("DELETE", upload_dir, timeout=TIMEOUT)
            except requests.RequestException:
                pass  # best-effort cleanup; upload dirs expire after 24 h anyway

        try:
            # 1. Create the transfer dir (Destination header on every request).
            resp = self._request(
                "MKCOL", upload_dir, f"MKCOL {upload_dir}",
                headers={"Destination": dest_url},
            )
            if resp.status_code not in (200, 201, 204, 405):
                self._raise_for_status(resp, f"MKCOL {upload_dir}")

            # 2. PUT zero-padded numeric chunks with OC-Total-Length.
            with local.open("rb") as fh:
                index = 0
                while True:
                    if cancel.is_set():
                        raise _UploadCancelled("Upload cancelled")
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    index += 1
                    chunk_name = str(index).zfill(CHUNK_NAME_PAD)
                    chunk_url = f"{upload_dir}/{chunk_name}"
                    resp = self._request(
                        "PUT", chunk_url, f"PUT chunk {index}/{total_chunks}",
                        data=chunk,
                        headers={
                            "Destination": dest_url,
                            "OC-Total-Length": str(size),
                            "Content-Length": str(len(chunk)),
                        },
                    )
                    self._raise_for_status(resp, f"PUT chunk {index}/{total_chunks}")
                    sent = min(index * CHUNK_SIZE, size)
                    self._emit(
                        emit, job_id, "running", sent / size * 100,
                        f"Uploading {name} — chunk {index} of {total_chunks}",
                    )

            # 3. Assemble: MOVE <dir>/.file -> final destination.
            if cancel.is_set():
                # Cancelled between the last chunk and assembly: abort and
                # DELETE the transfer dir instead of publishing the file.
                raise _UploadCancelled("Upload cancelled")
            resp = self._request(
                "MOVE", f"{upload_dir}/.file", "MOVE assemble",
                headers={"Destination": dest_url, "Overwrite": "T"},
            )
            self._raise_for_status(resp, "MOVE assemble")
        except _UploadCancelled:
            abort()
            raise
        except WebDAVError as exc:
            abort()
            if cancel.is_set():
                raise _UploadCancelled("Upload cancelled") from exc
            raise
        except Exception:
            abort()
            raise
