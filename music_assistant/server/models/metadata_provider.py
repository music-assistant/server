"""Model/base for a Metadata Provider implementation."""
from __future__ import annotations

from music_assistant.common.models.media_items import Album, Artist, MediaItemMetadata

from .provider import Provider


class MetadataProvider(Provider):
    """
    Base representation of a Metadata Provider (controller).

    Metadata Provider implementations should inherit from this base model.
    """

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Retrieve metadata for artist on this Metadata provider."""

    async def get_album_metadata(self, album: Album) -> MediaItemMetadata | None:
        """Retrieve metadata for album on this Metadata provider."""
