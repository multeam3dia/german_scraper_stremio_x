"""Serienstream adapter backed by the repository's existing s.to models."""
from __future__ import annotations

import re
from typing import Iterator

from aniworld.models.s_to import SerienstreamSeries
from .indexes import fetch_serienstream_index

from .base import BaseProvider, ProviderEpisode, ProviderSeason, ProviderTitle


def _year(value: object) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


class SerienstreamProvider(BaseProvider):
    provider_id = "serienstream"
    # The provider's /serien index is a single page containing the full A-Z list.
    # The crawler still marks the run partial if any title cannot be resolved.
    catalog_scan_complete = True
    supported_media_types = ("series",)

    def iter_catalog(self, session=None) -> Iterator[ProviderTitle]:
        """Discover series from s.to's alphabetical all-series index."""
        self.catalog_scan_complete = False
        items = fetch_serienstream_index(session=session)
        if not items:
            raise RuntimeError("SerienStream all-series index is empty")
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
        series = SerienstreamSeries(url, session=session)
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
                "directors": series.directors,
                "actors": series.actors,
                "producer": series.producer,
            },
        )

    def get_seasons(self, title: ProviderTitle) -> list[ProviderSeason]:
        series = SerienstreamSeries(title.url)
        result: list[ProviderSeason] = []
        for season in series.seasons or []:
            season_number = int(getattr(season, "season_number", 0) or 0)
            episodes: list[ProviderEpisode] = []
            for episode in season.episodes or []:
                episodes.append(ProviderEpisode(
                    provider=self.provider_id,
                    url=str(episode.url),
                    season_number=season_number,
                    episode_number=int(getattr(episode, "episode_number", 0) or 0),
                    # SerienStream only exposes episode titles on the episode
                    # page. Do not touch title_de/title_en during structure loading.
                    title=None,
                    title_original=None,
                ))
            result.append(ProviderSeason(
                provider=self.provider_id,
                url=str(season.url),
                season_number=season_number,
                episodes=episodes,
            ))
        return result

    def get_episode_streams(
        self,
        episode_url: str,
        display_title: str,
        binge_group: str | None = None,
    ) -> list[dict]:
        """Fetch one SerienStream episode page and resolve its available streams."""
        from aniworld.models.s_to.episode import SerienstreamEpisode
        from scraper import extract_streams

        episode = SerienstreamEpisode(episode_url)
        return extract_streams(episode, "SerienStream", display_title, binge_group)
