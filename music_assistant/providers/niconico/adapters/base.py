"""Base adapter class for NicoNico adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter


class NiconicoBaseAdapter:
    """Base adapter for MusicAssistant bridge classes."""

    def __init__(self, adapter: NicoNicoMusicAssistantAdapter) -> None:
        """Initialize the NiconicoBaseAdapter with a reference to the parent adapter."""
        self.adapter = adapter
