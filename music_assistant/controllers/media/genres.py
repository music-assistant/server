"""Manage MediaItems of type Genre - Stub Implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Genre, Track

from .base import MediaControllerBase

# NOTE: Genre support is not yet fully implemented.
# This is a stub controller to prevent errors when Genre MediaType is encountered.

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


class GenreController(MediaControllerBase[Genre]):
    """Stub controller for Genre MediaType - not yet fully implemented."""

    db_table = "genres"  # Not actually used yet
    media_type = MediaType.GENRE
    item_cls = Genre

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)

    async def _add_library_item(self, item: Genre, overwrite_existing: bool = False) -> int:
        """Add a new item record to the database - stub implementation."""
        raise NotImplementedError("Genre support is not yet implemented")

    async def _update_library_item(
        self, item_id: str | int, update: Genre, overwrite: bool = False
    ) -> None:
        """Update existing record in the database - stub implementation."""
        raise NotImplementedError("Genre support is not yet implemented")

    async def search(
        self,
        query: str,
        provider_instance_id_or_domain: str | None = None,
        limit: int = 25,
    ) -> list[Genre]:
        """Search for genres - stub implementation."""
        return []

    async def radio_mode_base_tracks(
        self,
        item: Genre,
        preferred_provider_instances: list[str] | None = None,
    ) -> list[Track]:
        """
        Get the list of base tracks from the controller - stub implementation.

        :param item: The Genre to get base tracks for.
        :param preferred_provider_instances: List of preferred provider instance IDs to use.
        """
        raise NotImplementedError("Genre support is not yet implemented")

    async def match_providers(self, db_item: Genre) -> None:
        """Try to find match on all providers - stub implementation."""
        raise NotImplementedError("Genre support is not yet implemented")
