"""Base service for nicovideo."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant.providers.nicovideo.config import NicovideoConfig
    from music_assistant.providers.nicovideo.converters import NicovideoConverterHub
    from music_assistant.providers.nicovideo.services.hub import NicovideoServiceHub


class NicovideoBaseService:
    """Base service for MusicAssistant integration classes."""

    def __init__(self, service_hub: NicovideoServiceHub) -> None:
        """Initialize the NicovideoBaseService with a reference to the parent service hub."""
        self.service_hub = service_hub
        self.logger = service_hub.logger.getChild(self.__class__.__name__)

    @property
    def nicovideo_config(self) -> NicovideoConfig:
        """Get the config helper instance."""
        return self.service_hub.nicovideo_config

    @property
    def converter_hub(self) -> NicovideoConverterHub:
        """Get the main converter instance."""
        return self.service_hub.converter_hub
