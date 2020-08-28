"""Playermanager: Orchestrates all data from music providers and sync to internal database."""

import base64
import functools
import os
import time
from typing import List, Optional

import aiohttp
from PIL import Image
from music_assistant.cache import async_cached, async_cached_generator
from music_assistant.constants import EVENT_MUSIC_SYNC_STATUS
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
from music_assistant.models.provider import ProviderType
from music_assistant.utils import LOGGER, compare_strings, run_periodic


def sync_task(desc):
    """Decorator to report a sync task."""

    def wrapper(func):
        @functools.wraps(func)
        async def async_wrapped(*args):
            method_class = args[0]
            prov_id = args[1]
            # check if this sync task is not already running
            for sync_prov_id, sync_desc in method_class.running_sync_jobs:
                if sync_prov_id == prov_id and sync_desc == desc:
                    LOGGER.warning(
                        "Syncjob %s for provider %s is already running!", desc, prov_id
                    )
                    return
            sync_job = (prov_id, desc)
            method_class.running_sync_jobs.append(sync_job)
            await method_class.mass.signal_event(
                EVENT_MUSIC_SYNC_STATUS, method_class.running_sync_jobs
            )
            await func(*args)
            LOGGER.info("Finished syncing %s for provider %s", desc, prov_id)
            method_class.running_sync_jobs.remove(sync_job)
            await method_class.mass.signal_event(
                EVENT_MUSIC_SYNC_STATUS, method_class.running_sync_jobs
            )

        return async_wrapped

    return wrapper


class MusicManager:
    """Several helpers around the musicproviders."""

    def __init__(self, mass):
        self.running_sync_jobs = []
        self.mass = mass
        self.cache = mass.cache
        self.providers = {}

    async def async_setup(self):
        """Async initialize of module."""
        # load providers
        # await self.load_modules()
        # schedule sync task
        self.mass.create_task(self.__async_music_providers_sync())


