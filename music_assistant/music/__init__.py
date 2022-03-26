"""MusicController: Orchestrates all data from music providers and sync to internal database."""
from __future__ import annotations

import statistics
from typing import Dict, List, Tuple

from music_assistant import EventDetails
from music_assistant.config.models import ConfigItem
from music_assistant.constants import EventType
from music_assistant.helpers.cache import cached

from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.helpers.errors import MusicAssistantError
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.music.albums import AlbumsController
from music_assistant.music.artists import ArtistsController
from music_assistant.music.library import MusicLibrary
from music_assistant.music.models import (
    MediaItem,
    MediaItemProviderId,
    MediaItemType,
    MediaType,
    MusicProvider,
    SearchResult,
)
from music_assistant.music.playlists import PlaylistController
from music_assistant.music.providers.filesystem import FileProvider
from music_assistant.music.providers.qobuz import QobuzProvider
from music_assistant.music.providers.spotify import SpotifyProvider
from music_assistant.music.providers.tunein import TuneInProvider
from music_assistant.music.radio import RadioController
from music_assistant.music.tracks import TracksController
from music_assistant.tasks import TaskInfo

PROVIDERS = [
    FileProvider,
    QobuzProvider,
    SpotifyProvider,
    TuneInProvider,
]

TABLE_PROV_MAPPINGS = "provider_mappings"
TABLE_TRACK_LOUDNESS = "track_loudness"
TABLE_PLAYLOG = "playlog"


