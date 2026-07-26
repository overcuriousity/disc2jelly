"""Tests for disc2jelly.app.metadata (TMDb client + Jellyfin naming)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import metadata
from app.metadata import (
    MetadataError,
    MovieMatch,
    clean_query,
    jellyfin_movie_relpath,
    search_movies,
    strip_banned_chars,
)

V3_KEY = "0123456789abcdef0123456789abcdef"
V4_TOKEN = ("eyJhbGciOiJIUzI1NiJ9." + "x" * 120 + "." + "y" * 40)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no JSON")
        return self._json


def make_result(**kw):
    base = {
        "id": 603,
        "title": "The Matrix",
        "original_title": "The Matrix",
        "release_date": "1999-03-31",
        "overview": "A hacker learns the truth.",
    }
    base.update(kw)
    return base


def patch_get(monkeypatch, response):
    """Monkeypatch requests.get; returns list of captured call kwargs."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return response

    monkeypatch.setattr(metadata.requests, "get", fake_get)
    return calls


# ---------------------------------------------------------------------------
# search_movies: auth detection
# ---------------------------------------------------------------------------

def test_search_v3_key_sent_as_query_param(monkeypatch):
    calls = patch_get(monkeypatch, FakeResponse(200, {"results": []}))
    search_movies(V3_KEY, "The Matrix")
    assert calls[0]["params"]["api_key"] == V3_KEY
    assert calls[0]["params"]["query"] == "The Matrix"
    assert "Authorization" not in calls[0]["headers"]


def test_search_v4_token_sent_as_bearer(monkeypatch):
    calls = patch_get(monkeypatch, FakeResponse(200, {"results": []}))
    search_movies(V4_TOKEN, "Alien")
    assert calls[0]["headers"]["Authorization"] == f"Bearer {V4_TOKEN}"
    assert "api_key" not in calls[0]["params"]


def test_search_uses_tmdb_v3_endpoint_and_timeout(monkeypatch):
    calls = patch_get(monkeypatch, FakeResponse(200, {"results": []}))
    search_movies(V3_KEY, "x")
    assert calls[0]["url"] == "https://api.themoviedb.org/3/search/movie"
    assert calls[0]["timeout"] == 10


def test_search_parses_results(monkeypatch):
    payload = {"page": 1, "results": [make_result(), make_result(
        id=808, title="Das Boot: Müller!", original_title="Das Boot",
        release_date="1981-09-17", overview="U-Boot.")]}
    patch_get(monkeypatch, FakeResponse(200, payload))
    matches = search_movies(V3_KEY, "matrix")
    assert matches == [
        MovieMatch(tmdb_id=603, title="The Matrix", original_title="The Matrix",
                   year=1999, overview="A hacker learns the truth."),
        MovieMatch(tmdb_id=808, title="Das Boot: Müller!", original_title="Das Boot",
                   year=1981, overview="U-Boot."),
    ]


def test_search_empty_release_date_guards_year(monkeypatch):
    payload = {"results": [make_result(release_date=""), make_result(id=1, release_date=None)]}
    patch_get(monkeypatch, FakeResponse(200, payload))
    matches = search_movies(V3_KEY, "x")
    assert [m.year for m in matches] == [None, None]


def test_search_missing_optional_fields(monkeypatch):
    patch_get(monkeypatch, FakeResponse(200, {"results": [{"id": 7}]}))
    (match,) = search_movies(V3_KEY, "x")
    assert match.tmdb_id == 7
    assert match.title == "" and match.original_title == ""
    assert match.year is None and match.overview == ""


def test_search_401_raises_metadata_error(monkeypatch):
    patch_get(monkeypatch, FakeResponse(401, text="Invalid API key"))
    with pytest.raises(MetadataError, match="401"):
        search_movies(V3_KEY, "x")
    with pytest.raises(MetadataError, match="401"):
        search_movies(V4_TOKEN, "x")


def test_search_http_error_raises_metadata_error(monkeypatch):
    patch_get(monkeypatch, FakeResponse(500, text="boom"))
    with pytest.raises(MetadataError, match="500"):
        search_movies(V3_KEY, "x")


