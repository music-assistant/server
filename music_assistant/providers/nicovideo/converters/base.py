"""Base classes for nicovideo converters."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant.models.music_provider import MusicProvider
    from music_assistant.providers.nicovideo.converters.item_mappings import ItemMappingConverter
    from music_assistant.providers.nicovideo.converters.manager import (
        NicovideoConverterManager,
    )


class NicovideoConverterBase:
    """Base class for specialized nicovideo converters."""

    def __init__(self, converter_manager: NicovideoConverterManager) -> None:
        """Initialize with reference to main converter."""
        self.converter_manager = converter_manager
        self.logger = converter_manager.logger.getChild(self.__class__.__name__)

    @property
    def provider(self) -> MusicProvider:
        """Get the main converter manager instance."""
        return self.converter_manager.provider

    @property
    def item_mapper(self) -> ItemMappingConverter:
        """Get the item mapper instance."""
        return self.converter_manager.item_mapper
