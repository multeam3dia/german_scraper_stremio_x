"""Confidence-aware provider-title identity resolution via TMDB.

The resolver is intentionally conservative: it never promotes a title based on
fuzzy title similarity alone. A candidate must have strong title/metadata evidence
and a clear margin over the runner-up. IMDb is attached when available, but is not
required for a confident canonical TMDB identity.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import difflib
from functools import lru_cache
import json
import logging
import os
import re
import unicodedata
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

WIKIDATA_ENTITY_BASE = "https://www.wikidata.org/wiki/Special:EntityData"

from catalog_db import (
    init_db,
    get_unresolved_titles,
    mark_unresolved_title_resolved,
    record_discovery,
    record_unresolved_attempt,
    get_tmdb_cached_response,
    set_tmdb_cached_response,
    purge_expired_tmdb_cache,
    record_tmdb_cache_usage,
    get_tmdb_cache_usage,
    get_tmdb_cache_usage_totals,
    mark_unresolved_title_identified,
)

from catalog_identity import (
    add_external_id,
    create_title,
    get_title_by_external_id,
    get_title_by_provider_url,
    link_provider_title,
)

logger = logging.getLogger(__name__)
TMDB_API_BASE = "https://api.themoviedb.org/3"
IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
MIN_CONFIDENCE = 0.94
MIN_MARGIN = 0.08
MIN_TITLE_SCORE_WITH_YEAR = 0.94

# Persistent resolver retry delays, indexed by the number of prior resolver attempts.
# A new item (zero attempts) is always eligible; failures back off up to 90 days.
RESOLVER_RETRY_DAYS = (1, 3, 7, 14, 30, 60, 90)


def resolver_retry_due(record: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Return whether an unresolved record is eligible for another resolver pass."""
    try:
        attempts = max(0, int(record.get("resolver_attempts") or 0))
    except (TypeError, ValueError):
        attempts = 0
    if attempts == 0:
        return True
    last_attempt = record.get("resolver_last_attempt_at")
    if not last_attempt:
        return True
    try:
        timestamp = datetime.fromisoformat(str(last_attempt))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    delay_days = RESOLVER_RETRY_DAYS[min(attempts - 1, len(RESOLVER_RETRY_DAYS) - 1)]
    return current >= timestamp + timedelta(days=delay_days)

GENRE_ALIASES = {
    "abenteuer": "adventure", "komodie": "comedy", "krimi": "crime",
    "kriminalfilm": "crime", "dokumentation": "documentary", "doku": "documentary",
    "familie": "family", "geschichte": "history", "musik": "music",
    "liebe": "romance", "romantik": "romance", "sci fi": "science fiction",
    "science fiction": "science fiction", "krieg": "war",
    "action abenteuer": "action", "action adventure": "action",
    "action and adventure": "action", "action and abenteuer": "action",
    "sci fi fantasy": "fantasy", "zeichentrick": "animation",
    "alltagsleben": "slice of life", "slice of life": "slice of life",
}

# AniWorld mixes audio/subtitle availability labels into its genre list.
# These are not genres and must not distort the TMDB genre comparison.
IGNORED_PROVIDER_GENRES = {
    "ger", "gersub", "eng", "engsub", "dub", "sub", "deutsch",
    "deutsch sub", "deutsche synchro", "omu", "originalton",
}

COUNTRY_ALIASES = {
    "japan": "jp", "deutschland": "de", "germany": "de",
    "vereinigte staaten": "us", "united states": "us", "usa": "us",
    "grossbritannien": "gb", "great britain": "gb", "united kingdom": "gb",
    "frankreich": "fr", "france": "fr", "sudkorea": "kr",
    "south korea": "kr", "china": "cn", "kanada": "ca", "canada": "ca",
}


class TMDBConfigurationError(RuntimeError):
    """TMDB credentials are not configured."""


class TMDBAPIError(RuntimeError):
    """TMDB could not return a valid response."""


@lru_cache(maxsize=16384)
def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("&", " and ")
    # Preserve non-Latin scripts (notably Japanese and Chinese anime titles)
    # instead of silently deleting them before title comparison.
    return " ".join(re.findall(r"[^\W_]+", value, flags=re.UNICODE))


@lru_cache(maxsize=32768)
def _similarity_ratio(left: str, right: str) -> float:
    """Cache repeated directional SequenceMatcher comparisons."""
    return difflib.SequenceMatcher(None, left, right).ratio()


def _normalize(value: Any) -> str:
    return _normalize_text(str(value or ""))


