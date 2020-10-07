"""MusicManager: Orchestrates all data from music providers and sync to internal database."""
# pylint: disable=too-many-lines
import asyncio
import base64
import functools
import logging
import os
import time
from typing import Any, List, Optional

import aiohttp
from music_assistant.constants import EVENT_MUSIC_SYNC_STATUS, EVENT_PROVIDER_REGISTERED
from music_assistant.helpers.cache import async_cached, async_cached_generator
from music_assistant.helpers.encryption import async_encrypt_string
from music_assistant.helpers.musicbrainz import MusicBrainz
from music_assistant.helpers.util import callback, compare_strings, run_periodic
from music_assistant.models.media_types import (
    Album,
    Artist,
    ExternalId,
    MediaItem,
    MediaType,
    Playlist,
    Radio,
    SearchResult,
    Track,
)
from music_assistant.models.provider import MusicProvider, ProviderType
from music_assistant.models.streamdetails import ContentType, StreamDetails, StreamType
from PIL import Image

LOGGER = logging.getLogger("music_manager")


def sync_task(desc):
    """Return decorator to report a sync task."""

    def wrapper(func):
        @functools.wraps(func)
        async def async_wrapped(*args):
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
            method_class.running_sync_jobs.append(sync_job)
            method_class.mass.signal_event(
                EVENT_MUSIC_SYNC_STATUS, method_class.running_sync_jobs
            )
            await func(*args)
            LOGGER.info("Finished syncing %s for provider %s", desc, prov_id)
            method_class.running_sync_jobs.remove(sync_job)
            method_class.mass.signal_event(
                EVENT_MUSIC_SYNC_STATUS, method_class.running_sync_jobs
            )

        return async_wrapped

    return wrapper


