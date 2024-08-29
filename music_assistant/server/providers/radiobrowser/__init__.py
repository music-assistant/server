"""RadioBrowser musicprovider support for MusicAssistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, cast

from radios import FilterBy, Order, RadioBrowser, RadioBrowserError, Station

from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import (
    ConfigEntryType,
    LinkType,
    ProviderFeature,
    StreamType,
)
from music_assistant.common.models.errors import MediaNotFoundError
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
    UniqueList,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.server.controllers.cache import use_cache
from music_assistant.server.models.music_provider import MusicProvider

SUPPORTED_FEATURES = (
    ProviderFeature.SEARCH,
    ProviderFeature.BROWSE,
    # RadioBrowser doesn't support a library feature at all
    # but MA users like to favorite their radio stations and
    # have that included in backups so we store it in the config.
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.LIBRARY_RADIOS_EDIT,
)

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

CONF_STORED_RADIOS = "stored_radios"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return RadioBrowserProvider(mass, manifest, config)


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
    return (
        ConfigEntry(
            # RadioBrowser doesn't support a library feature at all
            # but MA users like to favorite their radio stations and
            # have that included in backups so we store it in the config.
            key=CONF_STORED_RADIOS,
            type=ConfigEntryType.STRING,
            label=CONF_STORED_RADIOS,
            default_value=[],
            required=False,
            multi_value=True,
            hidden=True,
        ),
    )


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

        # copy the radiobrowser items that were added to the library
        # TODO: remove this logic after version 2.3.0 or later
        if not self.config.get_value(CONF_STORED_RADIOS) and self.mass.music.database:
            async for db_row in self.mass.music.database.iter_items(
                "provider_mappings",
                {"media_type": "radio", "provider_domain": "radiobrowser"},
            ):
                await self.library_add(await self.get_radio(db_row["provider_item_id"]))

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 10
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
        result.radio = [await self._parse_radio(item) for item in searchresult]

        return result

    async def browse(self, path: str) -> Sequence[MediaItemType]:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provid://artists).
        """
        part_parts = path.split("://")[1].split("/")
        subpath = part_parts[0] if part_parts else ""
        subsubpath = part_parts[1] if len(part_parts) > 1 else ""

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
            return await self.get_by_popularity()

        if subpath == "tag" and subsubpath:
            return await self.get_by_tag(subsubpath)

        if subpath == "tag":
            return await self.get_tag_folders(path)

        if subpath == "country" and subsubpath:
            return await self.get_by_country(subsubpath)

        if subpath == "country":
            return await self.get_country_folders(path)

        return []

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        stored_radios = self.config.get_value(CONF_STORED_RADIOS)
        if TYPE_CHECKING:
            stored_radios = cast(list[str], stored_radios)
        for item in stored_radios:
            yield await self.get_radio(item)

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        stored_radios = self.config.get_value(CONF_STORED_RADIOS)
        if TYPE_CHECKING:
            stored_radios = cast(list[str], stored_radios)
        if item.item_id in stored_radios:
            return False
        self.logger.debug("Adding radio %s to stored radios", item.item_id)
        stored_radios = [*stored_radios, item.item_id]
        self.mass.config.set_raw_provider_config_value(
            self.instance_id, CONF_STORED_RADIOS, stored_radios
        )
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        stored_radios = self.config.get_value(CONF_STORED_RADIOS)
        if TYPE_CHECKING:
            stored_radios = cast(list[str], stored_radios)
        if prov_item_id not in stored_radios:
            return False
        self.logger.debug("Removing radio %s from stored radios", prov_item_id)
        stored_radios = [x for x in stored_radios if x != prov_item_id]
        self.mass.config.set_raw_provider_config_value(
            self.instance_id, CONF_STORED_RADIOS, stored_radios
        )
        return True

    @use_cache(3600 * 24)
    async def get_tag_folders(self, base_path: str) -> list[BrowseFolder]:
        """Get a list of tag names as BrowseFolder."""
        tags = await self.radios.tags(
            hide_broken=True,
            order=Order.STATION_COUNT,
            reverse=True,
        )
        tags.sort(key=lambda tag: tag.name)
        return [
            BrowseFolder(
                item_id=tag.name.lower(),
                provider=self.domain,
                path=base_path + "/" + tag.name.lower(),
                name=tag.name,
            )
            for tag in tags
        ]

    @use_cache(3600 * 24)
    async def get_country_folders(self, base_path: str) -> list[BrowseFolder]:
        """Get a list of country names as BrowseFolder."""
        items: list[BrowseFolder] = []
        for country in await self.radios.countries(order=Order.NAME, hide_broken=True):
            folder = BrowseFolder(
                item_id=country.code.lower(),
                provider=self.domain,
                path=base_path + "/" + country.code.lower(),
                name=country.name,
            )
            folder.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=country.favicon,
                        provider=self.lookup_key,
                        remotely_accessible=True,
                    )
                ]
            )
            items.append(folder)
        return items

    @use_cache(3600)
    async def get_by_popularity(self) -> Sequence[Radio]:
        """Get radio stations by popularity."""
        stations = await self.radios.stations(
            hide_broken=True,
            limit=5000,
            order=Order.CLICK_COUNT,
            reverse=True,
        )
        items = []
        for station in stations:
            items.append(await self._parse_radio(station))
        return items

    @use_cache(3600)
    async def get_by_tag(self, tag: str) -> Sequence[Radio]:
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

    @use_cache(3600)
    async def get_by_country(self, country_code: str) -> list[Radio]:
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
        if not radio:
            raise MediaNotFoundError(f"Radio station {prov_radio_id} not found")
        return await self._parse_radio(radio)

    async def _parse_radio(self, radio_obj: Station) -> Radio:
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
        radio.metadata.popularity = radio_obj.votes
        radio.metadata.links = {MediaItemLink(type=LinkType.WEBSITE, url=radio_obj.homepage)}
        radio.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=radio_obj.favicon,
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        )

        return radio

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a radio station."""
        stream = await self.radios.station(uuid=item_id)
        if not stream:
            raise MediaNotFoundError(f"Radio station {item_id} not found")
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
