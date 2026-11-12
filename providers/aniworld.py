"""AniWorld adapter backed by the repository's existing AniWorld models."""
from __future__ import annotations

import re
from typing import Iterator

from aniworld.models.aniworld_to import AniworldSeries
from .indexes import fetch_aniworld_index

from .base import BaseProvider, ProviderEpisode, ProviderSeason, ProviderTitle


def _year(value: object) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


class AniWorldProvider(BaseProvider):
    provider_id = "aniworld"
    catalog_scan_complete = True
    supported_media_types = ("series",)

    def iter_catalog(self, session=None) -> Iterator[ProviderTitle]:
        """Enumerate the complete A-Z index without traversing genre pages."""
        self.catalog_scan_complete = False
        items = fetch_aniworld_index(session=session)
        if not items:
            raise RuntimeError("AniWorld A-Z index is empty")
        self.catalog_scan_complete = True
        for item in items:
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url or not title:
                continue
            yield ProviderTitle(
                provider=self.provider_id,
                media_type="series",
                title=title,
                url=url,
                poster_url=item.get("poster_url"),
            )

    def get_title(self, url: str, session=None) -> ProviderTitle:
        series = AniworldSeries(url, session=session)
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
                    # Read only values already parsed from the season page.
                    # The public properties fetch the episode page when empty.
                    title=getattr(episode, "_AniworldEpisode__title_de", None) or None,
                    title_original=getattr(episode, "_AniworldEpisode__title_en", None) or None,
                    is_movie=bool(getattr(episode, "is_movie", False) or is_movies),
                ))
            result.append(ProviderSeason(
                provider=self.provider_id,
                url=str(season.url),
                season_number=season_number,
                is_movie_collection=is_movies,
                episodes=episodes,
            ))
        return result

    def get_episode_streams(
        self,
        episode_url: str,
        display_title: str,
        binge_group: str | None = None,
    ) -> list[dict]:
        """Fetch one AniWorld episode page and resolve its available streams."""
        from aniworld.models.aniworld_to.episode import AniworldEpisode
        from scraper import _extract_aniworld_streams

        episode = AniworldEpisode(episode_url)
        return _extract_aniworld_streams(episode, display_title, binge_group)