################ GET MediaItem(s) by id and provider #################

    async def async_get_artist(self,
                               item_id: str,
                               provider_id: str,
                               lazy: bool = True) -> Artist:
        """Return artist details for the given provider artist id."""
        db_id = await self.mass.database.async_get_database_id(
            provider_id, item_id, MediaType.Artist
        )
        if db_id is None:
            # artist not yet in local database so fetch details
            provider = self.mass.get_provider(provider_id)
            cache_key = f"{provider_id}.get_artist.{item_id}"
            artist = await async_cached(
                self.cache, cache_key, provider.async_get_artist(item_id)
            )
            if not artist:
                raise Exception("artist not found: %s" % item_id)
            if lazy:
                self.mass.create_task(self.async_add_artist(artist))
                artist.is_lazy = True
                return artist
            db_id = await self.async_add_artist(artist)
        return await self.mass.database.async_get_artist(db_id)

    async def async_get_album(
        self, item_id: str, provider_id: str, lazy=True, album_details: Optional[Album] = None
    ) -> Album:
        """Return album details for the given provider album id."""
        db_id = await self.mass.database.async_get_database_id(
            provider_id, item_id, MediaType.Album
        )
        if db_id is None:
            # album not yet in local database so fetch details
            if not album_details:
                provider = self.mass.get_provider(provider_id)
                cache_key = f"{provider_id}.get_album.{item_id}"
                album_details = await async_cached(
                    self.cache, cache_key, provider.get_album(item_id)
                )
            if not album_details:
                raise Exception("album not found: %s" % item_id)
            if lazy:
                self.mass.create_task(self.async_add_album(album_details))
                album_details.is_lazy = True
                return album_details
            db_id = await self.async_add_album(album_details)
        return await self.mass.database.async_get_album(db_id)

    async def async_get_track(
            self, item_id: str, provider_id: str, lazy=True, track_details=None) -> Track:
        """Return track details for the given provider track id."""
        db_id = await self.mass.database.async_get_database_id(
            provider_id, item_id, MediaType.Track
        )
        if db_id is None:
            # track not yet in local database so fetch details
            if not track_details:
                provider = self.mass.get_provider(provider_id)
                cache_key = f"{provider_id}.get_track.{item_id}"
                track_details = await async_cached(
                    self.cache, cache_key, provider.async_get_track(item_id))
            if not track_details:
                raise Exception("track not found: %s" % item_id)
            if lazy:
                self.mass.create_task(self.async_add_track(track_details))
                track_details.is_lazy = True
                return track_details
            db_id = await self.async_add_track(track_details)
        return await self.mass.database.async_get_track(db_id)

    async def async_get_playlist(self, item_id: str, provider_id: str) -> Playlist:
        """Return playlist details for the given provider playlist id."""
        db_id = await self.mass.database.async_get_database_id(
            provider_id, item_id, MediaType.Playlist
        )
        if db_id is None:
            # item not yet in local database so fetch and store details
            provider = self.mass.get_provider(provider_id)
            item_details = await provider.async_get_playlist(item_id)
            db_id = await self.mass.database.async_add_playlist(item_details)
        return await self.mass.database.async_get_playlist(db_id)

    async def async_get_radio(self, item_id: str, provider_id: str) -> Radio:
        """Return radio details for the given provider playlist id."""
        db_id = await self.mass.database.async_get_database_id(
            provider_id, item_id, MediaType.Radio
        )
        if db_id is None:
            # item not yet in local database so fetch and store details
            provider = self.mass.get_provider(provider_id)
            item_details = await provider.async_get_radio(item_id)
            db_id = await self.mass.database.async_add_radio(item_details)
        return await self.mass.database.async_get_radio(db_id)

    async def async_get_album_tracks(self, item_id: str, provider_id: str) -> List[Track]:
        """Return album tracks for the given provider album id. Generator!"""
        if provider_id == "database":
            # album tracks are not stored in db, we always fetch them (cached) from the provider.
            db_item = await self.mass.database.async_get_album(item_id)
            provider_id = db_item.provider_ids[0].provider
            item_id = db_item.provider_ids[0].item_id
        provider = self.mass.get_provider(provider_id)
        cache_key = f"{provider_id}.album_tracks.{item_id}"
        async for item in async_cached_generator(
                self.cache, cache_key, provider.async_get_album_tracks(item_id)):
            if not item:
                continue
            db_id = await self.mass.database.async_get_database_id(
                item.provider, item.item_id, MediaType.Track
            )
            if db_id:
                # return database track instead if we have a match
                db_item = await self.mass.database.async_get_track(db_id, fulldata=False)
                db_item.disc_number = item.disc_number
                db_item.track_number = item.track_number
                yield db_item
            else:
                yield item

    async def async_get_playlist_tracks(self, item_id: str, provider_id: str) -> List[Track]:
        """Return playlist tracks for the given provider playlist id. Generator!"""
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
        async for item in async_cached_generator(
            self.cache,
            cache_key,
            provider.async_get_playlist_tracks(item_id),
            checksum=cache_checksum
        ):
            if not item:
                continue
            db_id = await self.mass.database.async_get_database_id(
                item.provider, item.item_id, MediaType.Track
            )
            if db_id:
                # return database track instead if we have a match
                item = await self.mass.database.async_get_track(db_id, fulldata=False)
            item.position = pos
            pos += 1
            yield item

    async def async_get_artist_toptracks(self, artist_id: str, provider_id: str) -> List[Track]:
        """Return top tracks for an artist. Generator!"""
        if provider_id == "database":
            # albums in database
            async for item in self.mass.database.async_get_artist_tracks(artist_id):
                yield item
        else:
            # items from provider
            provider = self.mass.get_provider(provider_id)
            cache_key = f"{provider_id}.artist_toptracks.{artist_id}"
            async for item in async_cached_generator(
                    self.cache, cache_key, provider.async_get_artist_toptracks(artist_id)):
                if item:
                    db_id = await self.mass.database.async_get_database_id(
                        provider_id, item.item_id, MediaType.Track
                    )
                    if db_id:
                        # return database track instead if we have a match
                        yield await self.mass.database.async_get_track(db_id)
                    else:
                        yield item

    async def async_get_artist_albums(self, artist_id: str, provider_id: str) -> List[Track]:
        """Return (all) albums for an artist. Generator!"""
        if provider_id == "database":
            # albums in database
            async for item in self.mass.database.async_get_artist_albums(artist_id):
                yield item
        else:
            # items from provider
            provider = self.mass.get_provider(provider_id)
            cache_key = f"{provider_id}.artist_albums.{artist_id}"
            async for item in async_cached_generator(
                    self.cache, cache_key, provider.async_get_artist_albums(artist_id)):
                db_id = await self.mass.database.async_get_database_id(
                    provider_id, item.item_id, MediaType.Album)
                if db_id:
                    # return database album instead if we have a match
                    yield await self.mass.database.async_get_album(db_id)
                else:
                    yield item


