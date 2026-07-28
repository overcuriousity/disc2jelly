"""Tests for disc2jelly.app.webdav (WebDAVClient) with mocked requests."""

from __future__ import annotations

import threading

import pytest
import requests

from app import webdav
from app.webdav import WebDAVClient, WebDAVError

BASE = "https://nas.example/remote.php/dav/files/kimi/movies-inbox"
UPLOADS = "https://nas.example/remote.php/dav/uploads/kimi"
USER = "kimi"
DEST_REL = ("Movies/Alien Covenant (2017) [tmdbid-126]/"
            "Alien Covenant (2017) [tmdbid-126].mkv")
DEST_URL = (
    BASE + "/Movies/Alien%20Covenant%20%282017%29%20%5Btmdbid-126%5D/"
    "Alien%20Covenant%20%282017%29%20%5Btmdbid-126%5D.mkv"
)


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeServer:
    """Records every request and delegates the response to `handler`.

    handler(method, url, kwargs, calls) -> FakeResponse
    Streaming bodies (data=reader) are drained so progress callbacks fire.
    """

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        data = kwargs.get("data")
        body = b""
        if hasattr(data, "read"):
            while True:
                block = data.read(65536)
                if not block:
                    break
                body += block
        elif isinstance(data, bytes):
            body = data
        call = {
            "method": method,
            "url": url,
            "headers": dict(kwargs.get("headers") or {}),
            "body": body,
        }
        self.calls.append(call)
        return self.handler(method, url, kwargs, self.calls)


def make_client(monkeypatch, handler, base_url=BASE):
    server = FakeServer(handler)
    client = WebDAVClient(base_url, USER, "secret")
    monkeypatch.setattr(client.session, "request", server.request)
    return client, server


def ok_handler(method, url, kwargs, calls):
    return FakeResponse(201 if method == "MKCOL" else 207)


