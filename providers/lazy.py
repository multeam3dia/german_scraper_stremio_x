"""On-demand loading of provider season structures and episode streams.

The catalog crawler only verifies title availability. This module fetches a
provider's season pages when Stremio opens a series and fetches one episode
page only when playback for that episode is requested.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from catalog_db import (
    get_available_provider_entries,
    get_series_structure,
    is_provider_structure_fresh,
    mark_provider_structure_fetched,
    record_series_structure,
)
from . import PROVIDERS
from .base import ProviderTitle

logger = logging.getLogger(__name__)

try:
    STRUCTURE_CACHE_TTL_SECONDS = max(
        300, int(os.getenv("SERIES_STRUCTURE_CACHE_TTL_SECONDS", "86400"))
    )
except (TypeError, ValueError):
    STRUCTURE_CACHE_TTL_SECONDS = 86400


def ensure_series_structure(imdb_id: str) -> list[dict[str, Any]]:
    """Load stale/missing season lists for known providers and return the cache."""
    entries = get_available_provider_entries("series", imdb_id)
    for entry in entries:
        provider_id = str(entry.get("provider") or "").strip().lower()
        provider_class = PROVIDERS.get(provider_id)
        provider_url = str(entry.get("provider_url") or "").strip()
        if not provider_class or not provider_url:
            continue
        if is_provider_structure_fresh(
            "series",
            imdb_id,
            provider_id,
            ttl_seconds=STRUCTURE_CACHE_TTL_SECONDS,
        ):
            continue

        previous = [
            item for item in get_series_structure("series", imdb_id)
            if item.get("provider") == provider_id
        ]
        try:
            provider = provider_class()
            title = ProviderTitle(
                provider=provider_id,
                media_type="series",
                title=str(entry.get("provider_title") or imdb_id),
                url=provider_url,
            )
            seasons = provider.get_seasons(title)
            # A transient parser/site failure must not erase a previously
            # usable structure. A legitimate first-time empty result is cached.
            if not seasons and previous:
                raise RuntimeError("provider returned an empty season structure")
            record_series_structure(
                "series",
                imdb_id,
                provider_id,
                [season.to_dict() for season in seasons],
            )
            mark_provider_structure_fetched("series", imdb_id, provider_id)
            logger.info(
                "Loaded lazy series structure: provider=%s imdb=%s seasons=%d",
                provider_id,
                imdb_id,
                len(seasons),
            )
        except Exception as exc:
            logger.warning(
                "Could not refresh series structure for %s (%s): %s",
                imdb_id,
                provider_id,
                type(exc).__name__,
            )

    available_ids = {
        str(entry.get("provider") or "").strip().lower()
        for entry in entries
        if str(entry.get("provider") or "").strip().lower() in PROVIDERS
    }
    return [
        item for item in get_series_structure("series", imdb_id)
        if item.get("provider") in available_ids
    ]


def _canonical_meta(imdb_id: str) -> dict[str, Any]:
    try:
        from scraper import get_metadata

        result = get_metadata("series", imdb_id)
        return dict(result) if isinstance(result, dict) else {}
    except Exception as exc:
        logger.debug("Cinemeta lookup failed for %s: %s", imdb_id, type(exc).__name__)
        return {}


def _canonical_videos(meta: dict[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
    videos: dict[tuple[int, int], dict[str, Any]] = {}
    for video in meta.get("videos") or []:
        if not isinstance(video, dict):
            continue
        try:
            key = (int(video.get("season")), int(video.get("episode")))
        except (TypeError, ValueError):
            continue
        videos[key] = video
    return videos


def get_series_meta(imdb_id: str) -> dict[str, Any]:
    """Return Cinemeta metadata with only episodes confirmed by a provider."""
    meta = _canonical_meta(imdb_id)
    entries = get_available_provider_entries("series", imdb_id)
    if not meta:
        meta = {
            "id": imdb_id,
            "type": "series",
            "name": str(entries[0].get("provider_title") or imdb_id) if entries else imdb_id,
            "posterShape": "poster",
        }

    structures = ensure_series_structure(imdb_id)
    canonical = _canonical_videos(meta)
    available_ids = {
        str(entry.get("provider") or "").strip().lower()
        for entry in entries
    }
    videos_by_key: dict[tuple[int, int], dict[str, Any]] = {}

    if canonical:
        # Prefer Cinemeta's canonical numbering. AniWorld episode numbers can
        # differ; _find_provider_episode maps those by exact episode title.
        for key, source_video in sorted(canonical.items()):
            season_number, episode_number = key
            if episode_number <= 0:
                continue
            target_title = str(source_video.get("title") or "").strip() or None
            confirmed = any(
                _find_provider_episode(
                    provider_id,
                    structures,
                    season_number,
                    episode_number,
                    target_title,
                )
                for provider_id in available_ids
            )
            if not confirmed:
                continue
            video = {
                "id": f"{imdb_id}:{season_number}:{episode_number}",
                "title": str(target_title or f"Episode {episode_number}"),
                "season": season_number,
                "episode": episode_number,
            }
            for field in ("released", "thumbnail", "overview"):
                if source_video.get(field):
                    video[field] = source_video[field]
            videos_by_key[key] = video
    else:
        # If Cinemeta has no episode list, fall back to the provider's own
        # numbering and title data, still deduplicating across providers.
        for season_data in structures:
            if season_data.get("is_movie_collection"):
                continue
            try:
                season_number = int(season_data.get("season_number", 0))
            except (TypeError, ValueError):
                continue
            for episode_data in season_data.get("episodes") or []:
                if episode_data.get("is_movie"):
                    continue
                try:
                    episode_number = int(episode_data.get("episode_number", 0))
                except (TypeError, ValueError):
                    continue
                if episode_number <= 0:
                    continue
                key = (season_number, episode_number)
                if key in videos_by_key:
                    continue
                title = (
                    episode_data.get("title")
                    or episode_data.get("title_original")
                    or f"Episode {episode_number}"
                )
                videos_by_key[key] = {
                    "id": f"{imdb_id}:{season_number}:{episode_number}",
                    "title": str(title),
                    "season": season_number,
                    "episode": episode_number,
                }

    meta["id"] = imdb_id
    meta["type"] = "series"
    meta["videos"] = [videos_by_key[key] for key in sorted(videos_by_key)]
    return {"meta": meta}

def _find_provider_episode(
    provider_id: str,
    structures: list[dict[str, Any]],
    season_number: int,
    episode_number: int,
    target_title: str | None,
) -> dict[str, Any] | None:
    provider_seasons = [
        item for item in structures if item.get("provider") == provider_id
    ]

    # AniWorld occasionally numbers episodes differently from TMDB/Cinemeta.
    # When Cinemeta provides a title, require an exact normalized title match
    # rather than risking playback of a different episode at the same number.
    if provider_id == "aniworld" and target_title:
        from scraper import clean_for_match

        wanted = clean_for_match(target_title)
        if not wanted or wanted in {
            "episode", "folge", "episodetba", "tba", "untitled"
        }:
            return None
        matches = []
        for season_data in provider_seasons:
            if season_data.get("is_movie_collection"):
                continue
            for episode_data in season_data.get("episodes") or []:
                if episode_data.get("is_movie"):
                    continue
                names = {
                    clean_for_match(episode_data.get("title") or ""),
                    clean_for_match(episode_data.get("title_original") or ""),
                }
                names.discard("")
                if wanted in names:
                    matches.append((season_data, episode_data))
        if len(matches) == 1:
            return matches[0][1]
        if len(matches) > 1:
            same_position = [
                pair for pair in matches
                if int(pair[0].get("season_number", -1)) == season_number
                and int(pair[1].get("episode_number", -1)) == episode_number
            ]
            if len(same_position) == 1:
                return same_position[0][1]
        return None

    for season_data in provider_seasons:
        try:
            current_season = int(season_data.get("season_number", -1))
        except (TypeError, ValueError):
            continue
        if current_season != season_number or season_data.get("is_movie_collection"):
            continue
        for episode_data in season_data.get("episodes") or []:
            try:
                current_episode = int(episode_data.get("episode_number", -1))
            except (TypeError, ValueError):
                continue
            if current_episode == episode_number and not episode_data.get("is_movie"):
                return episode_data
    return None


def get_series_streams_for_episode(
    imdb_id: str,
    season_number: int,
    episode_number: int,
) -> list[dict[str, Any]]:
    """Resolve only providers recorded as available for this exact IMDb title."""
    entries = get_available_provider_entries("series", imdb_id)
    structures = ensure_series_structure(imdb_id)
    meta = _canonical_meta(imdb_id)
    target_video = None
    try:
        from scraper import _get_cinemeta_episode

        target_video = _get_cinemeta_episode(meta, season_number, episode_number)
    except Exception:
        target_video = None
    target_title = (
        str(target_video.get("title") or "").strip()
        if isinstance(target_video, dict) else None
    )
    display_title = str(meta.get("name") or (entries[0].get("provider_title") if entries else imdb_id))
    display_label = f"{display_title} S{season_number:02d}E{episode_number:02d}"

    streams: list[dict[str, Any]] = []
    for entry in entries:
        provider_id = str(entry.get("provider") or "").strip().lower()
        provider_class = PROVIDERS.get(provider_id)
        if not provider_class:
            continue
        episode_data = _find_provider_episode(
            provider_id,
            structures,
            season_number,
            episode_number,
            target_title,
        )
        episode_url = str((episode_data or {}).get("url") or "").strip()
        if not episode_url:
            continue
        try:
            provider = provider_class()
            streams.extend(
                provider.get_episode_streams(
                    episode_url,
                    display_label,
                    binge_group=imdb_id,
                )
            )
        except Exception as exc:
            logger.warning(
                "Episode playback lookup failed for %s (%s): %s",
                imdb_id,
                provider_id,
                type(exc).__name__,
            )
    return streams
