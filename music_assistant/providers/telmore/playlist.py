"""Telmore Musik playlist manager."""

from typing import TYPE_CHECKING

from music_assistant.providers.yousee.playlist import YouSeePlaylistManager

if TYPE_CHECKING:
    from music_assistant.providers.telmore.provider import TelmoreMusikProvider


class TelmorePlaylistManager(YouSeePlaylistManager):
    """Manages Telmore Musik playlist operations."""

    def __init__(self, provider: TelmoreMusikProvider):
        """Initialize playlist manager."""
        self.provider = provider  # type: ignore[assignment]
        self.api = provider.api
        self.auth = provider.auth  # type: ignore[assignment]
        self.logger = provider.logger
