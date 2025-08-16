"""
Manager class for nicovideo converters.

Converters Layer: Data transformation
- Converts niconico.py objects to Music Assistant models
- Handles metadata mapping and normalization
- Manages item relationships and cross-references
- Provides consistent data format for provider mixins
"""

from __future__ import annotations

from logging import Logger
from typing import TYPE_CHECKING

from music_assistant.providers.nicovideo.converters.album import NicovideoAlbumConverter
from music_assistant.providers.nicovideo.converters.artist import NicovideoArtistConverter
from music_assistant.providers.nicovideo.converters.helper import NicovideoConverterHelper
from music_assistant.providers.nicovideo.converters.playlist import (
    NicovideoPlaylistConverter,
)
from music_assistant.providers.nicovideo.converters.stream import NicovideoStreamConverter
from music_assistant.providers.nicovideo.converters.track import NicovideoTrackConverter

if TYPE_CHECKING:
    from music_assistant.models.music_provider import MusicProvider


class NicovideoConverterManager:
    """Central manager for all nicovideo converters to Music Assistant media items."""

    def __init__(self, provider: MusicProvider, logger: Logger) -> None:
        """Initialize with provider and create specialized converters."""
        self.provider = provider
        self.logger = logger
        self.helper = NicovideoConverterHelper(self)

        # Initialize specialized converters
        self.track = NicovideoTrackConverter(self)
        self.album = NicovideoAlbumConverter(self)
        self.playlist = NicovideoPlaylistConverter(self)
        self.artist = NicovideoArtistConverter(self)
        self.stream = NicovideoStreamConverter(self)