def _tmdb_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call TMDB using either a v4 read token or a v3 API key."""
    token = os.getenv("TMDB_READ_ACCESS_TOKEN", "").strip()
    api_key = os.getenv("TMDB_API_KEY", "").strip()
    if not token and not api_key:
        raise TMDBConfigurationError(
            "Set TMDB_READ_ACCESS_TOKEN or TMDB_API_KEY to enable identity matching"
        )

    query = dict(params or {})
    headers = {"Accept": "application/json", "User-Agent": "GermanScraperStremioX/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        query["api_key"] = api_key
    url = f"{TMDB_API_BASE}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"

    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise TMDBAPIError(f"TMDB returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise TMDBAPIError("Could not reach TMDB") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TMDBAPIError("TMDB returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise TMDBAPIError("TMDB returned an unexpected response")
    return payload


def _coerce_year(value: Any) -> int | None:
    match = re.search(r"\b(?:19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def _year_from_result(item: dict[str, Any], media_type: str) -> int | None:
    date_key = "first_air_date" if media_type == "series" else "release_date"
    return _coerce_year(item.get(date_key))


def _title_variants(item: dict[str, Any], media_type: str) -> list[str]:
    keys = ("name", "original_name") if media_type == "series" else ("title", "original_title")
    values = [str(item[key]) for key in keys if item.get(key)]
    aliases = item.get("_alternative_titles")
    if isinstance(aliases, list):
        values.extend(str(value) for value in aliases if value)
    return list(dict.fromkeys(values))


def _best_title_match(
    provider_title: str, item: dict[str, Any], media_type: str
) -> tuple[float, str | None]:
    source = _normalize(provider_title)
    variants = [(value, _normalize(value)) for value in _title_variants(item, media_type)]
    variants = [(raw, value) for raw, value in variants if value]
    if not source or not variants:
        return 0.0, None
    raw, normalized = max(
        variants,
        key=lambda pair: 1.0 if source == pair[1] else _similarity_ratio(source, pair[1]),
    )
    # Ignore spacing differences as well: AniWorld occasionally concatenates
    # words (for example "TwoCar" vs. TMDB's "Two Car").
    compact_source = source.replace(" ", "")
    compact_variant = normalized.replace(" ", "")
    score = (
        1.0 if source == normalized or compact_source == compact_variant
        else _similarity_ratio(source, normalized)
    )
    return round(score, 4), raw


def _strip_format_suffix(value: str) -> tuple[str, str | None]:
    """Remove a trailing format label while preserving what was removed.

    Labels such as 'Shorts', 'OVA' and 'Special' can describe a related
    release rather than a different work. They are therefore a weaker title
    signal and always need independent corroboration before auto-matching.
    """
    normalized = " ".join(str(value or "").strip().split())
    match = re.search(r"(?i)(?:\s*[-:–—]\s*|\s+)(shorts?|ova|specials?|movie|film)$", normalized)
    if not match:
        return normalized, None
    core = normalized[:match.start()].strip(" -:–—")
    return (core, match.group(1).casefold()) if core else (normalized, None)


def _format_suffix_match(
    provider_title: str, item: dict[str, Any], media_type: str
) -> tuple[float, str | None, str | None]:
    """Return a deliberately sub-threshold match after removing a format suffix."""
    source_core, source_suffix = _strip_format_suffix(provider_title)
    best: tuple[float, str | None, str | None] = (0.0, None, None)
    for raw in _title_variants(item, media_type):
        candidate_core, candidate_suffix = _strip_format_suffix(raw)
        if not candidate_suffix:
            continue
        score = _title_similarity(source_core, {"name": candidate_core,
            "original_name": candidate_core} if media_type == "series" else
            {"title": candidate_core, "original_title": candidate_core}, media_type)
        if score > best[0]:
            best = (score, raw, candidate_suffix)
    return best


def _title_similarity(
    provider_title: str, item: dict[str, Any], media_type: str
) -> float:
    return _best_title_match(provider_title, item, media_type)[0]


def _search_query_variants(
    provider_title: str, alternative_titles: list[str] | None = None,
) -> list[str]:
    """Build bounded TMDB queries while reserving slots for provider aliases."""
    candidates = [provider_title]
    # AniWorld format labels are often appended to the actual work title.
    # Keep the full title first, then try a conservative base-title variant.
    without_format_suffix = re.sub(
        r"\s*(?:[:\-–]\s*)?(?:sd\s+theatre|short\s+animation\s+series|"
        r"short\s+animation|special\s+episode|special|ova)$",
        "", provider_title, flags=re.IGNORECASE,
    ).strip()
    if without_format_suffix and _normalize(without_format_suffix) != _normalize(provider_title):
        candidates.append(without_format_suffix)
    punctuation_light = re.sub(r"[^\w\s]", " ", provider_title, flags=re.UNICODE)
    punctuation_light = " ".join(punctuation_light.split())
    if punctuation_light and _normalize(punctuation_light) != _normalize(provider_title):
        candidates.append(punctuation_light)
    without_parenthetical = re.sub(r"\s*[（(][^）)]*[）)]\s*", " ", provider_title)
    without_parenthetical = " ".join(without_parenthetical.split())
    if without_parenthetical and _normalize(without_parenthetical) not in {
        _normalize(value) for value in candidates
    }:
        candidates.append(without_parenthetical)

    # Keep at most two derived queries in addition to the original spelling.
    # Otherwise titles with several cleanup variants can crowd provider-supplied
    # alternate titles out of the seven-query budget.
    query_candidates = candidates[:3]
    seen = {_normalize(value) for value in query_candidates}
    for alias in alternative_titles or []:
        alias = str(alias or "").strip()
        normalized_alias = _normalize(alias)
        if alias and normalized_alias not in seen and len(query_candidates) < 7:
            query_candidates.append(alias)
            seen.add(normalized_alias)
    return query_candidates
def _alternative_title_values(payload: dict[str, Any]) -> list[str]:
    """Read title aliases from both TMDB TV and movie alternative-title responses."""
    values: list[str] = []
    for key in ("results", "titles"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                value = row.get("title") or row.get("name")
                if value:
                    values.append(str(value))
    return list(dict.fromkeys(values))


def _best_provider_title_match(
    provider_title: str, alternative_titles: list[str], item: dict[str, Any],
    media_type: str,
) -> tuple[float, str | None]:
    """Compare titles while treating a trailing parenthesized year as metadata."""
    # AniWorld commonly embeds the release year in the display title
    # (e.g. "Kanon (2006)"). TMDB stores that year separately. Comparing the
    # raw strings penalizes an otherwise exact title match, so remove only a
    # terminal four-digit year in parentheses before title scoring.
    title_for_match = re.sub(
        r"\s*[（(](?:19|20)\d{2}[）)]\s*$", "", provider_title
    ).strip()
    titles = [title_for_match or provider_title]
    titles.extend(str(value) for value in alternative_titles if str(value or "").strip())
    matches = [_best_title_match(title, item, media_type) for title in titles]
    # On equal scores, prefer provider-supplied aliases over the cleaned display
    # title so the diagnostic records the strongest exact variant (e.g. Japanese).
    return max(
        enumerate(matches),
        key=lambda pair: (pair[1][0], pair[0] > 0),
        default=(0, (0.0, None)),
    )[1]


def _provider_title_similarity(
    provider_title: str, alternative_titles: list[str], item: dict[str, Any],
    media_type: str,
) -> float:
    return _best_provider_title_match(
        provider_title, alternative_titles, item, media_type
    )[0]


def _normalize_genre(value: Any) -> str:
    normalized = _normalize(value)
    return GENRE_ALIASES.get(normalized, normalized)


def _genre_similarity(
    provider_genres: list[str],
    item: dict[str, Any],
    genre_map: dict[int, str],
) -> float | None:
    """Compare actual genres, excluding AniWorld audio/subtitle labels."""
    provider_set = {
        _normalize_genre(value)
        for value in provider_genres
        if _normalize_genre(value)
        and _normalize(value) not in IGNORED_PROVIDER_GENRES
    }
    candidate_set = set()
    for raw_id in item.get("genre_ids") or []:
        try:
            name = genre_map.get(int(raw_id))
        except (TypeError, ValueError):
            name = None
        if name:
            normalized = _normalize_genre(name)
            if normalized:
                candidate_set.add(normalized)
    if not provider_set or not candidate_set:
        return None
    return round(len(provider_set & candidate_set) / len(provider_set | candidate_set), 4)


def _person_similarity(provider_people: list[str], candidate_people: list[str]) -> float | None:
    """Compare optional cast names; missing metadata is neutral, not a mismatch."""
    provider_set = {_normalize(value) for value in provider_people if _normalize(value)}
    candidate_set = {_normalize(value) for value in candidate_people if _normalize(value)}
    if not provider_set or not candidate_set:
        return None
    # Provider cast lists are usually shorter than TMDB's; overlap coefficient
    # avoids penalizing a provider for not listing the complete cast.
    return round(len(provider_set & candidate_set) / min(len(provider_set), len(candidate_set)), 4)


def _candidate_credits(
    item: dict[str, Any], media_type: str,
    search_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any] | None:
    """Fetch one credits payload for a shortlisted candidate."""
    tmdb_id = item.get("id")
    if type(tmdb_id) is not int or tmdb_id <= 0:
        return None
    endpoint_type = "tv" if media_type == "series" else "movie"
    try:
        payload = search_fn(f"/{endpoint_type}/{tmdb_id}/credits", {})
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _candidate_details(
    item: dict[str, Any], media_type: str,
    search_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any] | None:
    """Fetch production-company metadata for a shortlisted candidate."""
    tmdb_id = item.get("id")
    if type(tmdb_id) is not int or tmdb_id <= 0:
        return None
    endpoint_type = "tv" if media_type == "series" else "movie"
    try:
        payload = search_fn(f"/{endpoint_type}/{tmdb_id}", {"language": "de-DE"})
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None



def _candidate_season_episode_titles(
    item: dict[str, Any], provider_structure: dict[str, Any],
    search_fn: Callable[..., dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Fetch a small number of TMDB season pages for episode-title corroboration."""
    tmdb_id = item.get("id")
    if type(tmdb_id) is not int or tmdb_id <= 0:
        return {}
    seasons = provider_structure.get("seasons")
    if not isinstance(seasons, list):
        return {}
    season_numbers = []
    for season in seasons:
        if not isinstance(season, dict):
            continue
        try:
            number = int(season.get("season_number"))
        except (TypeError, ValueError):
            continue
        if number > 0 and isinstance(season.get("episodes"), list):
            season_numbers.append(number)
    # The first and last numbered seasons give useful coverage without
    # turning identity resolution into a full-series metadata crawl.
    season_numbers = list(dict.fromkeys(season_numbers))
    if len(season_numbers) > 2:
        season_numbers = [season_numbers[0], season_numbers[-1]]
    result: dict[str, list[dict[str, Any]]] = {}
    for number in season_numbers:
        try:
            payload = search_fn(
                f"/tv/{tmdb_id}/season/{number}", {"language": "en-US"}
            )
        except Exception:
            continue
        rows = payload.get("episodes") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            result[str(number)] = [row for row in rows if isinstance(row, dict)]
    return result


def _episode_title_similarity(
    provider_structure: dict[str, Any],
    candidate_episode_titles: dict[str, list[dict[str, Any]]] | None,
) -> float | None:
    """Compare episode titles season-by-season, tolerating merged source episodes."""
    if not isinstance(candidate_episode_titles, dict):
        return None

    generic_title = re.compile(
        r"^(?:episode|episod|ep|folge|part|chapter|kapitel|chap|ova|special|season|staffel|"
        r"series|s)\s*\d*$|^\d+$"
    )

    def clean_rows(rows: Any, provider_side: bool) -> list[list[str]]:
        result = []
        for episode in rows or []:
            if not isinstance(episode, dict):
                continue
            keys = ("title", "title_original") if provider_side else ("name", "original_name")
            names = [
                _normalize(episode.get(key))
                for key in keys
                if _normalize(episode.get(key))
            ]
            names = [name for name in names if not generic_title.fullmatch(name)]
            if names:
                result.append(names)
        return result

    def similarity(left: str, right: str) -> float:
        if left == right:
            return 1.0
        return difflib.SequenceMatcher(None, left, right).ratio()

    total_provider = 0
    total_candidate = 0
    total_matched_provider = 0
    total_matched_candidate = 0

    for season in provider_structure.get("seasons", []):
        if not isinstance(season, dict):
            continue
        season_number = season.get("season_number")
        try:
            season_key = str(int(season_number))
        except (TypeError, ValueError):
            continue
        provider_rows = clean_rows(season.get("episodes"), True)
        candidate_rows = clean_rows(candidate_episode_titles.get(season_key), False)
        if not provider_rows or not candidate_rows:
            continue
        total_provider += len(provider_rows)
        total_candidate += len(candidate_rows)

        edges = []
        for p_index, p_names in enumerate(provider_rows):
            for c_index, c_names in enumerate(candidate_rows):
                score = max(
                    (similarity(left, right) for left in p_names for right in c_names),
                    default=0.0,
                )
                edges.append((score, p_index, (c_index,)))
                if c_index + 1 < len(candidate_rows):
                    joined_variants = [
                        " ".join((left, right))
                        for left in c_names
                        for right in candidate_rows[c_index + 1]
                    ]
                    merged_score = max(
                        (similarity(provider_name, joined)
                         for provider_name in p_names for joined in joined_variants),
                        default=0.0,
                    )
                    edges.append((merged_score, p_index, (c_index, c_index + 1)))

        used_provider: set[int] = set()
        used_candidate: set[int] = set()
        matched_provider = 0
        matched_candidate = 0
        for score, p_index, c_indices in sorted(edges, reverse=True):
            if score < 0.78 or p_index in used_provider or any(
                index in used_candidate for index in c_indices
            ):
                continue
            used_provider.add(p_index)
            used_candidate.update(c_indices)
            matched_provider += 1
            matched_candidate += len(c_indices)
        total_matched_provider += matched_provider
        total_matched_candidate += matched_candidate

    if min(total_provider, total_candidate) < 3:
        return None
    return round(min(
        total_matched_provider / total_provider,
        total_matched_candidate / total_candidate,
    ), 4)


def _names_from_people(rows: Any, key: str = "name") -> list[str]:
    if not isinstance(rows, list):
        return []
    return [
        str(row.get(key) or "")
        for row in rows
        if isinstance(row, dict) and row.get(key)
    ]


def _normalize_company(value: Any) -> str:
    """Normalize legal suffixes without weakening company-name matching."""
    tokens = _normalize(value).split()
    legal_suffixes = {
        "inc", "incorporated", "ltd", "limited", "llc", "plc",
        "corp", "corporation", "co", "company", "gmbh",
    }
    while tokens and tokens[-1] in legal_suffixes:
        tokens.pop()
    return " ".join(tokens)


