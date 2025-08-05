"""Base adapter class for nicovideo adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.nicovideo.config import NicovideoConfig

if TYPE_CHECKING:
    from music_assistant.providers.nicovideo.adapter import NicovideoMusicAssistantAdapter


class NicovideoBaseAdapter:
    """Base adapter for MusicAssistant bridge classes."""

    def __init__(self, adapter: NicovideoMusicAssistantAdapter) -> None:
        """Initialize the NicovideoBaseAdapter with a reference to the parent adapter."""
        self.adapter = adapter

    @property
    def nicovideo_config(self) -> NicovideoConfig:
        """Get the config helper instance."""
        return self.adapter.nicovideo_config
