"""Model/base for a Metadata Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .media_capabilities import MusicDiscoveryMixin, RecommendationsMixin
from .provider import Provider

if TYPE_CHECKING:
    from music_assistant_models.media_items import (
        Album,
        Artist,
        MediaItemMetadata,
        Playlist,
        Track,
    )


class MetadataProvider(RecommendationsMixin, MusicDiscoveryMixin, Provider):
    """
    Base representation of a Metadata Provider (controller).

    Metadata Provider implementations should inherit from this base model.
    """

    @property
    def priority(self) -> int:
        """Priority for this provider (lower = more preferred)."""
        return 50

    @property
    def rate_limited(self) -> bool:
        """Whether the provider currently holds its requests back because of a rate limit."""
        return False

    @property
    def is_local(self) -> bool:
        """
        Whether this provider reads metadata from local storage instead of an online service.

        A local provider is also asked for metadata when online metadata is disabled and for
        artists without a MusicBrainz id, and its artist bio is considered first.
        """
        return False

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

    async def get_playlist_metadata(self, playlist: Playlist) -> MediaItemMetadata | None:
        """Retrieve metadata for a playlist on this Metadata provider."""
        if ProviderFeature.PLAYLIST_METADATA in self.supported_features:
            raise NotImplementedError
        return None

    async def save_artist_metadata(self, artist: Artist) -> None:
        """
        Store the artist's metadata, merged from all providers, before it is saved to the library.

        Called on every artist metadata refresh; the provider may also update the given artist.

        :param artist: The library artist with its merged metadata.
        """
