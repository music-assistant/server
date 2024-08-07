"""Model/base for a Metadata Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.common.models.enums import ProviderFeature

from .provider import Provider

if TYPE_CHECKING:
    from music_assistant.common.models.media_items import Album, Artist, MediaItemMetadata, Track

# ruff: noqa: ARG001, ARG002

DEFAULT_SUPPORTED_FEATURES = (
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
)


class MetadataProvider(Provider):
    """Base representation of a Metadata Provider (controller).

    Metadata Provider implementations should inherit from this base model.
    """

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return DEFAULT_SUPPORTED_FEATURES

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Retrieve metadata for an artist on this Metadata provider."""
        if ProviderFeature.ARTIST_METADATA in self.supported_features:
            raise NotImplementedError

    async def get_album_metadata(self, album: Album) -> MediaItemMetadata | None:
        """Retrieve metadata for an album on this Metadata provider."""
        if ProviderFeature.ALBUM_METADATA in self.supported_features:
            raise NotImplementedError

    async def get_track_metadata(self, track: Track) -> MediaItemMetadata | None:
        """Retrieve metadata for a track on this Metadata provider."""
        if ProviderFeature.TRACK_METADATA in self.supported_features:
            raise NotImplementedError

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        return path
