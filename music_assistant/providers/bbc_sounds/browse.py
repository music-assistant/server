"""Browse- and menu-related code for the BBC Sounds provider."""

from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING

from auntie_sounds.models import (
    CategoryItemContainer,
    Container,
    LiveStation,
    Menu,
    MenuItem,
    Playlist,
    RadioClip,
    RadioShow,
    SoundsTypes,
    Station,
)
from auntie_sounds.models import PodcastEpisode as SoundsPodcastEpisode
from auntie_sounds.schedule import catch_up_dates, schedule_dates
from music_assistant_models.enums import ImageType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    RecommendationFolder,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers import datetime
from music_assistant.providers.bbc_sounds.constants import Constants, ValidMenuIDs
from music_assistant.providers.bbc_sounds.now_playing import _station_programme_display

if TYPE_CHECKING:
    from music_assistant.providers.bbc_sounds.provider import BBCSoundsProvider

# Keep a backwards-compatible list around to keep the previous URL working
_STATION_DATE_MENUS = (ValidMenuIDs.SCHEDULES, ValidMenuIDs.STATIONS)


def _ordinal(day: int) -> str:
    """Return the English ordinal suffix for a day of the month."""
    if 4 <= day <= 20 or 24 <= day <= 30:
        return "th"
    return ("st", "nd", "rd")[day % 10 - 1]  # codespell:ignore


def _schedule_date_title(day: date, today: date) -> str:
    """Return a folder name for a schedule date, e.g. "Today" or "Sunday 8th November"."""
    match (day - today).days:
        case 0:
            return "Today"
        case -1:
            return "Yesterday"
        case 1:
            return "Tomorrow"
    return f"{day:%A} {day.day}{_ordinal(day.day)} {day:%B}"