class MusicController:
    """Several helpers around the musicproviders."""

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.logger = mass.logger.getChild("music")
        self.mass = mass
        self.artists = ArtistsController(mass)
        self.albums = AlbumsController(mass)
        self.tracks = TracksController(mass)
        self.radio = RadioController(mass)
        self.playlists = PlaylistController(mass)
        self.library = MusicLibrary(mass)
        self._db_add_progress = set()
        self._providers: Dict[str, MusicProvider] = {}
        mass.subscribe(self.__on_mass_event, EventType.CONFIG_CHANGED)

    async def setup(self):
        """Async initialize of module."""
        await self.__setup_database_tables()
        # setup generic controllers
        await self.artists.setup()
        await self.albums.setup()
        await self.tracks.setup()
        await self.radio.setup()
        await self.playlists.setup()
        # setup music providers
        for prov_cls in PROVIDERS:
            prov: MusicProvider = prov_cls(self.mass)
            self._providers[prov.id] = prov
            await prov.setup()
            if prov.available:
                self.mass.signal_event(EventType.PROVIDER_AVAILABLE, prov)

    @property
    def providers(self) -> Tuple[MusicProvider]:
        """Return all (available) music providers."""
        return tuple(x for x in self._providers.values() if x.available)

    def get_provider(self, provider_id: str) -> MusicProvider | None:
        """Return provider/plugin by id."""
        prov = self._providers.get(provider_id, None)
        if prov is None or not prov.available:
            self.logger.warning("Provider %s is not available", provider_id)
        return prov

    async def search(
        self, search_query, media_types: List[MediaType], limit: int = 10
    ) -> SearchResult:
        """
        Perform global search for media items on all providers.

            :param search_query: Search query.
            :param media_types: A list of media_types to include.
            :param limit: number of items to return in the search (per type).
        """
        result = SearchResult([], [], [], [], [])
        # include results from all music providers
        provider_ids = ["database"] + [item.id for item in self.providers]
        for provider_id in provider_ids:
            provider_result = await self.search_provider(
                search_query, provider_id, media_types, limit
            )
            result.artists += provider_result.artists
            result.albums += provider_result.albums
            result.tracks += provider_result.tracks
            result.playlists += provider_result.playlists
            result.radios += provider_result.radios
            # TODO: sort by name and filter out duplicates ?
        return result

    async def search_provider(
        self,
        search_query: str,
        provider_id: str,
        media_types: List[MediaType],
        limit: int = 10,
    ) -> SearchResult:
        """
        Perform search on given provider.

            :param search_query: Search query
            :param provider_id: provider_id of the provider to perform the search on.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: number of items to return in the search (per type).
        """
        if provider_id == "database":
            # get results from database
            return SearchResult(
                artists=await self.artists.search(search_query, "database", limit),
                albums=await self.albums.search(search_query, "database", limit),
                tracks=await self.tracks.search(search_query, "database", limit),
                playlists=await self.playlists.search(search_query, "database", limit),
                radios=await self.radio.search(search_query, "database", limit),
            )
        provider = self.get_provider(provider_id)
        cache_key = f"{provider_id}.search.{search_query}.{media_types}.{limit}"
        return await cached(
            self.mass.cache,
            cache_key,
            provider.search,
            search_query,
            media_types,
            limit,
        )

    async def get_item_by_uri(self, uri: str) -> MediaItemType:
        """Fetch MediaItem by uri."""
        if "://" in uri:
            provider = uri.split("://")[0]
            item_id = uri.split("/")[-1]
            media_type = MediaType(uri.split("/")[-2])
        else:
            # spotify new-style uri
            provider, media_type, item_id = uri.split(":")
            media_type = MediaType(media_type)
        return await self.get_item(item_id, provider, media_type)

    async def get_item(
        self,
        item_id: str,
        provider_id: str,
        media_type: MediaType,
        refresh: bool = False,
        lazy: bool = True,
    ) -> MediaItemType:
        """Get single music item by id and media type."""
        ctrl = self._get_controller(media_type)
        return await ctrl.get(item_id, provider_id, refresh=refresh, lazy=lazy)

    async def refresh_items(self, items: List[MediaItem]) -> List[TaskInfo]:
        """
        Refresh MediaItems to force retrieval of full info and matches.

        Creates background tasks to process the action.
        """
        result = []
        for media_item in items:
            job_desc = f"Refresh metadata of {media_item.uri}"
            result.append(self.mass.tasks.add(job_desc, self.refresh_item, media_item))
        return result

    async def refresh_item(
        self,
        media_item: MediaItem,
    ):
        """Try to refresh a mediaitem by requesting it's full object or search for substitutes."""
        try:
            return await self.get_item(
                media_item.item_id,
                media_item.provider,
                media_item.media_type,
                refresh=True,
                lazy=False,
            )
        except MusicAssistantError:
            pass
        searchresult: SearchResult = await self.search(
            media_item.name, [media_item.media_type], 20
        )
        for items in [
            searchresult.artists,
            searchresult.albums,
            searchresult.tracks,
            searchresult.playlists,
            searchresult.radios,
        ]:
            for item in items:
                if item.available:
                    await self.get_item(
                        item.item_id, item.provider, item.media_type, lazy=False
                    )

    async def get_provider_mapping(
        self, media_type: MediaType, provider: str, provider_item_id: str
    ) -> int | None:
        """Lookup database id for media item from provider id."""
        if result := self.mass.database.get_row(
            TABLE_PROV_MAPPINGS,
            {
                "media_type": media_type.value,
                "provider": provider,
                "prov_item_id": provider_item_id,
            },
        ):
            return result["item_id"]
        return None

    async def add_provider_mappings(
        self,
        item_id: int,
        media_type: MediaType,
        provider_ids: List[MediaItemProviderId],
    ):
        """Add provider ids for media item to database."""
        for prov in provider_ids:
            await self.add_provider_mapping(item_id, media_type, prov)

    async def add_provider_mapping(
        self,
        item_id: int,
        media_type: MediaType,
        provider_id: MediaItemProviderId,
    ):
        """Add provider id for media item to database."""
        await self.mass.database.insert_or_replace(
            TABLE_PROV_MAPPINGS,
            {
                "item_id": item_id,
                "media_type": media_type.value,
                "prov_item_id": provider_id.item_id,
                "provider": provider_id.provider,
                "quality": provider_id.quality,
                "details": provider_id.details,
            },
        )

    async def add_to_library(
        self, media_type: MediaType, provider_item_id: str, provider: str
    ) -> None:
        """Add an item to the library."""
        ctrl = self._get_controller(media_type)
        await ctrl.add_to_library(provider_item_id, provider)

    async def remove_from_library(self, media_type: MediaType, item_id: int) -> None:
        """Remove item from the library."""
        ctrl = self._get_controller(media_type)
        await ctrl.remove_from_library(item_id)

    async def set_track_loudness(self, item_id: str, provider: str, loudness: int):
        """List integrated loudness for a track in db."""
        await self.mass.database.insert_or_replace(
            TABLE_TRACK_LOUDNESS,
            {"item_id": item_id, "provider": provider, "loudness": loudness},
        )

    async def get_track_loudness(
        self, provider_item_id: str, provider: str
    ) -> float | None:
        """Get integrated loudness for a track in db."""
        if result := self.mass.database.get_row(
            TABLE_TRACK_LOUDNESS,
            {
                "item_id": provider_item_id,
                "provider": provider,
            },
        ):
            return result["loudness"]
        return None

    async def get_provider_loudness(self, provider: str) -> float | None:
        """Get average integrated loudness for tracks of given provider."""
        all_items = []
        for db_row in await self.mass.database.get_rows(
            TABLE_TRACK_LOUDNESS,
            {
                "provider": provider,
            },
        ):
            all_items.append(db_row["loudness"])
        if all_items:
            return statistics.fmean(all_items)
        return None

    async def mark_item_played(self, item_id: str, provider: str):
        """Mark item as played in playlog."""
        timestamp = utc_timestamp()
        await self.mass.database.insert_or_replace(
            TABLE_PLAYLOG,
            {"item_id": item_id, "provider": provider, "timestamp": timestamp},
        )

    async def library_add_items(self, items: List[MediaItem]) -> List[TaskInfo]:
        """
        Add media item(s) to the library.

        Creates background tasks to process the action.
        """
        result = []
        for media_item in items:
            job_desc = f"Add {media_item.uri} to library"
            result.append(
                self.mass.tasks.add(
                    job_desc,
                    self.add_to_library,
                    media_item.media_type,
                    media_item.item_id,
                    media_item.provider,
                )
            )
        return result

    async def library_remove_items(self, items: List[MediaItem]) -> List[TaskInfo]:
        """
        Remove media item(s) from the library.

        Creates background tasks to process the action.
        """
        result = []
        for media_item in items:
            job_desc = f"Remove {media_item.uri} from library"
            result.append(
                self.mass.tasks.add(
                    job_desc,
                    self.remove_from_library,
                    media_item.media_type,
                    media_item.item_id,
                    media_item.provider,
                )
            )
        return result

    def _get_controller(
        self, media_type: MediaType
    ) -> ArtistsController | AlbumsController | TracksController | RadioController | PlaylistController:
        """Return controller for MediaType."""
        if media_type == MediaType.ARTIST:
            return self.artists
        if media_type == MediaType.ALBUM:
            return self.albums
        if media_type == MediaType.TRACK:
            return self.tracks
        if media_type == MediaType.RADIO:
            return self.radio
        if media_type == MediaType.PLAYLIST:
            return self.playlists

    async def __setup_database_tables(self) -> None:
        """Init generic database tables."""
        await self.mass.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_PROV_MAPPINGS}(
                    item_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    prov_item_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    quality INTEGER NOT NULL,
                    details TEXT NULL,
                    UNIQUE(item_id, media_type, prov_item_id, provider)
                    );"""
        )
        await self.mass.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_TRACK_LOUDNESS}(
                    item_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    loudness REAL,
                    UNIQUE(item_id, provider));"""
        )
        await self.mass.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE_PLAYLOG}(
                item_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                timestamp REAL,
                UNIQUE(item_id, provider));"""
        )

    async def __on_mass_event(
        self, event: EventType, event_details: EventDetails
    ) -> None:
        """Handle events on the eventbus."""
        if event == EventType.CONFIG_CHANGED:
            config: ConfigItem = event_details
            # handle re-setup of provider if the config changed
            prov = self._providers.get(config.owner)
            if not prov:
                return
            prev_available = prov.available
            await prov.setup()
            if prov.available and not prev_available:
                self.mass.signal_event(EventType.PROVIDER_AVAILABLE, prov)
