"""
NicovideoMusicProviderMixinBase: Interface definitions for _for_mixin patterns.

This abstract base class defines the common interface for all nicovideo provider mixins:
- Abstract properties for shared resources (config, adapter)
- _for_mixin method signatures for delegation patterns
- Default implementations returning None for optional functionality
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from music_assistant_models.enums import MediaType, ProviderFeature
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.providers.nicovideo.config import NicovideoConfig
    from music_assistant.providers.nicovideo.services.manager import NicovideoServiceManager


class NicovideoMusicProviderMixinBase(MusicProvider):
    """Interface for _for_mixin delegation patterns."""

    # Class variable where each mixin declares its supported features
    _supported_features: ClassVar[set[ProviderFeature]] = set()

    @property
    @abstractmethod
    def nicovideo_config(self) -> NicovideoConfig:
        """Get the config helper instance."""

    @property
    @abstractmethod
    def service_manager(self) -> NicovideoServiceManager:
        """Get the nicovideo service manager instance."""

    async def handle_async_init_for_mixin(self) -> None:
        """Handle async initialization for this mixin."""

    async def unload_for_mixin(self, is_removed: bool = False) -> None:
        """Handle unload/close for this mixin."""

    @classmethod
    def get_supported_features_for_mixin(cls) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return cls._supported_features.copy()

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
