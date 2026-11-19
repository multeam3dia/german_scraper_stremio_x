"""Confidence-aware provider-title to IMDb identity resolution via TMDB.

The resolver is intentionally conservative: it never promotes a title based on
fuzzy title similarity alone. A candidate must have a strong title match, a
valid IMDb ID from TMDB external IDs, and a clear margin over the runner-up.
Provider years are retained for diagnostics but are not used to exclude matches.
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import unicodedata
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from catalog_db import (
    init_db,
    get_unresolved_titles,
    mark_unresolved_title_resolved,
    record_discovery,
)

logger = logging.getLogger(__name__)
TMDB_API_BASE = "https://api.themoviedb.org/3"
IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
MIN_CONFIDENCE = 0.94
MIN_MARGIN = 0.08
MIN_TITLE_SCORE_WITH_YEAR = 0.94

GENRE_ALIASES = {
    "abenteuer": "adventure", "komodie": "comedy", "krimi": "crime",
    "kriminalfilm": "crime", "dokumentation": "documentary", "doku": "documentary",
    "familie": "family", "geschichte": "history", "musik": "music",
    "liebe": "romance", "romantik": "romance", "sci fi": "science fiction",
    "science fiction": "science fiction", "krieg": "war",
    "action abenteuer": "action", "action adventure": "action",
    "action and adventure": "action", "action and abenteuer": "action",
    "sci fi fantasy": "fantasy", "zeichentrick": "animation",
}


class TMDBConfigurationError(RuntimeError):
    """TMDB credentials are not configured."""


class TMDBAPIError(RuntimeError):
    """TMDB could not return a valid response."""


def _normalize(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", value))


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
        key=lambda pair: 1.0 if source == pair[1] else difflib.SequenceMatcher(None, source, pair[1]).ratio(),
    )
    # Ignore spacing differences as well: AniWorld occasionally concatenates
    # words (for example "TwoCar" vs. TMDB's "Two Car").
    compact_source = source.replace(" ", "")
    compact_variant = normalized.replace(" ", "")
    score = (
        1.0 if source == normalized or compact_source == compact_variant
        else difflib.SequenceMatcher(None, source, normalized).ratio()
    )
    return round(score, 4), raw


def _title_similarity(
    provider_title: str, item: dict[str, Any], media_type: str
) -> float:
    return _best_title_match(provider_title, item, media_type)[0]


def _search_query_variants(provider_title: str) -> list[str]:
    """Build a small, conservative set of TMDB search queries."""
    candidates = [provider_title]
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
    return candidates[:3]


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


def _normalize_genre(value: Any) -> str:
    normalized = _normalize(value)
    return GENRE_ALIASES.get(normalized, normalized)


def _genre_similarity(
    provider_genres: list[str],
    item: dict[str, Any],
    genre_map: dict[int, str],
) -> float | None:
    """Compare provider genre labels with TMDB genre IDs when both are available."""
    provider_set = {
        _normalize_genre(value) for value in provider_genres if _normalize_genre(value)
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
        payload = search_fn(f"/{endpoint_type}/{tmdb_id}", {})
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _names_from_people(rows: Any, key: str = "name") -> list[str]:
    if not isinstance(rows, list):
        return []
    return [
        str(row.get(key) or "")
        for row in rows
        if isinstance(row, dict) and row.get(key)
    ]


def _metadata_signal_scores(
    item: dict[str, Any], media_type: str, metadata: dict[str, Any],
    provider: str, search_fn: Callable[..., dict[str, Any]],
) -> dict[str, float | None]:
    """Compare optional provider credits/studio against TMDB, without penalizing gaps."""
    directors = metadata.get("directors")
    directors = directors if isinstance(directors, list) else []
    actors = metadata.get("actors")
    actors = actors if isinstance(actors, list) else []

    # AniWorld's 'producer' field is a production studio/company. On
    # SerienStream the same label refers to people, so do not compare it here.
    studio = metadata.get("producer") if provider == "aniworld" else None
    studios = [studio] if isinstance(studio, str) and studio.strip() else []

    scores: dict[str, float | None] = {
        "actor_score": None, "director_score": None, "studio_score": None,
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

    if studios:
        details = _candidate_details(item, media_type, search_fn)
        companies = _names_from_people(details.get("production_companies")) if details else []
        scores["studio_score"] = _person_similarity(studios, companies)
    return scores


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


def find_tmdb_match(
    record: dict[str, Any],
    *,
    search_fn: Callable[..., dict[str, Any]] = _tmdb_get,
    genre_map: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Return a conservative TMDB candidate; does not write to the catalog."""
    media_type = record.get("media_type")
    if media_type not in {"movie", "series"}:
        return {"status": "invalid", "reason": "Unsupported media type"}

    provider_title = str(record.get("provider_title") or "").strip()
    if not provider_title:
        return {"status": "no_match", "reason": "Provider title is empty"}
    provider_year = _coerce_year(record.get("year"))
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    provider_genres = metadata.get("provider_genres")
    if not isinstance(provider_genres, list):
        provider_genres = []
    provider = str(record.get("provider") or "")
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
    query_variants = _search_query_variants(provider_title)
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
            (_title_similarity(provider_title, item, media_type)
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
    except (TMDBAPIError, TMDBConfigurationError) as exc:
        return {"status": "error", "reason": str(exc)}
    except Exception as exc:
        return {"status": "error", "reason": f"Search failed: {type(exc).__name__}"}

    all_candidates = list(raw_candidates.values()) + anonymous_candidates
    if not all_candidates:
        return {"status": "no_match", "reason": "TMDB returned no candidates",
                "diagnostics": {
                    "provider_title": provider_title, "provider_year": provider_year,
                    "provider_genres": provider_genres, "search_attempts": search_attempts,
                    "tmdb_result_count": 0, "title_compatible_candidates": 0,
                    "candidates": [],
                }}

    # TMDB's main/original title may not match the provider label. Check
    # alternative titles for a bounded shortlist before rejecting candidates.
    preliminary = sorted(
        all_candidates,
        key=lambda item: _title_similarity(provider_title, item, media_type),
        reverse=True,
    )
    for item in preliminary[:10]:
        tmdb_id = item.get("id")
        if type(tmdb_id) is not int or tmdb_id <= 0:
            continue
        if _title_similarity(provider_title, item, media_type) >= MIN_TITLE_SCORE_WITH_YEAR:
            continue
        try:
            aliases_payload = search_fn(
                f"/{endpoint_type}/{tmdb_id}/alternative_titles", {}
            )
            item["_alternative_titles"] = _alternative_title_values(aliases_payload)
        except Exception as exc:
            item["_alternative_titles_error"] = type(exc).__name__

    # Fetch corroborating metadata for a bounded shortlist BEFORE applying the
    # title threshold. This lets independent provider metadata rescue translated
    # or abbreviated titles without lowering the global title threshold.
    metadata_scores: dict[int, dict[str, float | None]] = {}
    for item in preliminary[:5]:
        tmdb_id = item.get("id")
        if type(tmdb_id) is int and tmdb_id > 0:
            metadata_scores[tmdb_id] = _metadata_signal_scores(
                item, media_type, metadata, provider, search_fn
            )

    def scores_for(item: dict[str, Any]) -> dict[str, float | None]:
        tmdb_id = item.get("id")
        return metadata_scores.get(tmdb_id, {}) if type(tmdb_id) is int else {}

    def corroboration_values(item: dict[str, Any]) -> list[float]:
        signals = scores_for(item)
        # Genres are useful for ranking, but not strong enough to independently
        # rescue a weak title match. Require two separate people/studio signals.
        return [
            value for key, value in signals.items()
            if key in {"actor_score", "director_score", "studio_score"}
            and value is not None
        ]

    def corroboration_score(item: dict[str, Any]) -> float | None:
        values = corroboration_values(item)
        return round(sum(values) / len(values), 4) if values else None

    candidates: list[tuple[float, dict[str, Any], float, int | None, float | None]] = []
    rejected_diagnostics = []
    for item in preliminary[:10]:
        title_score, matched_title = _best_title_match(provider_title, item, media_type)
        independent_signals = corroboration_values(item)
        strong_metadata = (
            len([value for value in independent_signals if value >= 0.85]) >= 2
            and corroboration_score(item) is not None
            and corroboration_score(item) >= 0.85
        )
        if title_score < MIN_TITLE_SCORE_WITH_YEAR and not strong_metadata:
            rejected_diagnostics.append({
                "tmdb_id": item.get("id"),
                "title": (item.get("name") if media_type == "series" else item.get("title")) or "",
                "original_title": (
                    item.get("original_name") if media_type == "series"
                    else item.get("original_title")
                ),
                "alternative_titles": item.get("_alternative_titles") or [],
                "title_score": title_score,
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

        candidate_year = _year_from_result(item, media_type)
        base_score = _candidate_score(
            provider_title, provider_year, item, media_type, provider_genres, genre_map
        )
        if strong_metadata and title_score < MIN_TITLE_SCORE_WITH_YEAR:
            # A metadata-rescued candidate must clear a high evidence floor.
            # The separate margin check below still protects against competing
            # candidates with similarly strong evidence.
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
        return {"status": "no_match", "reason": "No TMDB candidate had sufficient title or independent metadata evidence",
                "diagnostics": {
                    "provider_title": provider_title, "provider_year": provider_year,
                    "provider_genres": provider_genres,
                    "search_attempts": search_attempts,
                    "tmdb_result_count": len(all_candidates),
                    "title_compatible_candidates": 0,
                    "candidates": rejected_diagnostics,
                }}

    def adjusted_score(pair: tuple[float, dict[str, Any], float, int | None, float | None]) -> float:
        corroboration = corroboration_score(pair[1])
        # Metadata can rank candidates, but does not override a strong title
        # match. Rescue eligibility itself requires two strong independent signals.
        score = pair[0] + (
            max(0.0, corroboration - 0.5) * 0.20
            if corroboration is not None else 0.0
        )
        # A positive genre overlap is a separate, weak ranking signal. A
        # mismatch remains neutral, but a full match must be strong enough to
        # distinguish otherwise identical title/year candidates.
        genre_score = pair[4]
        if genre_score is not None:
            score += genre_score * 0.10
        # Year is a bounded ranking hint, never an exclusion rule or a confidence
        # score. A same-year candidate gets +0.05; a 10+ year gap gets -0.05.
        item_year = pair[3]
        if provider_year is not None and item_year is not None:
            delta = abs(provider_year - item_year)
            score += 0.05 - min(delta, 10) * 0.01
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
                      "matched_variant": _best_title_match(provider_title, item, media_type)[1]},
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
                       "status": "not_available" if signals.get("actor_score") is None else
                                 ("match" if signals["actor_score"] >= 0.5 else "mismatch")},
            "directors": {"provider": metadata.get("directors") or [], "score": signals.get("director_score"),
                          "status": "not_available" if signals.get("director_score") is None else
                                    ("match" if signals["director_score"] >= 0.5 else "mismatch")},
            "studio": {"provider": metadata.get("producer") if provider == "aniworld" else None,
                       "score": signals.get("studio_score"),
                       "status": "not_available" if signals.get("studio_score") is None else
                                 ("match" if signals["studio_score"] >= 0.5 else "mismatch")},
        })
    diagnostics = {
        "search": {"query": provider_title, "languages": languages,
                   "attempts": search_attempts,
                   "tmdb_result_count": len(all_candidates),
                   "title_compatible_candidates": len(candidates)},
        "provider_data": {"title": provider_title, "year": provider_year,
                          "genres": provider_genres, "actors": metadata.get("actors") or [],
                          "directors": metadata.get("directors") or [],
                          "studio": metadata.get("producer") if provider == "aniworld" else None},
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
    if not IMDB_ID_RE.fullmatch(imdb_id):
        return {"status": "missing_imdb", "reason": "TMDB candidate has no valid IMDb ID",
                "diagnostics": diagnostics, "external_ids": external, **candidate}
    return {"status": "matched", "imdb_id": imdb_id,
            "diagnostics": diagnostics, "external_ids": external, **candidate}


def resolve_unresolved_titles(
    *,
    provider: str | None = None,
    limit: int = 500,
    apply: bool = False,
    search_fn: Callable[..., dict[str, Any]] = _tmdb_get,
) -> dict[str, Any]:
    """Evaluate queued titles and optionally persist only high-confidence matches."""
    init_db()
    records = get_unresolved_titles(provider=provider, limit=limit)
    summary: dict[str, Any] = {
        "scanned": len(records),
        "matched": 0,
        "applied": 0,
        "ambiguous": 0,
        "missing_imdb": 0,
        "no_match": 0,
        "errors": 0,
        "dry_run": not apply,
        "results": [],
    }
    genre_maps: dict[str, dict[int, str]] = {}
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
                    payload = search_fn(endpoint, {"language": "de-DE"})
                    genre_maps[media_type] = {
                        int(item["id"]): str(item["name"])
                        for item in payload.get("genres", [])
                        if isinstance(item, dict) and item.get("id") is not None and item.get("name")
                    }
                except Exception:
                    genre_maps[media_type] = {}
            genre_map = genre_maps[media_type]
        result = find_tmdb_match(record, search_fn=search_fn, genre_map=genre_map)
        result_row = {
            "provider": record["provider"],
            "title": record["provider_title"],
            "url": record["provider_url"],
            **result,
        }
        summary["results"].append(result_row)
        status = result["status"]
        if status == "matched":
            summary["matched"] += 1
            if apply:
                metadata = dict(record.get("metadata") or {})
                metadata["identity_resolution"] = {
                    "source": "tmdb",
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
                summary["applied"] += 1
        elif status == "ambiguous":
            summary["ambiguous"] += 1
        elif status == "missing_imdb":
            summary["missing_imdb"] += 1
        elif status == "no_match":
            summary["no_match"] += 1
        else:
            summary["errors"] += 1
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve provider titles to IMDb IDs via TMDB")
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
