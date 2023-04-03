"""RadioBrowser musicprovider support for MusicAssistant."""
from __future__ import annotations

# from collections.abc import AsyncGenerator
from time import time
from typing import TYPE_CHECKING

from aioradios import RadioBrowser
from asyncio_throttle import Throttler

# from music_assistant.common.helpers.util import create_sort_name
from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import ProviderFeature
from music_assistant.common.models.errors import MediaNotFoundError
from music_assistant.common.models.media_items import (
    BrowseFolder,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaType,
    ProviderMapping,
    Radio,
    SearchResults,
    StreamDetails,
)

# from music_assistant.server.helpers.audio import get_radio_stream
# from music_assistant.server.helpers.playlists import fetch_playlist
# from music_assistant.server.helpers.tags import parse_tags
from music_assistant.server.models.music_provider import MusicProvider

SUPPORTED_FEATURES = (
    ProviderFeature.SEARCH,
    ProviderFeature.BROWSE,
)

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = RadioBrowserProvider(mass, manifest, config)

    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant, manifest: ProviderManifest  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return tuple()  # we do not have any config entries (yet)
    # return (
    #     ConfigEntry(
    #         key=CONF_USERNAME, type=ConfigEntryType.STRING, label="Username", required=True
    #     ),
    # )


class RadioBrowserProvider(MusicProvider):
    """Provider implementation for RadioBrowser."""

    _throttler: Throttler

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        # self._throttler = Throttler(rate_limit=1, period=1)
        self.rb = RadioBrowser(self.mass.http_session)
        await self.rb.init()

    async def search(
        self, search_query: str, media_types=list[MediaType] | None, limit: int = 10
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        result = SearchResults()
        searchtypes = []
        if MediaType.RADIO in media_types:
            searchtypes.append("radio")

        # time_start = time.time()

        searchresult = await self.rb.search(name=search_query, limit=limit)

        # self.logger.debug(
        #     "Processing RadioBrowser search took %s seconds",
        #     round(time.time() - time_start, 2),
        # )
        print(searchresult[0])
        for item in searchresult:
            # media_type = item["kind"]
            result.radio.append(await self._parse_radio(item))
        # result.radio.append(await self._parse_radio(searchresult[0]))
        # print(result)

        return result

    async def browse(self, path: str) -> BrowseFolder:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provid://artists).
        """
        print("browse")
        _, subpath = path.split("://")
        print(subpath)

        # this reference implementation can be overridden with a provider specific approach
        if not subpath:
            # return main listing
            root_items: list[BrowseFolder] = []
            # if ProviderFeature.LIBRARY_ARTISTS in self.supported_features:
            root_items.append(
                BrowseFolder(
                    item_id="popular",
                    provider=self.domain,
                    path=path + "popular",
                    name="",
                    label="Popular",
                )
            )
            return BrowseFolder(
                item_id="root",
                provider=self.domain,
                path=path,
                name=self.name,
                items=root_items,
            )
        if subpath == "radios":
            return BrowseFolder(
                item_id="radios",
                provider=self.domain,
                path=path,
                name="",
                label="radios",
                items=[x async for x in self.get_library_radios()],
            )

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get radio station details."""
        # if not prov_radio_id.startswith("http"):
        # prov_radio_id, media_type = prov_radio_id.split("--", 1)
        # params = {"c": "composite", "detail": "listing", "id": prov_radio_id}
        radio = await self.rb.search_by_uuid(prov_radio_id)
        print(radio[0])
        return await self._parse_radio(radio[0])
        # if result and result.get("body") and result["body"][0].get("children"):
        #     item = result["body"][0]["children"][0]
        #     stream_info = await self.__get_data("Tune.ashx", id=prov_radio_id)
        #     for stream in stream_info["body"]:
        #         if stream["media_type"] != media_type:
        #             continue
        #         return await self._parse_radio(item)
        # # fallback - e.g. for handle custom urls ...
        # async for radio in self.get_library_radios():
        #     if radio.item_id == prov_radio_id:
        #         return radio
        # return None

    async def _parse_radio(self, radio_obj: dict) -> Radio:
        """Parse Radio object from json obj returned from api."""
        # if "name" in radio_obj:
        # name = radio_obj["name"]
        # print(name)

        # url = radio_obj["url"]
        # print(url)
        # item_id = radio_obj["stationuuid"]
        # print(item_id)
        # content_type = radio_obj["codec"]
        # print(content_type)
        # bit_rate = radio_obj.get("bitrate")  # TODO !
        # print(bit_rate)

        radio = Radio(
            item_id=radio_obj["stationuuid"], provider=self.domain, name=radio_obj["name"]
        )
        radio.add_provider_mapping(
            ProviderMapping(
                item_id=radio_obj["stationuuid"],
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                # content_type=content_type,
                # bit_rate=bit_rate,
                # details=url,
            )
        )
        # preset number is used for sorting (not present at stream time)
        # preset_number = radio_obj.get("preset_number")
        # if preset_number and folder:
        #     radio.sort_name = f'{folder}-{details["preset_number"]}'
        # elif preset_number:
        #     radio.sort_name = radio_obj["preset_number"]
        # radio.sort_name += create_sort_name(name)
        # if "text" in radio_obj:
        #     radio.metadata.description = radio_obj["text"]
        # images
        if img := radio_obj.get("favicon"):
            radio.metadata.images = [MediaItemImage(ImageType.THUMB, img)]
        if img := radio_obj.get("favicon"):
            radio.metadata.images = [MediaItemImage(ImageType.LOGO, img)]
        return radio

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a radio station."""
        if item_id.startswith("http"):
            # custom url
            return StreamDetails(
                provider=self.instance_id,
                item_id=item_id,
                content_type=ContentType.UNKNOWN,
                media_type=MediaType.RADIO,
                data=item_id,
            )
        item_id, media_type = item_id.split("--", 1)
        stream_info = await self.__get_data("Tune.ashx", id=item_id)
        for stream in stream_info["body"]:
            if stream["media_type"] != media_type:
                continue
            # check if the radio stream is not a playlist
            url = stream["url"]
            direct = None
            # if url.endswith("m3u8") or url.endswith("m3u") or url.endswith("pls"):  # noqa: SIM102
            #     if playlist := await fetch_playlist(self.mass, url):
            #         if len(playlist) > 1 or ".m3u" in playlist[0] or ".pls" in playlist[0]:
            #             # this is most likely an mpeg-dash stream, let ffmpeg handle that
            #             direct = playlist[0]
            #         url = playlist[0]
            return StreamDetails(
                provider=self.domain,
                item_id=item_id,
                content_type=ContentType(stream["media_type"]),
                media_type=MediaType.RADIO,
                data=url,
                expires=time() + 24 * 3600,
                direct=direct,
            )
        raise MediaNotFoundError(f"Unable to retrieve stream details for {item_id}")

    # async def get_audio_stream(
    #     self, streamdetails: StreamDetails, seek_position: int = 0  # noqa: ARG002
    # ) -> AsyncGenerator[bytes, None]:
    #     """Return the audio stream for the provider item."""
    #     async for chunk in get_radio_stream(self.mass, streamdetails.data, streamdetails):
    #         yield chunk

    # async def __get_data(self, endpoint: str, **kwargs):
    #     """Get data from api."""
    #     if endpoint.startswith("http"):
    #         url = endpoint
    #     else:
    #         url = f"https://opml.radiotime.com/{endpoint}"
    #         kwargs["formats"] = "ogg,aac,wma,mp3"
    #         kwargs["username"] = self.config.get_value(CONF_USERNAME)
    #         kwargs["partnerId"] = "1"
    #         kwargs["render"] = "json"
    #     async with self._throttler:
    #         async with self.mass.http_session.get(url, params=kwargs, ssl=False) as response:
    #             result = await response.json()
    #             if not result or "error" in result:
    #                 self.logger.error(url)
    #                 self.logger.error(kwargs)
    #                 result = None
    #             return result
