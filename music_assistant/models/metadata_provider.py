"""Model/base for a Metadata Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .provider import Provider

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist, MediaItemMetadata, Track


class MetadataProvider(Provider):
    """Base representation of a Metadata Provider (controller).

    Metadata Provider implementations should inherit from this base model.
    """

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Retrieve metadata for an artist on this Metadata provider."""
        if ProviderFeature.ARTIST_METADATA in self.supported_features:
            raise NotImplementedError
        return None

    async def get_album_metadata(self, album: Album) -> MediaItemMetadata | None:
        """Retrieve metadata for an album on this Metadata provider."""
        if ProviderFeature.ALBUM_METADATA in self.supported_features:
            raise NotImplementedError
        return None

    async def get_track_metadata(self, track: Track) -> MediaItemMetadata | None:
        """Retrieve metadata for a track on this Metadata provider."""
        if ProviderFeature.TRACK_METADATA in self.supported_features:
            raise NotImplementedError
        return None

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        return path
