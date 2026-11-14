"""Cover Art Archive Metadata provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp
from music_assistant_models.enums import ExternalID, ImageType, ProviderFeature
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from music_assistant_models.media_items import Album, MediaItemImage, MediaItemMetadata, UniqueList

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
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
REDIRECT_STATUSES = (301, 302, 307, 308)


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

    # the archive allows a client one request per second
    # a thumbnail request must not hang on an archive outage: two attempts, then a 404
    throttler = ThrottlerManager(rate_limit=1, period=1, retry_attempts=2, initial_backoff=2)

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

        image_url = await self.get_release_group_cover_url(mbid)
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

    async def resolve_image(self, path: str) -> str | None:
        """
        Resolve the image of a release group, given as the image path, to its front cover URL.

        :param path: MusicBrainz release group ID, or a cover URL that was already resolved.
        :return: The cover URL, or None when the archive has no cover for the release group.
        """
        # album metadata stores the cover URL itself as the path, it needs no lookup
        if path.startswith(("http://", "https://")):
            return path
        return await self.get_release_group_cover_url(path)

    @use_cache(86400 * 30)
    async def get_release_group_cover_url(self, release_group_id: str) -> str | None:
        """
        Return the URL of a release group's front cover, or None if the archive has none.

        :param release_group_id: MusicBrainz release group ID.
        :raises RetriesExhausted: The archive could not be asked, even after retrying.
        """
        # the archive answers 404 only when the release group has no front cover at all, for
        # every size alike, so one request for the 1200px size is enough
        return await self._head_cover(f"{CAA_BASE_URL}/release-group/{release_group_id}/front-1200")

    @throttle_with_retries
    async def _head_cover(self, url: str) -> str | None:
        """Return the URL one cover request resolves to, or None when the archive has no such cover."""
        try:
            # the archive answers with a redirect to the image file on archive.org, which is
            # often slow or down; the redirect alone tells the cover exists, so don't follow it
            async with self.mass.http_session.head(url, allow_redirects=False) as response:
                if response.status in REDIRECT_STATUSES and (
                    location := response.headers.get("Location")
                ):
                    return location
                if response.status == 200:
                    return str(response.url)
                if response.status == 404:
                    return None
        except (aiohttp.ClientError, TimeoutError) as err:
            # a network failure is transient — surface it as ResourceTemporarilyUnavailable
            # so it is retried instead of cached as "no cover art"
            raise ResourceTemporarilyUnavailable("Cover Art Archive request failed") from err
        # any other status (5xx, 429, a redirect without a location, ...) is no answer
        # about the cover either, so it is just as transient
        raise ResourceTemporarilyUnavailable(
            f"Cover Art Archive request failed with status {response.status}"
        )
