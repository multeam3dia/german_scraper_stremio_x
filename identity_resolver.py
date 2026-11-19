"""Confidence-aware provider-title to IMDb identity resolution via TMDB.

The resolver is intentionally conservative: it never promotes a title based on
fuzzy title similarity alone. A candidate must have a strong title match, a
compatible release year, a valid IMDb ID from TMDB external IDs, and a clear
margin over the runner-up.
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
    return [str(item[key]) for key in keys if item.get(key)]


def _title_similarity(
    provider_title: str, item: dict[str, Any], media_type: str
) -> float:
    source = _normalize(provider_title)
    variants = [_normalize(value) for value in _title_variants(item, media_type)]
    variants = [value for value in variants if value]
    if not source or not variants:
        return 0.0
    return round(max(
        1.0 if source == value else difflib.SequenceMatcher(None, source, value).ratio()
        for value in variants
    ), 4)


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
    provider_year = _coerce_year(provider_year)
    result_year = _year_from_result(item, media_type)
    if provider_year is None or result_year is None:
        year_score = 0.5
    elif result_year == provider_year:
        year_score = 1.0
    else:
        year_score = max(0.0, 1.0 - abs(result_year - provider_year) / 5.0)
    score = (title_score * 0.88) + (year_score * 0.12)
    genre_score = (
        _genre_similarity(provider_genres or [], item, genre_map or {})
        if provider_genres and genre_map else None
    )
    # Genre is a small tie-breaker, never a replacement for title/year evidence.
    if genre_score is not None:
        score += (genre_score - 0.5) * 0.04
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
    # Search by title only. Filtering at the API level can hide remakes or
    # nearby premiere years before the resolver has a chance to compare them.
    params: dict[str, Any] = {
        "query": provider_title,
        "include_adult": "false",
        "language": "de-DE",
    }

    try:
        response = search_fn(f"/search/{endpoint_type}", params)
    except (TMDBAPIError, TMDBConfigurationError) as exc:
        return {"status": "error", "reason": str(exc)}
    except Exception as exc:
        return {"status": "error", "reason": f"Search failed: {type(exc).__name__}"}

    results = response.get("results")
    if not isinstance(results, list) or not results:
        return {"status": "no_match", "reason": "TMDB returned no candidates"}

    exact_year: list[tuple[float, dict[str, Any], float, int | None, float | None]] = []
    near_year: list[tuple[float, dict[str, Any], float, int | None, float | None]] = []
    missing_year: list[tuple[float, dict[str, Any], float, int | None, float | None]] = []
    exact_title_without_year: list[tuple[float, dict[str, Any], float, int | None, float | None]] = []
    saw_title_candidate = False
    saw_year_mismatch = False

    for item in results:
        if not isinstance(item, dict):
            continue
        title_score = _title_similarity(provider_title, item, media_type)
        if title_score < MIN_TITLE_SCORE_WITH_YEAR:
            continue
        saw_title_candidate = True
        candidate_year = _year_from_result(item, media_type)
        score = _candidate_score(
            provider_title, provider_year, item, media_type, provider_genres, genre_map
        )
        genre_score = (
            _genre_similarity(provider_genres, item, genre_map or {})
            if provider_genres and genre_map else None
        )
        row = (score, item, title_score, candidate_year, genre_score)

        if provider_year is None:
            # Without a provider year, only an exact normalized title can be
            # auto-matched. Similar titles need independent date evidence.
            if title_score == 1.0:
                exact_title_without_year.append(row)
        elif candidate_year is None:
            missing_year.append(row)
        elif candidate_year == provider_year:
            exact_year.append(row)
        elif abs(candidate_year - provider_year) == 1:
            # A one-year difference can be a regional premiere-date variation,
            # but is not strong enough evidence for automatic identity assignment.
            near_year.append(row)
        else:
            saw_year_mismatch = True

    # Prefer an exact year. Near-year or yearless results are retained for
    # manual review, never promoted automatically.
    if provider_year is None:
        candidates = exact_title_without_year
        review_reason = None
    elif exact_year:
        candidates = exact_year
        review_reason = None
    elif near_year:
        candidates = near_year
        review_reason = "Candidate year differs by one year; manual review required"
    elif missing_year:
        candidates = missing_year
        review_reason = "TMDB candidate has no release year; manual review required"
    else:
        candidates = []
        review_reason = None

    if not candidates:
        if provider_year is None and saw_title_candidate:
            reason = "Provider year is missing and no exact title candidate was found"
        elif saw_year_mismatch:
            reason = "No title candidate has a compatible release year"
        else:
            reason = "TMDB returned no sufficiently strong title candidate"
        return {"status": "no_match", "reason": reason}

    candidates.sort(key=lambda pair: pair[0], reverse=True)

    # Optional cast/director/studio evidence is only fetched for the top
    # five exact-title candidates. Missing metadata is neutral. These signals
    # only affect the runner-up margin; title/year confidence remains mandatory.
    metadata_scores: dict[int, dict[str, float | None]] = {}
    if len(candidates) > 1:
        for _, item, _, _, _ in candidates[:5]:
            tmdb_id = item.get("id")
            if type(tmdb_id) is int and tmdb_id > 0:
                metadata_scores[tmdb_id] = _metadata_signal_scores(
                    item, media_type, metadata, provider, search_fn
                )

    def scores_for(item: dict[str, Any]) -> dict[str, float | None]:
        tmdb_id = item.get("id")
        return metadata_scores.get(tmdb_id, {}) if type(tmdb_id) is int else {}

    def corroboration_score(item: dict[str, Any]) -> float | None:
        values = [value for value in scores_for(item).values() if value is not None]
        return round(sum(values) / len(values), 4) if values else None

    def adjusted_score(pair: tuple[float, dict[str, Any], float, int | None, float | None]) -> float:
        corroboration = corroboration_score(pair[1])
        # A bounded +/-0.02 nudge lets metadata resolve close candidates
        # without overpowering the primary title/year score.
        return pair[0] + ((corroboration - 0.5) * 0.04 if corroboration is not None else 0.0)

    candidates.sort(key=adjusted_score, reverse=True)
    best_score, best, title_score, candidate_year, best_genre_score = candidates[0]
    runner_up = candidates[1][0] if len(candidates) > 1 else 0.0
    margin = adjusted_score(candidates[0]) - (
        adjusted_score(candidates[1]) if len(candidates) > 1 else 0.0
    )
    runner_up_genre = candidates[1][4] if len(candidates) > 1 else None
    if best_genre_score is not None and runner_up_genre is not None:
        margin += (best_genre_score - runner_up_genre) * 0.10
    best_metadata_score = corroboration_score(best)
    runner_metadata_score = (
        corroboration_score(candidates[1][1]) if len(candidates) > 1 else None
    )
    if best_metadata_score is not None and runner_metadata_score is not None:
        # Combined optional metadata can contribute at most 0.12 to the margin.
        margin += (best_metadata_score - runner_metadata_score) * 0.12
    margin = round(margin, 4)
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
    if review_reason:
        return {"status": "ambiguous", "reason": review_reason, **candidate}
    if best_score < MIN_CONFIDENCE:
        return {"status": "ambiguous", "reason": "Best candidate is below confidence threshold", **candidate}
    if margin < MIN_MARGIN:
        return {"status": "ambiguous", "reason": "Multiple candidates have similar scores", **candidate}
    if type(best.get("id")) is not int or best["id"] <= 0:
        return {"status": "ambiguous", "reason": "TMDB candidate has no numeric ID", **candidate}

    try:
        external = search_fn(f"/{endpoint_type}/{best['id']}/external_ids", {})
    except Exception as exc:
        return {"status": "error", "reason": f"External ID lookup failed: {type(exc).__name__}", **candidate}
    imdb_id = str(external.get("imdb_id") or "").strip()
    if not IMDB_ID_RE.fullmatch(imdb_id):
        return {"status": "ambiguous", "reason": "TMDB candidate has no valid IMDb ID", **candidate}

    return {"status": "matched", "imdb_id": imdb_id, **candidate}


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
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
