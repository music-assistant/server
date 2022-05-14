"""FanartTv Metadata provider."""
from __future__ import annotations

from json.decoder import JSONDecodeError
from typing import TYPE_CHECKING, Optional

import aiohttp
from asyncio_throttle import Throttler

from music_assistant.helpers.app_vars import (  # pylint: disable=no-name-in-module
    app_var,
)
from music_assistant.helpers.cache import use_cache
from music_assistant.models.media_items import (
    Album,
    Artist,
    ImageType,
    MediaItemImage,
    MediaItemMetadata,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# TODO: add support for personal api keys ?


IMG_MAPPING = {
    "artistthumb": ImageType.THUMB,
    "hdmusiclogo": ImageType.LOGO,
    "musicbanner": ImageType.BANNER,
    "artistbackground": ImageType.FANART,
}


class FanartTv:
    """Fanart.tv metadata provider."""

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.mass = mass
        self.cache = mass.cache
        self.logger = mass.logger.getChild("fanarttv")
        self.throttler = Throttler(rate_limit=2, period=1)

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Retrieve metadata for artist on fanart.tv."""
        if not artist.musicbrainz_id:
            return
        self.logger.debug("Fetching metadata for Artist %s on Fanart.tv", artist.name)
        if data := await self._get_data(f"music/{artist.musicbrainz_id}"):
            metadata = MediaItemMetadata()
            metadata.images = []
            for key, img_type in IMG_MAPPING.items():
                items = data.get(key)
                if not items:
                    continue
                for item in items:
                    metadata.images.append(MediaItemImage(img_type, item["url"]))
            return metadata
        return None

    async def get_album_metadata(self, album: Album) -> MediaItemMetadata | None:
        """Retrieve metadata for album on fanart.tv."""
        if not album.musicbrainz_id:
            return
        self.logger.debug("Fetching metadata for Album %s on Fanart.tv", album.name)
        if data := await self._get_data(f"music/albums/{album.musicbrainz_id}"):
            if data and data.get("albums"):
                data = data["albums"][album.musicbrainz_id]
                metadata = MediaItemMetadata()
                metadata.images = []
                for key, img_type in IMG_MAPPING.items():
                    items = data.get(key)
                    if not items:
                        continue
                    for item in items:
                        metadata.images.append(MediaItemImage(img_type, item["url"]))
                return metadata
        return None

    @use_cache(86400 * 14)
    async def _get_data(self, endpoint, **kwargs) -> Optional[dict]:
        """Get data from api."""
        url = f"http://webservice.fanart.tv/v3/{endpoint}"
        kwargs["api_key"] = app_var(4)
        async with self.throttler:
            async with self.mass.http_session.get(
                url, params=kwargs, verify_ssl=False
            ) as response:
                try:
                    result = await response.json()
                except (
                    aiohttp.ContentTypeError,
                    JSONDecodeError,
                ):
                    self.logger.error("Failed to retrieve %s", endpoint)
                    text_result = await response.text()
                    self.logger.debug(text_result)
                    return None
                except aiohttp.ClientConnectorError:
                    self.logger.error("Failed to retrieve %s", endpoint)
                    return None
                if "error" in result and "limit" in result["error"]:
                    self.logger.error(result["error"])
                    return None
                return result