################ GET MediaItems that are added in the library ################

    async def async_get_library_artists(
        self, orderby: str = "name", provider_filter: str = None
    ) -> List[Artist]:
        """Return all library artists, optionally filtered by provider. Generator!"""
        async for item in self.mass.database.async_get_library_artists(
            provider_id=provider_filter, orderby=orderby
        ):
            yield item

    async def async_get_library_albums(self,
                                       orderby: str = "name",
                                       provider_filter: str = None) -> List[Album]:
        """Return all library albums, optionally filtered by provider. Generator!"""
        async for item in self.mass.database.async_get_library_albums(
            provider_id=provider_filter, orderby=orderby
        ):
            yield item

    async def async_get_library_tracks(self,
                                       orderby: str = "name",
                                       provider_filter: str = None) -> List[Track]:
        """Return all library tracks, optionally filtered by provider. Generator!"""
        async for item in self.mass.database.async_get_library_tracks(
            provider_id=provider_filter, orderby=orderby
        ):
            yield item

    async def async_get_library_playlists(
        self, orderby: str = "name", provider_filter: str = None
    ) -> List[Playlist]:
        """Return all library playlists, optionally filtered by provider. Generator!"""
        async for item in self.mass.database.async_get_library_playlists(
            provider_id=provider_filter, orderby=orderby
        ):
            yield item

    async def async_get_library_radios(
        self, orderby: str = "name", provider_filter: str = None
    ) -> List[Playlist]:
        """Return all library radios, optionally filtered by provider. Generator!"""
        async for item in self.mass.database.async_get_library_radios(
            provider_id=provider_filter, orderby=orderby
        ):
            yield item


