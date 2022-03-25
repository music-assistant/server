"""LibraryController: Orchestrates synchronisation of music providers into the library."""

import time
from typing import List

from music_assistant.constants import EventType
from music_assistant.helpers.typing import EventDetails, MusicAssistant
from music_assistant.helpers.web import api_route
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

    @api_route("library", method="POST")
    async def library_add_items(self, items: List[MediaItem]) -> List[TaskInfo]:
        """
        Add media item(s) to the library.

        Creates background tasks to process the action.
        """
        result = []
        for media_item in items:
            job_desc = f"Add {media_item.uri} to library"
            result.append(
                self.mass.tasks.add(job_desc, self.library_add_item, media_item)
            )
        return result

    async def library_add_item(self, item: MediaItem):
        """Add media item to the library."""
        # make sure we have a valid full item
        item = await self.mass.music.get_item(
            item.item_id, item.provider, item.media_type, lazy=False
        )
        # add to provider's libraries
        for prov in item.provider_ids:
            provider = self.mass.get_provider(prov.provider)
            if provider:
                await provider.library_add(prov.item_id, item.media_type)
        # mark as library item in internal db
        await self.mass.database.add_to_library(item.item_id, item.media_type)

    @api_route("library", method="DELETE")
    async def library_remove_items(self, items: List[MediaItem]) -> List[TaskInfo]:
        """
        Remove media item(s) from the library.

        Creates background tasks to process the action.
        """
        result = []
        for media_item in items:
            job_desc = f"Remove {media_item.uri} from library"
            result.append(
                self.mass.tasks.add(job_desc, self.library_remove_item, media_item)
            )
        return result

    async def library_remove_item(self, item: MediaItem) -> None:
        """Remove media item(s) from the library."""
        # remove from provider's libraries
        for prov in item.provider_ids:
            provider = self.mass.get_provider(prov.provider)
            if provider:
                await provider.library_remove(prov.item_id, item.media_type)
        # mark as library item in internal db
        if item.provider == "database":
            await self.mass.database.remove_from_library(item.item_id, item.media_type)

    @api_route("library/playlists/{db_playlist_id}/tracks", method="POST")
    async def add_playlist_tracks(
        self, db_playlist_id: int, tracks: List[Track]
    ) -> List[TaskInfo]:
        """Add multiple tracks to playlist. Creates background tasks to process the action."""
        result = []
        playlist = await self.mass.music.get_playlist(db_playlist_id, "database")
        if not playlist:
            raise RuntimeError("Playlist %s not found" % db_playlist_id)
        if not playlist.is_editable:
            raise RuntimeError("Playlist %s is not editable" % playlist.name)
        for track in tracks:
            job_desc = f"Add track {track.uri} to playlist {playlist.uri}"
            result.append(
                self.mass.tasks.add(
                    job_desc, self.add_playlist_track, db_playlist_id, track
                )
            )
        return result

    async def add_playlist_track(self, db_playlist_id: int, track: Track) -> None:
        """Add track to playlist - make sure we dont add duplicates."""
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.mass.music.get_playlist(db_playlist_id, "database")
        if not playlist:
            raise RuntimeError("Playlist %s not found" % db_playlist_id)
        if not playlist.is_editable:
            raise RuntimeError("Playlist %s is not editable" % playlist.name)
        # make sure we have recent full track details
        track = await self.mass.music.get_track(
            track.item_id, track.provider, refresh=True, lazy=False
        )
        # a playlist can only have one provider (for now)
        playlist_prov = next(iter(playlist.provider_ids))
        # grab all existing track ids in the playlist so we can check for duplicates
        cur_playlist_track_ids = set()
        for item in await self.mass.music.get_playlist_tracks(
            playlist_prov.item_id, playlist_prov.provider
        ):
            cur_playlist_track_ids.update(
                {
                    i.item_id
                    for i in item.provider_ids
                    if i.provider == playlist_prov.provider
                }
            )
        # check for duplicates
        for track_prov in track.provider_ids:
            if (
                track_prov.provider == playlist_prov.provider
                and track_prov.item_id in cur_playlist_track_ids
            ):
                raise RuntimeError(
                    "Track already exists in playlist %s" % playlist.name
                )
        # add track to playlist
        # we can only add a track to a provider playlist if track is available on that provider
        # a track can contain multiple versions on the same provider
        # simply sort by quality and just add the first one (assuming track is still available)
        track_id_to_add = None
        for track_version in sorted(
            track.provider_ids, key=lambda x: x.quality, reverse=True
        ):
            if not track.available:
                continue
            if track_version.provider == playlist_prov.provider:
                track_id_to_add = track_version.item_id
                break
            if playlist_prov.provider == "file":
                # the file provider can handle uri's from all providers so simply add the uri
                track_id_to_add = track.uri
                break
        if not track_id_to_add:
            raise RuntimeError(
                "Track is not available on provider %s" % playlist_prov.provider
            )
        # actually add the tracks to the playlist on the provider
        # invalidate cache
        playlist.checksum = str(time.time())
        await self.mass.database.update_playlist(playlist.item_id, playlist)
        # return result of the action on the provider
        provider = self.mass.get_provider(playlist_prov.provider)
        return await provider.add_playlist_tracks(
            playlist_prov.item_id, [track_id_to_add]
        )

    @api_route("library/playlists/{db_playlist_id}/tracks", method="DELETE")
    async def remove_playlist_tracks(
        self, db_playlist_id: int, tracks: List[Track]
    ) -> List[TaskInfo]:
        """Remove multiple tracks from playlist. Creates background tasks to process the action."""
        result = []
        playlist = await self.mass.music.get_playlist(db_playlist_id, "database")
        if not playlist:
            raise RuntimeError("Playlist %s not found" % db_playlist_id)
        if not playlist.is_editable:
            raise RuntimeError("Playlist %s is not editable" % playlist.name)
        for track in tracks:
            job_desc = f"Remove track {track.uri} from playlist {playlist.uri}"
            result.append(
                self.mass.tasks.add(
                    job_desc, self.remove_playlist_track, db_playlist_id, track
                )
            )
        return result

    async def remove_playlist_track(self, db_playlist_id, track: Track) -> None:
        """Remove track from playlist."""
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.mass.music.get_playlist(db_playlist_id, "database")
        if not playlist or not playlist.is_editable:
            return False
        # playlist can only have one provider (for now)
        prov_playlist = next(iter(playlist.provider_ids))
        track_ids_to_remove = set()
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

    async def sync_provider(self, prov_id: str):
        """
        Setup library sync for a provider.

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
                    db_item.item_id, MediaType.ARTIST
                )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.remove_from_library(db_id, MediaType.ARTIST)
        # store ids in cache for next sync
        await self.mass.cache.set(cache_key, cur_db_ids)

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
