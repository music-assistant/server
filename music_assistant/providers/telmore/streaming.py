"""Streaming operations for Telmore Musik."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.yousee.streaming import YouSeeStreamingManager

if TYPE_CHECKING:
    from music_assistant.providers.telmore.provider import TelmoreMusikProvider


class TelmoreStreamingManager(YouSeeStreamingManager):
    """Manages Telmore Musik streaming operations."""

    def __init__(self, provider: TelmoreMusikProvider):
        """Initialize streaming manager."""
        self.provider = provider  # type: ignore[assignment]
        self.api = provider.api
        self.mass = provider.mass
        self.logger = provider.logger
