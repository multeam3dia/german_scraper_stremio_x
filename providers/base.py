"""Common provider contracts.

Provider adapters normalize site-specific models into small, serializable records.
They deliberately do not resolve IMDb IDs: identity matching is a separate,
confidence-aware pipeline step.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator


@dataclass(slots=True)
class ProviderTitle:
    provider: str
    media_type: str
    title: str
    url: str
    imdb_id: str | None = None
    year: int | None = None
    poster_url: str | None = None
    description: str | None = None
    provider_genres: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProviderVariant:
    audio_language: str
    subtitle_language: str | None = None
    hoster: str | None = None
    source_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProviderEpisode:
    provider: str
    url: str
    season_number: int
    episode_number: int
    title: str | None = None
    title_original: str | None = None
    is_movie: bool = False
    variants: list[ProviderVariant] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["variants"] = [variant.to_dict() for variant in self.variants]
        return result


@dataclass(slots=True)
class ProviderSeason:
    provider: str
    url: str
    season_number: int
    is_movie_collection: bool = False
    episodes: list[ProviderEpisode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "url": self.url,
            "season_number": self.season_number,
            "is_movie_collection": self.is_movie_collection,
            "episodes": [episode.to_dict() for episode in self.episodes],
        }


class BaseProvider(ABC):
    """Stable interface shared by catalog crawlers and playback integrations."""

    provider_id: str
    supported_media_types: tuple[str, ...]

    @abstractmethod
    def iter_catalog(self) -> Iterator[ProviderTitle]:
        """Yield provider title references; implementations document scan coverage."""

    @abstractmethod
    def get_title(self, url: str) -> ProviderTitle:
        """Fetch and normalize one provider detail page."""

    @abstractmethod
    def get_seasons(self, title: ProviderTitle) -> list[ProviderSeason]:
        """Return seasons/film collections and their episode/source variants."""

    def get_episode_streams(
        self,
        episode_url: str,
        display_title: str,
        binge_group: str | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve streams for one episode, only when playback is requested."""
        return []

    def healthcheck(self) -> dict[str, Any]:
        return {"provider": self.provider_id, "implemented": True}
