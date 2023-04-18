"""RadioBrowser musicprovider support for MusicAssistant."""
from __future__ import annotations

import mimetypes
from collections.abc import AsyncGenerator

# from collections.abc import AsyncGenerator
from time import time
from typing import TYPE_CHECKING

# from aioradios import RadioBrowser
from asyncio_throttle import Throttler

from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import LinkType, ProviderFeature
from music_assistant.common.models.media_items import (
    BrowseFolder,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemLink,
    MediaItemMetadata,
    MediaType,
    ProviderMapping,
    Radio,
    SearchResults,
    StreamDetails,
)

# from music_assistant.common.helpers.util import create_sort_name
from music_assistant.constants import __version__
from music_assistant.server.helpers.audio import get_radio_stream

# from music_assistant.server.helpers.playlists import fetch_playlist
# from music_assistant.server.helpers.tags import parse_tags
from music_assistant.server.models.music_provider import MusicProvider

from .radios.radio_browser import FilterBy, Order, RadioBrowser, RadioBrowserError, Station

SUPPORTED_FEATURES = (
    ProviderFeature.SEARCH,
    ProviderFeature.BROWSE,
)

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

CODEC_TO_MIMETYPE = {
    "MP3": "audio/mpeg",
    "AAC": "audio/aac",
    "AAC+": "audio/aac",
    "OGG": "application/ogg",
}


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
        # self.rb = RadioBrowser(self.mass.http_session)
        # try:
        #     await self.rb.init()
        # except asyncio.exceptions.TimeoutError:
        #     await asyncio.sleep(2)
        # session = self.mass.http_session()
        self.radios = RadioBrowser(
            session=self.mass.http_session, user_agent=f"MusicAssistant/{__version__}"
        )
        stations = await self.radios.stations(limit=10, order=Order.CLICK_COUNT, reverse=True)
        for station in stations:
            print(f"{station.name} ({station.click_count})")

        print(await self.radios.station(uuid="9608b51d-0601-11e8-ae97-52543be04c81"))

        # Print top 10 stations in a country
        stations = await self.radios.stations(
            limit=10,
            order=Order.CLICK_COUNT,
            reverse=True,
            filter_by=FilterBy.COUNTRY_CODE_EXACT,
            filter_term="NL",
        )
        for station in stations:
            print(f"{station.name} ({station.click_count})")

        try:
            await self.radios.stats()
        except RadioBrowserError as err:
            self.logger.error("Could not connect to Radio Browser API", err)

        # hass.data[DOMAIN] = radios
        return True

    async def search(
        self, search_query: str, media_types=list[MediaType] | None, limit: int = 10
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        # result = SearchResults()
        # searchtypes = []
        # if MediaType.RADIO in media_types:
        #     searchtypes.append("radio")

        # time_start = time()

        # searchresult = await self.rb.search(name=search_query, limit=limit)

        # self.logger.debug(
        #     "Processing RadioBrowser search took %s seconds",
        #     round(time() - time_start, 2),
        # )
        # print(searchresult[0])
        # for item in searchresult:
        #     result.radio.append(await self._parse_radio(item))

        # return
        result = SearchResults()
        searchtypes = []
        if MediaType.RADIO in media_types:
            searchtypes.append("radio")

        time_start = time()

        searchresult = await self.radios.search(name=search_query, limit=limit)
        print(searchresult)

        self.logger.debug(
            "Processing RadioBrowser search took %s seconds",
            round(time() - time_start, 2),
        )
        for item in searchresult:
            result.radio.append(await self._parse_radio(item))

        return result

    async def browse(self, path: str) -> BrowseFolder:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provid://artists).
        """
        print("browse")
        print(path)
        _, subpath = path.split("://")
        print(subpath)
        # _, subsubpath = path.split("://")
        # print(await self.rb.countries(orderby="stationcount"))
        # print(await self.rb.countries(orderby="stationcount"))
        # countries = await self.radios.countries()
        countries = await self.radios.countries(order=Order.NAME)
        languages = await self.radios.languages(order=Order.NAME, hide_broken=True)
        tags = await self.radios.tags(
            hide_broken=True,
            limit=100,
            order=Order.STATION_COUNT,
            reverse=True,
        )
        tags.sort(key=lambda tag: tag.name)

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
            root_items.append(
                BrowseFolder(
                    item_id="tag",
                    provider=self.domain,
                    path=path + "tag",
                    name="",
                    label="By tag",
                )
            )
            return BrowseFolder(
                item_id="root",
                provider=self.domain,
                path=path,
                name=self.name,
                items=root_items,
            )
        if subpath == "tag":
            sub_items: list[BrowseFolder] = []
            for tag in tags:
                print("subpath", path)
                # metadata = MediaItemMetadata()
                # favicon = country.favicon
                # sub_items: list[BrowseFolder] = []
                sub_items.append(
                    BrowseFolder(
                        item_id=tag.name,
                        provider=self.domain,
                        path=path + "/" + tag.name.lower(),
                        name="",
                        label=tag.name,
                        # metadata=ima
                        # browsefolder=[MediaItemMetadata = MediaItemMetadata],
                    )
                )
            return BrowseFolder(
                item_id="tag",
                provider=self.domain,
                path=path,
                name=self.name,
                items=sub_items,
            )
        if subpath == "popular":
            stations = await self.radios.stations(
                hide_broken=True,
                limit=250,
                order=Order.CLICK_COUNT,
                reverse=True,
            )
            items = []
            for station in stations:
                print(station)
                items.append(await self._parse_radio(station))
            return BrowseFolder(
                item_id="radios",
                provider=self.domain,
                path=path,
                name="",
                label="radios",
                items=[x for x in items],
            )
        if subpath == "country":
            sub_items: list[BrowseFolder] = []
            for country in countries:
                print(country)
                print("subpath", path)
                metadata = MediaItemMetadata()
                favicon = country.favicon
                # sub_items: list[BrowseFolder] = []
                sub_items.append(
                    BrowseFolder(
                        item_id=country.name,
                        provider=self.domain,
                        path=path + "/" + country.name.lower(),
                        name="",
                        label=country.name,
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

    async def browse(self, path: str) -> BrowseFolder:
        """Return media."""
        radios = self.radios

        # if radios is None:
        #     raise BrowseError("Radio Browser not initialized")

        return BrowseFolder(
            provider=self.domain,
            path=path,
            label="radios",
            item_id="radios",
            name="",
            # media_class=MediaClass.CHANNEL,
            # media_content_type=MediaType.RADIO,
            # title=self.entry.title,
            # can_play=False,
            # can_expand=True,
            # children_media_class=MediaClass.BrowseFolder,
            items=[
                *await self._async_build_popular(radios, path),
                *await self._async_build_by_tag(radios, path),
                *await self._async_build_by_country(radios, path),
            ],
        )

    # @callback
    @staticmethod
    def _async_get_station_mime_type(station: Station) -> str | None:
        """Determine mime type of a radio station."""
        mime_type = CODEC_TO_MIMETYPE.get(station.codec)
        if not mime_type:
            mime_type, _ = mimetypes.guess_type(station.url)
        return mime_type

    # @callback
    def _async_build_stations(
        self, radios: RadioBrowser, stations: list[Station]
    ) -> list[BrowseFolder]:
        """Build list of media sources from radio stations."""
        items: list[BrowseFolder] = []

        for station in stations:
            # if station.codec == "UNKNOWN" or not (
            #     mime_type := self._async_get_station_mime_type(station)
            # ):
            #     continue

            items.append(
                BrowseFolder(
                    item_id="radios",
                    provider=self.domain,
                    path=station.uuid,
                    # media_class=MediaClass.Radio,
                    # media_content_type=mime_type,
                    name=station.name,
                    # can_play=True,
                    # can_expand=False,
                    # thumbnail=station.favicon,
                )
            )

        return items

    async def _async_build_by_country(self, radios: RadioBrowser, path: str) -> list[BrowseFolder]:
        """Handle browsing radio stations by country."""
        category, _, country_code = (path or "").partition("/")
        if country_code:
            stations = await radios.stations(
                filter_by=FilterBy.COUNTRY_CODE_EXACT,
                filter_term=country_code,
                hide_broken=True,
                order=Order.NAME,
                reverse=False,
            )
            return self._async_build_stations(radios, stations)

        # We show country in the root additionally, when there is no item
        if not path or category == "country":
            countries = await radios.countries(order=Order.NAME)
            return [
                BrowseFolder(
                    provider=self.domain,
                    path=f"country/{country.code}",
                    # media_class=MediaClass.DIRECTORY,
                    # media_content_type=MediaType.MUSIC,
                    title=country.name,
                    can_play=False,
                    can_expand=True,
                    thumbnail=country.favicon,
                )
                for country in countries
            ]

        return []

    async def _async_build_popular(self, radios: RadioBrowser, path: str) -> list[BrowseFolder]:
        """Handle browsing popular radio stations."""
        if path == "popular":
            stations = await radios.stations(
                hide_broken=True,
                limit=250,
                order=Order.CLICK_COUNT,
                reverse=True,
            )
            return self._async_build_stations(radios, stations)

        if not path:
            return [
                BrowseFolder(
                    provider=self.domain,
                    path="popular",
                    # media_class=MediaClass.DIRECTORY,
                    # media_content_type=MediaType.MUSIC,
                    title="Popular",
                    can_play=False,
                    can_expand=True,
                )
            ]

        return []

    async def _async_build_by_tag(self, radios: RadioBrowser, path: str) -> list[BrowseFolder]:
        """Handle browsing radio stations by tags."""
        category, _, tag = (path or "").partition("/")
        if category == "tag" and tag:
            stations = await radios.stations(
                filter_by=FilterBy.TAG_EXACT,
                filter_term=tag,
                hide_broken=True,
                order=Order.NAME,
                reverse=False,
            )
            return self._async_build_stations(radios, stations)

        if category == "tag":
            tags = await radios.tags(
                hide_broken=True,
                limit=100,
                order=Order.STATION_COUNT,
                reverse=True,
            )

            # Now we have the top tags, reorder them by name
            tags.sort(key=lambda tag: tag.name)

            return [
                BrowseFolder(
                    provider=self.domain,
                    path=f"tag/{tag.name}",
                    # media_class=MediaClass.DIRECTORY,
                    # media_content_type=MediaType.MUSIC,
                    title=tag.name.title(),
                    can_play=False,
                    can_expand=True,
                )
                for tag in tags
            ]

        if not path:
            return [
                BrowseFolder(
                    provider=self.domain,
                    path="tag",
                    # media_class=MediaClass.DIRECTORY,
                    # media_content_type=MediaType.MUSIC,
                    title="By Category",
                    can_play=False,
                    can_expand=True,
                )
            ]

        return []

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get radio station details."""
        # radio = await self.rb.search_by_uuid(prov_radio_id)
        # print(radio[0])
        # return await self._parse_radio(radio[0])
        radio = await self.rb.search_by_uuid(prov_radio_id)
        print(radio[0])
        return await self._parse_radio(radio[0])

    async def _parse_radio(self, radio_obj: dict) -> Radio:
        # """Parse Radio object from json obj returned from api."""
        # radio = Radio(
        #     item_id=radio_obj["stationuuid"], provider=self.domain, name=radio_obj["name"]
        # )
        # radio.add_provider_mapping(
        #     ProviderMapping(
        #         item_id=radio_obj["stationuuid"],
        #         provider_domain=self.domain,
        #         provider_instance=self.instance_id,
        #     )
        # )
        # radio.metadata.label = radio_obj["tags"]
        # radio.metadata.popularity = radio_obj["votes"]
        # radio.metadata.links = [MediaItemLink(LinkType.WEBSITE, radio_obj["homepage"])]
        # radio.metadata.images = [MediaItemImage(ImageType.THUMB, radio_obj.get("favicon"))]

        # return radio
        """Parse Radio object from json obj returned from api."""
        radio = Radio(item_id=radio_obj.uuid, provider=self.domain, name=radio_obj.name)
        radio.add_provider_mapping(
            ProviderMapping(
                item_id=radio_obj.uuid,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
            )
        )
        radio.metadata.label = radio_obj.tags
        radio.metadata.popularity = radio_obj.votes
        radio.metadata.links = [MediaItemLink(LinkType.WEBSITE, radio_obj.homepage)]
        radio.metadata.images = [MediaItemImage(ImageType.THUMB, radio_obj.favicon)]

        return radio

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        # """Get streamdetails for a radio station."""
        # print(item_id)
        # stream = await self.rb.search_by_uuid(item_id)
        # print(stream[0])
        # url = stream[0]["url"]
        # url_resolved = stream[0]["url_resolved"]
        # await self.rb.vote_for_station(item_id)
        # return StreamDetails(
        #     provider=self.domain,
        #     item_id=item_id,
        #     # content_type=ContentType.UNKNOWN,
        #     content_type=ContentType.try_parse(stream[0]["codec"]),
        #     media_type=MediaType.RADIO,
        #     data=url,
        #     expires=time() + 24 * 3600,
        #     direct=url_resolved,
        # )
        """Get streamdetails for a radio station."""
        print(item_id)
        stream = await self.radios.station(uuid=item_id)
        print(stream)
        url = stream.url
        url_resolved = stream.url_resolved
        await self.radios.station_click(uuid=item_id)
        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            # content_type=ContentType.UNKNOWN,
            content_type=ContentType.try_parse(stream.codec),
            media_type=MediaType.RADIO,
            data=url,
            expires=time() + 24 * 3600,
            direct=url_resolved,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0  # noqa: ARG002
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        # async for chunk in get_radio_stream(self.mass, streamdetails.data, streamdetails):
        #     yield chunk
        async for chunk in get_radio_stream(self.mass, streamdetails.data, streamdetails):
            yield chunk
