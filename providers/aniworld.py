"""AniWorld adapter backed by the repository's existing AniWorld models."""
from __future__ import annotations

import re
from typing import Iterator

from aniworld.models.aniworld_to import AniworldSeries
from .indexes import fetch_aniworld_index

from .base import BaseProvider, ProviderEpisode, ProviderSeason, ProviderTitle


def _structure_summary(series: AniworldSeries) -> dict:
    """Summarize parsed season pages and titles without fetching episode pages."""
    seasons = []
    for season in series.seasons or []:
        episodes = list(getattr(season, "episodes", []) or [])
        season_number = int(getattr(season, "season_number", 0) or 0)
        episode_titles = []
        for episode in episodes:
            title_de = getattr(episode, "_AniworldEpisode__title_de", None)
            title_en = getattr(episode, "_AniworldEpisode__title_en", None)
            titles = [str(value).strip() for value in (title_de, title_en) if value]
            if titles:
                episode_titles.append({
                    "episode_number": int(getattr(episode, "episode_number", 0) or 0),
                    "titles": list(dict.fromkeys(titles)),
                })
        seasons.append({
            "season_number": season_number,
            "episode_count": len(episodes),
            "is_movie_collection": bool(getattr(season, "are_movies", False)),
            "episode_titles": episode_titles,
        })
    return {
        "season_count": len(seasons),
        "episode_count": sum(item["episode_count"] for item in seasons),
        "seasons": seasons,
        "complete": bool(seasons),
    }


def _year(value: object) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def _is_standalone_movie(series: AniworldSeries) -> bool:
    """Classify a catalog entry as a movie only when its structure proves it.

    AniWorld exposes films attached to a series under a separate /filme
    collection. A series page with only that collection and no numbered seasons
    is treated as a standalone movie entry. Mixed series-plus-film pages remain
    series. An empty/unavailable structure is deliberately inconclusive.
    """
    if not bool(series.has_movies):
        return False
    seasons = list(series.seasons or [])
    return bool(seasons) and all(
        bool(getattr(season, "are_movies", False)) for season in seasons
    )


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
        media_type = "movie" if _is_standalone_movie(series) else "series"
        return ProviderTitle(
            provider=self.provider_id,
            media_type=media_type,
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
                "standalone_movie": media_type == "movie",
                "provider_structure": _structure_summary(series),
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
