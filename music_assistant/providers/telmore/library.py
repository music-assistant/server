"""Library management for Telmore Musik."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.yousee.library import YouSeeLibraryManager

if TYPE_CHECKING:
    from music_assistant.providers.telmore.provider import TelmoreMusikProvider


class TelmoreLibraryManager(YouSeeLibraryManager):
    """Manages Telmore Musik library operations."""

    def __init__(self, provider: TelmoreMusikProvider):
        """Initialize library manager."""
        self.provider = provider  # type: ignore[assignment]
        self.api = provider.api
        self.auth = provider.auth  # type: ignore[assignment]
        self.logger = provider.logger
