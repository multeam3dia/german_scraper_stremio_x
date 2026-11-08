"""AniWorld adapter backed by the repository's existing AniWorld models."""
from __future__ import annotations

import re
from typing import Iterator

from aniworld.models.aniworld_to import AniworldSeries
from aniworld.search import GENRE_FALLBACK, fetch_genres, fetch_genre_animes

from .base import BaseProvider, ProviderEpisode, ProviderSeason, ProviderTitle, ProviderVariant


def _year(value: object) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def _variants(episode: object) -> list[ProviderVariant]:
    provider_data = getattr(episode, "provider_data", {}) or {}
    data = getattr(provider_data, "_data", provider_data) or {}
    variants: list[ProviderVariant] = []
    for language_pair, hosters in data.items():
        audio = getattr(language_pair[0], "value", str(language_pair[0]))
        subtitles = getattr(language_pair[1], "value", str(language_pair[1]))
        for hoster, source_url in (hosters or {}).items():
            variants.append(ProviderVariant(
                audio_language=str(audio),
                subtitle_language=None if subtitles.lower() == "none" else str(subtitles),
                hoster=str(hoster),
                source_url=str(source_url),
            ))
    return variants


class AniWorldProvider(BaseProvider):
    provider_id = "aniworld"
    catalog_scan_complete = True
    supported_media_types = ("series",)

    def iter_catalog(self) -> Iterator[ProviderTitle]:
        """Enumerate all visible AniWorld genre pages, deduplicated by canonical URL."""
        seen: set[str] = set()
        genres = fetch_genres() or []
        fallback_genres = [
            {"name": name, "slug": slug} for name, slug in GENRE_FALLBACK
        ]
        # The fallback list is useful for discovery, but it is not proof that
        # the homepage's current genre index was read completely. Do not use
        # such a pass to mark old provider entries as missing.
        self.catalog_scan_complete = bool(genres) and genres != fallback_genres
        if not genres:
            raise RuntimeError("AniWorld genre index is empty")
        for genre in genres:
            slug = str(genre.get("slug") or "").strip()
            if not slug:
                continue
            page = 1
            while True:
                result = fetch_genre_animes(slug, page=page)
                if result is None:
                    # A failed page is not interpreted as end-of-catalog.
                    raise RuntimeError(f"AniWorld genre scan failed: {slug}, page {page}")
                for item in result.get("results") or []:
                    url = str(item.get("url") or "").strip()
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    yield ProviderTitle(
                        provider=self.provider_id,
                        media_type="series",
                        title=str(item.get("title") or "").strip(),
                        url=url,
                        poster_url=item.get("poster_url"),
                        provider_genres=[str(genre.get("name"))] if genre.get("name") else [],
                    )
                if not result.get("has_more"):
                    break
                page += 1

    def get_title(self, url: str) -> ProviderTitle:
        series = AniworldSeries(url)
        imdb_id = str(series.imdb or "").strip() or None
        return ProviderTitle(
            provider=self.provider_id,
            media_type="series",
            title=str(series.title),
            url=url,
            imdb_id=imdb_id,
            year=_year(series.release_year),
            poster_url=series.poster_url or None,
            description=series.description or None,
            provider_genres=[str(value) for value in (series.genres or [])],
            metadata={
                "release_year": series.release_year,
                "country": series.country,
                "age_rating": series.age_rating,
                "rating": series.rating,
                "directors": series.directors,
                "actors": series.actors,
                "producer": series.producer,
                "has_movies": bool(series.has_movies),
            },
        )

    def get_seasons(self, title: ProviderTitle) -> list[ProviderSeason]:
        series = AniworldSeries(title.url)
        result: list[ProviderSeason] = []
        for season in series.seasons or []:
            is_movies = bool(getattr(season, "are_movies", False))
            season_number = int(getattr(season, "season_number", 0) or 0)
            episodes: list[ProviderEpisode] = []
            for episode in season.episodes or []:
                episodes.append(ProviderEpisode(
                    provider=self.provider_id,
                    url=str(episode.url),
                    season_number=season_number,
                    episode_number=int(getattr(episode, "episode_number", 0) or 0),
                    title=getattr(episode, "title_de", None) or None,
                    title_original=getattr(episode, "title_en", None) or None,
                    is_movie=bool(getattr(episode, "is_movie", False) or is_movies),
                    variants=_variants(episode),
                ))
            result.append(ProviderSeason(
                provider=self.provider_id,
                url=str(season.url),
                season_number=season_number,
                is_movie_collection=is_movies,
                episodes=episodes,
            ))
        return result