def _company_similarity(
    provider_companies: list[str], candidate_companies: list[str],
) -> float | None:
    """Compare studio names while ignoring common legal-entity suffixes."""
    provider_set = {
        _normalize_company(value) for value in provider_companies
        if _normalize_company(value)
    }
    candidate_set = {
        _normalize_company(value) for value in candidate_companies
        if _normalize_company(value)
    }
    if not provider_set or not candidate_set:
        return None
    return round(len(provider_set & candidate_set) / min(
        len(provider_set), len(candidate_set)
    ), 4)


def _country_similarity(provider_country: Any, candidate_details: dict[str, Any] | None) -> float | None:
    """Compare a provider country with TMDB production-country metadata."""
    source = _normalize(provider_country)
    if not source or not isinstance(candidate_details, dict):
        return None
    source_code = COUNTRY_ALIASES.get(source, source)
    rows = candidate_details.get("production_countries")
    if not isinstance(rows, list):
        return None
    candidate_values: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _normalize(row.get("name"))
        code = _normalize(row.get("iso_3166_1"))
        if name:
            candidate_values.add(COUNTRY_ALIASES.get(name, name))
        if code:
            candidate_values.add(code)
    if not candidate_values:
        return None
    return 1.0 if source_code in candidate_values else 0.0


def _metadata_signal_scores(
    item: dict[str, Any], media_type: str, metadata: dict[str, Any],
    provider: str, search_fn: Callable[..., dict[str, Any]],
) -> dict[str, float | None]:
    """Compare optional provider credits/studio against TMDB, without penalizing gaps."""
    directors = metadata.get("directors")
    directors = (
        [str(value).strip() for value in directors if value is not None and str(value).strip()]
        if isinstance(directors, list) else []
    )
    actors = metadata.get("actors")
    actors = (
        [str(value).strip() for value in actors if value is not None and str(value).strip()]
        if isinstance(actors, list) else []
    )

    # AniWorld sometimes mislabels production companies as actors (e.g. the
    # studio name is copied into the actor list). Remove exact company duplicates
    # from actor evidence; the studio signal is evaluated separately.
    studio = metadata.get("producer") if provider == "aniworld" else None
    if isinstance(studio, str) and studio.strip():
        actors = [
            actor for actor in actors
            if not isinstance(actor, str)
            or _normalize_company(actor) != _normalize_company(studio)
        ]
    country = metadata.get("country")
    # AniWorld occasionally puts a country label into the studio field.
    # Treat an exact country/studio duplicate as missing studio metadata so it
    # cannot create a false corroboration mismatch.
    if (
        isinstance(studio, str) and isinstance(country, str)
        and _normalize_company(studio) == _normalize_company(country)
    ):
        studio = None
    studios = [studio] if isinstance(studio, str) and studio.strip() else []

    scores: dict[str, float | None] = {
        "actor_score": None, "director_score": None,
        "studio_score": None, "country_score": None,
    }
    if (actors or directors) and type(item.get("id")) is int:
        credits = _candidate_credits(item, media_type, search_fn)
        if credits is not None:
            scores["actor_score"] = _person_similarity(
                actors, _names_from_people(credits.get("cast"))
            ) if actors else None
            crew = credits.get("crew")
            director_names = [
                str(person.get("name") or "")
                for person in crew
                if isinstance(person, dict) and person.get("job") == "Director"
            ] if isinstance(crew, list) else []
            scores["director_score"] = _person_similarity(
                directors, director_names
            ) if directors else None

    country = metadata.get("country")
    if studios or country:
        details = _candidate_details(item, media_type, search_fn)
        if studios:
            companies = _names_from_people(details.get("production_companies")) if details else []
            scores["studio_score"] = _company_similarity(studios, companies)
        if country:
            scores["country_score"] = _country_similarity(country, details)
    return scores


def _independent_corroboration_values(
    signals: dict[str, float | None], provider_metadata: dict[str, Any],
) -> list[float]:
    """Return corroboration scores without counting one person twice across roles."""
    values = []
    actor_score = signals.get("actor_score")
    director_score = signals.get("director_score")
    actors = provider_metadata.get("actors")
    directors = provider_metadata.get("directors")
    actors = actors if isinstance(actors, list) else []
    directors = directors if isinstance(directors, list) else []
    actor_names = {_normalize(name) for name in actors if _normalize(name)}
    director_names = {_normalize(name) for name in directors if _normalize(name)}
    shared_people = actor_names & director_names

    if actor_score is not None:
        values.append(actor_score)
    if director_score is not None:
        # Actor/director evidence is not independent when a person appears
        # in both provider lists; retain only the stronger role score.
        if shared_people:
            if actor_score is None:
                values.append(director_score)
            elif director_score > actor_score:
                values[values.index(actor_score)] = director_score
        else:
            values.append(director_score)

    studio_score = signals.get("studio_score")
    if studio_score is not None:
        values.append(studio_score)
    return values


def _description_similarity(provider_text: str, candidate_text: str) -> float | None:
    """Weak same-language synopsis overlap; never used to reject a candidate."""
    stop = {
        "aber", "alle", "auch", "auf", "aus", "bei", "das", "dass", "der", "die",
        "ein", "eine", "einer", "eines", "er", "es", "für", "hat", "im", "in",
        "ist", "mit", "nach", "nicht", "oder", "sich", "sie", "und", "von", "zu",
        "the", "and", "for", "from", "with", "this", "that", "into", "their", "they",
    }
    left = {word for word in _normalize(provider_text).split() if len(word) > 3 and word not in stop}
    right = {word for word in _normalize(candidate_text).split() if len(word) > 3 and word not in stop}
    if len(left) < 4 or len(right) < 4:
        return None
    return round(len(left & right) / min(len(left), len(right)), 4)


