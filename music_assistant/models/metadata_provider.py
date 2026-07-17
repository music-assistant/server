"""Model/base for a Metadata Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .provider import Provider

if TYPE_CHECKING:
    from music_assistant_models.media_items import (
        Album,
        Artist,
        MediaItemMetadata,
        Playlist,
        RecommendationFolder,
        Track,
    )


class MetadataProvider(Provider):
    """
    Base representation of a Metadata Provider (controller).

    Metadata Provider implementations should inherit from this base model.
    """

    @property
    def priority(self) -> int:
        """Priority for this provider (lower = more preferred)."""
        return 50

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

    async def get_similar_tracks(self, track: Track, limit: int = 25) -> list[Track]:
        """
        Retrieve a list of similar tracks for the given track.

        Will only be called if ProviderFeature.SIMILAR_TRACKS is declared.

        :param track: The reference track.
        :param limit: Maximum number of similar tracks to return.
        """
        if ProviderFeature.SIMILAR_TRACKS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_similar_artists(self, artist: Artist, limit: int = 25) -> list[Artist]:
        """
        Retrieve a list of similar artists for the given artist.

        Will only be called if ProviderFeature.SIMILAR_ARTISTS is declared.

        :param artist: The reference artist.
        :param limit: Maximum number of similar artists to return.
        """
        if ProviderFeature.SIMILAR_ARTISTS in self.supported_features:
            raise NotImplementedError
        return []

    async def recommendations(self) -> list[RecommendationFolder]:
        """
        Retrieve a list of recommendation folders from this metadata provider.

        Will only be called if ProviderFeature.RECOMMENDATIONS is declared.

        Overrides may accept an optional ``wanted: set[str] | None = None`` parameter
        to build only the requested rows: the set holds the row item_ids to build;
        None means build all rows.
        """
        if ProviderFeature.RECOMMENDATIONS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_artist_toptracks(self, artist: Artist, limit: int = 25) -> list[Track]:
        """
        Retrieve a list of top tracks for the given artist.

        Will only be called if ProviderFeature.ARTIST_TOPTRACKS is declared.

        :param artist: The reference artist.
        :param limit: Maximum number of top tracks to return.
        """
        if ProviderFeature.ARTIST_TOPTRACKS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_artist_topalbums(self, artist: Artist, limit: int = 25) -> list[Album]:
        """
        Retrieve a list of top albums for the given artist.

        Will only be called if ProviderFeature.ARTIST_TOPALBUMS is declared.

        :param artist: The reference artist.
        :param limit: Maximum number of top albums to return.
        """
        if ProviderFeature.ARTIST_TOPALBUMS in self.supported_features:
            raise NotImplementedError
        return []

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        return path
