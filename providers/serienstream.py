"""Serienstream adapter backed by the repository's existing s.to models."""
from __future__ import annotations

import re
from typing import Iterator

from aniworld.models.s_to import SerienstreamSeries
from aniworld.search import fetch_all_series_index

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


class SerienstreamProvider(BaseProvider):
    provider_id = "serienstream"
    # The provider's /serien index is a single page containing the full A-Z list.
    # The crawler still marks the run partial if any title cannot be resolved.
    catalog_scan_complete = True
    supported_media_types = ("series",)

    def iter_catalog(self) -> Iterator[ProviderTitle]:
        """Discover series from s.to's alphabetical all-series index."""
        items = fetch_all_series_index()
        if items is None:
            raise RuntimeError("Serienstream all-series index discovery failed")

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

    def get_title(self, url: str) -> ProviderTitle:
        series = SerienstreamSeries(url)
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
                    title=getattr(episode, "title_de", None) or None,
                    title_original=getattr(episode, "title_en", None) or None,
                    variants=_variants(episode),
                ))
            result.append(ProviderSeason(
                provider=self.provider_id,
                url=str(season.url),
                season_number=season_number,
                episodes=episodes,
            ))
        return result
