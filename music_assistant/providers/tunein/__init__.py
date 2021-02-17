"""Tune-In musicprovider support for MusicAssistant."""
import logging
from typing import List, Optional

from asyncio_throttle import Throttler
from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.models.media_types import (
    MediaItemProviderId,
    MediaType,
    Radio,
    SearchResult,
    TrackQuality,
)
from music_assistant.models.provider import MusicProvider
from music_assistant.models.streamdetails import ContentType, StreamDetails, StreamType

PROV_ID = "tunein"
PROV_NAME = "TuneIn Radio"
LOGGER = logging.getLogger(PROV_ID)

CONFIG_ENTRIES = [
    ConfigEntry(
        entry_key=CONF_USERNAME,
        entry_type=ConfigEntryType.STRING,
        description=CONF_USERNAME,
    ),
    ConfigEntry(
        entry_key=CONF_PASSWORD,
        entry_type=ConfigEntryType.PASSWORD,
        description=CONF_PASSWORD,
    ),
]


async def setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = TuneInProvider()
    await mass.register_provider(prov)


class TuneInProvider(MusicProvider):
    """Provider implementation for Tune In."""

    # pylint: disable=abstract-method

    _username = None
    _password = None
    _throttler = None

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

    @property
    def supported_mediatypes(self) -> List[MediaType]:
        """Return MediaTypes the provider supports."""
        return [MediaType.Radio]

    async def on_start(self) -> bool:
        """Handle initialization of the provider based on config."""
        # pylint: disable=attribute-defined-outside-init
        config = self.mass.config.get_provider_config(self.id)
        if not config[CONF_USERNAME] or not config[CONF_PASSWORD]:
            LOGGER.debug("Username and password not set. Abort load of provider.")
            return False
        self._username = config[CONF_USERNAME]
        self._password = config[CONF_PASSWORD]
        self._throttler = Throttler(rate_limit=1, period=1)
        return True

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> SearchResult:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        result = SearchResult()
        # TODO: search for radio stations
        return result

    async def get_library_radios(self) -> List[Radio]:
        """Retrieve library/subscribed radio stations from the provider."""
        params = {"c": "presets"}
        result = await self._get_data("Browse.ashx", params)
        if result and "body" in result:
            return [
                await self._parse_radio(item)
                for item in result["body"]
                if item["type"] == "audio"
            ]
        return []

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get radio station details."""
        radio = None
        params = {"c": "composite", "detail": "listing", "id": prov_radio_id}
        result = await self._get_data("Describe.ashx", params)
        if result and result.get("body") and result["body"][0].get("children"):
            item = result["body"][0]["children"][0]
            radio = await self._parse_radio(item)
        return radio

    async def _parse_radio(self, details: dict) -> Radio:
        """Parse Radio object from json obj returned from api."""
        radio = Radio(item_id=details["preset_id"], provider=PROV_ID)
        if "name" in details:
            radio.name = details["name"]
        else:
            # parse name from text attr
            name = details["text"]
            if " | " in name:
                name = name.split(" | ")[1]
            name = name.split(" (")[0]
            radio.name = name
        # parse stream urls and format
        stream_info = await self._get_stream_urls(radio.item_id)
        for stream in stream_info["body"]:
            if stream["media_type"] == "aac":
                quality = TrackQuality.LOSSY_AAC
            elif stream["media_type"] == "ogg":
                quality = TrackQuality.LOSSY_OGG
            else:
                quality = TrackQuality.LOSSY_MP3
            radio.provider_ids.add(
                MediaItemProviderId(
                    provider=PROV_ID,
                    item_id="%s--%s" % (details["preset_id"], stream["media_type"]),
                    quality=quality,
                    details=stream["url"],
                )
            )
        # image
        if "image" in details:
            radio.metadata["image"] = details["image"]
        elif "logo" in details:
            radio.metadata["image"] = details["logo"]
        return radio

    async def _get_stream_urls(self, radio_id):
        """Return the stream urls for the given radio id."""
        params = {"id": radio_id}
        res = await self._get_data("Tune.ashx", params)
        return res

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a radio station."""
        radio_id = item_id.split("--")[0]
        if len(item_id.split("--")) > 1:
            media_type = item_id.split("--")[1]
        else:
            media_type = ""
        stream_info = await self._get_stream_urls(radio_id)
        for stream in stream_info["body"]:
            if stream["media_type"] == media_type or not media_type:
                return StreamDetails(
                    type=StreamType.URL,
                    item_id=item_id,
                    provider=PROV_ID,
                    path=stream["url"],
                    content_type=ContentType(stream["media_type"]),
                    sample_rate=44100,
                    bit_depth=16,
                    details=stream,
                )
        return None

    async def _get_data(self, endpoint, params=None):
        """Get data from api."""
        if not params:
            params = {}
        url = "https://opml.radiotime.com/%s" % endpoint
        params["render"] = "json"
        params["formats"] = "ogg,aac,wma,mp3"
        params["username"] = self._username
        params["partnerId"] = "1"
        async with self._throttler:
            async with self.mass.http_session.get(
                url, params=params, verify_ssl=False
            ) as response:
                result = await response.json()
                if not result or "error" in result:
                    LOGGER.error(url)
                    LOGGER.error(params)
                    result = None
                return result