def _structure_comparison(
    provider_structure: dict[str, Any], candidate_details: dict[str, Any] | None,
    candidate_episode_titles: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Compare provider season/episode counts with TMDB; missing data stays neutral."""
    if not isinstance(provider_structure, dict) or not provider_structure.get("complete"):
        return {"status": "not_available", "score": None, "mismatch": False}
    if not isinstance(candidate_details, dict):
        return {"status": "not_available", "score": None, "mismatch": False}

    try:
        provider_episodes = int(provider_structure.get("episode_count"))
        provider_seasons = int(provider_structure.get("season_count"))
        tmdb_episodes = int(candidate_details.get("number_of_episodes"))
        tmdb_seasons = int(candidate_details.get("number_of_seasons"))
    except (TypeError, ValueError):
        return {"status": "not_available", "score": None, "mismatch": False}
    if min(provider_episodes, provider_seasons, tmdb_episodes, tmdb_seasons) < 1:
        return {"status": "not_available", "score": None, "mismatch": False}

    episode_delta = abs(provider_episodes - tmdb_episodes)
    episode_ratio = episode_delta / max(provider_episodes, tmdb_episodes)
    season_delta = abs(provider_seasons - tmdb_seasons)
    # A large episode-count contradiction is strong negative evidence. A small
    # discrepancy is common for specials, split cours and provider numbering.
    episode_mismatch = episode_delta >= max(5, int(max(provider_episodes, tmdb_episodes) * 0.25))
    season_mismatch = season_delta >= 2 and min(provider_seasons, tmdb_seasons) >= 2
    mismatch = episode_mismatch or season_mismatch

    provider_by_season = {
        str(item.get("season_number")): int(item.get("episode_count", 0))
        for item in provider_structure.get("seasons", [])
        if isinstance(item, dict) and item.get("season_number") is not None
    }
    tmdb_by_season = candidate_details.get("seasons")
    season_comparable = isinstance(tmdb_by_season, list) and bool(provider_by_season)
    season_detail_mismatch = False
    if season_comparable:
        tmdb_map = {
            str(item.get("season_number")): int(item.get("episode_count", 0))
            for item in tmdb_by_season
            if isinstance(item, dict) and item.get("season_number") is not None
            and int(item.get("season_number", 0)) > 0
        }
        shared = set(provider_by_season) & set(tmdb_map)
        if shared:
            season_detail_mismatch = any(
                abs(provider_by_season[key] - tmdb_map[key]) >= max(
                    5, int(max(provider_by_season[key], tmdb_map[key]) * 0.35)
                )
                for key in shared
            )
            mismatch = mismatch or season_detail_mismatch

    episode_title_score = _episode_title_similarity(
        provider_structure, candidate_episode_titles
    )
    count_score = round(max(0.0, 1.0 - episode_ratio * 0.5 - season_delta * 0.05), 4)
    structure_score = max(
        count_score,
        episode_title_score or 0.0,
    )
    return {
        "status": "mismatch" if mismatch else "match",
        "score": 0.0 if mismatch else structure_score,
        "episode_title_score": episode_title_score,
        "mismatch": mismatch,
        "provider": {"season_count": provider_seasons, "episode_count": provider_episodes,
                     "seasons": provider_by_season},
        "tmdb": {"season_count": tmdb_seasons, "episode_count": tmdb_episodes,
                 "seasons": {
                     str(item.get("season_number")): int(item.get("episode_count", 0))
                     for item in tmdb_by_season
                     if isinstance(item, dict) and item.get("season_number") is not None
                     and int(item.get("season_number", 0)) > 0
                 } if isinstance(tmdb_by_season, list) else {}},
        "episode_delta": episode_delta,
        "season_delta": season_delta,
        "season_detail_mismatch": season_detail_mismatch,
    }


def _is_short_ambiguous_title(value: str) -> bool:
    normalized = _normalize(value)
    words = normalized.split()
    # Short, single-word titles are common collisions; even exact matches
    # need year plus independent metadata before automatic identity assignment.
    return len(words) == 1 and len(normalized) <= 6


def _candidate_score(
    provider_title: str, provider_year: int | None,
    item: dict[str, Any], media_type: str,
    provider_genres: list[str] | None = None,
    genre_map: dict[int, str] | None = None,
) -> float:
    title_score = _title_similarity(provider_title, item, media_type)
    if title_score <= 0:
        return 0.0
    # Year is applied only during ranking below, not to the candidate's
    # confidence score. Provider years can be unreliable (especially AniWorld).
    score = title_score
    genre_score = (
        _genre_similarity(provider_genres or [], item, genre_map or {})
        if provider_genres and genre_map else None
    )
    # Genre can only be a weak positive tie-breaker. A mismatch is neutral:
    # provider genre taxonomies are incomplete and often not comparable.
    if genre_score is not None:
        score += max(0.0, genre_score - 0.5) * 0.01
    return round(max(0.0, min(1.0, score)), 4)


def _wikidata_imdb_id(
    wikidata_id: str,
    *,
    fetch_fn: Callable[[str], dict[str, Any]] | None = None,
) -> str | None:
    """Resolve a Wikidata item to IMDb via its P345 identifier, when available."""
    identifier = str(wikidata_id or "").strip()
    if not re.fullmatch(r"Q[1-9]\d*", identifier):
        return None
    try:
        if fetch_fn is not None:
            payload = fetch_fn(identifier)
        else:
            request = Request(
                f"{WIKIDATA_ENTITY_BASE}/{identifier}.json",
                headers={"Accept": "application/json",
                         "User-Agent": "GermanScraperStremioX/1.0 (identity resolution)"},
            )
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        entity = payload.get("entities", {}).get(identifier, {})
        claims = entity.get("claims", {}).get("P345", [])
        for claim in claims:
            value = (
                claim.get("mainsnak", {}).get("datavalue", {}).get("value")
                if isinstance(claim, dict) else None
            )
            if isinstance(value, str) and IMDB_ID_RE.fullmatch(value):
                return value
    except Exception:
        logger.debug("Wikidata IMDb lookup failed for %s", identifier, exc_info=True)
    return None


def find_tmdb_match(
    record: dict[str, Any],
    *,
    search_fn: Callable[..., dict[str, Any]] = _tmdb_get,
    genre_map: dict[int, str] | None = None,
    wikidata_lookup_fn: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a conservative TMDB candidate; does not write to the catalog."""
    media_type = record.get("media_type")
    if media_type not in {"movie", "series"}:
        return {"status": "invalid", "reason": "Unsupported media type"}

    provider_title = str(record.get("provider_title") or "").strip()
    if not provider_title:
        return {"status": "no_match", "reason": "Provider title is empty"}

    # Memoize identical TMDB requests for this one title resolution. Some
    # candidates need details both for metadata corroboration and structure
    # checks; keep the cache local to avoid stale cross-title evidence.
    single_request_cache: dict[str, dict[str, Any]] = {}
    raw_search_fn = search_fn

    def cached_search(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        key = json.dumps([path, params or {}], sort_keys=True, ensure_ascii=False, default=str)
        if key not in single_request_cache:
            single_request_cache[key] = raw_search_fn(path, params)
        return copy.deepcopy(single_request_cache[key])

    search_fn = cached_search
    provider_year = _coerce_year(record.get("year"))
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    provider_aliases = metadata.get("alternative_titles")
    provider_aliases = (
        [str(value).strip() for value in provider_aliases if str(value).strip()]
        if isinstance(provider_aliases, list) else []
    )
    provider_genres = metadata.get("provider_genres")
    if not isinstance(provider_genres, list):
        provider_genres = []
    provider = str(record.get("provider") or "")
    if provider == "aniworld" and metadata.get("multi_movie_collection"):
        return {
            "status": "ambiguous",
            "reason": (
                "The AniWorld page contains multiple independent film parts; "
                "one page-level IMDb identity would incorrectly merge them"
            ),
            "multi_movie_collection": True,
            "diagnostics": {
                "provider_title": provider_title,
                "provider_year": provider_year,
                "provider_structure": metadata.get("provider_structure"),
                "movie_collection_count": metadata.get("movie_collection_count"),
            },
        }
    if provider_genres and genre_map is None:
        genre_endpoint = "/genre/tv/list" if media_type == "series" else "/genre/movie/list"
        try:
            genre_payload = search_fn(genre_endpoint, {"language": "de-DE"})
            genre_map = {
                int(item["id"]): str(item["name"])
                for item in genre_payload.get("genres", [])
                if isinstance(item, dict) and item.get("id") is not None and item.get("name")
            }
        except Exception:
            # Genre metadata is supplemental; matching can still use title and year.
            genre_map = {}

    endpoint_type = "tv" if media_type == "series" else "movie"
    search_path = f"/search/{endpoint_type}"
    search_attempts: list[dict[str, Any]] = []
    raw_candidates: dict[int, dict[str, Any]] = {}
    anonymous_candidates: list[dict[str, Any]] = []

    # Try the original spelling and at most two conservative query variants.
    # Keep the result language stable; only fall back to English if the German
    # searches return no candidates at all.
    query_variants = _search_query_variants(provider_title, provider_aliases)
    languages = ["de-DE"]
    try:
        for query in query_variants:
            response = search_fn(search_path, {
                "query": query, "include_adult": "false", "language": "de-DE",
            })
            results = response.get("results")
            results = results if isinstance(results, list) else []
            search_attempts.append({
                "query": query, "language": "de-DE", "result_count": len(results),
            })
            for item in results:
                if not isinstance(item, dict):
                    continue
                tmdb_id = item.get("id")
                if type(tmdb_id) is int and tmdb_id > 0:
                    existing = raw_candidates.get(tmdb_id)
                    if existing is None:
                        raw_candidates[tmdb_id] = dict(item)
                        raw_candidates[tmdb_id]["_matched_queries"] = [query]
                    else:
                        existing.setdefault("_matched_queries", []).append(query)
                else:
                    anonymous_candidates.append(dict(item))
        # German search results can exist but still be unrelated. In that case
        # also search in English instead of treating any result as a successful
        # search. This is especially important for international anime titles.
        best_german_score = max(
            (_provider_title_similarity(provider_title, provider_aliases, item, media_type)
             for item in list(raw_candidates.values()) + anonymous_candidates),
            default=0.0,
        )
        # Search one additional page for the primary title when page one is
        # weak. This bounded discovery step helps recover buried exact matches
        # without multiplying requests across every query variant.
        if best_german_score < MIN_TITLE_SCORE_WITH_YEAR and query_variants:
            query = query_variants[0]
            response = search_fn(search_path, {
                "query": query, "include_adult": "false",
                "language": "de-DE", "page": 2,
            })
            results = response.get("results")
            results = results if isinstance(results, list) else []
            search_attempts.append({
                "query": query, "language": "de-DE", "page": 2,
                "result_count": len(results),
            })
            for item in results:
                if not isinstance(item, dict):
                    continue
                tmdb_id = item.get("id")
                if type(tmdb_id) is int and tmdb_id > 0:
                    existing = raw_candidates.get(tmdb_id)
                    if existing is None:
                        raw_candidates[tmdb_id] = dict(item)
                        raw_candidates[tmdb_id]["_matched_queries"] = [query]
                    else:
                        existing.setdefault("_matched_queries", []).append(query)
                else:
                    anonymous_candidates.append(dict(item))
        # Re-evaluate after page two: it may have supplied a strong exact
        # match, making the English fallback unnecessary.
        best_german_score = max(
            (_provider_title_similarity(provider_title, provider_aliases, item, media_type)
             for item in list(raw_candidates.values()) + anonymous_candidates),
            default=0.0,
        )
        if best_german_score < MIN_TITLE_SCORE_WITH_YEAR:
            languages.append("en-US")
            for query in query_variants:
                response = search_fn(search_path, {
                    "query": query, "include_adult": "false", "language": "en-US",
                })
                results = response.get("results")
                results = results if isinstance(results, list) else []
                search_attempts.append({
                    "query": query, "language": "en-US", "result_count": len(results),
                })
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    tmdb_id = item.get("id")
                    if type(tmdb_id) is int and tmdb_id > 0:
                        existing = raw_candidates.get(tmdb_id)
                        if existing is None:
                            raw_candidates[tmdb_id] = dict(item)
                            raw_candidates[tmdb_id]["_matched_queries"] = [query]
                        else:
                            existing.setdefault("_matched_queries", []).append(query)
                    else:
                        anonymous_candidates.append(dict(item))
        # If ordinary title searches miss the candidate (or return only weak
        # title matches), use TMDB's year filter as a discovery aid. Search a
        # small +/-2-year window rather than treating AniWorld's year as exact:
        # provider dates can be off by a year or two. The year remains a ranking
        # signal later; it never hard-excludes candidates.
        best_discovered_title = max(
            (_provider_title_similarity(provider_title, provider_aliases, item, media_type)
             for item in list(raw_candidates.values()) + anonymous_candidates),
            default=0.0,
        )
        if provider_year is not None and best_discovered_title < MIN_TITLE_SCORE_WITH_YEAR:
            year_key = "first_air_date_year" if media_type == "series" else "year"
            discovery_queries = list(dict.fromkeys(
                query for query in query_variants
                if _normalize(query) != _normalize(provider_title)
            ))[:2]
            if not discovery_queries:
                discovery_queries = [provider_title]
            for year_offset in (0, -1, 1, -2, 2):
                search_year = provider_year + year_offset
                for query in discovery_queries[:1]:
                    response = search_fn(search_path, {
                        "query": query, year_key: search_year,
                        "include_adult": "false", "language": "en-US",
                    })
                    results = response.get("results")
                    results = results if isinstance(results, list) else []
                    search_attempts.append({
                        "query": query, "language": "en-US",
                        "year_filter": search_year, "result_count": len(results),
                    })
                    for item in results:
                        if not isinstance(item, dict):
                            continue
                        tmdb_id = item.get("id")
                        if type(tmdb_id) is int and tmdb_id > 0:
                            existing = raw_candidates.get(tmdb_id)
                            if existing is None:
                                raw_candidates[tmdb_id] = dict(item)
                                raw_candidates[tmdb_id]["_matched_queries"] = [query]
                            else:
                                existing.setdefault("_matched_queries", []).append(query)
                        else:
                            anonymous_candidates.append(dict(item))
    except (TMDBAPIError, TMDBConfigurationError) as exc:
        return {"status": "error", "reason": str(exc)}
    except Exception as exc:
        return {"status": "error", "reason": f"Search failed: {type(exc).__name__}"}

    all_candidates = list(raw_candidates.values()) + anonymous_candidates
    # Keep the empty-result case flowing into the conservative fallback below:
    # AniWorld sometimes labels standalone films as series, so an empty TV
    # search must not prevent a movie lookup. Other providers retain the normal
    # no-candidate result without probing the movie endpoint.

    # TMDB's main/original title may not match the provider label. Check
    # alternative titles for a bounded shortlist before rejecting candidates.
    # Title scoring repeatedly normalizes the same candidate variants. Cache per
    # candidate object for this resolution only; alternative titles are complete
    # before this point and candidates are not mutated during scoring.
    title_match_cache: dict[int, tuple[float, str | None]] = {}

    def cached_title_match(item: dict[str, Any]) -> tuple[float, str | None]:
        key = id(item)
        if key not in title_match_cache:
            title_match_cache[key] = _best_provider_title_match(
                provider_title, provider_aliases, item, media_type
            )
        return title_match_cache[key]


    def cached_title_similarity(item: dict[str, Any]) -> float:
        return cached_title_match(item)[0]


    # Evaluate each candidate title once while ordering the initial shortlist.
    # Reuse these scores below instead of running the same cached lookup again.
    scored_candidates = [
        (item, cached_title_similarity(item))
        for item in all_candidates
    ]
    scored_candidates.sort(key=lambda pair: pair[1], reverse=True)
    preliminary = [item for item, _score in scored_candidates]
    initial_title_scores = [score for _item, score in scored_candidates]
    initial_best = initial_title_scores[0] if initial_title_scores else 0.0
    initial_second = initial_title_scores[1] if len(initial_title_scores) > 1 else 0.0
    exact_leader_year = _year_from_result(preliminary[0], media_type) if preliminary else None
    requested_year = _coerce_year(record.get("year"))
    exact_leader_year_compatible = (
        requested_year is None
        or exact_leader_year is None
        or abs(exact_leader_year - requested_year) <= 1
    )
    clear_exact_leader = (
        initial_best >= 0.99
        and exact_leader_year_compatible
        and (len(initial_title_scores) == 1 or initial_best - initial_second >= 0.08)
    )
    # When a unique exact title already leads, alternative-title calls for
    # weaker candidates cannot improve the decision and only add API traffic.
    if not clear_exact_leader:
        for item in preliminary[:20]:
            tmdb_id = item.get("id")
            if type(tmdb_id) is not int or tmdb_id <= 0:
                continue
            # The initial ranking already computed this title score.
            if cached_title_similarity(item) >= MIN_TITLE_SCORE_WITH_YEAR:
                continue
            try:
                aliases_payload = search_fn(
                    f"/{endpoint_type}/{tmdb_id}/alternative_titles", {}
                )
                item["_alternative_titles"] = _alternative_title_values(aliases_payload)
            except Exception as exc:
                item["_alternative_titles_error"] = type(exc).__name__

    # Alternative titles may have changed candidate relevance. Re-rank before
    # bounded metadata/structure checks so alias-rescued candidates are not
    # excluded by their initial title position.
    title_match_cache.clear()
    preliminary.sort(key=cached_title_similarity, reverse=True)
    best_preliminary_title = cached_title_similarity(preliminary[0]) if preliminary else 0.0
    second_preliminary_title = cached_title_similarity(preliminary[1]) if len(preliminary) > 1 else 0.0
    uncertain_shortlist = (
        best_preliminary_title < 0.99
        or (len(preliminary) > 1 and best_preliminary_title - second_preliminary_title < 0.08)
    )

    # Fetch corroborating metadata only when the title evidence is uncertain.
    # A unique near-exact title can be resolved without credits/company API
    # calls, while weak or close candidates still get the bounded shortlist.
    metadata_scores: dict[int, dict[str, float | None]] = {}
    if uncertain_shortlist or media_type == "movie":
        for item in preliminary[:10]:
            tmdb_id = item.get("id")
            if type(tmdb_id) is int and tmdb_id > 0:
                metadata_scores[tmdb_id] = _metadata_signal_scores(
                    item, media_type, metadata, provider, search_fn
                )

    provider_structure = metadata.get("provider_structure")
    provider_description = str(record.get("description") or metadata.get("description") or "").strip()
    structure_scores: dict[int, dict[str, Any]] = {}
    description_scores: dict[int, float | None] = {}
    if uncertain_shortlist and (
        (media_type == "series" and isinstance(provider_structure, dict) and provider_structure.get("complete"))
        or provider_description
    ):
        for item in preliminary[:5]:
            tmdb_id = item.get("id")
            if type(tmdb_id) is not int or tmdb_id <= 0:
                continue
            details = _candidate_details(item, media_type, search_fn)
            if media_type == "series" and isinstance(provider_structure, dict) and provider_structure.get("complete"):
                season_titles = {}
                provider_seasons = provider_structure.get("seasons") or []
                has_episode_names = any(
                    isinstance(season, dict) and any(
                        isinstance(episode, dict) and (
                            episode.get("title") or episode.get("title_original")
                        )
                        for episode in season.get("episodes", []) or []
                    )
                    for season in provider_seasons
                )
                if has_episode_names and details and not _structure_comparison(
                    provider_structure, details
                ).get("mismatch"):
                    season_titles = _candidate_season_episode_titles(
                        item, provider_structure, search_fn
                    )
                structure_scores[tmdb_id] = _structure_comparison(
                    provider_structure, details, season_titles
                )
            overview = str((details or {}).get("overview") or "").strip()
            description_scores[tmdb_id] = _description_similarity(provider_description, overview)
    def scores_for(item: dict[str, Any]) -> dict[str, float | None]:
        tmdb_id = item.get("id")
        return metadata_scores.get(tmdb_id, {}) if type(tmdb_id) is int else {}

    corroboration_cache: dict[int, tuple[list[float], float | None]] = {}

    def corroboration_values(item: dict[str, Any]) -> list[float]:
        tmdb_id = item.get("id")
        if type(tmdb_id) is int and tmdb_id in corroboration_cache:
            return corroboration_cache[tmdb_id][0]
        signals = scores_for(item)
        # Genres are useful for ranking, but not strong enough to independently
        # rescue a weak title match. Actor/director overlaps are deduplicated.
        values = _independent_corroboration_values(signals, metadata)
        average = round(sum(values) / len(values), 4) if values else None
        if type(tmdb_id) is int:
            corroboration_cache[tmdb_id] = (values, average)
        return values

    def corroboration_score(item: dict[str, Any]) -> float | None:
        values = corroboration_values(item)
        tmdb_id = item.get("id")
        if type(tmdb_id) is int and tmdb_id in corroboration_cache:
            return corroboration_cache[tmdb_id][1]
        return round(sum(values) / len(values), 4) if values else None

    candidates: list[tuple[float, dict[str, Any], float, int | None, float | None]] = []
    rejected_diagnostics = []
    for item in preliminary[:20]:
        title_score, matched_title = cached_title_match(item)
        independent_signals = corroboration_values(item)
        strong_signal_count = sum(value >= 0.85 for value in independent_signals)
        independent_average = corroboration_score(item)
        strong_metadata = (
            strong_signal_count >= 2
            and independent_average is not None
            and independent_average >= 0.85
        )
        suffix_score, suffix_title, suffix_kind = _format_suffix_match(
            provider_title, item, media_type
        )
        suffix_variant = suffix_score >= 0.99
        candidate_year = _year_from_result(item, media_type)
        exact_year = provider_year is not None and candidate_year == provider_year
        # A reliable exact year plus one strong independent credit/company
        # match may rescue a translated or slightly different title. This is
        # deliberately stricter than year-only matching and still requires
        # the global runner-up margin below.
        structure = structure_scores.get(item.get("id"), {
            "status": "not_available", "score": None, "mismatch": False,
        })
        year_supported = (
            exact_year and title_score >= 0.70
            and any(value >= 0.85 for value in independent_signals)
        )
        # A trailing format label is weaker evidence than a title match. Accept
        # it only when the year agrees and at least one independent metadata
        # signal corroborates it, or when two independent metadata signals do.
        suffix_supported = suffix_variant and (
            strong_metadata or (exact_year and strong_signal_count >= 1
                                and (independent_average or 0) >= 0.85)
        )
        if structure.get("mismatch"):
            rejected_diagnostics.append({
                "tmdb_id": item.get("id"),
                "title": (item.get("name") if media_type == "series" else item.get("title")) or "",
                "title_score": title_score,
                "candidate_year": candidate_year,
                "structure": structure,
                "rejection": "provider_tmdb_episode_structure_mismatch",
            })
            continue
        if title_score < MIN_TITLE_SCORE_WITH_YEAR and not strong_metadata and not suffix_supported and not year_supported:
            rejected_diagnostics.append({
                "tmdb_id": item.get("id"),
                "title": (item.get("name") if media_type == "series" else item.get("title")) or "",
                "original_title": (
                    item.get("original_name") if media_type == "series"
                    else item.get("original_title")
                ),
                "alternative_titles": item.get("_alternative_titles") or [],
                "title_score": title_score,
                "candidate_year": candidate_year,
                "closest_title": matched_title,
                "matched_queries": item.get("_matched_queries") or [],
                "metadata_signals": scores_for(item),
                "rejection": (
                    "insufficient_title_or_independent_metadata_evidence"
                    if (
                        metadata.get("actors")
                        or metadata.get("directors")
                        or (provider == "aniworld" and metadata.get("producer"))
                    )
                    else "below_title_similarity_threshold"
                ),
            })
            continue

        base_score = max(
            _candidate_score(
                provider_title, provider_year, item, media_type, provider_genres, genre_map
            ),
            title_score,
        )
        if (strong_metadata or suffix_supported or year_supported) and (
            title_score < MIN_TITLE_SCORE_WITH_YEAR or suffix_supported
        ):
            # Metadata-rescued and format-suffix candidates must clear a high
            # evidence floor; the separate margin check protects against ties.
            evidence = corroboration_score(item) or 0.85
            base_score = max(base_score, min(0.97, 0.94 + (evidence - 0.85) * 0.2))
        genre_score = (
            _genre_similarity(provider_genres, item, genre_map or {})
            if provider_genres and genre_map else None
        )
        candidates.append((base_score, item, title_score, candidate_year, genre_score))

    # Provider year (especially AniWorld's) remains diagnostic only: it never
    # excludes a candidate. Strong metadata corroboration is provider-neutral.
    if not candidates:
        diagnostics = {
            "provider_title": provider_title, "provider_year": provider_year,
            "provider_genres": provider_genres,
            "search_attempts": search_attempts,
            "tmdb_result_count": len(all_candidates),
            "title_compatible_candidates": 0,
            "candidates": rejected_diagnostics,
        }
        # A series candidate rejected by episode structure can actually be a
        # feature film incorrectly exposed as a one-episode season. Probe the
        # movie endpoint only in this specific case. A type correction requires
        # exact year, a high-confidence movie match and multiple metadata signals.
        structural_rejection = any(
            isinstance(row, dict)
            and row.get("rejection") == "provider_tmdb_episode_structure_mismatch"
            for row in rejected_diagnostics
        )
        empty_aniworld_series_search = provider == "aniworld" and not all_candidates
        if media_type == "series" and (
            structural_rejection or empty_aniworld_series_search
        ):
            alternate_record = dict(record)
            alternate_record["media_type"] = "movie"
            alternate = find_tmdb_match(
                alternate_record, search_fn=search_fn, genre_map=None,
                wikidata_lookup_fn=wikidata_lookup_fn,
            )
            alternate_diagnostics = (
                alternate.get("diagnostics")
                if isinstance(alternate.get("diagnostics"), dict) else {}
            )
            alternate_rows = alternate_diagnostics.get("candidates")
            alternate_rows = alternate_rows if isinstance(alternate_rows, list) else []
            plausible_rejected_movie = next((
                row for row in alternate_rows
                if isinstance(row, dict)
                and float(row.get("title_score") or 0) >= 0.70
                and (
                    provider_year is None
                    or row.get("candidate_year") == provider_year
                )
            ), None)
            if alternate.get("status") in {"matched", "identified", "ambiguous"} or plausible_rejected_movie:
                diagnostics["alternate_movie_candidate"] = {
                    "status": alternate.get("status"),
                    "tmdb_id": alternate.get("tmdb_id") or (
                        plausible_rejected_movie.get("tmdb_id") if plausible_rejected_movie else None
                    ),
                    "title": alternate.get("candidate_title") or (
                        plausible_rejected_movie.get("title") if plausible_rejected_movie else None
                    ),
                    "year": alternate.get("candidate_year") or (
                        plausible_rejected_movie.get("candidate_year") if plausible_rejected_movie else None
                    ),
                    "score": alternate.get("score"),
                    "margin": alternate.get("margin"),
                    "rejection": plausible_rejected_movie.get("rejection") if plausible_rejected_movie else None,
                    "diagnostics": alternate_diagnostics,
                }

                # Correct a mistaken AniWorld series classification only when
                # the alternate movie is independently and strongly verified.
                # Exact year plus at least two independent metadata signals
                # prevents title-only/franchise-level guesses from changing type.
                movie_rows = alternate_diagnostics.get("candidates")
                movie_rows = movie_rows if isinstance(movie_rows, list) else []
                best_movie_row = movie_rows[0] if movie_rows and isinstance(movie_rows[0], dict) else {}
                signal_scores = [
                    best_movie_row.get(key, {}).get("score")
                    for key in ("actors", "directors", "studio")
                    if isinstance(best_movie_row.get(key), dict)
                ]
                strong_signal_count = sum(
                    isinstance(value, (int, float)) and value >= 0.85
                    for value in signal_scores
                )
                safe_movie_correction = (
                    provider == "aniworld"
                    and alternate.get("status") in {"matched", "identified"}
                    and provider_year is not None
                    and alternate.get("candidate_year") == provider_year
                    and float(alternate.get("title_score") or 0) >= 0.70
                    and float(alternate.get("score") or 0) >= MIN_CONFIDENCE
                    and float(alternate.get("margin") or 0) >= MIN_MARGIN
                    and strong_signal_count >= 2
                )
                if safe_movie_correction:
                    correction = {
                        "from": "series",
                        "to": "movie",
                        "reason": "exact_year_and_multiple_independent_metadata_signals",
                        "strong_metadata_signals": strong_signal_count,
                    }
                    return {
                        **alternate,
                        "media_type_correction": correction,
                        "diagnostics": {
                            **alternate_diagnostics,
                            "provider_media_type": "series",
                            "media_type_correction": correction,
                            "rejected_series_candidates": rejected_diagnostics,
                        },
                    }

                return {
                    "status": "ambiguous",
                    "reason": (
                        "A possible TMDB movie exists, but the provider entry is "
                        "classified as a series; manual media-type review is required"
                    ),
                    "media_type_conflict": True,
                    "diagnostics": diagnostics,
                }
        return {"status": "no_match", "reason": "No TMDB candidate had sufficient title or independent metadata evidence",
                "diagnostics": diagnostics}

    def adjusted_score(pair: tuple[float, dict[str, Any], float, int | None, float | None]) -> float:
        corroboration = corroboration_score(pair[1])
        # Metadata can rank candidates, but does not override a strong title
        # match. Rescue eligibility itself requires two strong independent signals.
        score = pair[0] + (
            max(0.0, corroboration - 0.5) * 0.20
            if corroboration is not None else 0.0
        )
        structure = structure_scores.get(pair[1].get("id"))
        if structure and structure.get("status") == "match":
            score += 0.04
        description_score = description_scores.get(pair[1].get("id"))
        if description_score is not None:
            score += min(0.03, max(0.0, description_score - 0.25) * 0.04)
        # A positive genre overlap is a separate, weak ranking signal. A
        # mismatch remains neutral, but a full match must be strong enough to
        # distinguish otherwise identical title/year candidates.
        genre_score = pair[4]
        if genre_score is not None:
            score += genre_score * 0.10
        # Country is a low-weight corroborating hint: production countries can
        # be incomplete or multi-national, so a mismatch never rejects a title.
        country_score = scores_for(pair[1]).get("country_score")
        if country_score is not None:
            score += country_score * 0.04
        # Year is a strong, fault-tolerant ranking signal. Exact matches
        # receive the largest bonus; +/-1-2 years remain plausible for AniWorld,
        # while larger gaps progressively lose ground. It is never a hard filter.
        item_year = pair[3]
        if provider_year is not None and item_year is not None:
            delta = abs(provider_year - item_year)
            year_bonus = {
                0: 0.20,
                1: 0.16,
                2: 0.12,
                3: 0.05,
                4: 0.00,
            }.get(delta, -min(0.15, (delta - 4) * 0.025))
            score += year_bonus
        return score

    candidates.sort(key=adjusted_score, reverse=True)
    best_score, best, title_score, candidate_year, best_genre_score = candidates[0]
    runner_up = candidates[1][0] if len(candidates) > 1 else 0.0
    margin = adjusted_score(candidates[0]) - (
        adjusted_score(candidates[1]) if len(candidates) > 1 else 0.0
    )
    # adjusted_score already includes the bounded metadata tie-breaker.
    # Do not add metadata or genre a second time to the candidate margin.
    best_metadata_score = corroboration_score(best)
    margin = round(margin, 4)
    candidate_diagnostics = []
    for _, item, item_title_score, item_year, item_genre_score in candidates[:10]:
        signals = scores_for(item)
        candidate_diagnostics.append({
            "tmdb_id": item.get("id"),
            "title": {"provider": provider_title,
                      "tmdb": (item.get("name") if media_type == "series" else item.get("title")) or "",
                      "status": "match" if item_title_score == 1.0 else "partial_match",
                      "score": item_title_score,
                      "matched_variant": cached_title_match(item)[1]},
            "original_title": (item.get("original_name") if media_type == "series" else item.get("original_title")),
            "alternative_titles": item.get("_alternative_titles") or [],
            "matched_queries": item.get("_matched_queries") or [],
            "year": {"provider": provider_year, "tmdb": item_year,
                     "status": "missing" if provider_year is None or item_year is None else
                               ("match" if provider_year == item_year else "mismatch"),
                     "delta": abs(item_year-provider_year) if item_year is not None and provider_year is not None else None,
                     "used_for_exclusion": False},
            "genres": {"provider": provider_genres, "score": item_genre_score,
                       "status": "not_comparable" if item_genre_score is None else
                                 ("match" if item_genre_score >= 0.5 else "mismatch")},
            "actors": {"provider": metadata.get("actors") or [], "score": signals.get("actor_score"),
                       "status": "not_available" if signals.get("actor_score") is None else                                 ("match" if signals["actor_score"] >= 0.5 else "mismatch")},
            "directors": {"provider": metadata.get("directors") or [], "score": signals.get("director_score"),
                          "status": "not_available" if signals.get("director_score") is None else                                    ("match" if signals["director_score"] >= 0.5 else "mismatch")},
            "studio": {"provider": metadata.get("producer") if provider == "aniworld" else None,                       "score": signals.get("studio_score"),
                       "status": "not_available" if signals.get("studio_score") is None else
                                 ("match" if signals["studio_score"] >= 0.5 else "mismatch")},
            "country": {"provider": metadata.get("country"),
                        "score": signals.get("country_score"),
                        "status": "not_available" if signals.get("country_score") is None else
                                  ("match" if signals["country_score"] >= 0.5 else "mismatch")},
            "structure": structure_scores.get(item.get("id"), {
                "status": "not_available", "score": None, "mismatch": False,
            }),
            "description": {"provider_available": bool(provider_description),
                           "similarity": description_scores.get(item.get("id")),
                           "status": "not_available" if description_scores.get(item.get("id")) is None else "weak_support"},
        })
    diagnostics = {
        "search": {"query": provider_title, "languages": languages,
                   "attempts": search_attempts,
                   "tmdb_result_count": len(all_candidates),
                   "title_compatible_candidates": len(candidates)},
        "provider_data": {"title": provider_title, "year": provider_year,
                          "genres": provider_genres, "actors": metadata.get("actors") or [],
                          "directors": metadata.get("directors") or [],
                          "studio": metadata.get("producer") if provider == "aniworld" else None,
                          "country": metadata.get("country")},
        "candidates": candidate_diagnostics,
    }
    candidate = {
        "tmdb_id": best.get("id"),
        "candidate_title": (best.get("name") if media_type == "series" else best.get("title")) or "",
        "candidate_original_title": (
            best.get("original_name") if media_type == "series" else best.get("original_title")
        ) or "",
        "candidate_year": candidate_year,
        "candidate_year_delta": (
            abs(candidate_year - provider_year)
            if candidate_year is not None and provider_year is not None else None
        ),
        "title_score": title_score,
        "score": best_score,
        "margin": margin,
        "genre_score": best_genre_score,
        "actor_score": scores_for(best).get("actor_score"),
        "director_score": scores_for(best).get("director_score"),
        "studio_score": scores_for(best).get("studio_score"),
        "metadata_score": best_metadata_score,
    }
    if _is_short_ambiguous_title(provider_title) and len(candidates) > 1:
        # For colliding short titles, an exact year is sufficient to distinguish
        # editions; without it, keep the result manual. Independent credits are
        # helpful but should not be mandatory when the year resolves the collision.
        short_title_supported = provider_year is not None and candidate_year == provider_year
        candidate["short_title_safeguard"] = {
            "applied": True,
            "supported": short_title_supported,
            "requires": "exact year when short-title candidates collide",
        }
        if not short_title_supported:
            return {
                "status": "ambiguous",
                "reason": "Short single-word title collision requires exact year",
                "diagnostics": diagnostics,
                **candidate,
            }
    if best_score < MIN_CONFIDENCE:
        return {"status": "ambiguous", "reason": "Best candidate is below confidence threshold",
                "diagnostics": diagnostics, **candidate}
    if margin < MIN_MARGIN:
        return {"status": "ambiguous", "reason": "Multiple candidates have similar scores",
                "diagnostics": diagnostics, **candidate}
    if type(best.get("id")) is not int or best["id"] <= 0:
        return {"status": "ambiguous", "reason": "TMDB candidate has no numeric ID",
                "diagnostics": diagnostics, **candidate}

    try:
        external = search_fn(f"/{endpoint_type}/{best['id']}/external_ids", {})
    except Exception as exc:
        return {"status": "error", "reason": f"External ID lookup failed: {type(exc).__name__}",
                "diagnostics": diagnostics, **candidate}
    imdb_id = str(external.get("imdb_id") or "").strip()
    imdb_id_source = "tmdb_external_ids"
    if not IMDB_ID_RE.fullmatch(imdb_id):
        wikidata_id = str(external.get("wikidata_id") or "").strip()
        if wikidata_id:
            imdb_id = _wikidata_imdb_id(wikidata_id, fetch_fn=wikidata_lookup_fn) or ""
            if IMDB_ID_RE.fullmatch(imdb_id):
                imdb_id_source = "wikidata_p345"
    if not IMDB_ID_RE.fullmatch(imdb_id):
        return {"status": "identified", "reason": "TMDB identity is confident; no IMDb ID is available",
                "diagnostics": diagnostics, "external_ids": external, **candidate}
    return {"status": "matched", "imdb_id": imdb_id,
            "imdb_id_source": imdb_id_source,
            "diagnostics": diagnostics, "external_ids": external, **candidate}


def _refresh_provider_evidence(record: dict[str, Any]) -> dict[str, Any]:
    """Fetch one provider detail page to enrich an uncertain legacy queue item."""
    provider_id = str(record.get("provider") or "").strip().lower()
    provider_url = str(record.get("provider_url") or "").strip()
    if not provider_id or not provider_url:
        return record
    try:
        from providers import PROVIDERS
        provider_class = PROVIDERS.get(provider_id)
        if provider_class is None:
            return record
        detail = provider_class().get_title(provider_url)
    except Exception as exc:
        logger.debug("Provider evidence refresh failed for %s: %s", provider_url, type(exc).__name__)
        return record

    refreshed = dict(record)
    metadata = dict(record.get("metadata") or {})
    if isinstance(getattr(detail, "metadata", None), dict):
        metadata.update(detail.metadata)
    if getattr(detail, "provider_genres", None):
        metadata["provider_genres"] = list(detail.provider_genres)
    if getattr(detail, "year", None):
        refreshed["year"] = detail.year
    if getattr(detail, "title", None):
        refreshed["provider_title"] = detail.title
    if getattr(detail, "media_type", None) in {"movie", "series"}:
        # Preserve the source classification for diagnostics while using the
        # refreshed page structure to choose the correct TMDB endpoint.
        refreshed.setdefault("provider_media_type", record.get("media_type"))
        refreshed["media_type"] = detail.media_type
    if getattr(detail, "description", None):
        metadata["description"] = detail.description
        if not refreshed.get("description"):
            refreshed["description"] = detail.description
    refreshed["metadata"] = metadata
    return refreshed


def resolve_unresolved_titles(
    *,
    provider: str | None = None,
    limit: int = 500,
    apply: bool = False,
    search_fn: Callable[..., dict[str, Any]] = _tmdb_get,
) -> dict[str, Any]:
    """Evaluate queued titles and optionally persist only high-confidence matches."""
    init_db()
    # Purge stale cache rows once per resolver run; the index keeps this bounded.
    TMDB_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
    tmdb_cache_purged = purge_expired_tmdb_cache(
        ttl_seconds=TMDB_CACHE_TTL_SECONDS
    )
    # Fetch a broad candidate window before applying cooldown and limit so recently
    # refreshed items cannot starve older records that are already retry-due.
    candidates = get_unresolved_titles(provider=provider, limit=5000)
    due_records = [record for record in candidates if resolver_retry_due(record)]
    records = due_records[:max(1, int(limit))]
    summary: dict[str, Any] = {
        "scanned": len(records),
        "deferred_by_retry_cooldown": len(candidates) - len(due_records),
        "matched": 0,
        "applied": 0,
        "ambiguous": 0,
        "identified_without_imdb": 0,
        "no_match": 0,
        "errors": 0,
        "dry_run": not apply,
        "tmdb_cache_hits": 0,
        "tmdb_cache_misses": 0,
        "tmdb_cache_memory_hits": 0,
        "tmdb_cache_persistent_hits": 0,
        "tmdb_cache_avoided_http_requests": 0,
        "tmdb_http_requests": 0,
        "tmdb_cache_purged": tmdb_cache_purged,
        "results": [],
    }
    # Resolve the complete batch before writing anything. This preflight catches
    # multiple distinct pages from one provider that collapse onto one TMDB ID
    # (for example sequel/season pages sharing a franchise-level external ID).
    evaluated: list[tuple[dict[str, Any], dict[str, Any]]] = []
    genre_maps: dict[str, dict[int, str]] = {}
    # Cache identical TMDB requests in memory during the batch and persist successful
    # responses for seven days to avoid repeated API traffic across resolver runs.
    request_cache: dict[str, dict[str, Any]] = {}

    def cached_search(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        cache_key = json.dumps(
            [path, params or {}], sort_keys=True, ensure_ascii=False, default=str
        )
        endpoint = str(path).split("?", 1)[0]
        if cache_key in request_cache:
            summary["tmdb_cache_hits"] += 1
            summary["tmdb_cache_memory_hits"] += 1
            summary["tmdb_cache_avoided_http_requests"] += 1
            record_tmdb_cache_usage(
                cache_key, endpoint=endpoint, avoided_http_request=True
            )
        else:
            persistent = get_tmdb_cached_response(
                cache_key, ttl_seconds=TMDB_CACHE_TTL_SECONDS
            )
            if persistent is not None:
                request_cache[cache_key] = persistent
                summary["tmdb_cache_hits"] += 1
                summary["tmdb_cache_persistent_hits"] += 1
                summary["tmdb_cache_avoided_http_requests"] += 1
                record_tmdb_cache_usage(
                    cache_key, endpoint=endpoint, avoided_http_request=True
                )
            else:
                summary["tmdb_cache_misses"] += 1
                summary["tmdb_http_requests"] += 1
                response = search_fn(path, params)
                request_cache[cache_key] = response
                # search_fn returning a dict means a valid, cacheable API payload.
                set_tmdb_cached_response(cache_key, response)
                record_tmdb_cache_usage(
                    cache_key, endpoint=endpoint, avoided_http_request=False
                )
        return copy.deepcopy(request_cache[cache_key])

    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        has_provider_genres = (
            isinstance(metadata.get("provider_genres"), list)
            and bool(metadata.get("provider_genres"))
        )
        genre_map = None
        if has_provider_genres:
            media_type = record["media_type"]
            if media_type not in genre_maps:
                endpoint = "/genre/tv/list" if media_type == "series" else "/genre/movie/list"
                try:
                    payload = cached_search(endpoint, {"language": "de-DE"})
                    genre_maps[media_type] = {
                        int(item["id"]): str(item["name"])
                        for item in payload.get("genres", [])
                        if isinstance(item, dict) and item.get("id") is not None and item.get("name")
                    }
                except Exception:
                    genre_maps[media_type] = {}
            genre_map = genre_maps[media_type]
        result = find_tmdb_match(record, search_fn=cached_search, genre_map=genre_map)
        provider_structure = metadata.get("provider_structure")
        has_structure = (
            isinstance(provider_structure, dict)
            and bool(provider_structure.get("complete"))
        )
        has_episode_titles = bool(
            isinstance(provider_structure, dict)
            and any(
                isinstance(season, dict) and any(
                    isinstance(episode, dict) and (
                        episode.get("title") or episode.get("title_original")
                    )
                    for episode in season.get("episodes", []) or []
                )
                for season in provider_structure.get("seasons", []) or []
            )
        )
        has_alternative_titles = bool(metadata.get("alternative_titles"))
        diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
        candidate_rows = diagnostics.get("candidates") if isinstance(diagnostics.get("candidates"), list) else []
        def _diagnostic_title_score(row: dict[str, Any]) -> float:
            value = row.get("title_score")
            title_info = row.get("title")
            if value is None and isinstance(title_info, dict):
                value = title_info.get("score")
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0

        promising_rejected = any(
            _diagnostic_title_score(row) >= 0.70
            for row in candidate_rows if isinstance(row, dict)
        )
        uncertain = result.get("status") == "ambiguous" or (
            result.get("status") == "no_match" and promising_rejected
        )
        if uncertain and (not has_structure or not has_episode_titles or not has_alternative_titles):
            enriched = _refresh_provider_evidence(record)
            enriched_metadata = enriched.get("metadata") if isinstance(enriched.get("metadata"), dict) else {}
            if (enriched_metadata.get("provider_structure") or enriched_metadata.get("alternative_titles") or enriched.get("description")):
                record = enriched
                result = find_tmdb_match(record, search_fn=cached_search, genre_map=genre_map)
        correction = result.get("media_type_correction")
        if isinstance(correction, dict) and correction.get("to") in {"movie", "series"}:
            record = dict(record)
            record["provider_media_type"] = record.get("media_type")
            record["media_type"] = correction["to"]
            corrected_metadata = dict(record.get("metadata") or {})
            corrected_metadata["media_type_correction"] = correction
            record["metadata"] = corrected_metadata
        evaluated.append((record, result))

    # A TMDB series/movie identity can be shared by several provider pages.
    # Until provider-page identity is modelled separately, do not merge distinct
    # same-provider titles into one GSX identity or resolve their queue entries.
    collision_groups: dict[tuple[str, str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for record, result in evaluated:
        if result.get("status") not in {"matched", "identified"}:
            continue
        tmdb_id = str(result.get("tmdb_id") or "")
        if not tmdb_id:
            continue
        key = (
            str(record.get("media_type") or ""),
            str(record.get("provider") or "").strip().lower(),
            tmdb_id,
        )
        collision_groups.setdefault(key, []).append((record, result))

    for (media_type, provider, tmdb_id), group in collision_groups.items():
        distinct_titles = {
            _normalize(record.get("provider_title"))
            for record, _ in group
            if _normalize(record.get("provider_title"))
        }
        distinct_urls = {
            str(record.get("provider_url") or "").strip()
            for record, _ in group
            if str(record.get("provider_url") or "").strip()
        }
        if len(distinct_titles) <= 1 or len(distinct_urls) <= 1:
            continue
        collision = {
            "media_type": media_type,
            "provider": provider,
            "tmdb_id": tmdb_id,
            "provider_entries": [
                {
                    "title": record.get("provider_title"),
                    "url": record.get("provider_url"),
                }
                for record, _ in group
            ],
        }
        for _, result in group:
            result["status"] = "ambiguous"
            result["reason"] = (
                "Distinct provider entries from the same provider resolve to "
                "the same TMDB identity; manual provider-page identity review is required"
            )
            result["identity_collision"] = collision

    for record, result in evaluated:
        record_unresolved_attempt(record["provider"], record["provider_url"])
        result_row = {
            "provider": record["provider"],
            "title": record["provider_title"],
            "url": record["provider_url"],
            "provider_media_type": record.get("provider_media_type", record["media_type"]),
            "resolved_media_type": record["media_type"],
            **result,
        }
        summary["results"].append(result_row)
        status = result["status"]
        if status in {"matched", "identified"}:
            if status == "matched":
                summary["matched"] += 1
            else:
                summary["identified_without_imdb"] += 1
            if apply:
                # Keep a canonical identity for every confident TMDB match,
                # including titles that have no IMDb listing.
                media_type = record["media_type"]
                tmdb_id = str(result["tmdb_id"])
                canonical = get_title_by_external_id(media_type, "tmdb", tmdb_id)
                if canonical is None:
                    canonical = get_title_by_provider_url(
                        record["provider"], record["provider_url"]
                    )
                if canonical is None:
                    title_id = create_title(
                        media_type,
                        result.get("candidate_title") or record["provider_title"],
                        year=result.get("candidate_year") or record.get("year"),
                        metadata={
                            "identity_resolution": "tmdb",
                            "provider_title": record["provider_title"],
                            "provider": record["provider"],
                            "score": result.get("score"),
                            "margin": result.get("margin"),
                        },
                    )
                else:
                    title_id = canonical["title_id"]
                add_external_id(
                    title_id, "tmdb", tmdb_id, source="tmdb_identity_resolver",
                    confidence=result.get("score"), verified=True,
                )
                external_ids = result.get("external_ids") or {}
                for source_key, namespace in (
                    ("tvdb_id", "tvdb"), ("wikidata_id", "wikidata")
                ):
                    external_value = external_ids.get(source_key)
                    if external_value:
                        add_external_id(
                            title_id, namespace, str(external_value),
                            source="tmdb_external_ids", verified=True,
                        )
                if result.get("imdb_id"):
                    add_external_id(
                        title_id, "imdb", result["imdb_id"],
                        source=result.get("imdb_id_source", "tmdb_external_ids"),
                        confidence=result.get("score"), verified=True,
                    )
                link_provider_title(
                    title_id, record["provider"], record["provider_url"],
                    provider_title=record["provider_title"],
                )
            if status == "matched" and apply:
                metadata = dict(record.get("metadata") or {})
                metadata["identity_resolution"] = {
                    "source": "tmdb",
                    "imdb_id_source": result.get("imdb_id_source", "tmdb_external_ids"),
                    "tmdb_id": result["tmdb_id"],
                    "score": result["score"],
                    "margin": result["margin"],
                    "candidate_title": result["candidate_title"],
                    "candidate_year": result.get("candidate_year"),
                    "candidate_year_delta": result.get("candidate_year_delta"),
                    "title_score": result.get("title_score"),
                    "genre_score": result.get("genre_score"),
                    "actor_score": result.get("actor_score"),
                    "director_score": result.get("director_score"),
                    "studio_score": result.get("studio_score"),
                    "metadata_score": result.get("metadata_score"),
                }
                record_discovery(
                    record["media_type"],
                    result["imdb_id"],
                    record["provider"],
                    record["provider_title"],
                    provider_title=record["provider_title"],
                    provider_url=record["provider_url"],
                    year=record.get("year") or result.get("candidate_year"),
                    metadata=metadata,
                )
                mark_unresolved_title_resolved(
                    record["provider"], record["provider_url"], result["imdb_id"]
                )
            elif status == "identified" and apply:
                mark_unresolved_title_identified(
                    record["provider"], record["provider_url"], title_id
                )
            if apply:
                summary["applied"] += 1
        elif status == "ambiguous":
            summary["ambiguous"] += 1
        elif status == "no_match":
            summary["no_match"] += 1
        else:
            summary["errors"] += 1
    cache_totals = get_tmdb_cache_usage_totals()
    summary["tmdb_cache_usage_totals"] = cache_totals
    summary["tmdb_cache_top_keys"] = get_tmdb_cache_usage(limit=10)
    total_cache_requests = summary["tmdb_cache_hits"] + summary["tmdb_cache_misses"]
    summary["tmdb_cache_hit_rate"] = round(
        summary["tmdb_cache_hits"] / total_cache_requests, 4
    ) if total_cache_requests else 0.0
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve provider titles to canonical TMDB identities (IMDb optional)")
    parser.add_argument("--provider", choices=("aniworld", "serienstream"))
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--apply", action="store_true",
        help="Persist high-confidence matches (default: report only)",
    )
    args = parser.parse_args()
    summary = resolve_unresolved_titles(
        provider=args.provider, limit=args.limit, apply=args.apply
    )
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    report_path = "/app/data/identity-resolver-report.json"
    with open(report_path, "w", encoding="utf-8") as report_file:
        report_file.write(rendered + "\n")
    compact = {key: value for key, value in summary.items() if key != "results"}
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    print(f"Full diagnostic report: {report_path}")


if __name__ == "__main__":
    main()