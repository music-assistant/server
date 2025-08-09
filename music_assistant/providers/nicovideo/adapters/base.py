"""Base adapter for nicovideo."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.nicovideo.converters import (
    NicovideoConverterHub,
)

if TYPE_CHECKING:
    from music_assistant.providers.nicovideo.adapters.hub import NicovideoAdapterHub
    from music_assistant.providers.nicovideo.config import NicovideoConfig


class NicovideoBaseAdapter:
    """Base adapter for MusicAssistant bridge classes."""

    def __init__(self, adapter: NicovideoAdapterHub) -> None:
        """Initialize the NicovideoBaseAdapter with a reference to the parent adapter."""
        self.adapter = adapter
        self.logger = adapter.logger.getChild(self.__class__.__name__)

    @property
    def nicovideo_config(self) -> NicovideoConfig:
        """Get the config helper instance."""
        return self.adapter.nicovideo_config

    @property
    def converter_hub(self) -> NicovideoConverterHub:
        """Get the main converter instance."""
        return self.adapter.converter_hub
