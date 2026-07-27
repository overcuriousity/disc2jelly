"""TMDb metadata client and Jellyfin-compliant movie/episode naming.

Implements the SPEC contract:
  - MovieMatch / ShowMatch / EpisodeInfo dataclasses
  - search_movies(api_key, query)  -> TMDb /3/search/movie (v3 api_key or v4 Bearer)
  - search_shows(api_key, query)   -> TMDb /3/search/tv
  - season_episodes(...)           -> TMDb /3/tv/<id>/season/<n>
  - parse_disc_label(label)        -> movie/series split + season/disc extraction
  - clean_query(disc_label)        -> heuristic title extraction from disc labels
  - jellyfin_movie_relpath(...)    -> Movies/<Title> (<Year>) [tmdbid-<id>]/<same>.mkv
  - jellyfin_episode_relpath(...)  -> Shows/<Series> (<Year>) [tmdbid-<id>]/
                                      Season <NN>/<Series> S<NN>E<NN> - <Title>.mkv

The TMDb credential falls back to a build-time default baked in by the Windows
installer (app/_baked.py, gitignored) so the end user never has to obtain one.
There is deliberately no key committed to this repo.

No imports from other disc2jelly app modules except the generated _baked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import requests

try:  # generated at build time; absent in a source checkout
    from ._baked import TMDB_API_KEY as DEFAULT_TMDB_API_KEY
except ImportError:
    DEFAULT_TMDB_API_KEY = ""

TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
TMDB_TV_SEARCH_URL = "https://api.themoviedb.org/3/search/tv"
TMDB_SEASON_URL = "https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}"
REQUEST_TIMEOUT = 10  # seconds, per SPEC

MOVIES_ROOT = "Movies"
SHOWS_ROOT = "Shows"
CONTAINER_SUFFIX = ".mkv"

# Characters Jellyfin reserves / that break filesystems (info.md §3).
BANNED_CHARS = '<>:"/\\|?*'
_BANNED_RE = re.compile("[" + re.escape(BANNED_CHARS) + "]")


class MetadataError(Exception):
    """Raised on TMDb failures: auth (401), HTTP errors, network errors, bad JSON."""


@dataclass
class MovieMatch:
    tmdb_id: int
    title: str
    original_title: str
    year: int | None
    overview: str


@dataclass
class ShowMatch:
    tmdb_id: int
    name: str
    original_name: str
    year: int | None
    overview: str


@dataclass
class EpisodeInfo:
    season: int
    episode: int
    name: str


@dataclass
class DiscHint:
    """What a disc volume label suggests about its contents."""

    kind: str            # "movie" | "series"
    title: str
    season: int | None
    disc: int | None


def resolve_api_key(configured: str) -> str:
    """Configured credential, else the build-time baked default, else ''."""
    return (configured or "").strip() or DEFAULT_TMDB_API_KEY


# ---------------------------------------------------------------------------
# TMDb search
# ---------------------------------------------------------------------------

def _is_v4_token(credential: str) -> bool:
    """Heuristic per SPEC: v4 read-access tokens are long JWTs (dots, >100 chars)."""
    return len(credential) > 100 and "." in credential


def _year_from_release_date(release_date: object) -> int | None:
    """Year = first 4 chars of release_date; empty/malformed -> None (info.md §4)."""
    if not isinstance(release_date, str):
        return None
    release_date = release_date.strip()
    if len(release_date) < 4:
        return None
    head = release_date[:4]
    if not head.isdigit():
        return None
    return int(head)


def _tmdb_require_credential(api_key: str) -> None:
    if not (api_key or "").strip():
        raise MetadataError("No TMDb API credential configured")


def _tmdb_get(api_key: str, url: str, params: dict[str, str]) -> dict:
    """Authenticated TMDb GET returning decoded JSON, or raising MetadataError."""
    credential = (api_key or "").strip()
    if not credential:
        raise MetadataError("No TMDb API credential configured")

    headers: dict[str, str] = {"Accept": "application/json"}
    if _is_v4_token(credential):
        headers["Authorization"] = f"Bearer {credential}"
    else:
        params = {**params, "api_key": credential}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise MetadataError(f"TMDb request failed: {exc}") from exc

    if resp.status_code == 401:
        raise MetadataError(
            "TMDb authentication failed (HTTP 401): check your API key / read access token"
        )
    if resp.status_code == 429:
        raise MetadataError("TMDb rate limit hit (HTTP 429): try again in a few seconds")
    if resp.status_code != 200:
        body = (resp.text or "")[:200]
        raise MetadataError(f"TMDb request failed: HTTP {resp.status_code}: {body}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise MetadataError("TMDb returned invalid JSON") from exc
    return data if isinstance(data, dict) else {}


def search_movies(api_key: str, query: str) -> list[MovieMatch]:
    """Search TMDb /3/search/movie. Accepts a v3 api_key OR a v4 Bearer token."""
    query = (query or "").strip()
    if not query:
        _tmdb_require_credential(api_key)
        return []

    data = _tmdb_get(
        api_key, TMDB_SEARCH_URL, {"query": query, "include_adult": "false"}
    )

    matches: list[MovieMatch] = []
    for item in data.get("results") or []:
        try:
            tmdb_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        matches.append(
            MovieMatch(
                tmdb_id=tmdb_id,
                title=item.get("title") or "",
                original_title=item.get("original_title") or "",
                year=_year_from_release_date(item.get("release_date")),
                overview=item.get("overview") or "",
            )
        )
    return matches


def search_shows(api_key: str, query: str) -> list[ShowMatch]:
    """Search TMDb /3/search/tv. Year comes from first_air_date."""
    query = (query or "").strip()
    if not query:
        _tmdb_require_credential(api_key)
        return []

    data = _tmdb_get(
        api_key, TMDB_TV_SEARCH_URL, {"query": query, "include_adult": "false"}
    )

    matches: list[ShowMatch] = []
    for item in data.get("results") or []:
        try:
            tmdb_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        matches.append(
            ShowMatch(
                tmdb_id=tmdb_id,
                name=item.get("name") or "",
                original_name=item.get("original_name") or "",
                year=_year_from_release_date(item.get("first_air_date")),
                overview=item.get("overview") or "",
            )
        )
    return matches


def season_episodes(api_key: str, tmdb_id: int, season: int) -> list[EpisodeInfo]:
    """Episode list for one season via TMDb /3/tv/<id>/season/<n>."""
    url = TMDB_SEASON_URL.format(tmdb_id=int(tmdb_id), season=int(season))
    data = _tmdb_get(api_key, url, {})

    episodes: list[EpisodeInfo] = []
    for item in data.get("episodes") or []:
        try:
            number = int(item["episode_number"])
        except (KeyError, TypeError, ValueError):
            continue
        episodes.append(
            EpisodeInfo(
                season=int(item.get("season_number") or season),
                episode=number,
                name=item.get("name") or "",
            )
        )
    return episodes


# ---------------------------------------------------------------------------
# Disc-label -> search query heuristic
# ---------------------------------------------------------------------------

# Small words kept lowercase in title case (unless first word).
_SMALL_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of",
    "on", "or", "per", "so", "the", "to", "up", "via", "with", "from", "into",
    "over", "von", "van", "de", "der", "die", "das", "le", "la", "les", "des",
}

# Noise markers found on disc labels (info.md §1 examples: BREAKINGBADS1,
# "Breaking Bad: Season 1: Disc 1", THE_MATRIX_16X9).
_NOISE_RES = [
    re.compile(r"^\d{1,2}X\d{1,2}$", re.I),                      # 16X9, 4X3 aspect
    re.compile(r"^(WS|FS)$", re.I),                               # widescreen/fullscreen
    re.compile(r"^(NTSC|PAL|SECAM)$", re.I),                      # TV norms
    re.compile(r"^(R\d|REGION\d)$", re.I),                        # region codes
    re.compile(r"^(BD|BLU[-]?RAY|DVD\d?|UHD|HD)$", re.I),         # media format
    re.compile(r"^(DISC|DISK|CD|DVD|BD|D|S|SEASON|SE|SERIES|VOL|VOLUME|"
               r"PART|PT|EP|EPISODE|NUM|NUMBER|NO)\d+$", re.I),  # DISC1, D1, S01, NUMBER2 (prefix required, so "2" in "TERMINATOR_2" survives)
    re.compile(r"^(DISC|DISK|SEASON|SERIES|VOLUME|VOL|PART|PT|EPISODE|EP|"
               r"NUM|NUMBER|NO)$", re.I),                         # bare markers
    re.compile(r"^(19|20)\d{2}$"),                                # stray year
    re.compile(r"^(EXTENDED|UNRATED|THEATRICAL|THEATER|DC|REMASTERED|"
               r"REMASTER|CE|SE)$", re.I),                        # edition tags (SE = special edition)
]

_SPLIT_RE = re.compile(r"[._\-:\s]+")

# Season/disc markers, both glued ("S02", "SEASON3") and bare ("SEASON" "3").
_SEASON_GLUED_RE = re.compile(r"^(?:SEASON|SERIES|SE|S)(\d{1,2})$", re.I)
_DISC_GLUED_RE = re.compile(r"^(?:DISC|DISK|D)(\d{1,2})$", re.I)
_SEASON_BARE_RE = re.compile(r"^(?:SEASON|SERIES)$", re.I)
_DISC_BARE_RE = re.compile(r"^(?:DISC|DISK)$", re.I)

# A single run-together ALL-CAPS token ending in S<n>, e.g. BREAKINGBADS1.
# The length floor keeps real titles like ALIENS3 out of it — there is no way
# to tell the two apart structurally, so this errs toward leaving movies alone
# and lets the UI's series toggle correct the rest.
_GLUED_LABEL_SEASON_RE = re.compile(r"^([A-Z]{6,})S(\d{1,2})$")


def _is_noise(token: str) -> bool:
    return any(rx.match(token) for rx in _NOISE_RES)


def _title_case(words: list[str], all_caps_mode: bool) -> str:
    """Normal title case: capitalize words, keep small words lower (except first).

    In all_caps_mode (label was ALL-CAPS/underscore style) every word is treated
    as lowercase material to be title-cased. Otherwise words with their own
    casing are kept as-is (only the first word is capitalized if it was lower).
    """
    out: list[str] = []
    for i, word in enumerate(words):
        low = word.lower()
        if all_caps_mode or word.isupper() or word.islower():
            if i > 0 and low in _SMALL_WORDS:
                out.append(low)
            elif all_caps_mode or word.isupper():
                out.append(word.capitalize())
            else:
                # mixed sentence case: leave alone, but capitalize first word
                out.append(word[0].upper() + word[1:] if i == 0 else word)
        else:
            out.append(word)
    return " ".join(out)


def clean_query(disc_label: str) -> str:
    """Turn a disc label into a sane TMDb search query.

    "THE_MATRIX_16X9" -> "the Matrix"-style title case ("The Matrix" casing
    per normal title-case rules), "LOTR_FOTR_D1" -> "Lotr Fotr".
    """
    label = (disc_label or "").strip()
    if not label:
        return ""
    tokens = [t for t in _SPLIT_RE.split(label) if t]
    kept = [t for t in tokens if not _is_noise(t)]
    if not kept:  # everything was noise; fall back to raw tokens
        kept = tokens
    all_caps_mode = all(t.isupper() or t.isdigit() for t in kept) and any(
        t.isalpha() for t in kept
    )
    return _title_case(kept, all_caps_mode)


def _extract_markers(tokens: list[str]) -> tuple[list[str], int | None, int | None]:
    """Pull season/disc numbers out of a token stream, returning what is left.

    Handles glued markers ("S02", "D3") and bare marker + number pairs
    ("SEASON", "1"). A disc marker on its own does not imply a series — plenty
    of movies ship across two discs.
    """
    remaining: list[str] = []
    season: int | None = None
    disc: int | None = None

    i = 0
    while i < len(tokens):
        token = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None

        glued_season = _SEASON_GLUED_RE.match(token)
        glued_disc = _DISC_GLUED_RE.match(token)

        if glued_season and season is None:
            season = int(glued_season.group(1))
        elif glued_disc and disc is None:
            disc = int(glued_disc.group(1))
        elif _SEASON_BARE_RE.match(token) and nxt and nxt.isdigit():
            season = season if season is not None else int(nxt)
            i += 1
        elif _DISC_BARE_RE.match(token) and nxt and nxt.isdigit():
            disc = disc if disc is not None else int(nxt)
            i += 1
        else:
            remaining.append(token)
        i += 1

    return remaining, season, disc


def parse_disc_label(disc_label: str) -> DiscHint:
    """Classify a disc label as movie or series and extract season/disc numbers.

    "Breaking Bad: Season 1: Disc 1" -> series, "Breaking Bad", season 1, disc 1
    "LOTR_FOTR_D1"                   -> movie,  "Lotr Fotr",     disc 1
    "THE_MATRIX_16X9"                -> movie,  "The Matrix"
    """
    label = (disc_label or "").strip()
    if not label:
        return DiscHint(kind="movie", title="", season=None, disc=None)

    tokens = [t for t in _SPLIT_RE.split(label) if t]

    # Run-together labels carry their season with no separator to split on.
    glued_season: int | None = None
    if len(tokens) == 1:
        glued = _GLUED_LABEL_SEASON_RE.match(tokens[0])
        if glued:
            tokens = [glued.group(1)]
            glued_season = int(glued.group(2))

    remaining, season, disc = _extract_markers(tokens)
    if season is None:
        season = glued_season

    kept = [t for t in remaining if not _is_noise(t)]
    if not kept:
        kept = remaining or tokens

    all_caps_mode = all(t.isupper() or t.isdigit() for t in kept) and any(
        t.isalpha() for t in kept
    )
    return DiscHint(
        kind="series" if season is not None else "movie",
        title=_title_case(kept, all_caps_mode),
        season=season,
        disc=disc,
    )


# ---------------------------------------------------------------------------
# Jellyfin naming
# ---------------------------------------------------------------------------

def strip_banned_chars(name: str) -> str:
    """Remove Jellyfin reserved chars and collapse resulting whitespace."""
    cleaned = _BANNED_RE.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def jellyfin_movie_relpath(
    title: str, year: int | None, tmdb_id: int | None = None
) -> Path:
    """Relative path per https://jellyfin.org/docs/general/server/media/movies.

    Folder: Movies/<Title> (<Year>)[ tmdbid-<id>]/
    File:   <folder name, char-for-char identical>.mkv
    """
    clean = strip_banned_chars(title or "")
    if not clean:
        raise MetadataError("Cannot build a Jellyfin path from an empty title")
    folder = _decorate(clean, year, tmdb_id)
    return Path(MOVIES_ROOT) / folder / f"{folder}{CONTAINER_SUFFIX}"


def _decorate(name: str, year: int | None, tmdb_id: int | None) -> str:
    """Append the "(<Year>) [tmdbid-<id>]" tags Jellyfin uses for matching."""
    if year is not None:
        name += f" ({year})"
    if tmdb_id is not None:
        name += f" [tmdbid-{tmdb_id}]"
    return name


def jellyfin_episode_relpath(
    series: str,
    year: int | None,
    tmdb_id: int | None,
    season: int,
    episode: int,
    ep_title: str,
) -> Path:
    """Relative path per https://jellyfin.org/docs/general/server/media/shows.

    Shows/<Series> (<Year>) [tmdbid-<id>]/Season <NN>/<Series> S<NN>E<NN> - <Title>.mkv

    The series folder carries the year and tmdbid tags; the episode file does
    not, because Jellyfin matches episodes on the SxxExx token alone.
    """
    clean_series = strip_banned_chars(series or "")
    if not clean_series:
        raise MetadataError("Cannot build a Jellyfin path from an empty series name")

    folder = _decorate(clean_series, year, tmdb_id)
    stem = f"{clean_series} S{season:02d}E{episode:02d}"
    clean_title = strip_banned_chars(ep_title or "")
    if clean_title:
        stem += f" - {clean_title}"
    return (
        Path(SHOWS_ROOT)
        / folder
        / f"Season {season:02d}"
        / f"{stem}{CONTAINER_SUFFIX}"
    )