################ ADD MediaItem(s) to database helpers ################


    async def async_add_artist(self, artist: Artist) -> int:
        """Add artist to local db and return the new database id."""
        musicbrainz_id = artist.external_ids.get(ExternalId.MUSICBRAINZ)
        if not musicbrainz_id:
            musicbrainz_id = await self.__async_get_artist_musicbrainz_id(artist)
        if not musicbrainz_id:
            LOGGER.error("Abort adding artist %s to db because of missing musicbrainz id.",
                         artist.name)
            return
        # grab additional metadata
        artist.external_ids[ExternalId.MUSICBRAINZ] = musicbrainz_id
        artist.metadata = await self.mass.metadata.async_get_artist_metadata(
            musicbrainz_id, artist.metadata)
        db_id = await self.mass.database.async_add_artist(artist)
        # also fetch same artist on all providers
        new_artist = await self.mass.database.async_get_artist(db_id)
        item_providers = [item.provider for item in new_artist.provider_ids]
        for provider in self.mass.get_providers(ProviderType.MUSIC_PROVIDER):
            if not provider.id in item_providers:
                await self.__async_match_artist(new_artist, provider.id)
        return db_id

    async def async_add_album(self, album: Album) -> int:
        """Add album to local db and return the new database id."""
        # we need to fetch album artist too
        album.artist = await self.async_get_artist(
            album.artist.item_id,
            album.artist.provider,
            lazy=False
        )
        db_id = await self.mass.database.async_add_album(album)
        # also fetch same album on all providers
        new_album = await self.mass.database.async_get_album(db_id)
        item_providers = [item.provider for item in new_album.provider_ids]
        for provider in self.mass.get_providers(ProviderType.MUSIC_PROVIDER):
            if not provider.id in item_providers:
                await self.__async_match_album(new_album, provider.id)
        return db_id

    async def async_add_track(self, track: Track, album_id=Optional[None]) -> int:
        """Add track to local db and return the new database id."""
        track_artists = []
        # we need to fetch track artists too
        for track_artist in track.artists:
            db_track_artist = await self.async_get_artist(
                track_artist.item_id,
                track_artist.provider,
                lazy=False
            )
            if db_track_artist:
                track_artists.append(db_track_artist)
        track.artists = track_artists
        # fetch album details - prefer optional provided album_id
        if album_id:
            album_details = await self.async_get_album(album_id, track.provider, lazy=False)
            if album_details:
                track.album = album_details
        # make sure we have a database album
        if track.album and track.album.provider != "database":
            track.album = await self.async_get_album(
                track.album.item_id,
                track.provider,
                lazy=False
            )
        db_id = await self.mass.database.async_add_track(track)
        # also fetch same track on all providers (will also get other quality versions)
        db_track = await self.mass.database.async_get_track(db_id)
        item_providers = [item.provider for item in db_track.provider_ids]
        for provider in self.mass.get_providers(ProviderType.MUSIC_PROVIDER):
            if not provider.id in item_providers:
                await self.__async_match_track(db_track, provider.id)
        return db_id

    async def __async_get_artist_musicbrainz_id(self, artist: Artist):
        """Fetch musicbrainz id by performing search using the artist name, albums and tracks."""
        # try with album first
        async for lookup_album in self.async_get_artist_albums(artist.item_id, artist.provider):
            if not lookup_album:
                continue
            musicbrainz_id = await self.mass.metadata.async_get_mb_artist_id(
                artist.name,
                albumname=lookup_album.name,
                album_upc=lookup_album.external_ids.get(ExternalId.UPC))
            if musicbrainz_id:
                return musicbrainz_id
        # fallback to track
        async for lookup_track in self.async_get_artist_toptracks(artist.item_id, artist.provider):
            if not lookup_track:
                continue
            musicbrainz_id = await self.mass.metadata.async_get_mb_artist_id(
                artist.name,
                trackname=lookup_track.name,
                track_isrc=lookup_track.external_ids.get(ExternalId.ISRC))
            if musicbrainz_id:
                return musicbrainz_id
        # lookup failed, use the shitty workaround to use the name as id.
        LOGGER.warning("Unable to get musicbrainz ID for artist %s !", artist.name)
        return artist.name

    async def __async_match_artist(self, artist: Artist, provider_id: str):
        """
            Try to find a matching artist on a provider for the provided (database) artist.
            This is used to link objects of different providers together.
                :attrib artist: Artist reference object.
                :provider_id: ID of the provider where we have to search for a match.
        """
        assert artist.provider != provider_id
        # try to get a match with some reference albums of this artist
        async for ref_album in self.async_get_artist_albums(artist.item_id, artist.provider):
            searchstr = "%s - %s" % (artist.name, ref_album.name)
            search_result = await self.async_search_provider(
                searchstr, provider_id, [MediaType.Album], limit=5)
            for strictness in [True, False]:
                for search_result_item in search_result.albums:
                    if not search_result_item:
                        continue
                    if not compare_strings(
                            search_result_item.name, ref_album.name, strict=strictness):
                        continue
                    # double safety check - artist must match exactly !
                    if not compare_strings(
                        search_result_item.artist.name, artist.name, strict=strictness
                    ):
                        continue
                    # just load this item in the database where it will be strictly matched
                    return await self.async_get_artist(
                        search_result_item.artist.item_id,
                        search_result_item.artist.provider, lazy=strictness)
        # try to get a match with some reference tracks of this artist
        async for search_track in await self.async_get_artist_toptracks(
                artist.item_id, artist.provider):
            searchstr = "%s - %s" % (artist.name, search_track.name)
            search_results = await self.async_search_provider(
                searchstr, provider_id, [MediaType.Track], limit=5)
            for strictness in [True, False]:
                for search_result_item in search_results["tracks"]:
                    if not search_result_item:
                        continue
                    if not compare_strings(search_result_item.name,
                                           search_track.name, strict=strictness):
                        continue
                    # double safety check - artist must match exactly !
                    for match_artist in search_result_item.artists:
                        if not compare_strings(
                            match_artist.name, artist.name, strict=strictness
                        ):
                            continue
                        # just load this item in the database where it will be strictly matched
                        return await self.async_get_artist(
                            match_artist.item_id, match_artist.provider, lazy=False)
        return None

    async def __async_match_album(self, album: Album, provider_id: str):
        """
            Try to find matching album(s) on a provider for the provided (database) album.
            This is used to link objects of different providers (and qualities) together.
                :attrib album: Album reference object.
                :provider_id: ID of the provider where we have to search for a match.
        """
        searchstr = "%s - %s" % (album.artist.name, album.name)
        if album.version:
            searchstr += " " + album.version
        search_result = await self.async_search_provider(
            searchstr, provider_id, [MediaType.Album], limit=5)
        for search_result_item in search_result.albums:
            if not search_result_item:
                continue
            if not (search_result_item.name in album.name or
                    album.name in search_result_item.name):
                continue
            if not compare_strings(search_result_item.artist.name,
                                   album.artist.name, strict=False):
                continue
            # some providers mess up versions in the title, try to fix that situation
            if (
                album.version
                and not search_result_item.version
                and album.name in search_result_item.name
                and album.version in search_result_item.name
            ):
                search_result_item.name = album.name
                search_result_item.version = album.version
            if album.version != search_result_item.version:
                continue
            # just load this item in the database where it will be strictly matched
            await self.async_get_album(search_result_item.item_id,
                                       provider_id, lazy=False, album_details=search_result_item)

    async def __async_match_track(self, track: Track, provider_id: str):
        """
            Try to find matching track(s) on a provider for the provided (database) track.
            This is used to link objects of different providers (and qualities) together.
                :attrib track: Track reference object.
                :provider_id: ID of the provider where we have to search for a match.
        """
        searchstr = "%s - %s" % (track.artists[0].name, track.name)
        if track.version:
            searchstr += " " + track.version
        search_result = await self.async_search_provider(
            searchstr, provider_id, [MediaType.Track], limit=5)
        for search_result_item in search_result.tracks:
            if (not search_result_item or not search_result_item.name
                    or not search_result_item.album):
                continue
            if not (
                (search_result_item.name in track.name
                 or track.name in search_result_item.name)
                and track.album
                and search_result_item.album.name == track.album.name
            ):
                continue
            # some providers mess up versions in the title, try to fix that situation
            if (
                track.version
                and not search_result_item.version
                and track.name in search_result_item.name
                and track.version in search_result_item.name
            ):
                search_result_item.name = track.name
                search_result_item.version = track.version
            if track.version != search_result_item.version:
                continue
            # double safety check - artist must match exactly !
            for artist in track.artists:
                for search_item_artist in search_result_item.artists:
                    if not compare_strings(artist.name, search_item_artist.name, strict=False):
                        continue
                    # just load this item in the database where it will be strictly matched
                    await self.async_get_track(
                        search_item_artist.item_id, provider_id,
                        lazy=False, track_details=search_result_item
                    )
                    break


