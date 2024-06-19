"""RadioBrowser musicprovider support for MusicAssistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from radios import FilterBy, Order, RadioBrowser, RadioBrowserError

from music_assistant.common.models.enums import LinkType, ProviderFeature, StreamType
from music_assistant.common.models.media_items import (
    AudioFormat,
    BrowseFolder,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemLink,
    MediaItemType,
    MediaType,
    ProviderMapping,
    Radio,
    SearchResults,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.server.controllers.cache import use_cache
from music_assistant.server.models.music_provider import MusicProvider

SUPPORTED_FEATURES = (ProviderFeature.SEARCH, ProviderFeature.BROWSE)

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = RadioBrowserProvider(mass, manifest, config)

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
    # ruff: noqa: ARG001 D205
    return ()  # we do not have any config entries (yet)


class RadioBrowserProvider(MusicProvider):
    """Provider implementation for RadioBrowser."""

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.radios = RadioBrowser(
            session=self.mass.http_session, user_agent=f"MusicAssistant/{self.mass.version}"
        )
        try:
            # Try to get some stats to check connection to RadioBrowser API
            await self.radios.stats()
        except RadioBrowserError as err:
            self.logger.exception("%s", err)

    async def search(
        self, search_query: str, media_types=list[MediaType], limit: int = 10
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        result = SearchResults()
        if MediaType.RADIO not in media_types:
            return result

        searchresult = await self.radios.search(name=search_query, limit=limit)

        for item in searchresult:
            result.radio.append(await self._parse_radio(item))

        return result

    @use_cache(86400 * 7)
    async def browse(self, path: str, offset: int, limit: int) -> list[MediaItemType]:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provid://artists).
        """
        subpath = path.split("://", 1)[1]
        subsubpath = "" if "/" not in subpath else subpath.split("/")[-1]

        if not subpath:
            # return main listing
            return [
                BrowseFolder(
                    item_id="popular",
                    provider=self.domain,
                    path=path + "popular",
                    name="",
                    label="radiobrowser_by_popularity",
                ),
                BrowseFolder(
                    item_id="country",
                    provider=self.domain,
                    path=path + "country",
                    name="",
                    label="radiobrowser_by_country",
                ),
                BrowseFolder(
                    item_id="tag",
                    provider=self.domain,
                    path=path + "tag",
                    name="",
                    label="radiobrowser_by_tag",
                ),
            ]

        if subpath == "popular":
            return await self.get_by_popularity(limit=limit, offset=offset)

        if subpath == "tag":
            tags = await self.radios.tags(
                hide_broken=True,
                limit=limit,
                offset=offset,
                order=Order.STATION_COUNT,
                reverse=True,
            )
            tags.sort(key=lambda tag: tag.name)
            return [
                BrowseFolder(
                    item_id=tag.name.lower(),
                    provider=self.domain,
                    path=path + "/" + tag.name.lower(),
                    name=tag.name,
                )
                for tag in tags
            ]

        if subpath == "country":
            items: list[BrowseFolder | Radio] = []
            for country in await self.radios.countries(
                order=Order.NAME, hide_broken=True, limit=limit, offset=offset
            ):
                folder = BrowseFolder(
                    item_id=country.code.lower(),
                    provider=self.domain,
                    path=path + "/" + country.code.lower(),
                    name=country.name,
                )
                folder.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=country.favicon,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
                items.append(folder)
            return items

        if subsubpath in await self.get_tag_names(limit=limit, offset=offset):
            return await self.get_by_tag(subsubpath)

        if subsubpath in await self.get_country_codes(limit=limit, offset=offset):
            return await self.get_by_country(subsubpath)
        return []

    async def get_tag_names(self, limit: int, offset: int):
        """Get a list of tag names."""
        tags = await self.radios.tags(
            hide_broken=True,
            limit=limit,
            offset=offset,
            order=Order.STATION_COUNT,
            reverse=True,
        )
        tags.sort(key=lambda tag: tag.name)
        tag_names = []
        for tag in tags:
            tag_names.append(tag.name.lower())
        return tag_names

    async def get_country_codes(self, limit: int, offset: int):
        """Get a list of country names."""
        countries = await self.radios.countries(
            order=Order.NAME, hide_broken=True, limit=limit, offset=offset
        )
        country_codes = []
        for country in countries:
            country_codes.append(country.code.lower())
        return country_codes

    async def get_by_popularity(self, limit: int, offset: int):
        """Get radio stations by popularity."""
        stations = await self.radios.stations(
            hide_broken=True,
            limit=limit,
            offset=offset,
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
        radio = Radio(
            item_id=radio_obj.uuid,
            provider=self.domain,
            name=radio_obj.name,
            provider_mappings={
                ProviderMapping(
                    item_id=radio_obj.uuid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        radio.metadata.label = radio_obj.tags
        radio.metadata.popularity = radio_obj.votes
        radio.metadata.links = [MediaItemLink(type=LinkType.WEBSITE, url=radio_obj.homepage)]
        radio.metadata.images = [
            MediaItemImage(
                type=ImageType.THUMB,
                path=radio_obj.favicon,
                provider=self.instance_id,
                remotely_accessible=True,
            )
        ]

        return radio

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a radio station."""
        stream = await self.radios.station(uuid=item_id)
        await self.radios.station_click(uuid=item_id)
        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(stream.codec),
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=stream.url_resolved,
            can_seek=False,
        )
