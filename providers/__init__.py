"""Provider abstraction and implementations for catalog discovery."""

from .base import (
    BaseProvider,
    ProviderEpisode,
    ProviderSeason,
    ProviderTitle,
    ProviderVariant,
)
from .aniworld import AniWorldProvider
from .serienstream import SerienstreamProvider
from .crawler import crawl_named_provider, crawl_provider

PROVIDERS = {
    "aniworld": AniWorldProvider,
    "serienstream": SerienstreamProvider,
}

__all__ = [
    "BaseProvider", "ProviderTitle", "ProviderSeason", "ProviderEpisode",
    "ProviderVariant", "AniWorldProvider", "SerienstreamProvider", "PROVIDERS",
    "crawl_provider", "crawl_named_provider",
]