class MusicManager:
    """Several helpers around the musicproviders."""

    def __init__(self, mass):
        """Initialize class."""
        self.running_sync_jobs = []
        self.mass = mass
        self.cache = mass.cache
        self.musicbrainz = MusicBrainz(mass)
        self._match_jobs = []
        self.mass.add_event_listener(self.mass_event, [EVENT_PROVIDER_REGISTERED])

    async def async_setup(self):
        """Async initialize of module."""
        # schedule sync task
        self.mass.add_job(self.__async_music_providers_sync())

    @property
    def providers(self) -> List[MusicProvider]:
        """Return all providers of type musicprovider."""
        return self.mass.get_providers(ProviderType.MUSIC_PROVIDER)

    @callback
    def mass_event(self, msg: str, msg_details: Any):
        """Handle message on eventbus."""
        if msg == EVENT_PROVIDER_REGISTERED:
            # schedule a sync task when a new provider registers
            provider = self.mass.get_provider(msg_details)
            if provider.type == ProviderType.MUSIC_PROVIDER:
                self.mass.add_job(self.async_music_provider_sync(msg_details))

    ################ GET MediaItem(s) by id and provider #################

    async def async_get_item(
        self, item_id: str, provider_id: str, media_type: MediaType, lazy: bool = True
    ):
        """Get single music item by id and media type."""
        if media_type == MediaType.Artist:
            return await self.async_get_artist(item_id, provider_id, lazy)
        if media_type == MediaType.Album:
            return await self.async_get_album(item_id, provider_id, lazy)
        if media_type == MediaType.Track:
            return await self.async_get_track(item_id, provider_id, lazy)
        if media_type == MediaType.Playlist:
            return await self.async_get_playlist(item_id, provider_id)
        if media_type == MediaType.Radio:
            return await self.async_get_radio(item_id, provider_id)
        return None

    async def async_get_artist(
        self, item_id: str, provider_id: str, lazy: bool = True
    ) -> Artist:
        """Return artist details for the given provider artist id."""
        assert item_id and provider_id
        db_id = await self.mass.database.async_get_database_id(
            provider_id, item_id, MediaType.Artist
        )
        if db_id is None:
            # artist not yet in local database so fetch details
            provider = self.mass.get_provider(provider_id)
            if not provider.available:
                return None
            cache_key = f"{provider_id}.get_artist.{item_id}"
            artist = await async_cached(
                self.cache, cache_key, provider.async_get_artist(item_id)
            )
            if not artist:
                raise Exception(
                    "Artist %s not found on provider %s" % (item_id, provider_id)
                )
            if lazy:
                self.mass.add_job(self.__async_add_artist(artist))
                artist.is_lazy = True
                return artist
            db_id = await self.__async_add_artist(artist)
        return await self.mass.database.async_get_artist(db_id)

    async def async_get_album(
        self,
        item_id: str,
        provider_id: str,
        lazy=True,
        album_details: Optional[Album] = None,
    ) -> Album:
        """Return album details for the given provider album id."""
        assert item_id and provider_id
        db_id = await self.mass.database.async_get_database_id(
            provider_id, item_id, MediaType.Album
        )
        if db_id is None:
            # album not yet in local database so fetch details
            if not album_details:
                provider = self.mass.get_provider(provider_id)
                if not provider.available:
                    return None
                cache_key = f"{provider_id}.get_album.{item_id}"
                album_details = await async_cached(
                    self.cache, cache_key, provider.async_get_album(item_id)
                )
            if not album_details:
                raise Exception(
                    "Album %s not found on provider %s" % (item_id, provider_id)
                )
            if lazy:
                self.mass.add_job(self.__async_add_album(album_details))
                album_details.is_lazy = True
                return album_details
            db_id = await self.__async_add_album(album_details)
        return await self.mass.database.async_get_album(db_id)

    async def async_get_track(
        self,
        item_id: str,
        provider_id: str,
        lazy: bool = True,
        track_details: Track = None,
        refresh: bool = False,
    ) -> Track:
        """Return track details for the given provider track id."""
        assert item_id and provider_id
        db_id = await self.mass.database.async_get_database_id(
            provider_id, item_id, MediaType.Track
        )
        if db_id and refresh:
            # in some cases (e.g. at playback time or requesting full track info)
            # it's useful to have the track refreshed from the provider instead of
            # the database cache to make sure that the track is available and perhaps
            # another or a higher quality version is available.
            if lazy:
                self.mass.add_job(self.__async_match_track(db_id))
            else:
                await self.__async_match_track(db_id)
        if not db_id:
            # track not yet in local database so fetch details
            if not track_details:
                provider = self.mass.get_provider(provider_id)
                if not provider.available:
                    return None
                cache_key = f"{provider_id}.get_track.{item_id}"
                track_details = await async_cached(
                    self.cache, cache_key, provider.async_get_track(item_id)
                )
            if not track_details:
                raise Exception(
                    "Track %s not found on provider %s" % (item_id, provider_id)
                )
            if lazy:
                self.mass.add_job(self.__async_add_track(track_details))
                track_details.is_lazy = True
                return track_details
            db_id = await self.__async_add_track(track_details)
        return await self.mass.database.async_get_track(db_id, fulldata=True)

    async def async_get_playlist(self, item_id: str, provider_id: str) -> Playlist:
        """Return playlist details for the given provider playlist id."""
        assert item_id and provider_id
        db_id = await self.mass.database.async_get_database_id(
            provider_id, item_id, MediaType.Playlist
        )
        if db_id is None:
            # item not yet in local database so fetch and store details
            provider = self.mass.get_provider(provider_id)
            if not provider.available:
                return None
            item_details = await provider.async_get_playlist(item_id)
            db_id = await self.mass.database.async_add_playlist(item_details)
        return await self.mass.database.async_get_playlist(db_id)

    async def async_get_radio(self, item_id: str, provider_id: str) -> Radio:
        """Return radio details for the given provider playlist id."""
        assert item_id and provider_id
        db_id = await self.mass.database.async_get_database_id(
            provider_id, item_id, MediaType.Radio
        )
        if db_id is None:
            # item not yet in local database so fetch and store details
            provider = self.mass.get_provider(provider_id)
            if not provider.available:
                return None
            item_details = await provider.async_get_radio(item_id)
            db_id = await self.mass.database.async_add_radio(item_details)
        return await self.mass.database.async_get_radio(db_id)

    async def async_get_album_tracks(
        self, item_id: str, provider_id: str
    ) -> List[Track]:
        """Return album tracks for the given provider album id. Generator."""
        assert item_id and provider_id
        album = await self.async_get_album(item_id, provider_id)
        if album.provider == "database":
            # album tracks are not stored in db, we always fetch them (cached) from the provider.
            provider_id = album.provider_ids[0].provider
            item_id = album.provider_ids[0].item_id
        provider = self.mass.get_provider(provider_id)
        cache_key = f"{provider_id}.album_tracks.{item_id}"
        async with self.mass.database.db_conn() as db_conn:
            async for item in async_cached_generator(
                self.cache, cache_key, provider.async_get_album_tracks(item_id)
            ):
                if not item:
                    continue
                db_id = await self.mass.database.async_get_database_id(
                    item.provider, item.item_id, MediaType.Track, db_conn
                )
                if db_id:
                    # return database track instead if we have a match
                    track = await self.mass.database.async_get_track(
                        db_id, fulldata=False, db_conn=db_conn
                    )
                    track.disc_number = item.disc_number
                    track.track_number = item.track_number
                else:
                    track = item
                if not track.album:
                    track.album = album
                yield track

    async def async_get_album_versions(
        self, item_id: str, provider_id: str
    ) -> List[Album]:
        """Return all versions of an album we can find on all providers. Generator."""
        album = await self.async_get_album(item_id, provider_id)
        provider_ids = [
            item.id for item in self.mass.get_providers(ProviderType.MUSIC_PROVIDER)
        ]
        search_query = f"{album.artist.name} - {album.name}"
        for prov_id in provider_ids:
            provider_result = await self.async_search_provider(
                search_query, prov_id, [MediaType.Album], 25
            )
            for item in provider_result.albums:
                if compare_strings(item.artist.name, album.artist.name):
                    yield item

    async def async_get_track_versions(
        self, item_id: str, provider_id: str
    ) -> List[Track]:
        """Return all versions of a track we can find on all providers. Generator."""
        track = await self.async_get_track(item_id, provider_id)
        provider_ids = [
            item.id for item in self.mass.get_providers(ProviderType.MUSIC_PROVIDER)
        ]
        search_query = f"{track.artists[0].name} - {track.name}"
        for prov_id in provider_ids:
            provider_result = await self.async_search_provider(
                search_query, prov_id, [MediaType.Track], 25
            )
            for item in provider_result.tracks:
                if not compare_strings(item.name, track.name):
                    continue
                for artist in item.artists:
                    # artist must match
                    if compare_strings(artist.name, track.artists[0].name):
                        yield item
                        break

    async def async_get_playlist_tracks(
        self, item_id: str, provider_id: str
    ) -> List[Track]:
        """Return playlist tracks for the given provider playlist id. Generator."""
        assert item_id and provider_id
        if provider_id == "database":
            # playlist tracks are not stored in db, we always fetch them (cached) from the provider.
            db_item = await self.mass.database.async_get_playlist(item_id)
            provider_id = db_item.provider_ids[0].provider
            item_id = db_item.provider_ids[0].item_id
        provider = self.mass.get_provider(provider_id)
        playlist = await provider.async_get_playlist(item_id)
        cache_checksum = playlist.checksum
        cache_key = f"{provider_id}.playlist_tracks.{item_id}"
        pos = 0
        async with self.mass.database.db_conn() as db_conn:
            async for item in async_cached_generator(
                self.cache,
                cache_key,
                provider.async_get_playlist_tracks(item_id),
                checksum=cache_checksum,
            ):
                if not item:
                    continue
                assert item.item_id and item.provider
                db_id = await self.mass.database.async_get_database_id(
                    item.provider, item.item_id, MediaType.Track, db_conn=db_conn
                )
                if db_id:
                    # return database track instead if we have a match
                    item = await self.mass.database.async_get_track(
                        db_id, fulldata=False, db_conn=db_conn
                    )
                item.position = pos
                pos += 1
                yield item

    async def async_get_artist_toptracks(
        self, artist_id: str, provider_id: str
    ) -> List[Track]:
        """Return top tracks for an artist. Generator."""
        async with self.mass.database.db_conn() as db_conn:
            if provider_id == "database":
                # tracks from all providers
                item_ids = []
                artist = await self.mass.database.async_get_artist(
                    artist_id, True, db_conn=db_conn
                )
                for prov_id in artist.provider_ids:
                    provider = self.mass.get_provider(prov_id.provider)
                    if (
                        not provider
                        or MediaType.Track not in provider.supported_mediatypes
                    ):
                        continue
                    async for item in self.async_get_artist_toptracks(
                        prov_id.item_id, prov_id.provider
                    ):
                        if item.item_id not in item_ids:
                            yield item
                            item_ids.append(item.item_id)
            else:
                # items from provider
                provider = self.mass.get_provider(provider_id)
                cache_key = f"{provider_id}.artist_toptracks.{artist_id}"
                async for item in async_cached_generator(
                    self.cache,
                    cache_key,
                    provider.async_get_artist_toptracks(artist_id),
                ):
                    if item:
                        assert item.item_id and item.provider
                        db_id = await self.mass.database.async_get_database_id(
                            item.provider,
                            item.item_id,
                            MediaType.Track,
                            db_conn=db_conn,
                        )
                        if db_id:
                            # return database track instead if we have a match
                            yield await self.mass.database.async_get_track(
                                db_id, fulldata=False, db_conn=db_conn
                            )
                        else:
                            yield item

    async def async_get_artist_albums(
        self, artist_id: str, provider_id: str
    ) -> List[Album]:
        """Return (all) albums for an artist. Generator."""
        async with self.mass.database.db_conn() as db_conn:
            if provider_id == "database":
                # albums from all providers
                item_ids = []
                artist = await self.mass.database.async_get_artist(
                    artist_id, True, db_conn=db_conn
                )
                for prov_id in artist.provider_ids:
                    provider = self.mass.get_provider(prov_id.provider)
                    if (
                        not provider
                        or MediaType.Album not in provider.supported_mediatypes
                    ):
                        continue
                    async for item in self.async_get_artist_albums(
                        prov_id.item_id, prov_id.provider
                    ):
                        if item.item_id not in item_ids:
                            yield item
                            item_ids.append(item.item_id)
            else:
                # items from provider
                provider = self.mass.get_provider(provider_id)
                cache_key = f"{provider_id}.artist_albums.{artist_id}"
                async for item in async_cached_generator(
                    self.cache, cache_key, provider.async_get_artist_albums(artist_id)
                ):
                    assert item.item_id and item.provider
                    db_id = await self.mass.database.async_get_database_id(
                        item.provider, item.item_id, MediaType.Album, db_conn=db_conn
                    )
                    if db_id:
                        # return database album instead if we have a match
                        yield await self.mass.database.async_get_album(
                            db_id, db_conn=db_conn
                        )
                    else:
                        yield item

    ################ GET MediaItems that are added in the library ################

    async def async_get_library_artists(
        self, orderby: str = "name", provider_filter: str = None
    ) -> List[Artist]:
        """Return all library artists, optionally filtered by provider. Generator."""
        async for item in self.mass.database.async_get_library_artists(
            provider_id=provider_filter, orderby=orderby
        ):
            yield item

    async def async_get_library_albums(
        self, orderby: str = "name", provider_filter: str = None
    ) -> List[Album]:
        """Return all library albums, optionally filtered by provider. Generator."""
        async for item in self.mass.database.async_get_library_albums(
            provider_id=provider_filter, orderby=orderby
        ):
            yield item

    async def async_get_library_tracks(
        self, orderby: str = "name", provider_filter: str = None
    ) -> List[Track]:
        """Return all library tracks, optionally filtered by provider. Generator."""
        async for item in self.mass.database.async_get_library_tracks(
            provider_id=provider_filter, orderby=orderby
        ):
            yield item

    async def async_get_library_playlists(
        self, orderby: str = "name", provider_filter: str = None
    ) -> List[Playlist]:
        """Return all library playlists, optionally filtered by provider. Generator."""
        async for item in self.mass.database.async_get_library_playlists(
            provider_id=provider_filter, orderby=orderby
        ):
            yield item

    async def async_get_library_radios(
        self, orderby: str = "name", provider_filter: str = None
    ) -> List[Playlist]:
        """Return all library radios, optionally filtered by provider. Generator."""
        async for item in self.mass.database.async_get_library_radios(
            provider_id=provider_filter, orderby=orderby
        ):
            yield item

    ################ ADD MediaItem(s) to database helpers ################

    async def __async_add_artist(self, artist: Artist) -> int:
        """Add artist to local db and return the new database id."""
        musicbrainz_id = artist.external_ids.get(ExternalId.MUSICBRAINZ)
        if not musicbrainz_id:
            musicbrainz_id = await self.__async_get_artist_musicbrainz_id(artist)
        # grab additional metadata
        artist.external_ids[ExternalId.MUSICBRAINZ] = musicbrainz_id
        artist.metadata = await self.mass.metadata.async_get_artist_metadata(
            musicbrainz_id, artist.metadata
        )
        db_id = await self.mass.database.async_add_artist(artist)
        # also fetch same artist on all providers
        await self.__async_match_artist(db_id)
        return db_id

    async def __async_add_album(self, album: Album) -> int:
        """Add album to local db and return the new database id."""
        # we need to fetch album artist too
        album.artist = await self.async_get_artist(
            album.artist.item_id, album.artist.provider, lazy=False
        )
        db_id = await self.mass.database.async_add_album(album)
        # also fetch same album on all providers
        await self.__async_match_album(db_id)
        return db_id

    async def __async_add_track(
        self, track: Track, album_id: Optional[str] = None
    ) -> int:
        """Add track to local db and return the new database id."""
        track_artists = []
        # we need to fetch track artists too
        for track_artist in track.artists:
            db_track_artist = await self.async_get_artist(
                track_artist.item_id, track_artist.provider, lazy=False
            )
            if db_track_artist:
                track_artists.append(db_track_artist)
        track.artists = track_artists
        # fetch album details - prefer optional provided album_id
        if album_id:
            album_details = await self.async_get_album(
                album_id, track.provider, lazy=False
            )
            if album_details:
                track.album = album_details
        # make sure we have a database album
        assert track.album
        if track.album.provider != "database":
            track.album = await self.async_get_album(
                track.album.item_id, track.provider, lazy=False
            )
        db_id = await self.mass.database.async_add_track(track)
        # also fetch same track on all providers (will also get other quality versions)
        await self.__async_match_track(db_id)
        return db_id

    async def __async_get_artist_musicbrainz_id(self, artist: Artist):
        """Fetch musicbrainz id by performing search using the artist name, albums and tracks."""
        # try with album first
        async for lookup_album in self.async_get_artist_albums(
            artist.item_id, artist.provider
        ):
            if not lookup_album:
                continue
            musicbrainz_id = await self.musicbrainz.async_get_mb_artist_id(
                artist.name,
                albumname=lookup_album.name,
                album_upc=lookup_album.external_ids.get(ExternalId.UPC),
            )
            if musicbrainz_id:
                return musicbrainz_id
        # fallback to track
        async for lookup_track in self.async_get_artist_toptracks(
            artist.item_id, artist.provider
        ):
            if not lookup_track:
                continue
            musicbrainz_id = await self.musicbrainz.async_get_mb_artist_id(
                artist.name,
                trackname=lookup_track.name,
                track_isrc=lookup_track.external_ids.get(ExternalId.ISRC),
            )
            if musicbrainz_id:
                return musicbrainz_id
        # lookup failed, use the shitty workaround to use the name as id.
        LOGGER.warning("Unable to get musicbrainz ID for artist %s !", artist.name)
        return artist.name

    async def __async_match_artist(self, db_artist_id: int):
        """
        Try to find matching artists on all providers for the provided (database) artist_id.

        This is used to link objects of different providers together.
            :attrib db_artist_id: Database artist_id.
        """
        match_job_id = f"artist.{db_artist_id}"
        if match_job_id in self._match_jobs:
            return
        self._match_jobs.append(match_job_id)
        artist = await self.mass.database.async_get_artist(db_artist_id)
        cur_providers = [item.provider for item in artist.provider_ids]
        for provider in self.mass.get_providers(ProviderType.MUSIC_PROVIDER):
            if provider.id in cur_providers:
                continue
            LOGGER.debug(
                "Trying to match artist %s on provider %s", artist.name, provider.name
            )
            match_found = False
            # try to get a match with some reference albums of this artist
            async for ref_album in self.async_get_artist_albums(
                artist.item_id, artist.provider
            ):
                if match_found:
                    break
                searchstr = "%s - %s" % (artist.name, ref_album.name)
                search_result = await self.async_search_provider(
                    searchstr, provider.id, [MediaType.Album], limit=5
                )
                for strictness in [True, False]:
                    if match_found:
                        break
                    for search_result_item in search_result.albums:
                        if not search_result_item:
                            continue
                        if not compare_strings(
                            search_result_item.name, ref_album.name, strict=strictness
                        ):
                            continue
                        # double safety check - artist must match exactly !
                        if not compare_strings(
                            search_result_item.artist.name,
                            artist.name,
                            strict=strictness,
                        ):
                            continue
                        # just load this item in the database where it will be strictly matched
                        await self.async_get_artist(
                            search_result_item.artist.item_id,
                            search_result_item.artist.provider,
                            lazy=False,
                        )
                        match_found = True
                        break
            # try to get a match with some reference tracks of this artist
            if not match_found:
                async for search_track in self.async_get_artist_toptracks(
                    artist.item_id, artist.provider
                ):
                    if match_found:
                        break
                    searchstr = "%s - %s" % (artist.name, search_track.name)
                    search_results = await self.async_search_provider(
                        searchstr, provider.id, [MediaType.Track], limit=5
                    )
                    for strictness in [True, False]:
                        if match_found:
                            break
                        for search_result_item in search_results.tracks:
                            if match_found:
                                break
                            if not search_result_item:
                                continue
                            if not compare_strings(
                                search_result_item.name,
                                search_track.name,
                                strict=strictness,
                            ):
                                continue
                            # double safety check - artist must match exactly !
                            for match_artist in search_result_item.artists:
                                if not compare_strings(
                                    match_artist.name, artist.name, strict=strictness
                                ):
                                    continue
                                # load this item in the database where it will be strictly matched
                                await self.async_get_artist(
                                    match_artist.item_id,
                                    match_artist.provider,
                                    lazy=False,
                                )
                                match_found = True
                                break
            if match_found:
                LOGGER.debug(
                    "Found match for Artist %s on provider %s",
                    artist.name,
                    provider.name,
                )
            else:
                LOGGER.warning(
                    "Could not find match for Artist %s on provider %s",
                    artist.name,
                    provider.name,
                )

    async def __async_match_album(self, db_album_id: int):
        """
        Try to find matching album on all providers for the provided (database) album_id.

        This is used to link objects of different providers/qualities together.
            :attrib db_album_id: Database album_id.
        """
        match_job_id = f"album.{db_album_id}"
        if match_job_id in self._match_jobs:
            return
        self._match_jobs.append(match_job_id)
        album = await self.mass.database.async_get_album(db_album_id)
        cur_providers = [item.provider for item in album.provider_ids]
        providers = self.mass.get_providers(ProviderType.MUSIC_PROVIDER)
        for provider in providers:
            if provider.id in cur_providers:
                continue
            LOGGER.debug(
                "Trying to match album %s on provider %s", album.name, provider.name
            )
            match_found = False
            searchstr = "%s - %s" % (album.artist.name, album.name)
            if album.version:
                searchstr += " " + album.version
            search_result = await self.async_search_provider(
                searchstr, provider.id, [MediaType.Album], limit=5
            )
            for search_result_item in search_result.albums:
                if not search_result_item:
                    continue
                if search_result_item.album_type != album.album_type:
                    continue
                if not (
                    compare_strings(search_result_item.name, album.name)
                    and compare_strings(search_result_item.version, album.version)
                ):
                    continue
                if not compare_strings(
                    search_result_item.artist.name, album.artist.name, strict=False
                ):
                    continue
                # just load this item in the database where it will be strictly matched
                await self.async_get_album(
                    search_result_item.item_id,
                    provider.id,
                    lazy=False,
                    album_details=search_result_item,
                )
                match_found = True
            if match_found:
                LOGGER.debug(
                    "Found match for Album %s on provider %s", album.name, provider.name
                )
            else:
                LOGGER.warning(
                    "Could not find match for Album %s on provider %s",
                    album.name,
                    provider.name,
                )

    async def __async_match_track(self, db_track_id: int):
        """
        Try to find matching track on all providers for the provided (database) track_id.

        This is used to link objects of different providers/qualities together.
            :attrib db_track_id: Database track_id.
        """
        match_job_id = f"track.{db_track_id}"
        if match_job_id in self._match_jobs:
            return
        self._match_jobs.append(match_job_id)
        track = await self.mass.database.async_get_track(db_track_id, fulldata=False)
        for provider in self.mass.get_providers(ProviderType.MUSIC_PROVIDER):
            LOGGER.debug(
                "Trying to match track %s on provider %s", track.name, provider.name
            )
            match_found = False
            searchstr = "%s - %s" % (track.artists[0].name, track.name)
            if track.version:
                searchstr += " " + track.version
            search_result = await self.async_search_provider(
                searchstr, provider.id, [MediaType.Track], limit=10
            )
            for search_result_item in search_result.tracks:
                if (
                    not search_result_item
                    or not search_result_item.name
                    or not search_result_item.album
                ):
                    continue
                if not (
                    compare_strings(search_result_item.name, track.name)
                    and compare_strings(search_result_item.version, track.version)
                ):
                    continue
                # double safety check - artist must match exactly !
                artist_match_found = False
                for artist in track.artists:
                    if artist_match_found:
                        break
                    for search_item_artist in search_result_item.artists:
                        if not compare_strings(
                            artist.name, search_item_artist.name, strict=False
                        ):
                            continue
                        # just load this item in the database where it will be strictly matched
                        await self.async_get_track(
                            search_item_artist.item_id,
                            provider.id,
                            lazy=False,
                            track_details=search_result_item,
                        )
                        match_found = True
                        artist_match_found = True
                        break
            if match_found:
                LOGGER.debug(
                    "Found match for Track %s on provider %s", track.name, provider.name
                )
            else:
                LOGGER.warning(
                    "Could not find match for Track %s on provider %s",
                    track.name,
                    provider.name,
                )

    ################ Various convenience/helper methods ################

    async def async_get_library_playlist_by_name(self, name: str) -> Playlist:
        """Get in-library playlist by name."""
        async for playlist in self.async_get_library_playlists():
            if playlist.name == name:
                return playlist
        return None

    async def async_get_radio_by_name(self, name: str) -> Radio:
        """Get in-library radio by name."""
        async for radio in self.async_get_library_radios():
            if radio.name == name:
                return radio
        return None

    async def async_search_provider(
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
            return await self.mass.database.async_search(search_query, media_types)
        provider = self.mass.get_provider(provider_id)
        cache_key = f"{provider_id}.search.{search_query}.{media_types}.{limit}"
        return await async_cached(
            self.cache,
            cache_key,
            provider.async_search(search_query, media_types, limit),
        )

    async def async_global_search(
        self, search_query, media_types: List[MediaType], limit: int = 10
    ) -> SearchResult:
        """
        Perform global search for media items on all providers.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: number of items to return in the search (per type).
        """
        result = SearchResult([], [], [], [], [])
        # include results from all music providers
        provider_ids = ["database"] + [
            item.id for item in self.mass.get_providers(ProviderType.MUSIC_PROVIDER)
        ]
        for provider_id in provider_ids:
            provider_result = await self.async_search_provider(
                search_query, provider_id, media_types, limit
            )
            result.artists += provider_result.artists
            result.albums += provider_result.albums
            result.tracks += provider_result.tracks
            result.playlists += provider_result.playlists
            result.radios += provider_result.radios
            # TODO: sort by name and filter out duplicates ?
        return result

    async def async_library_add(self, media_items: List[MediaItem]):
        """Add media item(s) to the library."""
        result = False
        for media_item in media_items:
            # make sure we have a database item
            db_item = await self.async_get_item(
                media_item.item_id,
                media_item.provider,
                media_item.media_type,
                lazy=False,
            )
            if not db_item:
                continue
            # add to provider's libraries
            for prov in db_item.provider_ids:
                provider = self.mass.get_provider(prov.provider)
                if provider:
                    result = await provider.async_library_add(
                        prov.item_id, media_item.media_type
                    )
                # mark as library item in internal db
                await self.mass.database.async_add_to_library(
                    db_item.item_id, db_item.media_type, prov.provider
                )
        return result

    async def async_library_remove(self, media_items: List[MediaItem]):
        """Remove media item(s) from the library."""
        result = False
        for media_item in media_items:
            # make sure we have a database item
            db_item = await self.async_get_item(
                media_item.item_id,
                media_item.provider,
                media_item.media_type,
                lazy=False,
            )
            if not db_item:
                continue
            # remove from provider's libraries
            for prov in db_item.provider_ids:
                provider = self.mass.get_provider(prov.provider)
                if provider:
                    result = await provider.async_library_remove(
                        prov.item_id, media_item.media_type
                    )
                # mark as library item in internal db
                await self.mass.database.async_remove_from_library(
                    db_item.item_id, db_item.media_type, prov.provider
                )
        return result

    async def async_add_playlist_tracks(self, db_playlist_id: int, tracks: List[Track]):
        """Add tracks to playlist - make sure we dont add duplicates."""
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.async_get_playlist(db_playlist_id, "database")
        if not playlist or not playlist.is_editable:
            return False
        # playlist can only have one provider (for now)
        playlist_prov = playlist.provider_ids[0]
        # grab all existing track ids in the playlist so we can check for duplicates
        cur_playlist_track_ids = []
        async for item in self.async_get_playlist_tracks(
            playlist_prov.item_id, playlist_prov.provider
        ):
            cur_playlist_track_ids.append(item.item_id)
            cur_playlist_track_ids += [i.item_id for i in item.provider_ids]
        track_ids_to_add = []
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
                    track_ids_to_add.append(track_version.item_id)
                    break
                if playlist_prov.provider == "file":
                    # the file provider can handle uri's from all providers so simply add the uri
                    uri = f"{track_version.provider}://{track_version.item_id}"
                    track_ids_to_add.append(uri)
                    break
        # actually add the tracks to the playlist on the provider
        if track_ids_to_add:
            # invalidate cache
            await self.mass.database.async_update_playlist(
                playlist.item_id, "checksum", str(time.time())
            )
            # return result of the action on the provider
            provider = self.mass.get_provider(playlist_prov.provider)
            return await provider.async_add_playlist_tracks(
                playlist_prov.item_id, track_ids_to_add
            )
        return False

    async def async_remove_playlist_tracks(self, db_playlist_id, tracks: List[Track]):
        """Remove tracks from playlist."""
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.async_get_playlist(db_playlist_id, "database")
        if not playlist or not playlist.is_editable:
            return False
        # playlist can only have one provider (for now)
        prov_playlist = playlist.provider_ids[0]
        track_ids_to_remove = []
        for track in tracks:
            # a track can contain multiple versions on the same provider, remove all
            for track_provider in track.provider_ids:
                if track_provider.provider == prov_playlist.provider:
                    track_ids_to_remove.append(track_provider.item_id)
        # actually remove the tracks from the playlist on the provider
        if track_ids_to_remove:
            # invalidate cache
            await self.mass.database.async_update_playlist(
                playlist.item_id, "checksum", str(time.time())
            )
            provider = self.mass.get_provider(prov_playlist.provider)
            return await provider.async_remove_playlist_tracks(
                prov_playlist.item_id, track_ids_to_remove
            )

    async def async_get_image_thumb(
        self, item_id: str, provider_id: str, media_type: MediaType, size: int = 50
    ):
        """Get path to (resized) thumb image for given media item."""
        assert item_id and provider_id and media_type
        cache_folder = os.path.join(self.mass.config.data_path, ".thumbs")
        cache_id = f"{item_id}{media_type}{provider_id}"
        cache_id = base64.b64encode(cache_id.encode("utf-8")).decode("utf-8")
        cache_file_org = os.path.join(cache_folder, f"{cache_id}0.png")
        cache_file_sized = os.path.join(cache_folder, f"{cache_id}{size}.png")
        if os.path.isfile(cache_file_sized):
            # return file from cache
            return cache_file_sized
        # no file in cache so we should get it
        img_url = ""
        # we only retrieve items that we already have in cache
        item = None
        if await self.mass.database.async_get_database_id(
            provider_id, item_id, media_type
        ):
            item = await self.async_get_item(item_id, provider_id, media_type)
        if not item:
            return ""
        if item and item.metadata.get("image"):
            img_url = item.metadata["image"]
        elif media_type == MediaType.Track and item.album:
            # try album image instead for tracks
            return await self.async_get_image_thumb(
                item.album.item_id, item.album.provider, MediaType.Album, size
            )
        elif media_type == MediaType.Album and item.artist:
            # try artist image instead for albums
            return await self.async_get_image_thumb(
                item.artist.item_id, item.artist.provider, MediaType.Artist, size
            )
        if not img_url:
            return None
        # fetch image and store in cache
        os.makedirs(cache_folder, exist_ok=True)
        # download base image
        async with aiohttp.ClientSession() as session:
            async with session.get(img_url, verify_ssl=False) as response:
                assert response.status == 200
                img_data = await response.read()
                with open(cache_file_org, "wb") as img_file:
                    img_file.write(img_data)
        if not size:
            # return base image
            return cache_file_org
        # save resized image
        basewidth = size
        img = Image.open(cache_file_org)
        wpercent = basewidth / float(img.size[0])
        hsize = int((float(img.size[1]) * float(wpercent)))
        img = img.resize((basewidth, hsize), Image.ANTIALIAS)
        img.save(cache_file_sized)
        # return file from cache
        return cache_file_sized

    async def async_get_stream_details(
        self, media_item: MediaItem, player_id: str = ""
    ) -> StreamDetails:
        """
        Get streamdetails for the given media_item.

        This is called just-in-time when a player/queue wants a MediaItem to be played.
        Do not try to request streamdetails in advance as this is expiring data.
            param media_item: The MediaItem (track/radio) for which to request the streamdetails for.
            param player_id: Optionally provide the player_id which will play this stream.
        """
        if media_item.provider == "uri":
            # special type: a plain uri was added to the queue
            streamdetails = StreamDetails(
                type=StreamType.URL,
                provider="uri",
                item_id=media_item.item_id,
                path=media_item.item_id,
                content_type=ContentType(media_item.item_id.split(".")[-1]),
                sample_rate=44100,
                bit_depth=16,
            )
        else:
            # always request the full db track as there might be other qualities available
            # except for radio
            if media_item.media_type == MediaType.Radio:
                full_track = media_item
            else:
                full_track = await self.async_get_track(
                    media_item.item_id, media_item.provider, lazy=True, refresh=False
                )
            # sort by quality and check track availability
            for prov_media in sorted(
                full_track.provider_ids, key=lambda x: x.quality, reverse=True
            ):
                # get streamdetails from provider
                music_prov = self.mass.get_provider(prov_media.provider)
                if not music_prov or not music_prov.available:
                    continue  # provider temporary unavailable ?

                streamdetails = await music_prov.async_get_stream_details(
                    prov_media.item_id
                )
                if streamdetails:
                    break

        if streamdetails:
            # set player_id on the streamdetails so we know what players stream
            streamdetails.player_id = player_id
            # store the path encrypted as we do not want it to be visible in the api
            streamdetails.path = await async_encrypt_string(streamdetails.path)
            # set streamdetails as attribute on the media_item
            # this way the app knows what content is playing
            media_item.streamdetails = streamdetails
            return streamdetails
        return None

    ################ Library synchronization logic ################

    @run_periodic(3600 * 3)
    async def __async_music_providers_sync(self):
        """Periodic sync of all music providers."""
        await asyncio.sleep(10)
        for prov in self.mass.get_providers(ProviderType.MUSIC_PROVIDER):
            await self.async_music_provider_sync(prov.id)

    async def async_music_provider_sync(self, prov_id: str):
        """
        Sync a music provider.

        param prov_id: {string} -- provider id to sync
        """
        provider = self.mass.get_provider(prov_id)
        if not provider:
            return
        if MediaType.Album in provider.supported_mediatypes:
            await self.async_library_albums_sync(prov_id)
        if MediaType.Track in provider.supported_mediatypes:
            await self.async_library_tracks_sync(prov_id)
        if MediaType.Artist in provider.supported_mediatypes:
            await self.async_library_artists_sync(prov_id)
        if MediaType.Playlist in provider.supported_mediatypes:
            await self.async_library_playlists_sync(prov_id)
        if MediaType.Radio in provider.supported_mediatypes:
            await self.async_library_radios_sync(prov_id)

    @sync_task("artists")
    async def async_library_artists_sync(self, provider_id: str):
        """Sync library artists for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        prev_db_ids = [
            item.item_id
            async for item in self.async_get_library_artists(
                provider_filter=provider_id
            )
        ]
        cur_db_ids = []
        async for item in music_provider.async_get_library_artists():
            db_item = await self.async_get_artist(item.item_id, provider_id, lazy=False)
            cur_db_ids.append(db_item.item_id)
            if db_item.item_id not in prev_db_ids:
                await self.mass.database.async_add_to_library(
                    db_item.item_id, MediaType.Artist, provider_id
                )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Artist, provider_id
                )

    @sync_task("albums")
    async def async_library_albums_sync(self, provider_id: str):
        """Sync library albums for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        prev_db_ids = [
            item.item_id
            async for item in self.async_get_library_albums(provider_filter=provider_id)
        ]
        cur_db_ids = []
        async for item in music_provider.async_get_library_albums():

            db_album = await self.async_get_album(
                item.item_id, provider_id, album_details=item, lazy=False
            )
            if not db_album:
                LOGGER.error("provider %s album: %s", provider_id, str(item))
            cur_db_ids.append(db_album.item_id)
            if db_album.item_id not in prev_db_ids:
                await self.mass.database.async_add_to_library(
                    db_album.item_id, MediaType.Album, provider_id
                )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Album, provider_id
                )

    @sync_task("tracks")
    async def async_library_tracks_sync(self, provider_id: str):
        """Sync library tracks for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        prev_db_ids = [
            item.item_id
            async for item in self.async_get_library_tracks(provider_filter=provider_id)
        ]
        cur_db_ids = []
        async for item in music_provider.async_get_library_tracks():
            db_item = await self.async_get_track(
                item.item_id, provider_id=provider_id, lazy=False
            )
            cur_db_ids.append(db_item.item_id)
            if db_item.item_id not in prev_db_ids:
                await self.mass.database.async_add_to_library(
                    db_item.item_id, MediaType.Track, provider_id
                )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Track, provider_id
                )

    @sync_task("playlists")
    async def async_library_playlists_sync(self, provider_id: str):
        """Sync library playlists for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        prev_db_ids = [
            item.item_id
            async for item in self.async_get_library_playlists(
                provider_filter=provider_id
            )
        ]
        cur_db_ids = []
        async for playlist in music_provider.async_get_library_playlists():
            if playlist is None:
                continue
            # always add to db because playlist attributes could have changed
            db_id = await self.mass.database.async_add_playlist(playlist)
            cur_db_ids.append(db_id)
            if db_id not in prev_db_ids:
                await self.mass.database.async_add_to_library(
                    db_id, MediaType.Playlist, playlist.provider
                )
            # We do not precache/store playlist tracks, these will be retrieved on request only
        # process playlist deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Playlist, provider_id
                )

    @sync_task("radios")
    async def async_library_radios_sync(self, provider_id: str):
        """Sync library radios for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        prev_db_ids = [
            item.item_id
            async for item in self.async_get_library_radios(provider_filter=provider_id)
        ]
        cur_db_ids = []
        async for item in music_provider.async_get_radios():
            if not item:
                continue
            db_id = await self.mass.database.async_get_database_id(
                item.provider, item.item_id, MediaType.Radio
            )
            if not db_id:
                db_id = await self.mass.database.async_add_radio(item)
            cur_db_ids.append(db_id)
            if db_id not in prev_db_ids:
                await self.mass.database.async_add_to_library(
                    db_id, MediaType.Radio, provider_id
                )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Radio, provider_id
                )
