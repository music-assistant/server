"""Base classes for recommendation sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    RecommendationFolder,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

RecommendationItems = Sequence[MediaItemType | ItemMapping | BrowseFolder]


class RecommendationSource(ABC):
    """A single recommendation row (one RecommendationFolder)."""

    def __init__(
        self,
        mass: MusicAssistant,
        *,
        item_id: str,
        name: str,
        translation_key: str,
        icon: str,
        provider: str = "library",
        enabled_by_default: bool = True,
    ) -> None:
        """Initialize the source."""
        self.mass = mass
        self.item_id = item_id
        self.name = name
        self.translation_key = translation_key
        self.icon = icon
        self.provider = provider
        self.enabled_by_default = enabled_by_default

    @abstractmethod
    async def get_items(self) -> RecommendationItems:
        """Return the items for this recommendation row."""

    def descriptor(self) -> RecommendationFolder:
        """Return this source's row descriptor: a RecommendationFolder without items."""
        return RecommendationFolder(
            item_id=self.item_id,
            provider=self.provider,
            name=self.name,
            translation_key=self.translation_key,
            icon=self.icon,
            enabled_by_default=self.enabled_by_default,
        )


class CallableRecommendationSource(RecommendationSource):
    """Recommendation source backed by a simple async items factory."""

    def __init__(
        self,
        mass: MusicAssistant,
        *,
        item_id: str,
        name: str,
        translation_key: str,
        icon: str,
        items_factory: Callable[[], Awaitable[RecommendationItems]],
        provider: str = "library",
        enabled_by_default: bool = True,
    ) -> None:
        """Initialize the callable source."""
        super().__init__(
            mass,
            item_id=item_id,
            name=name,
            translation_key=translation_key,
            icon=icon,
            provider=provider,
            enabled_by_default=enabled_by_default,
        )
        self._items_factory = items_factory

    async def get_items(self) -> RecommendationItems:
        """Return the items for this recommendation row."""
        return await self._items_factory()