################ Various convenience/helper methods ################

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
            self, search_query: str, provider_id: str,
            media_types: Optional[List[MediaType]], limit: int = 10) -> SearchResult:
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
            self.cache, cache_key, provider.async_search(search_query, media_types, limit))

    async def async_global_search(
            self, search_query, media_types: Optional[List[MediaType]],
            limit: int = 10) -> SearchResult:
        """
            Perform global search for media items on all providers.
                :param search_query: Search query.
                :param media_types: A list of media_types to include. All types if None.
                :param limit: number of items to return in the search (per type).
        """
        result = SearchResult([], [], [], [], [])
        # include results from all music providers, filter out duplicates
        provider_ids = ["database"] + [item.id for item in
                                       self.mass.get_providers(ProviderType.MUSIC_PROVIDER)]
        for provider_id in provider_ids:
            provider_result = await self.async_search_provider(
                search_query, provider_id, media_types, limit)
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
                media_item.item_id, media_item.provider, media_item.media_type, lazy=False
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
                media_item.item_id, media_item.provider, media_item.media_type, lazy=False
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
        playlist_prov = playlist.provider_ids[0].provider
        # grab all existing track ids in the playlist so we can check for duplicates
        cur_playlist_track_ids = []
        async for item in self.async_get_playlist_tracks(
                playlist_prov.item_id, playlist_prov.provider):
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
                elif playlist_prov.provider == "file":
                    # the file provider can handle uri's from all providers so simply add the uri
                    uri = f'{track_version.provider}://{track_version.item_id}'
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
        prov_playlist_playlist_id = prov_playlist.item_id
        prov_playlist_id = prov_playlist.provider
        track_ids_to_remove = []
        for track in tracks:
            # a track can contain multiple versions on the same provider, remove all
            for track_provider in track.provider_ids:
                if track_provider.provider == prov_playlist_id:
                    track_ids_to_remove.append(track_provider.item_id)
        # actually remove the tracks from the playlist on the provider
        if track_ids_to_remove:
            # invalidate cache
            await self.mass.database.async_update_playlist(
                playlist.item_id, "checksum", str(time.time())
            )
            return await self.providers[
                prov_playlist_id
            ].remove_playlist_tracks(prov_playlist_playlist_id, track_ids_to_remove)

    async def async_get_image_thumb(self, item_id: str,
                                    provider_id: str, media_type: MediaType, size: int = 50):
        """Get path to (resized) thumb image for given media item."""
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
        if await self.mass.database.async_get_database_id(provider_id, item_id, media_type):
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


