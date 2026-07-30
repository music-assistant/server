"""Cover Art Archive Metadata provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp
from music_assistant_models.enums import ExternalID, ImageType, ProviderFeature
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from music_assistant_models.media_items import Album, MediaItemImage, MediaItemMetadata, UniqueList

from music_assistant.controllers.cache import use_cache
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.ALBUM_METADATA,
}

CAA_BASE_URL = "https://coverartarchive.org"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return CoverArtArchiveMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)


class CoverArtArchiveMetadataProvider(MetadataProvider):
    """
    Cover Art Archive Metadata provider.

    Fetches album artwork from the Cover Art Archive using MusicBrainz release group IDs.
    """

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return ()

    @property
    def priority(self) -> int:
        """Priority for this provider (lower = more preferred)."""
        return 40

    async def get_album_metadata(self, album: Album) -> MediaItemMetadata | None:
        """
        Retrieve metadata for an album.

        :param album: Album to retrieve metadata for.
        """
        mbid = album.get_external_id(ExternalID.MB_RELEASEGROUP)
        if not mbid:
            return None

        image_url = await self._get_cover_art_url(mbid)
        if not image_url:
            return None

        self.logger.debug("Found cover art for album %s on Cover Art Archive", album.name)
        return MediaItemMetadata(
            images=UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.domain,
                        remotely_accessible=True,
                    )
                ]
            )
        )

    @use_cache(86400 * 30)
    async def _get_cover_art_url(self, release_group_id: str) -> str | None:
        """
        Retrieve cover art URL for a release group.

        :param release_group_id: MusicBrainz release group ID.
        """
        # Try 1200px first, fall back to 500px
        try:
            for size in ("front-1200", "front-500"):
                url = f"{CAA_BASE_URL}/release-group/{release_group_id}/{size}"
                async with self.mass.http_session.head(url, allow_redirects=True) as response:
                    if response.status == 200:
                        return str(response.url)
                    if response.status != 404:
                        response.raise_for_status()
        except (aiohttp.ClientError, TimeoutError) as err:
            # a non-404 status (5xx, 429, ...) or network failure is transient — surface it as
            # ResourceTemporarilyUnavailable so callers degrade instead of caching "no cover art"
            raise ResourceTemporarilyUnavailable("Cover Art Archive request failed") from err
        # a 404 for both sizes means this release group genuinely has no cover art
        return None
