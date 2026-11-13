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
PROVIDERS = {
    "aniworld": AniWorldProvider,
    "serienstream": SerienstreamProvider,
}

from .crawler import crawl_all_catalog_providers, crawl_named_provider, crawl_provider

__all__ = [
    "BaseProvider", "ProviderTitle", "ProviderSeason", "ProviderEpisode",
    "ProviderVariant", "AniWorldProvider", "SerienstreamProvider", "PROVIDERS",
    "crawl_provider", "crawl_named_provider", "crawl_all_catalog_providers",
]
