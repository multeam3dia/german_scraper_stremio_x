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

__all__ = [
    "BaseProvider", "ProviderTitle", "ProviderSeason", "ProviderEpisode",
    "ProviderVariant", "AniWorldProvider", "SerienstreamProvider", "PROVIDERS",
]
