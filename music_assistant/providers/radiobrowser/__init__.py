"""RadioBrowser musicprovider support for MusicAssistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    LinkType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    MediaItemImage,
    MediaItemLink,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails
from radios import FilterBy, Order, RadioBrowser, RadioBrowserError, Station

from music_assistant.constants import (
    CONF_ENTRY_LIBRARY_SYNC_BACK,
    CONF_ENTRY_LIBRARY_SYNC_RADIOS,
    CONF_ENTRY_PROVIDER_SYNC_INTERVAL_RADIOS,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.LIBRARY_RADIOS_EDIT,
}

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_STORED_RADIOS = "stored_radios"

CONF_ENTRY_LIBRARY_SYNC_RADIOS_HIDDEN = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_LIBRARY_SYNC_RADIOS.to_dict(),
        "hidden": True,
        "default_value": "import_only",
    }
)
CONF_ENTRY_PROVIDER_SYNC_INTERVAL_RADIOS_HIDDEN = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_PROVIDER_SYNC_INTERVAL_RADIOS.to_dict(),
        "hidden": True,
        "default_value": 180,
    }
)
CONF_ENTRY_LIBRARY_SYNC_BACK_HIDDEN = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_LIBRARY_SYNC_BACK.to_dict(),
        "hidden": True,
        "default_value": True,
    }
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return RadioBrowserProvider(mass, manifest, config, SUPPORTED_FEATURES)


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
            multi_value=True,
            label=CONF_STORED_RADIOS,
            default_value=[],
            required=False,
            hidden=True,
        ),
        # hide some of the default (dynamic) entries for library management
        CONF_ENTRY_LIBRARY_SYNC_RADIOS_HIDDEN,
        CONF_ENTRY_PROVIDER_SYNC_INTERVAL_RADIOS_HIDDEN,
        CONF_ENTRY_LIBRARY_SYNC_BACK_HIDDEN,
    )


class RadioBrowserProvider(MusicProvider):
    """Provider implementation for RadioBrowser."""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.radios = RadioBrowser(
            session=self.mass.http_session, user_agent=f"MusicAssistant/{self.mass.version}"
        )
        try:
            await self.radios.stats()
        except RadioBrowserError as err:
            raise ProviderUnavailableError(f"RadioBrowser API unavailable: {err}") from err

        # copy the radiobrowser items that were added to the library
        # TODO: remove this logic after version 2.3.0 or later
        if not self.config.get_value(CONF_STORED_RADIOS) and self.mass.music.database:
            async for db_row in self.mass.music.database.iter_items(
                "provider_mappings",
                {"media_type": "radio", "provider_domain": "radiobrowser"},
            ):
                await self.library_add(await self.get_radio(db_row["provider_item_id"]))

    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 10
    ) -> SearchResults:
        """Perform search on musicprovider."""
        result = SearchResults()
        if MediaType.RADIO not in media_types:
            return result

        try:
            searchresult = await self.radios.search(name=search_query, limit=limit)
            result.radio = [await self._parse_radio(item) for item in searchresult]
        except RadioBrowserError as err:
            self.logger.warning("RadioBrowser search failed for query '%s': %s", search_query, err)

        return result

    async def browse(self, path: str) -> Sequence[MediaItemType | BrowseFolder]:
        """Browse this provider's items."""
        path_parts = [] if "://" not in path else path.split("://")[1].split("/")

        subpath = path_parts[0] if len(path_parts) > 0 else ""
        subsubpath = path_parts[1] if len(path_parts) > 1 else ""
        subsubsubpath = path_parts[2] if len(path_parts) > 2 else ""

        if not subpath:
            return [
                BrowseFolder(
                    item_id="popularity",
                    provider=self.domain,
                    path=path + "popularity",
                    name="",
                    translation_key="radiobrowser_by_popularity",
                ),
                BrowseFolder(
                    item_id="category",
                    provider=self.domain,
                    path=path + "category",
                    name="",
                    translation_key="radiobrowser_by_category",
                ),
            ]

        if subpath == "popularity":
            if not subsubpath:
                return [
                    BrowseFolder(
                        item_id="popular",
                        provider=self.domain,
                        path=path + "/popular",
                        name="",
                        translation_key="radiobrowser_by_clicks",
                    ),
                    BrowseFolder(
                        item_id="votes",
                        provider=self.domain,
                        path=path + "/votes",
                        name="",
                        translation_key="radiobrowser_by_votes",
                    ),
                ]

            if subsubpath == "popular":
                return await self.get_by_popularity()

            if subsubpath == "votes":
                return await self.get_by_votes()

        if subpath == "category":
            if not subsubpath:
                return [
                    BrowseFolder(
                        item_id="country",
                        provider=self.domain,
                        path=path + "/country",
                        name="",
                        translation_key="radiobrowser_by_country",
                    ),
                    BrowseFolder(
                        item_id="language",
                        provider=self.domain,
                        path=path + "/language",
                        name="",
                        translation_key="radiobrowser_by_language",
                    ),
                    BrowseFolder(
                        item_id="tag",
                        provider=self.domain,
                        path=path + "/tag",
                        name="",
                        translation_key="radiobrowser_by_tag",
                    ),
                ]

            if subsubpath == "country":
                if subsubsubpath:
                    return await self.get_by_country(subsubsubpath)
                return await self.get_country_folders(path)

            if subsubpath == "language":
                if subsubsubpath:
                    return await self.get_by_language(subsubsubpath)
                return await self.get_language_folders(path)

            if subsubpath == "tag":
                if subsubsubpath:
                    return await self.get_by_tag(subsubsubpath)
                return await self.get_tag_folders(path)

        return []

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        stored_radios = self.config.get_value(CONF_STORED_RADIOS)
        if TYPE_CHECKING:
            stored_radios = cast("list[str]", stored_radios)
        for item in stored_radios:
            try:
                yield await self.get_radio(item)
            except MediaNotFoundError:
                self.logger.warning("Radio station %s no longer exists", item)

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        stored_radios = self.config.get_value(CONF_STORED_RADIOS)
        if TYPE_CHECKING:
            stored_radios = cast("list[str]", stored_radios)
        if item.item_id in stored_radios:
            return False
        self.logger.debug("Adding radio %s to stored radios", item.item_id)
        stored_radios = [*stored_radios, item.item_id]
        self.update_config_value(CONF_STORED_RADIOS, stored_radios)
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        stored_radios = self.config.get_value(CONF_STORED_RADIOS)
        if TYPE_CHECKING:
            stored_radios = cast("list[str]", stored_radios)
        if prov_item_id not in stored_radios:
            return False
        self.logger.debug("Removing radio %s from stored radios", prov_item_id)
        stored_radios = [x for x in stored_radios if x != prov_item_id]
        self.update_config_value(CONF_STORED_RADIOS, stored_radios)
        return True

    @use_cache(3600 * 6)  # Cache for 6 hours
    async def get_by_popularity(self) -> Sequence[Radio]:
        """Get radio stations by popularity."""
        try:
            stations = await self.radios.stations(
                hide_broken=True,
                limit=1000,
                order=Order.CLICK_COUNT,
                reverse=True,
            )
            return [await self._parse_radio(station) for station in stations]
        except RadioBrowserError as err:
            raise ProviderUnavailableError(f"Failed to fetch popular stations: {err}") from err

    @use_cache(3600 * 6)  # Cache for 6 hours
    async def get_by_votes(self) -> Sequence[Radio]:
        """Get radio stations by votes."""
        try:
            stations = await self.radios.stations(
                hide_broken=True,
                limit=1000,
                order=Order.VOTES,
                reverse=True,
            )
            return [await self._parse_radio(station) for station in stations]
        except RadioBrowserError as err:
            raise ProviderUnavailableError(f"Failed to fetch stations by votes: {err}") from err

    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def get_country_folders(self, base_path: str) -> list[BrowseFolder]:
        """Get a list of country names as BrowseFolder."""
        try:
            countries = await self.radios.countries(order=Order.NAME, hide_broken=True, limit=1000)
        except RadioBrowserError as err:
            raise ProviderUnavailableError(f"Failed to fetch countries: {err}") from err

        items: list[BrowseFolder] = []
        for country in countries:
            folder = BrowseFolder(
                item_id=country.code.lower(),
                provider=self.domain,
                path=base_path + "/" + country.code.lower(),
                name=country.name,
            )
            if country.favicon and country.favicon.strip():
                folder.image = MediaItemImage(
                    type=ImageType.THUMB,
                    path=country.favicon,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            items.append(folder)
        return items

    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def get_language_folders(self, base_path: str) -> list[BrowseFolder]:
        """Get a list of language names as BrowseFolder."""
        try:
            languages = await self.radios.languages(
                order=Order.STATION_COUNT, reverse=True, hide_broken=True, limit=1000
            )
        except RadioBrowserError as err:
            raise ProviderUnavailableError(f"Failed to fetch languages: {err}") from err

        return [
            BrowseFolder(
                item_id=language.name,
                provider=self.domain,
                path=base_path + "/" + language.name,
                name=language.name,
            )
            for language in languages
        ]

    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def get_tag_folders(self, base_path: str) -> list[BrowseFolder]:
        """Get a list of tag names as BrowseFolder."""
        try:
            tags = await self.radios.tags(
                hide_broken=True,
                order=Order.STATION_COUNT,
                reverse=True,
                limit=100,
            )
        except RadioBrowserError as err:
            raise ProviderUnavailableError(f"Failed to fetch tags: {err}") from err

        tags.sort(key=lambda tag: tag.name)
        return [
            BrowseFolder(
                item_id=tag.name,
                provider=self.domain,
                path=base_path + "/" + tag.name,
                name=tag.name.title(),
            )
            for tag in tags
        ]

    @use_cache(3600 * 24)  # Cache for 1 day
    async def get_by_country(self, country_code: str) -> list[Radio]:
        """Get radio stations by country."""
        try:
            stations = await self.radios.stations(
                filter_by=FilterBy.COUNTRY_CODE_EXACT,
                filter_term=country_code,
                hide_broken=True,
                limit=1000,
                order=Order.CLICK_COUNT,
                reverse=True,
            )
            return [await self._parse_radio(station) for station in stations]
        except RadioBrowserError as err:
            raise ProviderUnavailableError(
                f"Failed to fetch stations for country {country_code}: {err}"
            ) from err

    @use_cache(3600 * 24)  # Cache for 1 day
    async def get_by_language(self, language: str) -> list[Radio]:
        """Get radio stations by language."""
        try:
            stations = await self.radios.stations(
                filter_by=FilterBy.LANGUAGE_EXACT,
                filter_term=language,
                hide_broken=True,
                limit=1000,
                order=Order.CLICK_COUNT,
                reverse=True,
            )
            return [await self._parse_radio(station) for station in stations]
        except RadioBrowserError as err:
            raise ProviderUnavailableError(
                f"Failed to fetch stations for language {language}: {err}"
            ) from err

    @use_cache(3600 * 24)  # Cache for 1 day
    async def get_by_tag(self, tag: str) -> list[Radio]:
        """Get radio stations by tag."""
        try:
            stations = await self.radios.stations(
                filter_by=FilterBy.TAG_EXACT,
                filter_term=tag,
                hide_broken=True,
                limit=1000,
                order=Order.CLICK_COUNT,
                reverse=True,
            )
            return [await self._parse_radio(station) for station in stations]
        except RadioBrowserError as err:
            raise ProviderUnavailableError(
                f"Failed to fetch stations for tag {tag}: {err}"
            ) from err

    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get radio station details."""
        try:
            radio = await self.radios.station(uuid=prov_radio_id)
            if not radio:
                raise MediaNotFoundError(f"Radio station {prov_radio_id} not found")
            return await self._parse_radio(radio)
        except RadioBrowserError as err:
            raise ProviderUnavailableError(
                f"Failed to fetch radio station {prov_radio_id}: {err}"
            ) from err

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
        radio.metadata.popularity = radio_obj.click_count
        radio.metadata.links = {MediaItemLink(type=LinkType.WEBSITE, url=radio_obj.homepage)}
        radio.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=radio_obj.favicon,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        )
        return radio

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        try:
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
                allow_seek=False,
            )
        except RadioBrowserError as err:
            raise ProviderUnavailableError(
                f"Failed to get stream details for {item_id}: {err}"
            ) from err
