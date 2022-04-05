"""FanartTv Metadata provider."""

from json.decoder import JSONDecodeError
from typing import Dict

import aiohttp
from asyncio_throttle import Throttler
from music_assistant.helpers.typing import MusicAssistant

# TODO: add support for personal api keys ?
# TODO: Add support for album artwork ?


class FanartTv:
    """Fanart.tv metadata provider."""

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.mass = mass
        self.logger = mass.logger.getChild("fanarttv")
        self.throttler = Throttler(rate_limit=1, period=2)

    async def get_artist_images(self, mb_artist_id: str) -> Dict:
        """Retrieve images by musicbrainz artist id."""
        metadata = {}
        data = await self._get_data(f"music/{mb_artist_id}")
        if data:
            if data.get("hdmusiclogo"):
                metadata["logo"] = data["hdmusiclogo"][0]["url"]
            elif data.get("musiclogo"):
                metadata["logo"] = data["musiclogo"][0]["url"]
            if data.get("artistbackground"):
                count = 0
                for item in data["artistbackground"]:
                    key = "fanart" if count == 0 else f"fanart.{count}"
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
        url = f"http://webservice.fanart.tv/v3/{endpoint}"
        params["api_key"] = "639191cb0774661597f28a47e7e2bad5"
        async with self.throttler:
            async with self.mass.http_session.get(
                url, params=params, verify_ssl=False
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
