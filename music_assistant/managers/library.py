"""LibraryManager: Orchestrates synchronisation of music providers into the library."""
import asyncio
import functools
import logging
import time
from typing import Any, List

from music_assistant.constants import EVENT_MUSIC_SYNC_STATUS, EVENT_PROVIDER_REGISTERED
from music_assistant.helpers.util import callback, run_periodic
from music_assistant.helpers.web import api_route
from music_assistant.models.media_types import (
    Album,
    Artist,
    MediaItem,
    MediaType,
    Playlist,
    Radio,
    Track,
)
from music_assistant.models.provider import ProviderType

LOGGER = logging.getLogger("music_manager")


def sync_task(desc):
    """Return decorator to report a sync task."""

    def wrapper(func):
        @functools.wraps(func)
        async def wrapped(*args):
            method_class = args[0]
            prov_id = args[1]
            # check if this sync task is not already running
            for sync_prov_id, sync_desc in method_class.running_sync_jobs:
                if sync_prov_id == prov_id and sync_desc == desc:
                    LOGGER.debug(
                        "Syncjob %s for provider %s is already running!", desc, prov_id
                    )
                    return
            LOGGER.debug("Start syncjob %s for provider %s.", desc, prov_id)
            sync_job = (prov_id, desc)
            method_class.running_sync_jobs.add(sync_job)
            method_class.mass.signal_event(
                EVENT_MUSIC_SYNC_STATUS, method_class.running_sync_jobs
            )
            await func(*args)
            LOGGER.debug("Finished syncing %s for provider %s", desc, prov_id)
            method_class.running_sync_jobs.remove(sync_job)
            method_class.mass.signal_event(
                EVENT_MUSIC_SYNC_STATUS, method_class.running_sync_jobs
            )

        return wrapped

    return wrapper