class BrowseManager:
    """Manages the browse and menu functionality."""

    def __init__(self, provider: BBCSoundsProvider) -> None:
        """Initialise the browse manager."""
        self.provider = provider
        self.mass = self.provider.mass
        self.domain = self.provider.domain
        self.menu: Menu | None = None
        self.menu_last_fetched: float | None = None

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://artists).
        """
        self.provider.logger.debug("Browsing path: %s", path)
        if not path.startswith(f"{self.domain}://"):
            raise MusicAssistantError(f"Invalid path for {self.domain} provider: {path}")
        path_parts = path.split("://", 1)[1].split("/")
        self.provider.logger.debug("Path parts: %s", path_parts)

        path_parts = [
            f"{self.domain}:/",
            *[part for part in path_parts if len(part) > 0],
        ]
        dispatch_menu = path_parts[1] if len(path_parts) > 1 else ""

        if dispatch_menu == "live":
            return await self._browse_live(path_parts)
        if dispatch_menu == "catch_up":
            return await self._browse_catch_up(path_parts)
        if dispatch_menu in _STATION_DATE_MENUS:
            return await self._browse_schedules(path_parts)
        if dispatch_menu == "categories":
            return await self._browse_categories(path_parts)
        if dispatch_menu == "collections":
            return await self._browse_collections(path_parts)
        if dispatch_menu == "playlists":
            if len(path_parts) < 3:
                raise KeyError("Invalid subpath")
            return await self._get_playlist(path_parts[2])
        if (
            dispatch_menu != ""
            and dispatch_menu not in ValidMenuIDs
            and Constants.LATEST_NEWS_PLAYLIST_SUFFIX not in dispatch_menu
        ):
            raise KeyError("Invalid subpath")
        return await self._browse_menu(path_parts)

    async def _browse_menu(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if len(path_parts) == 1:
            # bbc_sounds://
            return await self._get_menu()
        return await self._get_subpath_menu(path_parts[1:])

    async def _browse_live(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if len(path_parts) >= 3 and path_parts[2] == "local":
            self.provider.logger.debug("Getting local stations")
            return await self._station_list(
                path_parts=path_parts,
                include_national=False,
                include_local=True,
                show_current_programme=True,
            )
        if len(path_parts) >= 3 and path_parts[2] == "international":
            self.provider.logger.debug("Getting international stations")
            return await self._station_list(
                path_parts=path_parts,
                include_national=False,
                include_local=False,
                include_international=True,
                show_current_programme=False,
            )
        self.provider.logger.debug("Getting national stations")
        return await self._station_list(
            path_parts=path_parts, include_local=False, show_current_programme=True
        )

    async def _browse_categories(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if len(path_parts) == 3:
            # bbc_sounds://categories/<category>
            return await self._get_category(path_parts[2])
        # bbc_sounds://categories/
        return await self._get_subpath_menu(path_parts[1:])

    async def _browse_collections(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if len(path_parts) == 3:
            # bbc_sounds://collections/<collection>
            return await self._get_collection(path_parts[2])
        # bbc_sounds://collections/
        return await self._get_subpath_menu(path_parts[1:])

    async def _browse_catch_up(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if len(path_parts) == 4:
            # bbc_sounds://catch_up/<station_id>/<date>
            return await self._get_station_catch_up_menu(
                path_parts=path_parts,
                station_id=path_parts[2],
                date=path_parts[3],
            )
        if len(path_parts) == 3:
            # bbc_sounds://catch_up/<station_id> OR
            # bbc_sounds://catch_up/local
            if path_parts[2] == "local":
                return await self._station_list_as_folders(
                    path_parts=path_parts,
                    include_national=False,
                    include_local=True,
                    include_international=False,
                )
            return await self._get_station_menu(path_parts)

        # bbc_sounds://catch_up
        return await self._station_list_as_folders(path_parts=path_parts)

    async def _browse_schedules(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        if len(path_parts) == 4:
            # bbc_sounds://schedules/<station_id>/<date>
            return await self._get_station_catch_up_menu(
                path_parts=path_parts,
                station_id=path_parts[2],
                date=path_parts[3],
            )
        if len(path_parts) == 3:
            # bbc_sounds://schedules/<station_id> OR bbc_sounds://schedules/local
            if path_parts[2] == "local":
                return await self._station_list_as_folders(
                    path_parts=path_parts, include_national=False, include_local=True
                )
            return await self._get_station_menu(path_parts)

        # bbc_sounds://stations
        return await self._station_list_as_folders(path_parts=path_parts)

    @use_cache(expiration=Constants.DEFAULT_EXPIRATION)
    async def _get_playlist(self, pid: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get a playlist from the API.

        When browsing the main menu, or through the "Top Picks for You" section, we might
        be presented with a Playlist. These don't usually contain their own contents,
        so we need a helper function to populate them.
        """
        playlist_contents = await self.provider.client.content.get_playlist_contents(pid=pid)
        if playlist_contents and type(playlist_contents) is list:
            return [
                obj
                for obj in [
                    await self._render_browse_item(item) for item in playlist_contents if item
                ]
                if obj
            ]
        return []

    async def _render_browse_item(
        self,
        item: SoundsTypes,
        path_parts: list[str] | None = None,
    ) -> BrowseFolder | Track | Podcast | PodcastEpisode | RecommendationFolder | Radio | None:
        new_item = await self.provider.adaptor.new_object(item, path_parts=path_parts)
        if isinstance(
            new_item,
            (BrowseFolder | Track | Podcast | PodcastEpisode | RecommendationFolder | Radio),
        ):
            return new_item
        return None

    @property
    def _menu_is_stale(self) -> bool:
        if not self.menu:
            self.provider.logger.debug("No menu set")
            return True
        if not self.menu.sub_items:
            self.provider.logger.debug("Menu has no items")
            return True
        if self.menu_last_fetched is not None and (
            (datetime.utc_timestamp() - self.menu_last_fetched) >= Constants.SHORT_EXPIRATION
        ):
            self.provider.logger.debug("Menu has expired")
            return True
        return False

    async def _get_station_menu(
        self,
        path_parts: list[str],
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        List the dates a station's programmes can be browsed for.

        :param path_parts: The browse path, e.g. bbc_sounds://catch_up/<station_id>.
        """
        # the schedule looks ahead, catch-up (and the old stations menu) looks back
        dates = schedule_dates() if path_parts[1] == ValidMenuIDs.SCHEDULES else catch_up_dates()
        today = dates[0]
        item_list: list[MediaItemType | ItemMapping | BrowseFolder] = []
        for day in dates:
            folder = MenuItem(id=day.isoformat(), title=_schedule_date_title(day, today))
            if new_folder := await self._render_browse_item(folder, path_parts=path_parts):
                item_list.append(new_folder)
        return item_list

    async def _refresh_menu_from_api(self) -> None:
        """Get a fresh copy of the main Sounds menu from the API."""
        self.provider.logger.debug("No cached menu, fetching from API")
        # TODO: Keep recommendations=True until full recommendation rail browsing lands
        self.menu = await self.provider.client.get_menu(
            include_recommendations=True,
        )
        self.menu_last_fetched = datetime.utc_timestamp()

    async def _convert_menu(self) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        menu_items = []
        if self.menu:
            for item in self.menu.sub_items:
                new_item = await self._render_browse_item(item)
                if isinstance(new_item, (MediaItemType | ItemMapping | BrowseFolder)):
                    menu_items.append(new_item)
        return menu_items

    async def _get_menu(self) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Access self.menu data, refreshing when necessary."""
        if self._menu_is_stale:
            await self._refresh_menu_from_api()

        if not self.menu or not self.menu.sub_items:
            raise MusicAssistantError("Menu API response is empty or invalid")
        utc_timestamp = datetime.utc_timestamp()
        if self.menu_last_fetched:
            menu_valid_for_mins = int(
                (Constants.SHORT_EXPIRATION - (utc_timestamp - self.menu_last_fetched)) / 60
            )
            self.provider.logger.debug("Menu valid for %s more mins", menu_valid_for_mins)
        return await self._convert_menu()

    async def _station_list_as_folders(
        self,
        path_parts: list[str],
        include_local: bool = False,
        include_national: bool = True,
        include_international: bool = False,
    ) -> list[BrowseFolder]:
        """Get list of stations as BrowseFolders."""
        radio_list: list[BrowseFolder] = []
        if include_national:
            radio_list.extend(
                [
                    BrowseFolder(
                        item_id="local",
                        translation_key="local_radio",
                        name="Local Radio",
                        provider=self.domain,
                        path="/".join([*path_parts, "local"]),
                    ),
                ]
            )
            path = "/".join([*path_parts])
        else:
            path = "/".join([*path_parts[:-1]])
        stations = await self.provider.client.stations.get_stations(
            include_national_stations=include_national,
            include_local_stations=include_local,
            include_international_stations=include_international,
        )
        self.provider.logger.debug("Output: %s", stations)
        for station in stations:
            if station:
                radio_list.append(
                    BrowseFolder(
                        item_id=station.id,
                        path=f"{path}/{station.id}",
                        name=(
                            station.network.short_title
                            if station.network and station.network.short_title
                            else "Unknown station"
                        ),
                        provider=self.domain,
                        image=MediaItemImage(
                            type=ImageType.THUMB,
                            provider=self.domain,
                            path=station.network.logo_url,
                            remotely_accessible=True,
                        )
                        if station.network and station.network.logo_url
                        else None,
                    )
                )
        return radio_list

    @use_cache(expiration=Constants.DYNAMIC_EXPIRATION)
    async def _station_list(
        self,
        path_parts: list[str],
        include_local: bool = False,
        include_national: bool = True,
        include_international: bool = False,
        show_current_programme: bool = False,
    ) -> list[Radio | BrowseFolder]:
        """
        Get list of stations as Radios.

        We do this manually so we can append the current programme. We don't want this by
        default as it gets cached in other places.
        """
        # National is the default, so only show the extra folders there
        radio_list: list[Radio | BrowseFolder] = []
        if include_national:
            radio_list.extend(
                [
                    BrowseFolder(
                        item_id="local",
                        translation_key="local_radio",
                        name="Local Radio",
                        provider=self.domain,
                        path="/".join([*path_parts, "local"]),
                    ),
                    BrowseFolder(
                        item_id="international",
                        translation_key="international_radio",
                        name="International Radio",
                        provider=self.domain,
                        path="/".join([*path_parts, "international"]),
                    ),
                ]
            )

        def _get_station_title(station: LiveStation | Station) -> str:
            if isinstance(station, LiveStation):
                return str(station.network.short_title) if station.network else "Unknown Station"
            return station.title or station.id

        for station in await self.provider.client.stations.get_stations(
            include_local_stations=include_local,
            include_national_stations=include_national,
            include_international_stations=include_international,
        ):
            if (
                station
                and station.id
                and (isinstance(station, LiveStation) or not station.international)
            ):
                # A plain Station has no programme, so its titles and synopses are empty
                station_info = (
                    _station_programme_display(station)
                    if show_current_programme and isinstance(station, LiveStation)
                    else None
                )
                description = station_info.title if station_info else None
                display_name = _get_station_title(station)
                metadata = MediaItemMetadata(
                    description=description,
                    images=(
                        UniqueList(
                            [
                                MediaItemImage(
                                    type=ImageType.THUMB,
                                    provider=self.domain,
                                    path=station.network.logo_url,
                                    remotely_accessible=True,
                                ),
                            ]
                        )
                        if station and station.network and station.network.logo_url
                        else None
                    ),
                )
            elif isinstance(station, Station) and station.international:
                display_name = station.title if station.title is not None else station.id
                metadata = MediaItemMetadata(description=station.description)
            else:
                continue
            radio_list.append(
                Radio(
                    item_id=station.item_id,
                    name=display_name,
                    provider=self.domain,
                    metadata=metadata,
                    provider_mappings={
                        ProviderMapping(
                            item_id=station.item_id,
                            provider_domain=self.domain,
                            provider_instance=self.provider.instance_id,
                        )
                    },
                )
            )
        return radio_list

    async def _get_subpath_menu(
        self, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        item_list: list[MediaItemType | ItemMapping | BrowseFolder] = []
        if not self.menu:
            return item_list
        sub_menu: SoundsTypes | None = self.menu.get(path_parts[0])

        if isinstance(sub_menu, Container):
            for part in path_parts[1:]:
                # only a MenuItem can be descended into, but the last part can be anything
                if not isinstance(sub_menu, MenuItem):
                    break
                sub_menu = sub_menu.get(part)
                if sub_menu is None:
                    break
            else:
                if isinstance(sub_menu, MenuItem) and sub_menu.sub_items is not None:
                    for item in sub_menu.sub_items:
                        if new_item := await self._render_browse_item(
                            item, path_parts=[f"{self.domain}:/", *path_parts]
                        ):
                            item_list.append(new_item)
                # Playlists are returned empty in the main menu API
                elif isinstance(sub_menu, Playlist) and sub_menu.item_id is not None:
                    if sub_menu.item_id is not None:
                        item_list += await self._get_playlist(sub_menu.item_id)
        else:
            self.provider.logger.warning("Sub menu not a container: %s", sub_menu)
        return item_list

    async def _get_station_catch_up_menu(
        self,
        station_id: str,
        path_parts: list[str],
        date: str,
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Lookup a catch-up menu for a station."""
        self.provider.logger.debug("Getting catch-up for %s for %s", station_id, date)
        schedule = await self.provider.client.schedules.get_schedule(
            station_id=station_id,
            date=date,
        )
        items = []
        if schedule and schedule.sub_items:
            for folder in schedule.sub_items:
                new_folder = await self._render_browse_item(folder, path_parts=path_parts)
                if new_folder:
                    items.append(new_folder)
        return items

    @use_cache(expiration=Constants.DEFAULT_EXPIRATION)
    async def _get_category(
        self, category_name: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        category: CategoryItemContainer | None = await self.provider.client.content.get_category(
            category=category_name
        )

        if category is not None and category.sub_items:
            return [
                obj
                for obj in [await self._render_browse_item(item) for item in category.sub_items]
                if obj is not None
            ]
        return []

    @use_cache(expiration=Constants.DEFAULT_EXPIRATION)
    async def _get_collection(
        self, pid: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        collection: list[
            SoundsPodcastEpisode | RadioShow | RadioClip | Container
        ] = await self.provider.client.content.get_collection(pid=pid)
        if collection:
            rendered = [await self._render_browse_item(item) for item in collection]
            return [obj for obj in rendered if obj is not None]
        return []
