"""Tests for disc2jelly.app.destination (local folder / WebDAV output targets)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app import destination
from app.destination import (
    DestinationError,
    LocalDestination,
    WebDavDestination,
    for_config,
)


class _Cfg:
    def __init__(self, **kw):
        self.destination_kind = kw.get("destination_kind", "local")
        self.local_path = kw.get("local_path", "")
        self.webdav_url = kw.get("webdav_url", "")
        self.webdav_user = kw.get("webdav_user", "")
        self.webdav_password = kw.get("webdav_password", "")


def _collect() -> tuple[list[dict], callable]:
    events: list[dict] = []
    return events, events.append


def _source(tmp_path: Path, size: int = 4096) -> Path:
    src = tmp_path / "encoded.mkv"
    src.write_bytes(b"x" * size)
    return src


# --- LocalDestination -------------------------------------------------------


def test_local_writes_file_creating_parent_dirs(tmp_path: Path) -> None:
    src = _source(tmp_path)
    root = tmp_path / "out"
    events, emit = _collect()

    LocalDestination(root).send(
        src, "Movies/The Matrix (1999)/The Matrix (1999).mkv",
        emit, threading.Event(), "job1",
    )

    written = root / "Movies/The Matrix (1999)/The Matrix (1999).mkv"
    assert written.is_file()
    assert written.read_bytes() == src.read_bytes()


def test_local_creates_a_missing_root(tmp_path: Path) -> None:
    src = _source(tmp_path)
    root = tmp_path / "does" / "not" / "exist"
    _, emit = _collect()
    LocalDestination(root).send(src, "a.mkv", emit, threading.Event(), "j")
    assert (root / "a.mkv").is_file()


def test_local_overwrites_an_existing_file(tmp_path: Path) -> None:
    src = _source(tmp_path, size=10)
    root = tmp_path / "out"
    (root / "Movies").mkdir(parents=True)
    stale = root / "Movies" / "a.mkv"
    stale.write_bytes(b"OLD CONTENT THAT IS LONGER")
    _, emit = _collect()

    LocalDestination(root).send(src, "Movies/a.mkv", emit, threading.Event(), "j")
    assert stale.read_bytes() == b"x" * 10


def test_local_emits_progress_ending_at_100(tmp_path: Path) -> None:
    src = _source(tmp_path, size=5 * 1024 * 1024)
    events, emit = _collect()
    LocalDestination(tmp_path / "out").send(
        src, "a.mkv", emit, threading.Event(), "j"
    )
    assert events, "expected at least one progress event"
    assert events[-1]["percent"] == 100.0
    assert all(e["stage"] == "UPLOAD" for e in events)


def test_local_cancel_midway_raises_and_removes_partial(tmp_path: Path) -> None:
    src = _source(tmp_path, size=8 * 1024 * 1024)
    root = tmp_path / "out"
    cancel = threading.Event()
    events: list[dict] = []

    def emit(ev: dict) -> None:
        events.append(ev)
        cancel.set()  # cancel as soon as the first chunk lands

    with pytest.raises(DestinationError, match="[Cc]ancel"):
        LocalDestination(root).send(src, "a.mkv", emit, cancel, "j")

    assert not (root / "a.mkv").exists()


def test_local_rejects_an_empty_root() -> None:
    with pytest.raises(DestinationError):
        LocalDestination("")


def test_local_rejects_a_relpath_escaping_the_root(tmp_path: Path) -> None:
    src = _source(tmp_path)
    _, emit = _collect()
    with pytest.raises(DestinationError):
        LocalDestination(tmp_path / "out").send(
            src, "../escaped.mkv", emit, threading.Event(), "j"
        )


def test_local_reports_the_target_path_in_its_final_event(tmp_path: Path) -> None:
    src = _source(tmp_path)
    events, emit = _collect()
    LocalDestination(tmp_path / "out").send(
        src, "Movies/a.mkv", emit, threading.Event(), "j"
    )
    assert "Movies/a.mkv" in events[-1]["detail"].replace("\\", "/")


# --- WebDavDestination ------------------------------------------------------


class _FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def upload(self, local, rel_dest, emit, cancel, job_id):  # noqa: ANN001
        self.calls.append((local, rel_dest, job_id))


def test_webdav_delegates_to_the_client(tmp_path: Path) -> None:
    client = _FakeClient()
    src = _source(tmp_path)
    _, emit = _collect()

    WebDavDestination(client).send(src, "Movies/a.mkv", emit, threading.Event(), "j7")

    assert client.calls == [(src, "Movies/a.mkv", "j7")]


# --- for_config -------------------------------------------------------------


def test_for_config_picks_local(tmp_path: Path) -> None:
    dest = for_config(_Cfg(destination_kind="local", local_path=str(tmp_path)))
    assert isinstance(dest, LocalDestination)


def test_for_config_picks_webdav() -> None:
    dest = for_config(
        _Cfg(destination_kind="webdav", webdav_url="https://nas/dav", webdav_user="u")
    )
    assert isinstance(dest, WebDavDestination)


def test_for_config_defaults_to_local_when_kind_is_unknown(tmp_path: Path) -> None:
    dest = for_config(_Cfg(destination_kind="wat", local_path=str(tmp_path)))
    assert isinstance(dest, LocalDestination)


def test_for_config_local_without_a_path_uses_the_zero_config_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(destination, "DEFAULT_LOCAL_ROOT", tmp_path / "Videos/Disc2Jelly")
    dest = for_config(_Cfg(destination_kind="local", local_path=""))
    assert dest.root == tmp_path / "Videos/Disc2Jelly"