def test_search_network_error_raises_metadata_error(monkeypatch):
    def fake_get(url, **kwargs):
        raise metadata.requests.ConnectionError("dns fail")

    monkeypatch.setattr(metadata.requests, "get", fake_get)
    with pytest.raises(MetadataError):
        search_movies(V3_KEY, "x")


def test_search_requires_credential_and_query(monkeypatch):
    with pytest.raises(MetadataError):
        search_movies("", "x")
    assert search_movies(V3_KEY, "   ") == []


# ---------------------------------------------------------------------------
# clean_query heuristics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,expected", [
    ("THE_MATRIX_16X9", "The Matrix"),
    ("LOTR_FOTR_D1", "Lotr Fotr"),
    ("BREAKING_BAD_S01_D2", "Breaking Bad"),
    ("ALIEN_WS", "Alien"),
    ("TERMINATOR_2_JUDGMENT_DAY", "Terminator 2 Judgment Day"),
    ("DAS_BOOT_DIRECTORS_CUT_DISC1", "Das Boot Directors Cut"),
    ("THE_LORD_OF_THE_RINGS", "The Lord of the Rings"),  # small words lower
    ("Das Boot", "Das Boot"),                             # already clean: untouched
    ("Amélie", "Amélie"),                                 # unicode untouched
    ("2001_A_SPACE_ODYSSEY", "A Space Odyssey"),          # leading year-noise stripped
    ("SE7EN", "Se7en"),
])
def test_clean_query(label, expected):
    assert clean_query(label) == expected


def test_clean_query_empty():
    assert clean_query("") == ""
    assert clean_query("   ") == ""


# ---------------------------------------------------------------------------
# jellyfin_movie_relpath
# ---------------------------------------------------------------------------

def test_relpath_basic_with_year_and_tmdbid():
    rel = jellyfin_movie_relpath("The Matrix", 1999, 603)
    assert rel == Path("Movies/The Matrix (1999) [tmdbid-603]/"
                       "The Matrix (1999) [tmdbid-603].mkv")


def test_relpath_banned_chars_colon():
    rel = jellyfin_movie_relpath("Alien: Covenant", 2017, 126)
    assert rel == Path("Movies/Alien Covenant (2017) [tmdbid-126]/"
                       "Alien Covenant (2017) [tmdbid-126].mkv")


def test_relpath_all_banned_chars_stripped():
    assert strip_banned_chars('a<b>c:d"e/f\\g|h?i*j') == "abcdefghij"


def test_relpath_banned_chars_collapse_double_space():
    # "X / Y" -> "X  Y" -> collapse -> "X Y"
    rel = jellyfin_movie_relpath("X / Y", 2000)
    assert rel == Path("Movies/X Y (2000)/X Y (2000).mkv")


def test_relpath_umlauts_preserved():
    rel = jellyfin_movie_relpath("Das Boot: Müller!", 1981, 123)
    # ':' is banned, '!' and umlauts are not.
    assert rel == Path("Movies/Das Boot Müller! (1981) [tmdbid-123]/"
                       "Das Boot Müller! (1981) [tmdbid-123].mkv")


def test_relpath_year_none_omitted():
    rel = jellyfin_movie_relpath("Some Movie", None, 42)
    assert rel == Path("Movies/Some Movie [tmdbid-42]/Some Movie [tmdbid-42].mkv")


def test_relpath_no_tmdbid_no_tag():
    rel = jellyfin_movie_relpath("Some Movie", 2001)
    assert rel == Path("Movies/Some Movie (2001)/Some Movie (2001).mkv")


def test_relpath_year_none_no_tmdbid():
    rel = jellyfin_movie_relpath("Some Movie", None)
    assert rel == Path("Movies/Some Movie/Some Movie.mkv")


def test_relpath_file_matches_folder_char_for_char():
    rel = jellyfin_movie_relpath("Alien: Covenant", 2017, 126)
    assert rel.parent.name == rel.name[: -len(".mkv")]


def test_relpath_empty_title_raises():
    with pytest.raises(MetadataError):
        jellyfin_movie_relpath(":::", 2000)  # strips to nothing