################ Library synchronization logic ################


    @run_periodic(3600 * 3)
    async def __async_music_providers_sync(self):
        """Periodic sync of all music providers."""
        for prov in self.mass.get_providers(ProviderType.MUSIC_PROVIDER):
            self.mass.loop.create_task(self.async_music_provider_sync(prov.id))

    async def async_music_provider_sync(self, prov_id: str):
        """
            Sync a music provider.
            param prov_id: {string} -- provider id to sync
        """
        await self.async_library_albums_sync(prov_id)
        await self.async_library_tracks_sync(prov_id)
        await self.async_library_artists_sync(prov_id)
        await self.async_library_playlists_sync(prov_id)
        await self.async_library_radios_sync(prov_id)

    @sync_task("artists")
    async def async_library_artists_sync(self, provider_id: str):
        """Sync library artists for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        prev_db_ids = [
            item.item_id async for item in self.async_get_library_artists(
                provider_filter=provider_id)
        ]
        cur_db_ids = []
        async for item in music_provider.async_get_library_artists():
            db_item = await self.async_get_artist(item.item_id, provider_id, lazy=False)
            cur_db_ids.append(db_item.item_id)
            if not db_item.item_id in prev_db_ids:
                await self.mass.database.async_add_to_library(
                    db_item.item_id, MediaType.Artist, provider_id
                )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Artist, provider_id)

    @sync_task("albums")
    async def async_library_albums_sync(self, provider_id: str):
        """Sync library albums for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        prev_db_ids = [
            item.item_id async for item in self.async_get_library_albums(
                provider_filter=provider_id)
        ]
        cur_db_ids = []
        async for item in music_provider.async_get_library_albums():

            db_album = await self.async_get_album(
                item.item_id, provider_id, album_details=item, lazy=False
            )
            if not db_album:
                LOGGER.error("provider %s album: %s", provider_id, str(item))
            cur_db_ids.append(db_album.item_id)
            if not db_album.item_id in prev_db_ids:
                await self.mass.database.async_add_to_library(
                    db_album.item_id, MediaType.Album, provider_id
                )
            # precache album tracks
            async for album_track in self.async_get_album_tracks(item.item_id, item.provider):
                await self.async_get_track(album_track.item_id, album_track.provider)
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Album, provider_id)

    @sync_task("tracks")
    async def async_library_tracks_sync(self, provider_id: str):
        """Sync library tracks for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        prev_db_ids = [
            item.item_id async for item in self.async_get_library_tracks(
                provider_filter=provider_id)
        ]
        cur_db_ids = []
        async for item in music_provider.async_get_library_tracks():
            db_item = await self.async_get_track(item.item_id, provider_id=provider_id)
            cur_db_ids.append(db_item.item_id)
            if not db_item.item_id in prev_db_ids:
                await self.mass.database.async_add_to_library(
                    db_item.item_id, MediaType.Track, provider_id
                )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Track, provider_id)

    @sync_task("playlists")
    async def async_library_playlists_sync(self, provider_id: str):
        """Sync library playlists for given provider."""
        music_provider = self.providers[provider_id]
        prev_db_ids = [
            item.item_id
            async for item in self.async_get_library_playlists(provider_filter=provider_id)
        ]
        cur_db_ids = []
        async for playlist in music_provider.get_library_playlists():
            # always add to db because playlist attributes could have changed
            db_id = await self.mass.database.async_add_playlist(playlist)
            cur_db_ids.append(db_id)
            if not db_id in prev_db_ids:
                await self.mass.database.async_add_to_library(
                    db_id, MediaType.Playlist, playlist.provider)
            # precache playlist tracks
            async for playlist_track in self.async_get_playlist_tracks(
                    playlist.item_id, playlist.provider):
                await self.async_get_track(playlist_track.item_id, playlist_track.provider)
        # process playlist deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Playlist, provider_id
                )

    @sync_task("radios")
    async def async_library_radios_sync(self, prov_id):
        """sync library radios for given provider"""
        music_provider = self.providers[prov_id]
        prev_db_ids = [
            item.item_id async for item in self.async_get_library_radios(provider_filter=prov_id)
        ]
        cur_db_ids = []
        async for item in music_provider.get_radios():
            db_id = await self.mass.database.async_get_database_id(
                prov_id, item.item_id, MediaType.Radio
            )
            if not db_id:
                db_id = await self.mass.database.async_add_radio(item)
            cur_db_ids.append(db_id)
            if not db_id in prev_db_ids:
                await self.mass.database.async_add_to_library(db_id, MediaType.Radio, prov_id)
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(db_id, MediaType.Radio, prov_id)
