"""MusicController: Orchestrates all data from music providers and sync to internal database."""
from __future__ import annotations

import asyncio
import statistics
from typing import Dict, List, Tuple

from music_assistant.constants import EventType
from music_assistant.controllers.music.albums import AlbumsController
from music_assistant.controllers.music.artists import ArtistsController
from music_assistant.controllers.music.playlists import PlaylistController
from music_assistant.controllers.music.radio import RadioController
from music_assistant.controllers.music.tracks import TracksController
from music_assistant.helpers.cache import cached
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import create_task
from music_assistant.models.errors import (
    AlreadyRegisteredError,
    MusicAssistantError,
    SetupFailedError,
)
from music_assistant.models.media_items import (
    Album,
    MediaItem,
    MediaItemProviderId,
    MediaItemType,
    MediaType,
    Playlist,
)
from music_assistant.models.provider import MusicProvider

DB_PROV_MAPPINGS = "provider_mappings"
DB_TRACK_LOUDNESS = "track_loudness"
DB_PLAYLOG = "playlog"


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
        self._providers: Dict[str, MusicProvider] = {}

    async def setup(self):
        """Async initialize of module."""
        await self.__setup_database_tables()
        # setup generic controllers
        await self.artists.setup()
        await self.albums.setup()
        await self.tracks.setup()
        await self.radio.setup()
        await self.playlists.setup()
        self.__schedule_sync_tasks()

    @property
    def provider_count(self) -> int:
        """Return count of all registered music providers."""
        return len(self._providers)

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

    async def register_provider(self, provider: MusicProvider) -> None:
        """Register a music provider."""
        if provider.id in self._providers:
            raise AlreadyRegisteredError(
                f"Provider {provider.id} is already registered"
            )
        try:
            provider.mass = self.mass
            provider.logger = self.logger.getChild(provider.id)
            await provider.setup()
        except Exception as err:  # pylint: disable=broad-except
            raise SetupFailedError(f"Setup failed of provider {provider.id}") from err
        else:
            self._providers[provider.id] = provider
            self.mass.signal_event(EventType.PROVIDER_REGISTERED, provider)
            await self.schedule_provider_sync(provider.id)

    async def search(
        self, search_query, media_types: List[MediaType], limit: int = 10
    ) -> List[MediaItemType]:
        """
        Perform global search for media items on all providers.

            :param search_query: Search query.
            :param media_types: A list of media_types to include.
            :param limit: number of items to return in the search (per type).
        """
        # include results from all music providers
        provider_ids = ["database"] + [item.id for item in self.providers]
        # TODO: sort by name and filter out duplicates ?
        return await asyncio.gather(
            *[
                self.search_provider(search_query, prov_id, media_types, limit)
                for prov_id in provider_ids
            ]
        )

    async def search_provider(
        self,
        search_query: str,
        provider_id: str,
        media_types: List[MediaType],
        limit: int = 10,
    ) -> List[MediaItemType]:
        """
        Perform search on given provider.

            :param search_query: Search query
            :param provider_id: provider_id of the provider to perform the search on.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: number of items to return in the search (per type).
        """
        if provider_id == "database":
            # get results from database
            return (
                await self.artists.search(search_query, "database", limit)
                + await self.albums.search(search_query, "database", limit)
                + await self.tracks.search(search_query, "database", limit)
                + await self.playlists.search(search_query, "database", limit)
                + await self.radio.search(search_query, "database", limit)
            )
        provider = self.get_provider(provider_id)
        media_types_str = ".".join(sorted([x.value for x in media_types]))
        cache_key = f"{provider_id}.search.{search_query}.{media_types_str}.{limit}"
        return await cached(
            self.mass.cache,
            cache_key,
            provider.search,
            search_query,
            media_types,
            limit,
        )

    async def get_item_by_uri(
        self, uri: str, force_refresh: bool = False, lazy: bool = True
    ) -> MediaItemType:
        """Fetch MediaItem by uri."""
        if "://" in uri:
            provider = uri.split("://")[0]
            item_id = uri.split("/")[-1]
            media_type = MediaType(uri.split("/")[-2])
        else:
            # spotify new-style uri
            provider, media_type, item_id = uri.split(":")
            media_type = MediaType(media_type)
        return await self.get_item(
            item_id, provider, media_type, force_refresh=force_refresh, lazy=lazy
        )

    async def get_item(
        self,
        item_id: str,
        provider_id: str,
        media_type: MediaType,
        force_refresh: bool = False,
        lazy: bool = True,
    ) -> MediaItemType:
        """Get single music item by id and media type."""
        ctrl = self._get_controller(media_type)
        return await ctrl.get(
            item_id, provider_id, force_refresh=force_refresh, lazy=lazy
        )

    async def refresh_items(self, items: List[MediaItem]) -> None:
        """
        Refresh MediaItems to force retrieval of full info and matches.

        Creates background tasks to process the action.
        """
        for media_item in items:
            job_desc = f"Refresh metadata of {media_item.uri}"
            self.mass.add_job(self.refresh_item(media_item), job_desc)

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
                force_refresh=True,
                lazy=False,
            )
        except MusicAssistantError:
            pass

        for item in await self.search(media_item.name, [media_item.media_type], 20):
            if item.available:
                await self.get_item(
                    item.item_id, item.provider, item.media_type, lazy=False
                )

    async def get_provider_mapping(
        self, media_type: MediaType, provider_id: str, provider_item_id: str
    ) -> int | None:
        """Lookup database id for media item from provider id."""
        if result := await self.mass.database.get_row(
            DB_PROV_MAPPINGS,
            {
                "media_type": media_type.value,
                "provider": provider_id,
                "prov_item_id": provider_item_id,
            },
        ):
            return result["item_id"]
        return None

    async def add_provider_mappings(
        self,
        item_id: int,
        media_type: MediaType,
        prov_ids: List[MediaItemProviderId],
    ):
        """Add provider ids for media item to database."""
        for prov in prov_ids:
            await self.add_provider_mapping(item_id, media_type, prov)

    async def add_provider_mapping(
        self,
        item_id: int,
        media_type: MediaType,
        prov_id: MediaItemProviderId,
    ):
        """Add provider id for media item to database."""
        await self.mass.database.insert_or_replace(
            DB_PROV_MAPPINGS,
            {
                "item_id": item_id,
                "media_type": media_type.value,
                "prov_item_id": prov_id.item_id,
                "provider": prov_id.provider,
                "quality": prov_id.quality.value,
                "details": prov_id.details,
            },
        )

    async def add_to_library(
        self, media_type: MediaType, provider_item_id: str, provider_id: str
    ) -> None:
        """Add an item to the library."""
        ctrl = self._get_controller(media_type)
        await ctrl.add_to_library(provider_item_id, provider_id)

    async def remove_from_library(
        self, media_type: MediaType, provider_item_id: str, provider_id: str
    ) -> None:
        """Remove item from the library."""
        ctrl = self._get_controller(media_type)
        await ctrl.remove_from_library(provider_item_id, provider_id)

    async def set_track_loudness(self, item_id: str, provider_id: str, loudness: int):
        """List integrated loudness for a track in db."""
        await self.mass.database.insert_or_replace(
            DB_TRACK_LOUDNESS,
            {"item_id": item_id, "provider": provider_id, "loudness": loudness},
        )

    async def get_track_loudness(
        self, provider_item_id: str, provider_id: str
    ) -> float | None:
        """Get integrated loudness for a track in db."""
        if result := await self.mass.database.get_row(
            DB_TRACK_LOUDNESS,
            {
                "item_id": provider_item_id,
                "provider": provider_id,
            },
        ):
            return result["loudness"]
        return None

    async def get_provider_loudness(self, provider_id: str) -> float | None:
        """Get average integrated loudness for tracks of given provider."""
        all_items = []
        for db_row in await self.mass.database.get_rows(
            DB_TRACK_LOUDNESS,
            {
                "provider": provider_id,
            },
        ):
            all_items.append(db_row["loudness"])
        if all_items:
            return statistics.fmean(all_items)
        return None

    async def mark_item_played(self, item_id: str, provider_id: str):
        """Mark item as played in playlog."""
        timestamp = utc_timestamp()
        await self.mass.database.insert_or_replace(
            DB_PLAYLOG,
            {"item_id": item_id, "provider": provider_id, "timestamp": timestamp},
        )

    async def library_add_items(self, items: List[MediaItem]) -> None:
        """
        Add media item(s) to the library.

        Creates background tasks to process the action.
        """
        for media_item in items:
            job_desc = f"Add {media_item.uri} to library"
            self.mass.add_job(
                self.add_to_library(
                    media_item.media_type, media_item.item_id, media_item.provider
                ),
                job_desc,
            )

    async def library_remove_items(self, items: List[MediaItem]) -> None:
        """
        Remove media item(s) from the library.

        Creates background tasks to process the action.
        """
        for media_item in items:
            job_desc = f"Remove {media_item.uri} from library"
            self.mass.add_job(
                self.remove_from_library(
                    media_item.media_type, media_item.item_id, media_item.provider
                ),
                job_desc,
            )

    async def schedule_provider_sync(self, provider_id: str):
        """Schedule library sync for a provider."""
        provider = self.get_provider(provider_id)
        if not provider:
            return
        for media_type in provider.supported_mediatypes:
            self.mass.add_job(
                self._library_items_sync(
                    media_type,
                    provider_id,
                ),
                f"Library sync of {media_type.value}s for provider {provider.name}",
            )

    async def _library_items_sync(
        self, media_type: MediaType, provider_id: str
    ) -> None:
        """Sync library items for given provider."""
        music_provider = self.get_provider(provider_id)
        if not music_provider or not music_provider.available:
            return
        controller = self._get_controller(media_type)
        # create a set of all previous and current db id's
        prev_ids = set()
        for db_item in await controller.library():
            for prov_id in db_item.provider_ids:
                if prov_id.provider == provider_id:
                    prev_ids.add(db_item.item_id)
        cur_ids = set()
        for prov_item in await music_provider.get_library_items(media_type):
            prov_item: MediaItemType = prov_item
            db_item: MediaItemType = await controller.get_db_item_by_prov_id(
                prov_item.provider, prov_item.item_id
            )
            if not db_item and media_type == MediaType.ARTIST:
                # for artists we need a fully matched item (with musicbrainz id)
                db_item = await controller.get(
                    prov_item.item_id, prov_item.provider, details=prov_item
                )
            elif not db_item:
                # for other mediatypes its enough to simply dump the item in the db
                db_item = await controller.add_db_item(prov_item)
            cur_ids.add(db_item.item_id)
            if not db_item.in_library:
                await controller.set_db_library(db_item.item_id, True)
            # sync album tracks
            if media_type == MediaType.ALBUM:
                self.mass.add_job(
                    self._sync_album_tracks(db_item),
                    f"Sync album tracks for album {db_item.name}",
                )
            # sync playlist tracks
            if media_type == MediaType.PLAYLIST:
                self.mass.add_job(
                    self._sync_playlist_tracks(db_item),
                    f"Sync playlist tracks for playlist {db_item.name}",
                )

        # process deletions
        for item_id in prev_ids:
            if item_id not in cur_ids:
                await controller.set_db_library(item_id, False)

    async def _sync_album_tracks(self, db_album: Album) -> None:
        """Store album tracks of in-library album in database."""
        for prov_id in db_album.provider_ids:
            for album_track in await self.albums.get_provider_album_tracks(
                prov_id.item_id, prov_id.provider
            ):
                db_track = await self.tracks.get_db_item_by_prov_id(
                    album_track.provider, album_track.item_id
                )
                if not db_track:
                    db_track = await self.tracks.add_db_item(album_track)
                # add track to album_tracks
                await self.mass.music.albums.add_db_album_track(
                    db_album.item_id,
                    db_track.item_id,
                    album_track.disc_number,
                    album_track.track_number,
                )

    async def _sync_playlist_tracks(self, db_playlist: Playlist) -> None:
        """Store playlist tracks of in-library playlist in database."""
        for prov_id in db_playlist.provider_ids:
            provider = self.get_provider(prov_id.provider)
            if not provider:
                continue
            for playlist_track in await self.playlists.get_provider_playlist_tracks(
                prov_id.item_id, prov_id.provider
            ):
                db_track = await self.tracks.get_db_item_by_prov_id(
                    playlist_track.provider, playlist_track.item_id
                )
                if not db_track:
                    db_track = await self.tracks.add_db_item(playlist_track)
                assert playlist_track.position is not None
                await self.playlists.add_db_playlist_track(
                    db_playlist.item_id,
                    db_track.item_id,
                    playlist_track.position,
                )

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
        async with self.mass.database.get_db() as _db:
            await _db.execute(
                f"""CREATE TABLE IF NOT EXISTS {DB_PROV_MAPPINGS}(
                        item_id INTEGER NOT NULL,
                        media_type TEXT NOT NULL,
                        prov_item_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        quality INTEGER NOT NULL,
                        details TEXT NULL,
                        UNIQUE(item_id, media_type, prov_item_id, provider)
                        );"""
            )
            await _db.execute(
                f"""CREATE TABLE IF NOT EXISTS {DB_TRACK_LOUDNESS}(
                        item_id INTEGER NOT NULL,
                        provider TEXT NOT NULL,
                        loudness REAL,
                        UNIQUE(item_id, provider));"""
            )
            await _db.execute(
                f"""CREATE TABLE IF NOT EXISTS {DB_PLAYLOG}(
                    item_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    timestamp REAL,
                    UNIQUE(item_id, provider));"""
            )

    def __schedule_sync_tasks(self):
        """Schedule the sync tasks."""
        for prov in self.providers:
            create_task(self.schedule_provider_sync(prov.id))
        # reschedule self
        self.mass.loop.call_later(3 * 3600, self.__schedule_sync_tasks)
