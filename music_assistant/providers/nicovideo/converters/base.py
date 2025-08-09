"""Base classes for nicovideo converters."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant.models.music_provider import MusicProvider
    from music_assistant.providers.nicovideo.converters.hub import (
        NicovideoConverterHub,
    )
    from music_assistant.providers.nicovideo.converters.item_mappings import ItemMappingConverter


class NicovideoConverterBase:
    """Base class for specialized nicovideo converters."""

    def __init__(self, converter_hub: NicovideoConverterHub) -> None:
        """Initialize with reference to main converter."""
        self.converter_hub = converter_hub
        self.logger = converter_hub.logger.getChild(self.__class__.__name__)

    @property
    def provider(self) -> MusicProvider:
        """Get the main converter hub instance."""
        return self.converter_hub.provider

    @property
    def item_mapper(self) -> ItemMappingConverter:
        """Get the item mapper instance."""
        return self.converter_hub.item_mapper
