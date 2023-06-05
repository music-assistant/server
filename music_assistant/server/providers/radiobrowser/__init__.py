"""RadioBrowser musicprovider support for MusicAssistant."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from time import time
from typing import TYPE_CHECKING

from radios import FilterBy, Order, RadioBrowser, RadioBrowserError

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
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
from music_assistant.constants import __version__ as MASS_VERSION  # noqa: N812
from music_assistant.server.helpers.audio import get_radio_stream
from music_assistant.server.models.music_provider import MusicProvider

SUPPORTED_FEATURES = (ProviderFeature.SEARCH, ProviderFeature.BROWSE)

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
    # ruff: noqa: ARG001 D205
    return tuple()  # we do not have any config entries (yet)


class RadioBrowserProvider(MusicProvider):
    """Provider implementation for RadioBrowser."""

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self.radios = RadioBrowser(
            session=self.mass.http_session, user_agent=f"MusicAssistant/{MASS_VERSION}"
        )
        try:
            # Try to get some stats to check connection to RadioBrowser API
            await self.radios.stats()
        except RadioBrowserError as err:
            self.logger.error("%s", err)

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

        searchresult = await self.radios.search(name=search_query, limit=limit)

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
        _, subpath = path.split("://")
        subsubpath = "" if "/" not in subpath else subpath.split("/")[-1]

        if not subpath:
            # return main listing
            root_items: list[BrowseFolder] = []
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

        if subpath == "popular":
            return BrowseFolder(
                item_id="radios",
                provider=self.domain,
                path=path,
                name="",
                label="radios",
                items=[x for x in await self.get_by_popularity()],
            )

        if subpath == "tag":
            sub_items: list[BrowseFolder] = []
            tags = await self.radios.tags(
                hide_broken=True,
                limit=100,
                order=Order.STATION_COUNT,
                reverse=True,
            )
            tags.sort(key=lambda tag: tag.name)
            for tag in tags:
                folder = BrowseFolder(
                    item_id=tag.name.lower(),
                    provider=self.domain,
                    path=path + "/" + tag.name.lower(),
                    name="",
                    label=tag.name,
                )
                sub_items.append(folder)
            return BrowseFolder(
                item_id="tag",
                provider=self.domain,
                path=path,
                name=self.name,
                items=sub_items,
            )

        if subpath == "country":
            sub_items: list[BrowseFolder] = []
            for country in await self.radios.countries(order=Order.NAME):
                folder = BrowseFolder(
                    item_id=country.code.lower(),
                    provider=self.domain,
                    path=path + "/" + country.code.lower(),
                    name="",
                    label=country.name,
                )
                folder.metadata.images = [MediaItemImage(ImageType.THUMB, country.favicon)]
                sub_items.append(folder)
            return BrowseFolder(
                item_id="country",
                provider=self.domain,
                path=path,
                name=self.name,
                items=sub_items,
            )

        if subsubpath in await self.get_tag_names():
            return BrowseFolder(
                item_id="radios",
                provider=self.domain,
                path=path,
                name="",
                label="radios",
                items=[x for x in await self.get_by_tag(subsubpath)],
            )

        if subsubpath in await self.get_country_codes():
            return BrowseFolder(
                item_id="radios",
                provider=self.domain,
                path=path,
                name="",
                label="radios",
                items=[x for x in await self.get_by_country(subsubpath)],
            )

    async def get_tag_names(self):
        """Get a list of tag names."""
        tags = await self.radios.tags(
            hide_broken=True,
            limit=100,
            order=Order.STATION_COUNT,
            reverse=True,
        )
        tags.sort(key=lambda tag: tag.name)
        tag_names = []
        for tag in tags:
            tag_names.append(tag.name.lower())
        return tag_names

    async def get_country_codes(self):
        """Get a list of country names."""
        countries = await self.radios.countries(order=Order.NAME)
        country_codes = []
        for country in countries:
            country_codes.append(country.code.lower())
        return country_codes

    async def get_by_popularity(self):
        """Get radio stations by popularity."""
        stations = await self.radios.stations(
            hide_broken=True,
            limit=250,
            order=Order.CLICK_COUNT,
            reverse=True,
        )
        items = []
        for station in stations:
            items.append(await self._parse_radio(station))
        return items

    async def get_by_tag(self, tag: str):
        """Get radio stations by tag."""
        items = []
        stations = await self.radios.stations(
            filter_by=FilterBy.TAG_EXACT,
            filter_term=tag,
            hide_broken=True,
            order=Order.NAME,
            reverse=False,
        )
        for station in stations:
            items.append(await self._parse_radio(station))
        return items

    async def get_by_country(self, country_code: str):
        """Get radio stations by country."""
        items = []
        stations = await self.radios.stations(
            filter_by=FilterBy.COUNTRY_CODE_EXACT,
            filter_term=country_code,
            hide_broken=True,
            order=Order.NAME,
            reverse=False,
        )
        for station in stations:
            items.append(await self._parse_radio(station))
        return items

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get radio station details."""
        radio = await self.radios.station(uuid=prov_radio_id)
        return await self._parse_radio(radio)

    async def _parse_radio(self, radio_obj: dict) -> Radio:
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
        """Get streamdetails for a radio station."""
        stream = await self.radios.station(uuid=item_id)
        url_resolved = stream.url_resolved
        await self.radios.station_click(uuid=item_id)
        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            content_type=ContentType.try_parse(stream.codec),
            media_type=MediaType.RADIO,
            data=url_resolved,
            expires=time() + 24 * 3600,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0  # noqa: ARG002
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        async for chunk in get_radio_stream(self.mass, streamdetails.data, streamdetails):
            yield chunk
