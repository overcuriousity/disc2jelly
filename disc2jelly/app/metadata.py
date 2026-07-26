"""TMDb metadata client and Jellyfin-compliant movie naming.

Implements the SPEC contract:
  - MovieMatch dataclass
  - search_movies(api_key, query)  -> TMDb /3/search/movie (v3 api_key or v4 Bearer)
  - clean_query(disc_label)        -> heuristic title extraction from disc labels
  - jellyfin_movie_relpath(...)    -> Movies/<Title> (<Year>) [tmdbid-<id>]/<same>.mkv

No imports from other disc2jelly app modules (owned by other coders).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import requests

TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
REQUEST_TIMEOUT = 10  # seconds, per SPEC

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


def search_movies(api_key: str, query: str) -> list[MovieMatch]:
    """Search TMDb /3/search/movie. Accepts a v3 api_key OR a v4 Bearer token."""
    credential = (api_key or "").strip()
    if not credential:
        raise MetadataError("No TMDb API credential configured")
    query = (query or "").strip()
    if not query:
        return []

    params: dict[str, str] = {"query": query, "include_adult": "false"}
    headers: dict[str, str] = {"Accept": "application/json"}
    if _is_v4_token(credential):
        headers["Authorization"] = f"Bearer {credential}"
    else:
        params["api_key"] = credential

    try:
        resp = requests.get(
            TMDB_SEARCH_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT
        )
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
        raise MetadataError(f"TMDb search failed: HTTP {resp.status_code}: {body}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise MetadataError("TMDb returned invalid JSON") from exc

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

_SPLIT_RE = re.compile(r"[._\-]+")


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
    folder = clean
    if year is not None:
        folder += f" ({year})"
    if tmdb_id is not None:
        folder += f" [tmdbid-{tmdb_id}]"
    return Path("Movies") / folder / f"{folder}.mkv"
