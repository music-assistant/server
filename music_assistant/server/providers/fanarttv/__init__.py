"""Fanart.tv Metadata provider for Music Assistant."""

from __future__ import annotations

from json import JSONDecodeError
from typing import TYPE_CHECKING

import aiohttp.client_exceptions

from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import ConfigEntryType, ExternalID, ProviderFeature
from music_assistant.common.models.media_items import ImageType, MediaItemImage, MediaItemMetadata
from music_assistant.server.controllers.cache import use_cache
from music_assistant.server.helpers.app_vars import app_var  # pylint: disable=no-name-in-module
from music_assistant.server.helpers.throttle_retry import Throttler
from music_assistant.server.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant.common.models.media_items import Album, Artist
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

SUPPORTED_FEATURES = (
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ALBUM_METADATA,
)

CONF_ENABLE_ARTIST_IMAGES = "enable_artist_images"
CONF_ENABLE_ALBUM_IMAGES = "enable_album_images"
CONF_CLIENT_KEY = "client_key"

IMG_MAPPING = {
    "artistthumb": ImageType.THUMB,
    "hdmusiclogo": ImageType.LOGO,
    "musicbanner": ImageType.BANNER,
    "artistbackground": ImageType.FANART,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return FanartTvMetadataProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_ENABLE_ARTIST_IMAGES,
            type=ConfigEntryType.BOOLEAN,
            label="Enable retrieval of artist images.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_ENABLE_ALBUM_IMAGES,
            type=ConfigEntryType.BOOLEAN,
            label="Enable retrieval of album image(s).",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_CLIENT_KEY,
            type=ConfigEntryType.SECURE_STRING,
            label="VIP Member Personal API Key (optional)",
            description="Support this metadata provider by becoming a VIP Member, "
            "resulting in higher rate limits and faster response times among other benefits. "
            "See https://wiki.fanart.tv/General/personal%20api/ for more information.",
            required=False,
        ),
    )


class FanartTvMetadataProvider(MetadataProvider):
    """Fanart.tv Metadata provider."""

    throttler: Throttler

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.cache = self.mass.cache
        if self.config.get_value(CONF_CLIENT_KEY):
            # loosen the throttler when a personal client key is used
            self.throttler = Throttler(rate_limit=1, period=1)
        else:
            self.throttler = Throttler(rate_limit=1, period=30)

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Retrieve metadata for artist on fanart.tv."""
        if not artist.mbid:
            return None
        if not self.config.get_value(CONF_ENABLE_ARTIST_IMAGES):
            return None
        self.logger.debug("Fetching metadata for Artist %s on Fanart.tv", artist.name)
        if data := await self._get_data(f"music/{artist.mbid}"):
            metadata = MediaItemMetadata()
            metadata.images = []
            for key, img_type in IMG_MAPPING.items():
                items = data.get(key)
                if not items:
                    continue
                for item in items:
                    metadata.images.append(
                        MediaItemImage(
                            type=img_type,
                            path=item["url"],
                            provider=self.domain,
                            remotely_accessible=True,
                        )
                    )
            return metadata
        return None

    async def get_album_metadata(self, album: Album) -> MediaItemMetadata | None:
        """Retrieve metadata for album on fanart.tv."""
        if (mbid := album.get_external_id(ExternalID.MB_RELEASEGROUP)) is None:
            return None
        if not self.config.get_value(CONF_ENABLE_ALBUM_IMAGES):
            return None
        self.logger.debug("Fetching metadata for Album %s on Fanart.tv", album.name)
        if data := await self._get_data(f"music/albums/{mbid}"):
            if data and data.get("albums"):
                data = data["albums"][mbid]
                metadata = MediaItemMetadata()
                metadata.images = []
                for key, img_type in IMG_MAPPING.items():
                    items = data.get(key)
                    if not items:
                        continue
                    for item in items:
                        metadata.images.append(
                            MediaItemImage(
                                type=img_type,
                                path=item["url"],
                                provider=self.domain,
                                remotely_accessible=True,
                            )
                        )
                return metadata
        return None

    @use_cache(86400 * 30)
    async def _get_data(self, endpoint, **kwargs) -> dict | None:
        """Get data from api."""
        url = f"http://webservice.fanart.tv/v3/{endpoint}"
        headers = {
            "api-key": app_var(4),
        }
        if client_key := self.config.get_value(CONF_CLIENT_KEY):
            headers["client_key"] = client_key
        async with (
            self.throttler,
            self.mass.http_session.get(url, params=kwargs, headers=headers, ssl=False) as response,
        ):
            try:
                result = await response.json()
            except (
                aiohttp.client_exceptions.ContentTypeError,
                JSONDecodeError,
            ):
                self.logger.error("Failed to retrieve %s", endpoint)
                text_result = await response.text()
                self.logger.debug(text_result)
                return None
            except (
                aiohttp.client_exceptions.ClientConnectorError,
                aiohttp.client_exceptions.ServerDisconnectedError,
            ):
                self.logger.warning("Failed to retrieve %s", endpoint)
                return None
            if "error" in result and "limit" in result["error"]:
                self.logger.warning(result["error"])
                return None
            return result