def write_file(tmp_path, size, name="rip.mkv"):
    p = tmp_path / name
    block = b"\x07" * 65536
    with p.open("wb") as fh:
        for _ in range(size // len(block)):
            fh.write(block)
        fh.write(block[: size % len(block)])
    return p


def methods(server):
    return [c["method"] for c in server.calls]


# ---------------------------------------------------------------------------
# Constructor / URL helpers
# ---------------------------------------------------------------------------

def test_constructor_requires_url():
    with pytest.raises(WebDAVError):
        WebDAVClient("", USER, "x")


def test_basic_auth_configured(monkeypatch):
    client, _ = make_client(monkeypatch, ok_handler)
    assert client.session.auth == (USER, "secret")


def test_uploads_root_derivation():
    client = WebDAVClient(BASE, USER, "x")
    assert client.uploads_root() == UPLOADS


def test_supports_chunking_only_for_files_tree():
    assert WebDAVClient(BASE, USER, "x").supports_chunking() is True
    assert WebDAVClient("https://s.example/dav", USER, "x").supports_chunking() is False
    # right tree, wrong user -> no chunking (uploads root would be someone else's)
    assert WebDAVClient(BASE, "other", "x").supports_chunking() is False


def test_uploads_root_underivable_raises():
    client = WebDAVClient("https://nas.example/webdav/movies", USER, "x")
    with pytest.raises(WebDAVError, match="dav/files"):
        client.uploads_root()


# ---------------------------------------------------------------------------
# ensure_dirs
# ---------------------------------------------------------------------------

def test_ensure_dirs_iterates_segments_url_encoded(monkeypatch):
    client, server = make_client(monkeypatch, ok_handler)
    client.ensure_dirs("Movies/Das Boot Müller (1981)")
    assert methods(server) == ["MKCOL", "MKCOL"]
    assert server.calls[0]["url"] == BASE + "/Movies"
    assert server.calls[1]["url"] == BASE + "/Movies/Das%20Boot%20M%C3%BCller%20%281981%29"


def test_ensure_dirs_405_tolerated(monkeypatch):
    client, server = make_client(
        monkeypatch, lambda m, u, k, c: FakeResponse(405, "already exists"))
    client.ensure_dirs("Movies/Existing")  # must not raise
    assert methods(server) == ["MKCOL", "MKCOL"]


def test_ensure_dirs_409_creates_parents_first_then_retries(monkeypatch):
    state = {"leaf_attempts": 0}

    def handler(method, url, kwargs, calls):
        if url.endswith("/a/b/c"):
            state["leaf_attempts"] += 1
            if state["leaf_attempts"] == 1:
                return FakeResponse(409, "parent missing")
            return FakeResponse(201)
        return FakeResponse(405)  # parents "exist"

    client, server = make_client(monkeypatch, handler)
    client.ensure_dirs("a/b/c")
    urls = [c["url"] for c in server.calls]
    # initial walk, then parent-first recovery, then successful retry
    assert urls == [
        BASE + "/a",
        BASE + "/a/b",
        BASE + "/a/b/c",      # 409
        BASE + "/a",          # recovery: parents first
        BASE + "/a/b",
        BASE + "/a/b/c",      # retry succeeds
    ]


def test_ensure_dirs_persistent_409_raises(monkeypatch):
    def handler(method, url, kwargs, calls):
        if url.endswith("/leaf"):
            return FakeResponse(409, "parent missing")
        return FakeResponse(405)

    client, _ = make_client(monkeypatch, handler)
    with pytest.raises(WebDAVError, match="409"):
        client.ensure_dirs("x/leaf")


def test_ensure_dirs_error_status_raises_with_server_text(monkeypatch):
    client, _ = make_client(
        monkeypatch, lambda m, u, k, c: FakeResponse(403, "forbidden by admin"))
    with pytest.raises(WebDAVError, match="403.*forbidden by admin"):
        client.ensure_dirs("Movies")


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------

def test_test_connection_ok(monkeypatch):
    client, server = make_client(
        monkeypatch, lambda m, u, k, c: FakeResponse(207, "multistatus"))
    ok, msg = client.test_connection()
    assert ok is True
    assert server.calls[0]["method"] == "PROPFIND"
    assert server.calls[0]["url"] == BASE
    assert server.calls[0]["headers"]["Depth"] == "0"


def test_test_connection_401(monkeypatch):
    client, _ = make_client(monkeypatch, lambda m, u, k, c: FakeResponse(401))
    ok, msg = client.test_connection()
    assert ok is False and "401" in msg


def test_test_connection_network_error(monkeypatch):
    def boom(method, url, **kwargs):
        raise requests.ConnectionError("name resolution failed")

    client = WebDAVClient(BASE, USER, "x")
    monkeypatch.setattr(client.session, "request", boom)
    ok, msg = client.test_connection()
    assert ok is False and "name resolution failed" in msg


# ---------------------------------------------------------------------------
# Small file upload: plain streamed PUT
# ---------------------------------------------------------------------------

def test_small_put_request_and_progress_sequence(monkeypatch, tmp_path):
    client, server = make_client(monkeypatch, ok_handler)
    size = 20 * 1024 * 1024  # 20 MiB -> progress events at 8/16/20 MiB
    local = write_file(tmp_path, size)
    events: list[dict] = []

    client.upload(local, DEST_REL, events.append, threading.Event(), "job1")

    put_calls = [c for c in server.calls if c["method"] == "PUT"]
    assert len(put_calls) == 1
    put = put_calls[0]
    assert put["url"] == DEST_URL
    assert put["headers"]["Content-Length"] == str(size)
    assert len(put["body"]) == size  # whole file streamed

    # parent dirs ensured before the PUT
    idx_mkcol = [i for i, c in enumerate(server.calls) if c["method"] == "MKCOL"]
    idx_put = server.calls.index(put)
    assert all(i < idx_put for i in idx_mkcol)

    stages = {e["stage"] for e in events}
    assert stages == {"UPLOAD"}
    assert all(e["job_id"] == "job1" for e in events)
    # 0% start, 40/80/100 from bytes sent, 100 done
    percents = [e["percent"] for e in events]
    assert percents[0] == 0.0
    assert percents[-1] == 100.0
    assert events[-1]["status"] == "done"
    assert percents == sorted(percents)
    assert 40.0 in percents and 80.0 in percents
    running = [e for e in events if e["status"] == "running"]
    assert len(running) >= 4  # start + one per 8 MiB boundary + EOF flush


def test_small_put_507_quota_error(monkeypatch, tmp_path):
    def handler(method, url, kwargs, calls):
        if method == "PUT":
            return FakeResponse(507, "Insufficient Storage")
        return FakeResponse(201)

    client, _ = make_client(monkeypatch, handler)
    local = write_file(tmp_path, 1024)
    with pytest.raises(WebDAVError) as excinfo:
        client.upload(local, "Movies/x.mkv", None, threading.Event(), "j")
    msg = str(excinfo.value)
    assert "507" in msg
    assert "quota" in msg.lower()


def test_upload_missing_local_file(monkeypatch, tmp_path):
    client, server = make_client(monkeypatch, ok_handler)
    with pytest.raises(WebDAVError, match="not found"):
        client.upload(tmp_path / "nope.mkv", "Movies/x.mkv", None,
                      threading.Event(), "j")
    assert server.calls == []


def test_upload_cancel_before_start(monkeypatch, tmp_path):
    client, server = make_client(monkeypatch, ok_handler)
    local = write_file(tmp_path, 1024)
    cancel = threading.Event()
    cancel.set()
    events: list[dict] = []
    with pytest.raises(WebDAVError, match="cancelled"):
        client.upload(local, "Movies/x.mkv", events.append, cancel, "j")
    assert server.calls == []
    assert events[-1]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Large file upload: Nextcloud chunking v2
# ---------------------------------------------------------------------------

@pytest.fixture
def tiny_limits(monkeypatch):
    """Shrink thresholds so chunking is exercised with small temp files."""
    monkeypatch.setattr(webdav, "SMALL_FILE_LIMIT", 1024 * 1024)  # 1 MiB
    monkeypatch.setattr(webdav, "CHUNK_SIZE", 512 * 1024)         # 512 KiB


def test_constants_match_spec():
    assert webdav.SMALL_FILE_LIMIT == 256 * 1024 * 1024
    assert webdav.CHUNK_SIZE == 64 * 1024 * 1024


def test_chunked_upload_request_sequence(monkeypatch, tmp_path, tiny_limits):
    client, server = make_client(monkeypatch, ok_handler)
    size = 512 * 1024 * 2 + 256 * 1024  # 1.25 MiB -> 3 chunks (last smaller)
    local = write_file(tmp_path, size)
    events: list[dict] = []

    client.upload(local, DEST_REL, events.append, threading.Event(), "job9")

    seq = [(c["method"], c["url"]) for c in server.calls]
    # 1. parent MKCOLs inside files tree
    assert seq[0][0] == "MKCOL" and "/dav/files/" in seq[0][1]
    assert seq[1][0] == "MKCOL" and "/dav/files/" in seq[1][1]
    # 2. transfer dir under uploads root, Destination header present
    mkcol_transfer = server.calls[2]
    assert mkcol_transfer["method"] == "MKCOL"
    assert mkcol_transfer["url"].startswith(UPLOADS + "/")
    assert mkcol_transfer["headers"]["Destination"] == DEST_URL
    upload_dir = mkcol_transfer["url"]
    # 3. zero-padded numeric chunks in order, each with headers
    chunk_calls = [c for c in server.calls if c["method"] == "PUT"]
    assert [c["url"] for c in chunk_calls] == [
        f"{upload_dir}/000001",
        f"{upload_dir}/000002",
        f"{upload_dir}/000003",
    ]
    for chunk in chunk_calls:
        assert chunk["headers"]["Destination"] == DEST_URL
        assert chunk["headers"]["OC-Total-Length"] == str(size)
    assert [len(c["body"]) for c in chunk_calls] == [512 * 1024, 512 * 1024, 256 * 1024]
    assert chunk_calls[0]["headers"]["Content-Length"] == str(512 * 1024)
    assert chunk_calls[2]["headers"]["Content-Length"] == str(256 * 1024)
    # 4. assemble via MOVE of .file AFTER all chunks
    move = server.calls[-1]
    assert move["method"] == "MOVE"
    assert move["url"] == f"{upload_dir}/.file"
    assert move["headers"]["Destination"] == DEST_URL
    assert methods(server).count("PUT") == 3
    # 5. progress events: 0% start, one per chunk (40/80/100), 100% done
    percents = [e["percent"] for e in events]
    assert percents[0] == 0.0 and percents[-1] == 100.0
    assert events[-1]["status"] == "done"
    assert 40.0 in percents and 80.0 in percents
    assert all(e["stage"] == "UPLOAD" for e in events)


def test_chunked_upload_aborts_on_mid_chunk_failure(monkeypatch, tmp_path, tiny_limits):
    def handler(method, url, kwargs, calls):
        if method == "PUT" and url.endswith("/000002"):
            return FakeResponse(500, "server exploded")
        return FakeResponse(201)

    client, server = make_client(monkeypatch, handler)
    local = write_file(tmp_path, 3 * 512 * 1024)
    with pytest.raises(WebDAVError, match="500"):
        client.upload(local, "Movies/x/y.mkv", None, threading.Event(), "j")

    upload_dir = next(c["url"] for c in server.calls
                      if c["method"] == "MKCOL" and "/dav/uploads/" in c["url"])
    last = server.calls[-1]
    assert last == {"method": "DELETE", "url": upload_dir, "headers": {}, "body": b""}
    assert "MOVE" not in methods(server)  # never assembled
    # no chunk PUTs after the failed one
    put_urls = [c["url"] for c in server.calls if c["method"] == "PUT"]
    assert put_urls == [f"{upload_dir}/000001", f"{upload_dir}/000002"]


def test_chunked_upload_507_raises_quota_message(monkeypatch, tmp_path, tiny_limits):
    def handler(method, url, kwargs, calls):
        if method == "PUT":
            return FakeResponse(507, "quota exceeded")
        return FakeResponse(201)

    client, server = make_client(monkeypatch, handler)
    local = write_file(tmp_path, 2 * 1024 * 1024)
    with pytest.raises(WebDAVError) as excinfo:
        client.upload(local, "Movies/x/y.mkv", None, threading.Event(), "j")
    assert "507" in str(excinfo.value)
    assert "quota" in str(excinfo.value).lower()
    assert "DELETE" in methods(server)  # transfer dir aborted


def test_chunked_upload_cancel_aborts_transfer(monkeypatch, tmp_path, tiny_limits):
    cancel = threading.Event()

    def handler(method, url, kwargs, calls):
        if method == "PUT":
            cancel.set()  # user cancels after first chunk lands
        return FakeResponse(201)

    client, server = make_client(monkeypatch, handler)
    local = write_file(tmp_path, 4 * 512 * 1024)
    events: list[dict] = []
    with pytest.raises(WebDAVError, match="cancelled"):
        client.upload(local, "Movies/x/y.mkv", events.append, cancel, "j")

    assert methods(server).count("PUT") == 1  # stopped after first chunk
    assert "MOVE" not in methods(server)
    assert methods(server)[-1] == "DELETE"
    assert events[-1]["status"] == "cancelled"


def test_chunked_upload_move_failure_aborts(monkeypatch, tmp_path, tiny_limits):
    def handler(method, url, kwargs, calls):
        if method == "MOVE":
            return FakeResponse(409, "assembly conflict")
        return FakeResponse(201)

    client, server = make_client(monkeypatch, handler)
    local = write_file(tmp_path, 2 * 1024 * 1024)
    with pytest.raises(WebDAVError, match="409"):
        client.upload(local, "Movies/x/y.mkv", None, threading.Event(), "j")
    assert methods(server)[-1] == "DELETE"


def test_plain_server_uses_single_put_for_big_file(monkeypatch, tmp_path, tiny_limits):
    """No /dav/files/<user> tree: chunking is unavailable, so PUT the lot."""
    plain = "https://streaming.example/dav"
    client, server = make_client(monkeypatch, ok_handler, base_url=plain)
    assert client.supports_chunking() is False
    size = 2 * 1024 * 1024
    local = write_file(tmp_path, size)

    client.upload(local, "Movies/x/y.mkv", None, threading.Event(), "j")

    puts = [c for c in server.calls if c["method"] == "PUT"]
    assert len(puts) == 1
    assert puts[0]["url"] == plain + "/Movies/x/y.mkv"
    assert len(puts[0]["body"]) == size
    assert "MOVE" not in methods(server)
    assert not any("/dav/uploads/" in c["url"] for c in server.calls)


def test_no_silent_fallback_to_plain_put_for_big_file(monkeypatch, tmp_path, tiny_limits):
    """A failing chunk must never degrade into a plain single PUT."""
    def handler(method, url, kwargs, calls):
        if method == "PUT" and "/dav/uploads/" in url:
            return FakeResponse(500, "chunk rejected")
        return FakeResponse(201)

    client, server = make_client(monkeypatch, handler)
    local = write_file(tmp_path, 2 * 1024 * 1024)
    with pytest.raises(WebDAVError):
        client.upload(local, DEST_REL, None, threading.Event(), "j")
    # the only PUT attempted was the chunk, never the final file URL
    assert [c["url"] for c in server.calls if c["method"] == "PUT"] != [DEST_URL]
    assert not any(c["method"] == "PUT" and c["url"] == DEST_URL for c in server.calls)


# ---------------------------------------------------------------------------
# Review fixes: redirect refusal, cancel cleanup
# ---------------------------------------------------------------------------


def test_plain_put_redirect_is_error_not_followed(monkeypatch, tmp_path):
    """PUT/MOVE must not follow redirects (streamed body cannot be re-sent)."""
    def handler(method, url, kwargs, calls):
        if method == "PUT":
            assert kwargs.get("allow_redirects") is False
            return FakeResponse(307, "Temporary Redirect")
        return FakeResponse(201)

    client, server = make_client(monkeypatch, handler)
    local = write_file(tmp_path, 100)
    with pytest.raises(WebDAVError, match="redirect"):
        client.upload(local, DEST_REL, None, threading.Event(), "j")
    assert methods(server).count("PUT") == 1  # not re-sent to Location


def test_chunked_move_redirect_is_error(monkeypatch, tmp_path, tiny_limits):
    def handler(method, url, kwargs, calls):
        if method == "MOVE":
            assert kwargs.get("allow_redirects") is False
            return FakeResponse(308, "Permanent Redirect")
        return FakeResponse(201)

    client, server = make_client(monkeypatch, handler)
    local = write_file(tmp_path, 2 * 1024 * 1024)
    with pytest.raises(WebDAVError, match="redirect"):
        client.upload(local, DEST_REL, None, threading.Event(), "j")
    assert methods(server)[-1] == "DELETE"  # transfer dir aborted


def test_plain_put_cancel_mid_stream_deletes_destination(monkeypatch, tmp_path):
    """Cancelling a plain PUT mid-stream removes the partial remote file."""
    monkeypatch.setattr(webdav, "PROGRESS_GRANULARITY", 65536)
    cancel = threading.Event()

    def emit(ev):
        if ev.get("percent"):  # ignore the 0% "starting" event
            cancel.set()       # user cancels once bytes are flowing

    client, server = make_client(monkeypatch, ok_handler)
    local = write_file(tmp_path, 512 * 1024)
    with pytest.raises(WebDAVError, match="cancelled"):
        client.upload(local, DEST_REL, emit, cancel, "j")
    deletes = [c for c in server.calls if c["method"] == "DELETE"]
    assert deletes and deletes[-1]["url"] == DEST_URL


def test_chunked_upload_cancel_before_move_aborts(monkeypatch, tmp_path, tiny_limits):
    """Cancel landing between the last chunk and assembly: no MOVE, cleanup."""
    cancel = threading.Event()

    def handler(method, url, kwargs, calls):
        if method == "PUT" and sum(1 for c in calls if c["method"] == "PUT") == 2:
            cancel.set()  # both chunks are up; cancel before MOVE
        return FakeResponse(201)

    client, server = make_client(monkeypatch, handler)
    local = write_file(tmp_path, 2 * 512 * 1024)  # exactly 2 chunks
    with pytest.raises(WebDAVError, match="cancelled"):
        client.upload(local, DEST_REL, None, cancel, "j")
    assert "MOVE" not in methods(server)
    assert methods(server)[-1] == "DELETE"
