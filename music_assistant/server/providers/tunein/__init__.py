"""Tune-In musicprovider support for MusicAssistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from asyncio_throttle import Throttler

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import ConfigEntryType, ProviderFeature, StreamType
from music_assistant.common.models.errors import InvalidDataError, LoginFailed, MediaNotFoundError
from music_assistant.common.models.media_items import (
    AudioFormat,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaType,
    ProviderMapping,
    Radio,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.constants import CONF_USERNAME
from music_assistant.server.models.music_provider import MusicProvider

SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.BROWSE,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    if not config.get_value(CONF_USERNAME):
        msg = "Username is invalid"
        raise LoginFailed(msg)

    prov = TuneInProvider(mass, manifest, config)
    if "@" in config.get_value(CONF_USERNAME):
        prov.logger.warning(
            "Email address detected instead of username, "
            "it is advised to use the tunein username instead of email."
        )
    await prov.handle_async_init()
    return prov


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
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
        ),
    )


class TuneInProvider(MusicProvider):
    """Provider implementation for Tune In."""

    _throttler: Throttler

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._throttler = Throttler(rate_limit=1, period=2)

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""

        async def parse_items(
            items: list[dict], folder: str | None = None
        ) -> AsyncGenerator[Radio, None]:
            for item in items:
                item_type = item.get("type", "")
                if "unavailable" in item.get("key", ""):
                    continue
                if not item.get("is_available", True):
                    continue
                if item_type == "audio":
                    if "preset_id" not in item:
                        continue
                    # each radio station can have multiple streams add each one as different quality
                    stream_info = await self._get_stream_info(item["preset_id"])
                    yield self._parse_radio(item, stream_info, folder)
                elif item_type == "link" and item.get("item") == "url":
                    # custom url
                    try:
                        yield self._parse_radio(item)
                    except InvalidDataError as err:
                        # there may be invalid custom urls, ignore those
                        self.logger.warning(str(err))
                elif item_type == "link":
                    # stations are in sublevel (new style)
                    if sublevel := await self.__get_data(item["URL"], render="json"):
                        async for subitem in parse_items(sublevel["body"], item["text"]):
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
        if not prov_radio_id.startswith("http"):
            if "--" in prov_radio_id:
                prov_radio_id, media_type = prov_radio_id.split("--", 1)
            else:
                media_type = None
            params = {"c": "composite", "detail": "listing", "id": prov_radio_id}
            result = await self.__get_data("Describe.ashx", **params)
            if result and result.get("body") and result["body"][0].get("children"):
                item = result["body"][0]["children"][0]
                stream_info = await self._get_stream_info(prov_radio_id)
                for stream in stream_info:
                    if media_type and stream["media_type"] != media_type:
                        continue
                    return self._parse_radio(item, [stream])
        # fallback - e.g. for handle custom urls ...
        async for radio in self.get_library_radios():
            if radio.item_id == prov_radio_id:
                return radio
        msg = f"Item {prov_radio_id} not found"
        raise MediaNotFoundError(msg)

    def _parse_radio(
        self, details: dict, stream_info: list[dict] | None = None, folder: str | None = None
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

        if stream_info is not None:
            # stream info is provided: parse stream objects into provider mappings
            radio = Radio(
                item_id=details["preset_id"],
                provider=self.lookup_key,
                name=name,
                provider_mappings={
                    ProviderMapping(
                        item_id=f'{details["preset_id"]}--{stream["media_type"]}',
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        audio_format=AudioFormat(
                            content_type=ContentType.try_parse(stream["media_type"]),
                            bit_rate=stream.get("bitrate", 128),
                        ),
                        details=stream["url"],
                        available=details.get("is_available", True),
                    )
                    for stream in stream_info
                },
            )
        else:
            # custom url (no stream object present)
            radio = Radio(
                item_id=details["URL"],
                provider=self.lookup_key,
                name=name,
                provider_mappings={
                    ProviderMapping(
                        item_id=details["URL"],
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        audio_format=AudioFormat(
                            content_type=ContentType.UNKNOWN,
                        ),
                        details=details["URL"],
                        available=details.get("is_available", True),
                    )
                },
            )

        # preset number is used for sorting (not present at stream time)
        preset_number = details.get("preset_number", 0)
        radio.position = preset_number
        if "text" in details:
            radio.metadata.description = details["text"]
        # image
        if img := details.get("image") or details.get("logo"):
            radio.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=img,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        return radio

    async def _get_stream_info(self, preset_id: str) -> list[dict]:
        """Get stream info for a radio station."""
        cache_key = f"tunein_stream_{preset_id}"
        if cache := await self.mass.cache.get(cache_key):
            return cache
        result = (await self.__get_data("Tune.ashx", id=preset_id))["body"]
        await self.mass.cache.set(cache_key, result)
        return result

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a radio station."""
        if item_id.startswith("http"):
            # custom url
            return StreamDetails(
                provider=self.instance_id,
                item_id=item_id,
                audio_format=AudioFormat(
                    content_type=ContentType.UNKNOWN,
                ),
                media_type=MediaType.RADIO,
                stream_type=StreamType.HTTP,
                path=item_id,
                can_seek=False,
            )
        if "--" in item_id:
            stream_item_id, media_type = item_id.split("--", 1)
        else:
            media_type = None
            stream_item_id = item_id
        for stream in await self._get_stream_info(stream_item_id):
            if media_type and stream["media_type"] != media_type:
                continue
            return StreamDetails(
                provider=self.domain,
                item_id=item_id,
                # set contenttype to unknown so ffmpeg can auto detect it
                audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
                media_type=MediaType.RADIO,
                stream_type=StreamType.HTTP,
                path=stream["url"],
                can_seek=False,
            )
        msg = f"Unable to retrieve stream details for {item_id}"
        raise MediaNotFoundError(msg)

    async def __get_data(self, endpoint: str, **kwargs):
        """Get data from api."""
        if endpoint.startswith("http"):
            url = endpoint
        else:
            url = f"https://opml.radiotime.com/{endpoint}"
            kwargs["formats"] = "ogg,aac,wma,mp3"
            kwargs["username"] = self.config.get_value(CONF_USERNAME)
            kwargs["partnerId"] = "1"
            kwargs["render"] = "json"
        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        headers = {"Accept-Language": f"{locale}, {language};q=0.9, *;q=0.5"}
        async with (
            self._throttler,
            self.mass.http_session.get(url, params=kwargs, headers=headers, ssl=False) as response,
        ):
            result = await response.json()
            if not result or "error" in result:
                self.logger.error(url)
                self.logger.error(kwargs)
                result = None
            return result
