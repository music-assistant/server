"""Tune-In musicprovider support for MusicAssistant."""
from __future__ import annotations

from typing import AsyncGenerator, List, Optional

from asyncio_throttle import Throttler

from music_assistant.helpers.cache import use_cache
from music_assistant.helpers.util import create_clean_string
from music_assistant.models.enums import ProviderType
from music_assistant.models.errors import LoginFailed
from music_assistant.models.media_items import (
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemProviderId,
    MediaItemType,
    MediaQuality,
    MediaType,
    Radio,
    StreamDetails,
    StreamType,
)
from music_assistant.models.provider import MusicProvider


class TuneInProvider(MusicProvider):
    """Provider implementation for Tune In."""

    _attr_type = ProviderType.TUNEIN
    _attr_name = "Tune-in Radio"
    _attr_supported_mediatypes = [MediaType.RADIO]
    _throttler = Throttler(rate_limit=1, period=1)

    async def setup(self) -> bool:
        """Handle async initialization of the provider."""
        if not self.config.enabled:
            return False
        if not self.config.username:
            raise LoginFailed("Username is invalid")
        return True

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> List[MediaItemType]:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        # pylint: disable=no-self-use
        # TODO: search for radio stations
        return []

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""

        async def parse_items(
            items: List[dict], folder: str = None
        ) -> AsyncGenerator[Radio, None]:
            for item in items:
                item_type = item.get("type", "")
                if item_type == "audio":
                    if "preset_id" not in item:
                        continue
                    # each radio station can have multiple streams add each one as different quality
                    stream_info = await self.__get_data(
                        "Tune.ashx", id=item["preset_id"]
                    )
                    for stream in stream_info["body"]:
                        yield await self._parse_radio(item, stream, folder)
                elif item_type == "link":
                    # stations are in sublevel (new style)
                    if sublevel := await self.__get_data(item["URL"], render="json"):
                        async for subitem in parse_items(
                            sublevel["body"], item["text"]
                        ):
                            yield subitem
                elif item.get("children"):
                    # stations are in sublevel (old style ?)
                    async for subitem in parse_items(item["children"], item["text"]):
                        yield subitem

        data = await self.__get_data("Browse.ashx", c="presets")
        if data and "body" in data:
            async for item in parse_items(data["body"]):
                yield item

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get radio station details."""
        prov_radio_id, media_type = prov_radio_id.split("--", 1)
        params = {"c": "composite", "detail": "listing", "id": prov_radio_id}
        result = await self.__get_data("Describe.ashx", **params)
        if result and result.get("body") and result["body"][0].get("children"):
            item = result["body"][0]["children"][0]
            stream_info = await self.__get_data("Tune.ashx", id=prov_radio_id)
            for stream in stream_info["body"]:
                if stream["media_type"] != media_type:
                    continue
                return await self._parse_radio(item, stream)
        return None

    async def _parse_radio(
        self, details: dict, stream: dict, folder: Optional[str] = None
    ) -> Radio:
        """Parse Radio object from json obj returned from api."""
        if "name" in details:
            name = details["name"]
        else:
            # parse name from text attr
            name = details["text"]
            if " | " in name:
                name = name.split(" | ")[1]
            name = name.split(" (")[0]
        item_id = f'{details["preset_id"]}--{stream["media_type"]}'
        radio = Radio(item_id=item_id, provider=self.type, name=name)
        if stream["media_type"] == "aac":
            quality = MediaQuality.LOSSY_AAC
        elif stream["media_type"] == "ogg":
            quality = MediaQuality.LOSSY_OGG
        else:
            quality = MediaQuality.LOSSY_MP3
        radio.add_provider_id(
            MediaItemProviderId(
                item_id=item_id,
                prov_type=self.type,
                prov_id=self.id,
                quality=quality,
                details=stream["url"],
            )
        )
        # preset number is used for sorting (not present at stream time)
        preset_number = details.get("preset_number")
        if preset_number and folder:
            radio.sort_name = f'{folder}-{details["preset_number"]}'
        elif preset_number:
            radio.sort_name = details["preset_number"]
        radio.sort_name += create_clean_string(name)
        if "text" in details:
            radio.metadata.description = details["text"]
        # images
        if img := details.get("image"):
            radio.metadata.images = [MediaItemImage(ImageType.THUMB, img)]
        if img := details.get("logo"):
            radio.metadata.images = [MediaItemImage(ImageType.LOGO, img)]
        return radio

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a radio station."""
        item_id, media_type = item_id.split("--", 1)
        stream_info = await self.__get_data("Tune.ashx", id=item_id)
        for stream in stream_info["body"]:
            if stream["media_type"] == media_type:
                return StreamDetails(
                    type=StreamType.URL,
                    item_id=item_id,
                    provider=self.type,
                    path=stream["url"],
                    content_type=ContentType(stream["media_type"]),
                    sample_rate=44100,
                    bit_depth=16,
                    media_type=MediaType.RADIO,
                    details=stream,
                )
        return None

    @use_cache(3600 * 2)
    async def __get_data(self, endpoint: str, **kwargs):
        """Get data from api."""
        if endpoint.startswith("http"):
            url = endpoint
        else:
            url = f"https://opml.radiotime.com/{endpoint}"
            kwargs["formats"] = "ogg,aac,wma,mp3"
            kwargs["username"] = self.config.username
            kwargs["partnerId"] = "1"
            kwargs["render"] = "json"
        async with self._throttler:
            async with self.mass.http_session.get(
                url, params=kwargs, verify_ssl=False
            ) as response:
                result = await response.json()
                if not result or "error" in result:
                    self.logger.error(url)
                    self.logger.error(kwargs)
                    result = None
                return result
