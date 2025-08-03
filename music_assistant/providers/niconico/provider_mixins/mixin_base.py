"""Abstract base class for NiconicoMusicProvider mixins."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from music_assistant_models.enums import MediaType, ProviderFeature
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter
from music_assistant.providers.niconico.config import NiconicoConfig
from music_assistant.providers.niconico.tag_manager import TagManager


class NiconicoMusicProviderMixinBase(ABC):
    """Abstract base class for NiconicoMusicProvider mixins."""

    # Class variable where each mixin declares its supported features
    _supported_features: ClassVar[set[ProviderFeature]] = set()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the mixin base."""
        super().__init__(*args, **kwargs)
        self._niconico_config: NiconicoConfig | None = None
        self._tag_manager: TagManager | None = None

    @property
    def niconico_config(self) -> NiconicoConfig:
        """Get the config helper instance."""
        if self._niconico_config is None:
            self._niconico_config = NiconicoConfig(self.provider)
        return self._niconico_config

    @property
    def tag_manager(self) -> TagManager:
        """Get the tag manager instance."""
        if self._tag_manager is None:
            self._tag_manager = TagManager(self.provider, self.niconico_adapter)
        return self._tag_manager

    @classmethod
    def get_supported_features_for_mixin(cls) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return cls._supported_features.copy()

    @property
    @abstractmethod
    def provider(self) -> MusicProvider:
        """Return the MusicProvider instance associated with this Provider."""

    @property
    @abstractmethod
    def niconico_adapter(self) -> NicoNicoMusicAssistantAdapter:
        """Return the NicoNicoMusicAssistantAdapter instance associated with this Provider."""

    async def get_stream_details_for_mixin(
        self, item_id: str, media_type: MediaType
    ) -> StreamDetails | None:
        """Get stream details (streaming URL and format) for given item."""
        return None  # Default implementation: this mixin doesn't handle streams

    async def library_add_for_mixin(self, item: MediaItemType) -> bool | None:
        """Add item to library. Return True/False on success/failure, None if not handled."""
        return None  # Default implementation: this mixin doesn't handle library add

    async def library_remove_for_mixin(
        self, prov_item_id: str, media_type: MediaType
    ) -> bool | None:
        """Remove item from library. Return True/False on success/failure, None if not handled."""
        return None  # Default implementation: this mixin doesn't handle library remove
