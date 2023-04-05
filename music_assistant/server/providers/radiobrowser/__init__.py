"""RadioBrowser musicprovider support for MusicAssistant."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

# from collections.abc import AsyncGenerator
from time import time
from typing import TYPE_CHECKING

from aioradios import RadioBrowser
from asyncio_throttle import Throttler

# from music_assistant.common.helpers.util import create_sort_name
from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import LinkType, ProviderFeature
from music_assistant.common.models.media_items import (
    BrowseFolder,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemLink,
    MediaType,
    ProviderMapping,
    Radio,
    SearchResults,
    StreamDetails,
)
from music_assistant.server.helpers.audio import get_radio_stream

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
        # async with self.mass.http_session(trust_env=True) as session:
        # self.rb = RadioBrowser(session)
        # async with session.get(url) as resp:
        #     print(resp.status)
        # self._throttler = Throttler(rate_limit=1, period=1)
        self.rb = RadioBrowser(self.mass.http_session)
        try:
            await self.rb.init()
        except asyncio.exceptions.TimeoutError:
            await asyncio.sleep(2)
        # except (
        #     # aiohttp.ContentTypeError,
        #     # JSONDecodeError,
        #     AssertionError,
        #     ValueError,
        # ) as err:
        #     text = await response.text()
        #     self.logger.exception(
        #         "Error while processing %s: %s", endpoint, text, exc_info=err
        #     )
        #     return None

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

        time_start = time()

        searchresult = await self.rb.search(name=search_query, limit=limit)

        self.logger.debug(
            "Processing RadioBrowser search took %s seconds",
            round(time() - time_start, 2),
        )
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
        print(path)
        _, subpath = path.split("://")
        print(subpath)
        # print(await self.rb.countries(orderby="stationcount"))
        # print(await self.rb.countries(orderby="stationcount"))
        countries = await self.rb.countries(orderby="stationcount", reverse=True)

        # this reference implementation can be overridden with a provider specific approach
        if not subpath:
            # return main listing
            root_items: list[BrowseFolder] = []
            # sub_items: list[BrowseFolder] = []
            # if ProviderFeature.LIBRARY_ARTISTS in self.supported_features:
            root_items.append(
                BrowseFolder(
                    item_id="popular",
                    provider=self.domain,
                    path=path + "popular",
                    name="",
                    label="By popularity",
                )
            )
            root_items.append(
                BrowseFolder(
                    item_id="country",
                    provider=self.domain,
                    path=path + "country",
                    name="",
                    label="By country",
                )
            )
            return BrowseFolder(
                item_id="root",
                provider=self.domain,
                path=path,
                name=self.name,
                items=root_items,
            )
        if subpath == "country":
            sub_items: list[BrowseFolder] = []
            for country in countries:
                print(country)
                print("subpath", path)
                # sub_items: list[BrowseFolder] = []
                # sub_items: list[BrowseFolder] = []
                sub_items.append(
                    BrowseFolder(
                        item_id=country["name"],
                        provider=self.domain,
                        path=path + "/" + country["name"].lower(),
                        name="",
                        label=country["name"],
                        # metadata=ima
                        # browsefolder=[MediaItemMetadata = MediaItemMetadata],
                    )
                )
            return BrowseFolder(
                item_id="country",
                provider=self.domain,
                path=path,
                name=self.name,
                items=sub_items,
            )

            # return BrowseFolder(
            #     item_id="country",
            #     provider=self.domain,
            #     path=path,
            #     name="",
            #     label="By country",
            #     items=["clicks", "votes"]
            #     # items=[x async for x in self.rb.stations(orderby="votes")],
            # )

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get radio station details."""
        radio = await self.rb.search_by_uuid(prov_radio_id)
        print(radio[0])
        return await self._parse_radio(radio[0])

    async def _parse_radio(self, radio_obj: dict) -> Radio:
        """Parse Radio object from json obj returned from api."""
        radio = Radio(
            item_id=radio_obj["stationuuid"], provider=self.domain, name=radio_obj["name"]
        )
        radio.add_provider_mapping(
            ProviderMapping(
                item_id=radio_obj["stationuuid"],
                provider_domain=self.domain,
                provider_instance=self.instance_id,
            )
        )
        radio.metadata.label = radio_obj["tags"]
        radio.metadata.popularity = radio_obj["votes"]
        radio.metadata.links = [MediaItemLink(LinkType.WEBSITE, radio_obj["homepage"])]
        radio.metadata.images = [MediaItemImage(ImageType.THUMB, radio_obj.get("favicon"))]

        return radio

        # async def _parse_all_counties(self) -> BrowseFolder:
        #     """Parse Radio object from json obj returned from api."""
        #     countries = await self.rb.countries(orderby="stationcount")
        #     for country in countries:
        #         country = BrowseFolder(
        #             item_id=country["iso_3166_1"], provider=self.domain, name=country["name"]
        #         )
        #         country.add_provider_mapping(
        #             ProviderMapping(
        #                 item_id=radio_obj["stationuuid"],
        #                 provider_domain=self.domain,
        #                 provider_instance=self.instance_id,
        #             )
        #         )
        #         radio.metadata.label = radio_obj["tags"]
        #         radio.metadata.popularity = radio_obj["votes"]
        #         radio.metadata.links = [MediaItemLink(LinkType.WEBSITE, radio_obj["homepage"])]
        #        radio.metadata.images = [MediaItemImage(ImageType.THUMB, radio_obj.get("favicon"))]

        return radio

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a radio station."""
        print(item_id)
        stream = await self.rb.search_by_uuid(item_id)
        print(stream[0])
        url = stream[0]["url"]
        url_resolved = stream[0]["url_resolved"]
        await self.rb.vote_for_station(item_id)
        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            # content_type=ContentType.UNKNOWN,
            content_type=ContentType.try_parse(stream[0]["codec"]),
            media_type=MediaType.RADIO,
            data=url,
            expires=time() + 24 * 3600,
            direct=url_resolved,
        )
        # direct=url_resolved,

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0  # noqa: ARG002
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        async for chunk in get_radio_stream(self.mass, streamdetails.data, streamdetails):
            yield chunk

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
