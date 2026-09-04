"""iTunes Artwork Metadata provider for Music Assistant."""

from __future__ import annotations

from json import JSONDecodeError
from typing import TYPE_CHECKING

import aiohttp
from music_assistant_models.enums import ExternalID, ImageType, ProviderFeature
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from music_assistant_models.media_items import MediaItemImage, MediaItemMetadata, UniqueList

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.external_ids import barcode_to_upc
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.media_items import Album
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.ALBUM_METADATA,
}

ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return ITunesArtworkMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)


class ITunesArtworkMetadataProvider(MetadataProvider):
    """
    iTunes Artwork Metadata provider.

    Fetches high-resolution album artwork from the iTunes catalog using UPC barcode lookup.
    """

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return ()

    @property
    def priority(self) -> int:
        """Priority for this provider (lower = more preferred)."""
        return 30

    async def get_album_metadata(self, album: Album) -> MediaItemMetadata | None:
        """
        Retrieve metadata for an album.

        :param album: Album to retrieve metadata for.
        """
        barcode = album.get_external_id(ExternalID.BARCODE)
        if not barcode:
            self.logger.debug(
                "No barcode available for album %s, skipping iTunes lookup", album.name
            )
            return None

        artwork_url = await self._get_artwork_url(barcode)
        if not artwork_url:
            return None

        self.logger.debug("Found artwork for album %s on iTunes", album.name)
        return MediaItemMetadata(
            images=UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=artwork_url,
                        provider=self.domain,
                        remotely_accessible=True,
                    )
                ]
            )
        )

    @use_cache(86400 * 30)
    async def _get_artwork_url(self, barcode: str) -> str | None:
        """
        Look up album artwork URL from iTunes by UPC barcode.

        :param barcode: UPC/EAN barcode for the album.
        """
        upc = barcode_to_upc(barcode)
        try:
            async with self.mass.http_session.get(
                ITUNES_LOOKUP_URL, params={"upc": upc}
            ) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, JSONDecodeError) as err:
            # non-2xx / network / parse failure is transient — surface it as
            # ResourceTemporarilyUnavailable so callers degrade instead of caching "no artwork"
            raise ResourceTemporarilyUnavailable("iTunes request failed") from err

        if not data.get("resultCount"):
            self.logger.debug("No results from iTunes for barcode %s", barcode)
            return None

        result = data["results"][0]
        artwork_url = result.get("artworkUrl100")
        if not artwork_url:
            self.logger.debug("No artwork URL in iTunes result for barcode %s", barcode)
            return None

        # Replace 100x100 with high-resolution 1500x1500
        return str(artwork_url).replace("100x100bb", "1500x1500bb")
