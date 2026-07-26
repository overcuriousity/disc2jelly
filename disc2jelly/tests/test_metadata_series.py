"""Tests for the TV/series half of disc2jelly.app.metadata.

Kept separate from test_metadata.py, which already covers the movie path at
224 lines. Shared fakes are imported from there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import metadata
from app.metadata import (
    MetadataError,
    jellyfin_episode_relpath,
    parse_disc_label,
    resolve_api_key,
    search_shows,
    season_episodes,
)
from tests.test_metadata import V3_KEY, FakeResponse, patch_get


# ---------------------------------------------------------------------------
# parse_disc_label — series detection
# ---------------------------------------------------------------------------


def test_colon_separated_season_and_disc() -> None:
    hint = parse_disc_label("Breaking Bad: Season 1: Disc 1")
    assert hint.kind == "series"
    assert hint.title == "Breaking Bad"
    assert hint.season == 1
    assert hint.disc == 1


def test_underscore_season_and_disc_markers() -> None:
    hint = parse_disc_label("FRIENDS_S02_D3")
    assert hint.kind == "series"
    assert hint.title == "Friends"
    assert hint.season == 2
    assert hint.disc == 3


def test_spelled_out_season_marker() -> None:
    hint = parse_disc_label("THE_OFFICE_SEASON_3")
    assert hint.kind == "series"
    assert hint.title == "The Office"
    assert hint.season == 3


def test_uk_series_marker_means_season() -> None:
    hint = parse_disc_label("SOPRANOS_SERIES_2")
    assert hint.kind == "series"
    assert hint.season == 2


def test_glued_season_suffix_on_long_allcaps_label() -> None:
    """BREAKINGBADS1 has no separators; the trailing S1 is still a season."""
    hint = parse_disc_label("BREAKINGBADS1")
    assert hint.kind == "series"
    assert hint.season == 1
    assert hint.title == "Breakingbad"


# ---------------------------------------------------------------------------
# parse_disc_label — movies must not be misread as series
# ---------------------------------------------------------------------------


def test_plain_movie_label_is_a_movie() -> None:
    hint = parse_disc_label("THE_MATRIX_16X9")
    assert hint.kind == "movie"
    assert hint.title == "The Matrix"
    assert hint.season is None


def test_disc_marker_alone_does_not_imply_series() -> None:
    """Movies span two discs too. Only a season marker means series."""
    hint = parse_disc_label("LOTR_FOTR_D1")
    assert hint.kind == "movie"
    assert hint.disc == 1
    assert hint.season is None


def test_short_allcaps_title_ending_in_s_digit_is_not_a_season() -> None:
    """ALIENS3 is a movie, not ALIEN season 3."""
    hint = parse_disc_label("ALIENS3")
    assert hint.kind == "movie"
    assert hint.season is None


def test_trailing_number_in_movie_title_survives() -> None:
    hint = parse_disc_label("TERMINATOR_2")
    assert hint.kind == "movie"
    assert hint.title == "Terminator 2"


def test_empty_label_is_a_movie_with_no_title() -> None:
    hint = parse_disc_label("")
    assert hint.kind == "movie"
    assert hint.title == ""


# ---------------------------------------------------------------------------
# search_shows
# ---------------------------------------------------------------------------


def _tv_result(**kw):
    base = {
        "id": 1396,
        "name": "Breaking Bad",
        "original_name": "Breaking Bad",
        "first_air_date": "2008-01-20",
        "overview": "A chemistry teacher turns to crime.",
    }
    base.update(kw)
    return base


def test_search_shows_hits_the_tv_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = FakeResponse(json_data={"results": [_tv_result()]})
    calls = patch_get(monkeypatch, resp)
    search_shows(V3_KEY, "Breaking Bad")
    assert calls[0]["url"] == "https://api.themoviedb.org/3/search/tv"


def test_search_shows_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_get(monkeypatch, FakeResponse(json_data={"results": [_tv_result()]}))
    shows = search_shows(V3_KEY, "Breaking Bad")
    assert len(shows) == 1
    assert shows[0].tmdb_id == 1396
    assert shows[0].name == "Breaking Bad"
    assert shows[0].year == 2008


def test_search_shows_year_comes_from_first_air_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_get(
        monkeypatch,
        FakeResponse(json_data={"results": [_tv_result(first_air_date="")]}),
    )
    assert search_shows(V3_KEY, "x")[0].year is None


def test_search_shows_propagates_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_get(monkeypatch, FakeResponse(status_code=401))
    with pytest.raises(MetadataError, match="401"):
        search_shows(V3_KEY, "Breaking Bad")


def test_search_shows_empty_query_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_get(monkeypatch, FakeResponse(json_data={"results": [_tv_result()]}))
    assert search_shows(V3_KEY, "   ") == []


# ---------------------------------------------------------------------------
# season_episodes
# ---------------------------------------------------------------------------


SEASON_PAYLOAD = {
    "season_number": 1,
    "episodes": [
        {"episode_number": 1, "season_number": 1, "name": "Pilot"},
        {"episode_number": 2, "season_number": 1, "name": "Cat's in the Bag..."},
        {"episode_number": 3, "season_number": 1, "name": "...And the Bag's in the River"},
    ],
}


def test_season_episodes_builds_the_season_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = patch_get(monkeypatch, FakeResponse(json_data=SEASON_PAYLOAD))
    season_episodes(V3_KEY, 1396, 1)
    assert calls[0]["url"] == "https://api.themoviedb.org/3/tv/1396/season/1"


def test_season_episodes_maps_episodes(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_get(monkeypatch, FakeResponse(json_data=SEASON_PAYLOAD))
    episodes = season_episodes(V3_KEY, 1396, 1)
    assert [e.episode for e in episodes] == [1, 2, 3]
    assert episodes[1].name == "Cat's in the Bag..."
    assert episodes[0].season == 1


def test_season_episodes_missing_season_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_get(monkeypatch, FakeResponse(status_code=404, text="not found"))
    with pytest.raises(MetadataError, match="404"):
        season_episodes(V3_KEY, 1396, 99)


def test_season_episodes_skips_entries_without_a_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"episodes": [{"name": "Orphan"}, {"episode_number": 4, "name": "Ok"}]}
    patch_get(monkeypatch, FakeResponse(json_data=payload))
    episodes = season_episodes(V3_KEY, 1396, 1)
    assert [e.episode for e in episodes] == [4]


# ---------------------------------------------------------------------------
# jellyfin_episode_relpath
# ---------------------------------------------------------------------------


def test_episode_relpath_full_shape() -> None:
    p = jellyfin_episode_relpath(
        series="Breaking Bad", year=2008, tmdb_id=1396,
        season=1, episode=1, ep_title="Pilot",
    )
    assert p == Path(
        "Shows/Breaking Bad (2008) [tmdbid-1396]/Season 01/Breaking Bad S01E01 - Pilot.mkv"
    )


def test_episode_relpath_pads_season_and_episode_to_two_digits() -> None:
    p = jellyfin_episode_relpath("Show", 2000, 1, season=2, episode=7, ep_title="X")
    assert p.parent.name == "Season 02"
    assert p.name == "Show S02E07 - X.mkv"


def test_episode_relpath_keeps_three_digit_episode_numbers() -> None:
    p = jellyfin_episode_relpath("Show", 2000, 1, season=1, episode=104, ep_title="X")
    assert p.name == "Show S01E104 - X.mkv"


def test_episode_relpath_without_episode_title() -> None:
    p = jellyfin_episode_relpath("Show", 2000, 1, season=1, episode=3, ep_title="")
    assert p.name == "Show S01E03.mkv"


def test_episode_relpath_strips_banned_chars_from_series_and_episode() -> None:
    p = jellyfin_episode_relpath(
        "Law & Order: SVU", 1999, 2734, season=1, episode=1,
        ep_title='Payback/Revenge?',
    )
    assert ":" not in str(p)
    assert "/" not in p.name
    assert "?" not in p.name


def test_episode_relpath_without_year_or_tmdb_id() -> None:
    p = jellyfin_episode_relpath("Show", None, None, season=1, episode=1, ep_title="A")
    assert p == Path("Shows/Show/Season 01/Show S01E01 - A.mkv")


def test_episode_relpath_rejects_empty_series_name() -> None:
    with pytest.raises(MetadataError):
        jellyfin_episode_relpath("", 2000, 1, season=1, episode=1, ep_title="A")


# ---------------------------------------------------------------------------
# API key resolution (build-time default)
# ---------------------------------------------------------------------------


def test_resolve_api_key_prefers_the_configured_key() -> None:
    assert resolve_api_key("user-key") == "user-key"


def test_resolve_api_key_falls_back_to_the_baked_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "DEFAULT_TMDB_API_KEY", "baked-key")
    assert resolve_api_key("") == "baked-key"
    assert resolve_api_key("   ") == "baked-key"


def test_resolve_api_key_returns_empty_when_nothing_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "DEFAULT_TMDB_API_KEY", "")
    assert resolve_api_key("") == ""
