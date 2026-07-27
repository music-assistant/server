"""Fanart.tv Metadata provider for Music Assistant."""

from __future__ import annotations

from json import JSONDecodeError
from typing import TYPE_CHECKING, Any, cast

import aiohttp.client_exceptions
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ExternalID, ImageType, ProviderFeature
from music_assistant_models.media_items import MediaItemImage, MediaItemMetadata, UniqueList

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.app_vars import app_var
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import Album, Artist
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ALBUM_METADATA,
}

CONF_ENABLE_ARTIST_IMAGES = "enable_artist_images"
CONF_ENABLE_ALBUM_IMAGES = "enable_album_images"
CONF_CLIENT_KEY = "client_key"

ARTIST_IMG_MAPPING = {
    "artistthumb": ImageType.THUMB,
    "hdmusiclogo": ImageType.LOGO,
    "musicbanner": ImageType.BANNER,
    "artistbackground": ImageType.FANART,
}

ALBUM_IMG_MAPPING = {
    "albumcover": ImageType.THUMB,
    "cdart": ImageType.DISCART,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return FanartTvMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)


class FanartTvMetadataProvider(MetadataProvider):
    """Fanart.tv Metadata provider."""

    throttler: Throttler

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return (
            ConfigEntry(
                key=CONF_ENABLE_ARTIST_IMAGES,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_ENABLE_ALBUM_IMAGES,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_CLIENT_KEY,
                type=ConfigEntryType.SECURE_STRING,
                required=False,
            ),
        )

    @property
    def priority(self) -> int:
        """Priority for this provider (lower = more preferred)."""
        return 10

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.cache = self.mass.cache
        if self.config.get_value(CONF_CLIENT_KEY):
            # loosen the throttler when a personal client key is used
            self.throttler = Throttler(rate_limit=1, period=1)
        else:
            self.throttler = Throttler(rate_limit=1, period=30)

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Retrieve metadata for artist on fanart.tv."""
        if not artist.mbid:
            return None
        if not self.config.get_value(CONF_ENABLE_ARTIST_IMAGES):
            return None
        self.logger.debug("Fetching metadata for Artist %s on Fanart.tv", artist.name)
        if data := await self._get_data(f"music/{artist.mbid}"):
            metadata = MediaItemMetadata()
            for key, img_type in ARTIST_IMG_MAPPING.items():
                items = data.get(key)
                if not items:
                    continue
                for item in items:
                    metadata.add_image(
                        MediaItemImage(
                            type=img_type,
                            path=item["url"],
                            provider=self.domain,
                            remotely_accessible=True,
                        )
                    )
            if metadata.images:
                self.logger.debug(
                    "Found %d image(s) for Artist %s on Fanart.tv",
                    len(metadata.images),
                    artist.name,
                )
            else:
                self.logger.debug(
                    "No images found for Artist %s on Fanart.tv (available keys: %s)",
                    artist.name,
                    list(data.keys()),
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
                if album_data := data["albums"].get(mbid):
                    metadata = MediaItemMetadata()
                    metadata.images = UniqueList()
                    for key, img_type in ALBUM_IMG_MAPPING.items():
                        items = album_data.get(key)
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
                    if metadata.images:
                        self.logger.debug(
                            "Found %d image(s) for Album %s on Fanart.tv",
                            len(metadata.images),
                            album.name,
                        )
                    else:
                        self.logger.debug(
                            "No images found for Album %s on Fanart.tv (available keys: %s)",
                            album.name,
                            list(album_data.keys()),
                        )
                    return metadata
        return None

    # None here only signals a failed or rate-limited request, so don't cache it
    @use_cache(86400 * 60, cache_none=False)  # Cache for 60 days
    async def _get_data(self, endpoint: str, **kwargs: str) -> dict[str, Any] | None:
        """Get data from api."""
        url = f"http://webservice.fanart.tv/v3/{endpoint}"
        headers = {
            "api-key": app_var("fanarttv_api_key"),
        }
        if client_key := self.config.get_value(CONF_CLIENT_KEY):
            headers["client_key"] = str(client_key)
        async with (
            self.throttler,
            self.mass.http_session_no_ssl.get(
                url, params=kwargs, headers=headers, ssl=False
            ) as response,
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
            return cast("dict[str, Any]", result)
