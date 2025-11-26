"""Base service for nicovideo."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from niconico import NicoNico

    from music_assistant.providers.nicovideo.config import NicovideoConfig
    from music_assistant.providers.nicovideo.converters import NicovideoConverterManager
    from music_assistant.providers.nicovideo.services.manager import NicovideoServiceManager


class NicovideoBaseService:
    """Base service for MusicAssistant integration classes."""

    def __init__(self, service_manager: NicovideoServiceManager) -> None:
        """Initialize the NicovideoBaseService with a reference to the parent service manager."""
        self.service_manager = service_manager
        self.logger = service_manager.logger.getChild(self.__class__.__name__)

    @property
    def nicovideo_config(self) -> NicovideoConfig:
        """Get the config helper instance."""
        return self.service_manager.nicovideo_config

    @property
    def converter_manager(self) -> NicovideoConverterManager:
        """Get the main converter instance."""
        return self.service_manager.converter_manager

    @property
    def niconico_py_client(self) -> NicoNico:
        """Get the niconico.py client instance."""
        return self.service_manager.niconico_py_client
