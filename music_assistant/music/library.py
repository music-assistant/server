"""LibraryController: Orchestrates synchronisation of music providers into the library."""
from __future__ import annotations
import time
from typing import List

from music_assistant.constants import EventType
from music_assistant.helpers.typing import EventDetails, MusicAssistant
from music_assistant.tasks import TaskInfo
from music_assistant.music.models import (
    Album,
    Artist,
    MediaItem,
    MediaType,
    MusicProvider,
    Playlist,
    Radio,
    Track,
)


class MusicLibrary:
    """Manage sync of musicproviders to library."""

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.running_sync_jobs = set()
        self.mass = mass
        self.logger = self.mass.music.logger.getChild("library")
        self.cache = mass.cache
        self._sync_tasks = set()
        self.mass.subscribe(self.__on_mass_event, EventType.PROVIDER_AVAILABLE)

    
    
    async def sync_provider(self, prov_id: str):
        """
        Sync library for a provider.

        param prov_id: {string} -- provider id to sync
        """
        provider = self.mass.music.get_provider(prov_id)
        if not provider:
            return
        periodic = 3 * 3600  # every 3 hours
        if MediaType.ALBUM in provider.supported_mediatypes:
            self.mass.tasks.add(
                f"Library sync of albums for provider {provider.name}",
                self.library_albums_sync,
                prov_id,
                periodic=periodic,
            )
        if MediaType.TRACK in provider.supported_mediatypes:
            self.mass.tasks.add(
                f"Library sync of tracks for provider {provider.name}",
                self.library_tracks_sync,
                prov_id,
                periodic=periodic,
            )
        if MediaType.ARTIST in provider.supported_mediatypes:
            self.mass.tasks.add(
                f"Library sync of artists for provider {provider.name}",
                self.library_artists_sync,
                prov_id,
                periodic=periodic,
            )
        if MediaType.PLAYLIST in provider.supported_mediatypes:
            self.mass.tasks.add(
                f"Library sync of playlists for provider {provider.name}",
                self.library_playlists_sync,
                prov_id,
                periodic=periodic,
            )
        if MediaType.RADIO in provider.supported_mediatypes:
            self.mass.tasks.add(
                f"Library sync of radio for provider {provider.name}",
                self.library_radios_sync,
                prov_id,
                periodic=periodic,
            )

    async def library_artists_sync(self, provider_id: str) -> None:
        """Sync library items for given provider."""
        music_provider = self.mass.music.get_provider(provider_id)
        cache_key = f"library_artists_{provider_id}"
        prev_db_ids = await self.mass.cache.get(cache_key, default=[])
        cur_db_ids = set()

        for item in await music_provider.get_library_artists():
            db_item = await self.mass.music.artists.get(
                item.item_id, provider_id, lazy=False
            )
            cur_db_ids.add(db_item.item_id)
            if not db_item.in_library:
                await self.add_to_library(db_item.item_id)
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.remove_from_library(db_id)
        # store ids in cache for next sync
        await self.mass.cache.set(cache_key, cur_db_ids)

    async def library_albums_sync(self, provider_id: str):
        """Sync library albums for given provider."""
        music_provider = self.mass.music.get_provider(provider_id)
        cache_key = f"library_albums_{provider_id}"
        prev_db_ids = await self.mass.cache.get(cache_key, default=[])
        cur_db_ids = set()

        for item in await music_provider.get_library_albums():
            db_album = await self.mass.music.get_album(
                item.item_id, provider_id, lazy=False
            )
            if not db_album.available and not item.available:
                # album availability changed, sort this out with auto matching magic
                db_album = await self.mass.music.match_album(db_album)
            cur_db_ids.add(db_album.item_id)
            if not db_album.in_library:
                await self.mass.database.add_to_library(
                    db_album.item_id, MediaType.ALBUM
                )
            # precache album tracks
            await self.mass.music.get_album_tracks(item.item_id, provider_id)
        # process album deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.remove_from_library(db_id, MediaType.ALBUM)
        # store ids in cache for next sync
        await self.mass.cache.set(cache_key, cur_db_ids)

    async def library_tracks_sync(self, provider_id: str):
        """Sync library tracks for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        cache_key = f"library_tracks_{provider_id}"
        prev_db_ids = await self.mass.cache.get(cache_key, default=[])
        cur_db_ids = set()
        for item in await music_provider.get_library_tracks():
            db_item = await self.mass.music.get_track(
                item.item_id, provider_id, track_details=item, lazy=False
            )
            if not db_item.available and not item.available:
                # track availability changed, sort this out with auto matching magic
                db_item = await self.mass.music.add_track(item)
            cur_db_ids.add(db_item.item_id)
            if not db_item.in_library:
                await self.mass.database.add_to_library(
                    db_item.item_id, MediaType.TRACK
                )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.remove_from_library(db_id, MediaType.TRACK)
        # store ids in cache for next sync
        await self.mass.cache.set(cache_key, cur_db_ids)

    async def library_playlists_sync(self, provider_id: str):
        """Sync library playlists for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        cache_key = f"library_playlists_{provider_id}"
        prev_db_ids = await self.mass.cache.get(cache_key, default=[])
        cur_db_ids = set()
        for playlist in await music_provider.get_library_playlists():
            db_item = await self.mass.music.get_playlist(
                playlist.item_id, provider_id, lazy=False
            )
            if db_item.checksum != playlist.checksum:
                db_item = await self.mass.database.add_playlist(playlist)
            cur_db_ids.add(db_item.item_id)
            await self.mass.database.add_to_library(db_item.item_id, MediaType.PLAYLIST)

        # process playlist deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.remove_from_library(db_id, MediaType.PLAYLIST)
        # store ids in cache for next sync
        await self.mass.cache.set(cache_key, cur_db_ids)

    async def library_radios_sync(self, provider_id: str):
        """Sync library radios for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        cache_key = f"library_radios_{provider_id}"
        prev_db_ids = await self.mass.cache.get(cache_key, default=[])
        cur_db_ids = set()
        for item in await music_provider.get_library_radios():
            db_radio = await self.mass.music.get_radio(
                item.item_id, provider_id, lazy=False
            )
            cur_db_ids.add(db_radio.item_id)
            await self.mass.database.add_to_library(db_radio.item_id, MediaType.RADIO)
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.remove_from_library(
                    db_id,
                    MediaType.RADIO,
                )
        # store ids in cache for next sync
        await self.mass.cache.set(cache_key, cur_db_ids)

    async def __on_mass_event(
        self, event: EventType, event_details: EventDetails
    ) -> None:
        """Handle events on the eventbus."""
        if event == EventType.PROVIDER_AVAILABLE:
            # schedule library sync once the provider is available
            prov: MusicProvider = event_details
            await self.sync_provider(prov.id)
