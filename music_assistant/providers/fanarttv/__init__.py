"""FanartTv Metadata provider."""

import logging
from json.decoder import JSONDecodeError
from typing import Dict, List

import aiohttp
from asyncio_throttle import Throttler
from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.provider import MetadataProvider

# TODO: add support for personal api keys ?
# TODO: Add support for album artwork ?

PROV_ID = "fanarttv"
PROV_NAME = "Fanart.tv"

LOGGER = logging.getLogger(PROV_ID)

CONFIG_ENTRIES = []


async def setup(mass) -> None:
    """Perform async setup of this Plugin/Provider."""
    prov = FanartTvProvider(mass)
    await mass.register_provider(prov)


class FanartTvProvider(MetadataProvider):
    """Fanart.tv metadata provider."""

    def __init__(self, mass):
        """Initialize class."""
        self.mass = mass
        self.throttler = Throttler(rate_limit=1, period=2)

    async def on_start(self) -> bool:
        """
        Handle initialization of the provider based on config.

        Return bool if start was succesfull. Called on startup.
        """
        return True  # we have nothing to initialize

    @property
    def id(self) -> str:
        """Return provider ID for this provider."""
        return PROV_ID

    @property
    def name(self) -> str:
        """Return provider Name for this provider."""
        return PROV_NAME

    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return Config Entries for this provider."""
        return CONFIG_ENTRIES

    async def get_artist_images(self, mb_artist_id: str) -> Dict:
        """Retrieve images by musicbrainz artist id."""
        metadata = {}
        data = await self._get_data("music/%s" % mb_artist_id)
        if data:
            if data.get("hdmusiclogo"):
                metadata["logo"] = data["hdmusiclogo"][0]["url"]
            elif data.get("musiclogo"):
                metadata["logo"] = data["musiclogo"][0]["url"]
            if data.get("artistbackground"):
                count = 0
                for item in data["artistbackground"]:
                    key = "fanart" if count == 0 else "fanart.%s" % count
                    metadata[key] = item["url"]
            if data.get("artistthumb"):
                url = data["artistthumb"][0]["url"]
                if "2a96cbd8b46e442fc41c2b86b821562f" not in url:
                    metadata["image"] = url
            if data.get("musicbanner"):
                metadata["banner"] = data["musicbanner"][0]["url"]
        return metadata

    async def _get_data(self, endpoint, params=None):
        """Get data from api."""
        if params is None:
            params = {}
        url = "http://webservice.fanart.tv/v3/%s" % endpoint
        params["api_key"] = "639191cb0774661597f28a47e7e2bad5"
        async with self.throttler:
            async with self.mass.http_session.get(
                url, params=params, verify_ssl=False
            ) as response:
                try:
                    result = await response.json()
                except (
                    aiohttp.client_exceptions.ContentTypeError,
                    JSONDecodeError,
                ):
                    LOGGER.error("Failed to retrieve %s", endpoint)
                    text_result = await response.text()
                    LOGGER.debug(text_result)
                    return None
                except aiohttp.client_exceptions.ClientConnectorError:
                    LOGGER.error("Failed to retrieve %s", endpoint)
                    return None
                if "error" in result and "limit" in result["error"]:
                    LOGGER.error(result["error"])
                    return None
                return result
