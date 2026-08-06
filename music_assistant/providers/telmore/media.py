"""Media retrieval operations for Telmore Musik."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.yousee.media import YouSeeMediaManager

if TYPE_CHECKING:
    from music_assistant.providers.telmore.provider import TelmoreMusikProvider


class TelmoreMediaManager(YouSeeMediaManager):
    """Handles retrieval of media items from Telmore Musik."""

    def __init__(self, provider: TelmoreMusikProvider):
        """Initialize media retriever."""
        self.provider = provider  # type: ignore[assignment]
        self.api = provider.api
        self.logger = provider.logger
