"""Tune-In musicprovider support for MusicAssistant."""
from __future__ import annotations

from typing import List, Optional

from asyncio_throttle import Throttler

from music_assistant.helpers.cache import use_cache
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

    def __init__(self, username: Optional[str]) -> None:
        """Initialize the provider."""
        self._attr_id = "tunein"
        self._attr_name = "Tune-in Radio"
        self._attr_supported_mediatypes = [MediaType.RADIO]
        self._username = username
        self._throttler = Throttler(rate_limit=1, period=1)

    async def setup(self) -> None:
        """Handle async initialization of the provider."""
        # we have nothing to setup

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> List[MediaItemType]:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        result = []
        # TODO: search for radio stations
        return result

    async def get_library_radios(self) -> List[Radio]:
        """Retrieve library/subscribed radio stations from the provider."""
        result = []
        radios = await self._get_data("Browse.ashx", c="presets")
        if radios and "body" in radios:
            for radio in radios["body"]:
                if radio.get("type", "") != "audio":
                    continue
                # each radio station can have multiple streams add each one as different quality
                stream_info = await self._get_data("Tune.ashx", id=radio["preset_id"])
                for stream in stream_info["body"]:
                    result.append(await self._parse_radio(radio, stream))
        return result

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get radio station details."""
        prov_radio_id, media_type = prov_radio_id.split("--", 1)
        params = {"c": "composite", "detail": "listing", "id": prov_radio_id}
        result = await self._get_data("Describe.ashx", **params)
        if result and result.get("body") and result["body"][0].get("children"):
            item = result["body"][0]["children"][0]
            stream_info = await self._get_data("Tune.ashx", id=prov_radio_id)
            for stream in stream_info["body"]:
                if stream["media_type"] != media_type:
                    continue
                return await self._parse_radio(item, stream)
        return None

    async def _parse_radio(self, details: dict, stream: dict) -> Radio:
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
        radio = Radio(item_id=item_id, provider=self.id, name=name)
        if stream["media_type"] == "aac":
            quality = MediaQuality.LOSSY_AAC
        elif stream["media_type"] == "ogg":
            quality = MediaQuality.LOSSY_OGG
        else:
            quality = MediaQuality.LOSSY_MP3
        radio.add_provider_id(
            MediaItemProviderId(
                provider=self.id,
                item_id=item_id,
                quality=quality,
                details=stream["url"],
            )
        )
        # images
        if img := details.get("image"):
            radio.metadata.images = {MediaItemImage(ImageType.THUMB, img)}
        if img := details.get("logo"):
            radio.metadata.images = {MediaItemImage(ImageType.LOGO, img)}
        return radio

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a radio station."""
        item_id, media_type = item_id.split("--", 1)
        stream_info = await self._get_data("Tune.ashx", id=item_id)
        for stream in stream_info["body"]:
            if stream["media_type"] == media_type:
                return StreamDetails(
                    type=StreamType.URL,
                    item_id=item_id,
                    provider=self.id,
                    path=stream["url"],
                    content_type=ContentType(stream["media_type"]),
                    sample_rate=44100,
                    bit_depth=16,
                    media_type=MediaType.RADIO,
                    details=stream,
                )
        return None

    @use_cache(3600 * 2)
    async def _get_data(self, endpoint, **kwargs):
        """Get data from api."""
        url = f"https://opml.radiotime.com/{endpoint}"
        kwargs["render"] = "json"
        kwargs["formats"] = "ogg,aac,wma,mp3"
        kwargs["username"] = self._username
        kwargs["partnerId"] = "1"
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