class LibraryManager:
    """Manage sync of musicproviders to library."""

    def __init__(self, mass):
        """Initialize class."""
        self.running_sync_jobs = set()
        self.mass = mass
        self.cache = mass.cache
        self.mass.add_event_listener(self.mass_event, EVENT_PROVIDER_REGISTERED)

    async def setup(self):
        """Async initialize of module."""
        # schedule sync task
        self.mass.add_job(self._music_providers_sync())

    @callback
    def mass_event(self, msg: str, msg_details: Any):
        """Handle message on eventbus."""
        if msg == EVENT_PROVIDER_REGISTERED:
            # schedule a sync task when a new provider registers
            provider = self.mass.get_provider(msg_details)
            if provider.type == ProviderType.MUSIC_PROVIDER:
                self.mass.add_job(self.music_provider_sync(msg_details))

    ################ GET MediaItems that are added in the library ################

    @api_route("library/artists")
    async def get_library_artists(self, orderby: str = "name") -> List[Artist]:
        """Return all library artists, optionally filtered by provider."""
        return await self.mass.database.get_library_artists(orderby=orderby)

    @api_route("library/albums")
    async def get_library_albums(self, orderby: str = "name") -> List[Album]:
        """Return all library albums, optionally filtered by provider."""
        return await self.mass.database.get_library_albums(orderby=orderby)

    @api_route("library/tracks")
    async def get_library_tracks(self, orderby: str = "name") -> List[Track]:
        """Return all library tracks, optionally filtered by provider."""
        return await self.mass.database.get_library_tracks(orderby=orderby)

    @api_route("library/playlists")
    async def get_library_playlists(self, orderby: str = "name") -> List[Playlist]:
        """Return all library playlists, optionally filtered by provider."""
        return await self.mass.database.get_library_playlists(orderby=orderby)

    @api_route("library/radios")
    async def get_library_radios(self, orderby: str = "name") -> List[Playlist]:
        """Return all library radios, optionally filtered by provider."""
        return await self.mass.database.get_library_radios(orderby=orderby)

    async def get_library_playlist_by_name(self, name: str) -> Playlist:
        """Get in-library playlist by name."""
        for playlist in await self.mass.music.get_library_playlists():
            if playlist.name == name:
                return playlist
        return None

    async def get_radio_by_name(self, name: str) -> Radio:
        """Get in-library radio by name."""
        for radio in await self.mass.music.get_library_radios():
            if radio.name == name:
                return radio
        return None

    @api_route("library/add")
    async def library_add(self, items: List[MediaItem]):
        """Add media item(s) to the library."""
        result = False
        for media_item in items:
            # add to provider's libraries
            for prov in media_item.provider_ids:
                provider = self.mass.get_provider(prov.provider)
                if provider:
                    result = await provider.library_add(
                        prov.item_id, media_item.media_type
                    )
            # mark as library item in internal db
            if media_item.provider == "database":
                await self.mass.database.add_to_library(
                    media_item.item_id, media_item.media_type, media_item.provider
                )
        return result

    @api_route("library/remove")
    async def library_remove(self, items: List[MediaItem]):
        """Remove media item(s) from the library."""
        result = False
        for media_item in items:
            # remove from provider's libraries
            for prov in media_item.provider_ids:
                provider = self.mass.get_provider(prov.provider)
                if provider:
                    result = await provider.library_remove(
                        prov.item_id, media_item.media_type
                    )
            # mark as library item in internal db
            if media_item.provider == "database":
                await self.mass.database.remove_from_library(
                    media_item.item_id, media_item.media_type, media_item.provider
                )
        return result

    @api_route("library/playlists/:db_playlist_id/tracks/add")
    async def add_playlist_tracks(self, db_playlist_id: int, tracks: List[Track]):
        """Add tracks to playlist - make sure we dont add duplicates."""
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.mass.music.get_playlist(db_playlist_id, "database")
        if not playlist or not playlist.is_editable:
            return False
        # playlist can only have one provider (for now)
        playlist_prov = next(iter(playlist.provider_ids))
        # grab all existing track ids in the playlist so we can check for duplicates
        cur_playlist_track_ids = set()
        for item in await self.mass.music.get_playlist_tracks(
            playlist_prov.item_id, playlist_prov.provider
        ):
            cur_playlist_track_ids.add(item.item_id)
            cur_playlist_track_ids.update({i.item_id for i in item.provider_ids})
        track_ids_to_add = set()
        for track in tracks:
            # check for duplicates
            already_exists = track.item_id in cur_playlist_track_ids
            for track_prov in track.provider_ids:
                if track_prov.item_id in cur_playlist_track_ids:
                    already_exists = True
            if already_exists:
                continue
            # we can only add a track to a provider playlist if track is available on that provider
            # this should all be handled in the frontend but these checks are here just to be safe
            # a track can contain multiple versions on the same provider
            # simply sort by quality and just add the first one (assuming track is still available)
            for track_version in sorted(
                track.provider_ids, key=lambda x: x.quality, reverse=True
            ):
                if track_version.provider == playlist_prov.provider:
                    track_ids_to_add.add(track_version.item_id)
                    break
                if playlist_prov.provider == "file":
                    # the file provider can handle uri's from all providers so simply add the uri
                    uri = f"{track_version.provider}://{track_version.item_id}"
                    track_ids_to_add.add(uri)
                    break
        # actually add the tracks to the playlist on the provider
        if track_ids_to_add:
            # invalidate cache
            playlist.checksum = str(time.time())
            await self.mass.database.update_playlist(playlist.item_id, playlist)
            # return result of the action on the provider
            provider = self.mass.get_provider(playlist_prov.provider)
            return await provider.add_playlist_tracks(
                playlist_prov.item_id, track_ids_to_add
            )
        return False

    @api_route("library/playlists/:db_playlist_id/tracks/remove")
    async def remove_playlist_tracks(self, db_playlist_id, tracks: List[Track]):
        """Remove tracks from playlist."""
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.mass.music.get_playlist(db_playlist_id, "database")
        if not playlist or not playlist.is_editable:
            return False
        # playlist can only have one provider (for now)
        prov_playlist = next(iter(playlist.provider_ids))
        track_ids_to_remove = set()
        for track in tracks:
            # a track can contain multiple versions on the same provider, remove all
            for track_provider in track.provider_ids:
                if track_provider.provider == prov_playlist.provider:
                    track_ids_to_remove.add(track_provider.item_id)
        # actually remove the tracks from the playlist on the provider
        if track_ids_to_remove:
            # invalidate cache
            playlist.checksum = str(time.time())
            await self.mass.database.update_playlist(playlist.item_id, playlist)
            provider = self.mass.get_provider(prov_playlist.provider)
            return await provider.remove_playlist_tracks(
                prov_playlist.item_id, track_ids_to_remove
            )

    @run_periodic(3600 * 3)
    async def _music_providers_sync(self):
        """Periodic sync of all music providers."""
        await asyncio.sleep(10)
        for prov in self.mass.get_providers(ProviderType.MUSIC_PROVIDER):
            await self.music_provider_sync(prov.id)

    async def music_provider_sync(self, prov_id: str):
        """
        Sync a music provider.

        param prov_id: {string} -- provider id to sync
        """
        provider = self.mass.get_provider(prov_id)
        if not provider:
            return
        if MediaType.Album in provider.supported_mediatypes:
            await self.library_albums_sync(prov_id)
        if MediaType.Track in provider.supported_mediatypes:
            await self.library_tracks_sync(prov_id)
        if MediaType.Artist in provider.supported_mediatypes:
            await self.library_artists_sync(prov_id)
        if MediaType.Playlist in provider.supported_mediatypes:
            await self.library_playlists_sync(prov_id)
        if MediaType.Radio in provider.supported_mediatypes:
            await self.library_radios_sync(prov_id)

    @sync_task("artists")
    async def library_artists_sync(self, provider_id: str):
        """Sync library artists for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        cache_key = f"library_artists_{provider_id}"
        prev_db_ids = await self.mass.cache.get(cache_key, default=[])
        cur_db_ids = set()
        for item in await music_provider.get_library_artists():
            db_item = await self.mass.music.get_artist(
                item.item_id, provider_id, lazy=False
            )
            cur_db_ids.add(db_item.item_id)
            if not db_item.in_library:
                await self.mass.database.add_to_library(
                    db_item.item_id, MediaType.Artist, provider_id
                )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.remove_from_library(
                    db_id, MediaType.Artist, provider_id
                )
        # store ids in cache for next sync
        await self.mass.cache.set(cache_key, cur_db_ids)

    @sync_task("albums")
    async def library_albums_sync(self, provider_id: str):
        """Sync library albums for given provider."""
        music_provider = self.mass.get_provider(provider_id)
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
                    db_album.item_id, MediaType.Album, provider_id
                )
            # precache album tracks
            await self.mass.music.get_album_tracks(item.item_id, provider_id)
        # process album deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.remove_from_library(
                    db_id, MediaType.Album, provider_id
                )
        # store ids in cache for next sync
        await self.mass.cache.set(cache_key, cur_db_ids)

    @sync_task("tracks")
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
                    db_item.item_id, MediaType.Track, provider_id
                )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.remove_from_library(
                    db_id, MediaType.Track, provider_id
                )
        # store ids in cache for next sync
        await self.mass.cache.set(cache_key, cur_db_ids)

    @sync_task("playlists")
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
                # precache playlist tracks
                for playlist_track in await self.mass.music.get_playlist_tracks(
                    playlist.item_id, provider_id
                ):
                    # try to find substitutes for unavailable tracks with matching technique
                    if not playlist_track.available:
                        await self.mass.music.get_track(
                            playlist_track.item_id,
                            playlist_track.provider,
                            playlist_track,
                        )
            cur_db_ids.add(db_item.item_id)
            await self.mass.database.add_to_library(
                db_item.item_id, MediaType.Playlist, playlist.provider
            )

        # process playlist deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.remove_from_library(
                    db_id, MediaType.Playlist, provider_id
                )
        # store ids in cache for next sync
        await self.mass.cache.set(cache_key, cur_db_ids)

    @sync_task("radios")
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
            await self.mass.database.add_to_library(
                db_radio.item_id, MediaType.Radio, provider_id
            )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.remove_from_library(
                    db_id, MediaType.Radio, provider_id
                )
        # store ids in cache for next sync
        await self.mass.cache.set(cache_key, cur_db_ids)
