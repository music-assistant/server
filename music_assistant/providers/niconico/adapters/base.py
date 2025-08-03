"""Base adapter class for NicoNico adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.niconico.config import NiconicoConfig

if TYPE_CHECKING:
    from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter


class NiconicoBaseAdapter:
    """Base adapter for MusicAssistant bridge classes."""

    def __init__(self, adapter: NicoNicoMusicAssistantAdapter) -> None:
        """Initialize the NiconicoBaseAdapter with a reference to the parent adapter."""
        self.adapter = adapter

    @property
    def niconico_config(self) -> NiconicoConfig:
        """Get the config helper instance."""
        return self.adapter.niconico_config
