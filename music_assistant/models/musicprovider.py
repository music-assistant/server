#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
from typing import List
from ..utils import LOGGER, compare_strings
from ..cache import cached_iterator, cached
from .media_types import Album, Artist, Track, Playlist, MediaType, Radio


class MusicProvider():
    """ 
        Model for a Musicprovider
        Common methods usable for every provider
        Provider specific get methods shoud be overriden in the provider specific implementation
        Uses a form of lazy provisioning to local db as cache
    """
    def __init__(self, mass):
        """[DO NOT OVERRIDE]"""
        self.prov_id = ''
        self.name = ''
        self.mass = mass
        self.cache = mass.cache

    async def setup(self, conf):
        """[SHOULD OVERRIDE] Setup the provider"""
        LOGGER.debug(conf)

    ### Common methods and properties ####

    async def artist(self,
                     prov_item_id,
                     lazy=True,
                     ref_album=None,
                     ref_track=None,
                     provider=None) -> Artist:
        """ return artist details for the given provider artist id """
        if not provider:
            provider = self.prov_id
        item_id = await self.mass.db.get_database_id(provider, prov_item_id,
                                                     MediaType.Artist)
        if item_id is not None:
            # artist not yet in local database so fetch details
            cache_key = f'{self.prov_id}.get_artist.{prov_item_id}'
            artist_details = await cached(self.cache, cache_key,
                                          self.get_artist, prov_item_id)
            if not artist_details:
                raise Exception('artist not found: %s' % prov_item_id)
            if lazy:
                asyncio.create_task(self.add_artist(artist_details))
                artist_details.is_lazy = True
                return artist_details
            item_id = await self.add_artist(artist_details,
                                            ref_album=ref_album,
                                            ref_track=ref_track)
        return await self.mass.db.artist(item_id)

    async def add_artist(self, artist_details, ref_album=None,
                         ref_track=None) -> int:
        """ add artist to local db and return the new database id"""
        musicbrainz_id = None
        for item in artist_details.external_ids:
            if item.get("musicbrainz"):
                musicbrainz_id = item["musicbrainz"]
        if not musicbrainz_id:
            musicbrainz_id = await self.get_artist_musicbrainz_id(
                artist_details, ref_album=ref_album, ref_track=ref_track)
        if not musicbrainz_id:
            return
        # grab additional metadata
        if musicbrainz_id:
            artist_details.external_ids.append({"musicbrainz": musicbrainz_id})
            artist_details.metadata = await self.mass.metadata.get_artist_metadata(
                musicbrainz_id, artist_details.metadata)
        item_id = await self.mass.db.add_artist(artist_details)
        # also fetch same artist on all providers
        new_artist = await self.mass.db.artist(item_id)
        if ref_track:
            new_artist_toptracks = [ref_track]
        else:
            new_artist_toptracks = [
                item async for item in self.get_artist_toptracks(
                    artist_details.item_id)
            ]
        if ref_album:
            new_artist_albums = [ref_album]
        else:
            new_artist_albums = [
                item async for item in self.get_artist_albums(
                    artist_details.item_id)
            ]
        if new_artist_toptracks or new_artist_albums:
            item_provider_keys = [
                item['provider'] for item in new_artist.provider_ids
            ]
            for prov_id, provider in self.mass.music.providers.items():
                if not prov_id in item_provider_keys:
                    await provider.match_artist(new_artist, new_artist_albums,
                                                new_artist_toptracks)
        return item_id

    async def get_artist_musicbrainz_id(self,
                                        artist_details: Artist,
                                        ref_album=None,
                                        ref_track=None):
        """ fetch musicbrainz id by performing search with both the artist and one of it's albums or tracks """
        musicbrainz_id = ""
        # try with album first
        if ref_album:
            lookup_albums = [ref_album]
        else:
            lookup_albums = [
                item async for item in self.get_artist_albums(
                    artist_details.item_id)
            ]
        for lookup_album in lookup_albums[:10]:
            lookup_album_upc = None
            if not lookup_album:
                continue
            for item in lookup_album.external_ids:
                if item.get("upc"):
                    lookup_album_upc = item["upc"]
                    break
            musicbrainz_id = await self.mass.metadata.get_mb_artist_id(
                artist_details.name,
                albumname=lookup_album.name,
                album_upc=lookup_album_upc)
            if musicbrainz_id:
                break
        # fallback to track
        if not musicbrainz_id:
            if ref_track:
                lookup_tracks = [ref_track]
            else:
                lookup_tracks = [
                    item async for item in self.get_artist_toptracks(
                        artist_details.item_id)
                ]
            for lookup_track in lookup_tracks[:25]:
                if not lookup_track:
                    continue
                lookup_track_isrc = None
                for item in lookup_track.external_ids:
                    if item.get("isrc"):
                        lookup_track_isrc = item["isrc"]
                        break
                musicbrainz_id = await self.mass.metadata.get_mb_artist_id(
                    artist_details.name,
                    trackname=lookup_track.name,
                    track_isrc=lookup_track_isrc)
                if musicbrainz_id:
                    break
        if not musicbrainz_id:
            LOGGER.debug("Unable to get musicbrainz ID for artist %s !",
                         artist_details.name)
            musicbrainz_id = artist_details.name
        return musicbrainz_id

    async def album(self,
                    prov_item_id,
                    lazy=True,
                    album_details=None,
                    provider=None) -> Album:
        """ return album details for the given provider album id"""
        if not provider:
            provider = self.prov_id
        item_id = await self.mass.db.get_database_id(provider, prov_item_id,
                                                     MediaType.Album)
        if not item_id:
            # album not yet in local database so fetch details
            if not album_details:
                cache_key = f'{self.prov_id}.get_album.{prov_item_id}'
                album_details = await cached(self.cache, cache_key,
                                             self.get_album, prov_item_id)
            if not album_details:
                raise Exception('album not found: %s' % prov_item_id)
            if lazy:
                asyncio.create_task(self.add_album(album_details))
                album_details.is_lazy = True
                return album_details
            item_id = await self.add_album(album_details)
        return await self.mass.db.album(item_id)

    async def add_album(self, album_details) -> int:
        """ add album to local db and return the new database id"""
        # we need to fetch album artist too
        db_album_artist = await self.artist(album_details.artist.item_id,
                                            lazy=False,
                                            ref_album=album_details,
                                            provider=album_details.artist.provider)
        album_details.artist = db_album_artist
        item_id = await self.mass.db.add_album(album_details)
        # also fetch same album on all providers
        new_album = await self.mass.db.album(item_id)
        item_provider_keys = [
            item['provider'] for item in new_album.provider_ids
        ]
        for prov_id, provider in self.mass.music.providers.items():
            if not prov_id in item_provider_keys:
                await provider.match_album(new_album)
        return item_id

    async def track(self,
                    prov_item_id,
                    lazy=True,
                    track_details=None,
                    provider=None) -> Track:
        """ return track details for the given provider track id """
        if not provider:
            provider = self.prov_id
        item_id = await self.mass.db.get_database_id(provider, prov_item_id,
                                                     MediaType.Track)
        if not item_id:
            # track not yet in local database so fetch details
            if not track_details:
                cache_key = f'{self.prov_id}.get_track.{prov_item_id}'
                track_details = await cached(self.cache, cache_key,
                                             self.get_track, prov_item_id)
            if not track_details:
                LOGGER.error('track not found: %s', prov_item_id)
                return None
            if lazy:
                asyncio.create_task(self.add_track(track_details))
                track_details.is_lazy = True
                return track_details
            item_id = await self.add_track(track_details)
        return await self.mass.db.track(item_id)

    async def add_track(self, track_details, prov_album_id=None) -> int:
        """ add track to local db and return the new database id"""
        track_artists = []
        # we need to fetch track artists too
        for track_artist in track_details.artists:
            db_track_artist = await self.artist(track_artist.item_id,
                                                lazy=False,
                                                ref_track=track_details,
                                                provider=track_artist.provider)
            if db_track_artist:
                track_artists.append(db_track_artist)
        track_details.artists = track_artists
        # fetch album details - prefer prov_album_id
        if prov_album_id:
            album_details = await self.album(prov_album_id, lazy=False)
            if album_details:
                track_details.album = album_details
        # make sure we have a database album
        if track_details.album and track_details.album.provider != 'database':
            track_details.album = await self.album(
                track_details.album.item_id,
                lazy=False,
                provider=track_details.album.provider)
        item_id = await self.mass.db.add_track(track_details)
        # also fetch same track on all providers (will also get other quality versions)
        new_track = await self.mass.db.track(item_id)
        item_provider_keys = [
            item['provider'] for item in new_track.provider_ids
        ]
        for prov_id, provider in self.mass.music.providers.items():
            if not prov_id in item_provider_keys:
                await provider.match_track(new_track)
        return item_id

    async def playlist(self, prov_playlist_id, provider=None) -> Playlist:
        """ return playlist details for the given provider playlist id """
        if not provider:
            provider = self.prov_id
        db_id = await self.mass.db.get_database_id(provider, prov_playlist_id,
                                                   MediaType.Playlist)
        if not db_id:
            # item not yet in local database so fetch and store details
            item_details = await self.get_playlist(prov_playlist_id)
            db_id = await self.mass.db.add_playlist(item_details)
        return await self.mass.db.playlist(db_id)

    async def radio(self, prov_radio_id, provider=None) -> Radio:
        """ return radio details for the given provider playlist id """
        if not provider:
            provider = self.prov_id
        db_id = await self.mass.db.get_database_id(provider, prov_radio_id,
                                                   MediaType.Radio)
        if not db_id:
            # item not yet in local database so fetch and store details
            item_details = await self.get_radio(prov_radio_id)
            db_id = await self.mass.db.add_radio(item_details)
        return await self.mass.db.radio(db_id)

    async def album_tracks(self, prov_album_id) -> List[Track]:
        """ return album tracks for the given provider album id"""
        cache_key = f'{self.prov_id}.album_tracks.{prov_album_id}'
        async for item in cached_iterator(self.cache,
                                          self.get_album_tracks(prov_album_id),
                                          cache_key):
            if not item:
                continue
            db_id = await self.mass.db.get_database_id(item.provider,
                                                       item.item_id,
                                                       MediaType.Track)
            if db_id:
                # return database track instead if we have a match
                db_item = await self.mass.db.track(db_id, fulldata=False)
                db_item.disc_number = item.disc_number
                db_item.track_number = item.track_number
                yield db_item
            else:
                yield item

    async def playlist_tracks(self, prov_playlist_id) -> List[Track]:
        """ return playlist tracks for the given provider playlist id"""
        playlist = await self.playlist(prov_playlist_id)
        cache_checksum = playlist.checksum
        cache_key = f'{self.prov_id}.playlist_tracks.{prov_playlist_id}'
        pos = 0
        async for item in cached_iterator(
                self.cache,
                self.get_playlist_tracks(prov_playlist_id),
                cache_key,
                checksum=cache_checksum):
            if not item:
                continue
            db_id = await self.mass.db.get_database_id(item.provider,
                                                       item.item_id,
                                                       MediaType.Track)
            if db_id:
                # return database track instead if we have a match
                item = await self.mass.db.track(db_id, fulldata=False)
            item.position = pos
            pos += 1
            yield item

    async def artist_toptracks(self, prov_artist_id) -> List[Track]:
        """ return top tracks for an artist """
        cache_key = f'{self.prov_id}.artist_toptracks.{prov_artist_id}'
        async for item in cached_iterator(
                self.cache, self.get_artist_toptracks(prov_artist_id),
                cache_key):
            if item:
                db_id = await self.mass.db.get_database_id(
                    self.prov_id, item.item_id, MediaType.Track)
                if db_id:
                    # return database track instead if we have a match
                    yield await self.mass.db.track(db_id)
                else:
                    yield item

    async def artist_albums(self, prov_artist_id) -> List[Track]:
        """ return (all) albums for an artist """
        cache_key = f'{self.prov_id}.artist_albums.{prov_artist_id}'
        async for item in cached_iterator(
                self.cache, self.get_artist_albums(prov_artist_id), cache_key):
            db_id = await self.mass.db.get_database_id(self.prov_id,
                                                       item.item_id,
                                                       MediaType.Album)
            if db_id:
                # return database album instead if we have a match
                yield await self.mass.db.album(db_id)
            else:
                yield item

    async def match_artist(self, searchartist: Artist,
                           searchalbums: List[Album],
                           searchtracks: List[Track]):
        """ try to match artist in this provider by supplying db artist """
        for searchalbum in searchalbums:
            searchstr = "%s - %s" % (searchartist.name, searchalbum.name)
            search_results = await self.search(searchstr, [MediaType.Album],
                                               limit=5)
            for strictness in [True, False]:
                for item in search_results["albums"]:
                    if (item and compare_strings(
                            item.name, searchalbum.name, strict=strictness)):
                        # double safety check - artist must match exactly !
                        if compare_strings(item.artist.name,
                                           searchartist.name,
                                           strict=strictness):
                            # just load this item in the database where it will be strictly matched
                            await self.artist(item.artist.item_id,
                                              lazy=strictness)
                            return
        for searchtrack in searchtracks:
            searchstr = "%s - %s" % (searchartist.name, searchtrack.name)
            search_results = await self.search(searchstr, [MediaType.Track],
                                               limit=5)
            for strictness in [True, False]:
                for item in search_results["tracks"]:
                    if (item and compare_strings(
                            item.name, searchtrack.name, strict=strictness)):
                        # double safety check - artist must match exactly !
                        for artist in item.artists:
                            if compare_strings(artist.name,
                                               searchartist.name,
                                               strict=strictness):
                                # just load this item in the database where it will be strictly matched
                                # we set skip matching to false to prevent endless recursive matching
                                await self.artist(artist.item_id, lazy=False)
                                return

    async def match_album(self, searchalbum: Album):
        """ try to match album in this provider by supplying db album """
        searchstr = "%s - %s" % (searchalbum.artist.name, searchalbum.name)
        if searchalbum.version:
            searchstr += ' ' + searchalbum.version
        search_results = await self.search(searchstr, [MediaType.Album],
                                           limit=5)
        for item in search_results["albums"]:
            if (item and
                (item.name in searchalbum.name
                 or searchalbum.name in item.name) and compare_strings(
                     item.artist.name, searchalbum.artist.name, strict=False)):
                # some providers mess up versions in the title, try to fix that situation
                if (searchalbum.version and not item.version
                        and searchalbum.name in item.name
                        and searchalbum.version in item.name):
                    item.name = searchalbum.name
                    item.version = searchalbum.version
                # just load this item in the database where it will be strictly matched
                # we set skip matching to false to prevent endless recursive matching
                await self.album(item.item_id, lazy=False, album_details=item)

    async def match_track(self, searchtrack: Track):
        """ try to match track in this provider by supplying db track """
        searchstr = "%s - %s" % (searchtrack.artists[0].name, searchtrack.name)
        if searchtrack.version:
            searchstr += ' ' + searchtrack.version
        searchartists = [item.name for item in searchtrack.artists]
        search_results = await self.search(searchstr, [MediaType.Track],
                                           limit=5)
        for item in search_results["tracks"]:
            if not item or not item.name or not item.album:
                continue
            if ((item.name in searchtrack.name
                 or searchtrack.name in item.name) and item.album
                    and item.album.name == searchtrack.album.name):
                # some providers mess up versions in the title, try to fix that situation
                if (searchtrack.version and not item.version
                        and searchtrack.name in item.name
                        and searchtrack.version in item.name):
                    item.name = searchtrack.name
                    item.version = searchtrack.version
                # double safety check - artist must match exactly !
                for artist in item.artists:
                    for searchartist in searchartists:
                        if compare_strings(artist.name,
                                           searchartist,
                                           strict=False):
                            # just load this item in the database where it will be strictly matched
                            await self.track(item.item_id,
                                             lazy=False,
                                             track_details=item)
                            break

    ### Provider specific implementation #####
    # pylint: disable=unused-argument

    async def search(self, searchstring, media_types=List[MediaType], limit=5):
        """ perform search on the provider """
        return {"artists": [], "albums": [], "tracks": [], "playlists": []}

    # pylint: disable=unreachable
    async def get_library_artists(self) -> List[Artist]:
        """ retrieve library artists from the provider """
        # iterator !
        return
        yield

    async def get_library_albums(self) -> List[Album]:
        """ retrieve library albums from the provider """
        # iterator !
        return
        yield

    async def get_library_tracks(self) -> List[Track]:
        """ retrieve library tracks from the provider """
        # iterator !
        return
        yield

    async def get_library_playlists(self) -> List[Playlist]:
        """ retrieve library/subscribed playlists from the provider """
        # iterator !
        return
        yield

    async def get_radios(self) -> List[Radio]:
        """ retrieve library/subscribed radio stations from the provider """
        # iterator !
        return
        yield

    async def get_artist(self, prov_artist_id) -> Artist:
        """ get full artist details by id """
        raise NotImplementedError

    async def get_artist_albums(self, prov_artist_id) -> List[Album]:
        """ get a list of all albums for the given artist """
        # iterator !
        return
        yield

    async def get_artist_toptracks(self, prov_artist_id) -> List[Track]:
        """ get a list of most popular tracks for the given artist """
        # iterator !
        return
        yield

    # pylint: enable=unreachable

    async def get_album(self, prov_album_id) -> Album:
        """ get full album details by id """
        raise NotImplementedError

    async def get_track(self, prov_track_id) -> Track:
        """ get full track details by id """
        raise NotImplementedError

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """ get full playlist details by id """
        raise NotImplementedError

    async def get_radio(self, prov_radio_id) -> Radio:
        """ get full radio details by id """
        raise NotImplementedError

    # pylint: disable=unreachable
    async def get_album_tracks(self, prov_album_id) -> List[Track]:
        """ get album tracks for given album id """
        # iterator !
        return
        yield

    async def get_playlist_tracks(self, prov_playlist_id) -> List[Track]:
        """ get all playlist tracks for given playlist id """
        # iterator !
        return
        yield

    async def add_library(self, prov_item_id, media_type: MediaType):
        """ add item to library """
        raise NotImplementedError

    async def remove_library(self, prov_item_id, media_type: MediaType):
        """ remove item from library """
        raise NotImplementedError

    async def add_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        """ add track(s) to playlist """
        raise NotImplementedError

    async def remove_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        """ remove track(s) from playlist """
        raise NotImplementedError

    async def get_stream_details(self, track_id):
        """ get streamdetails for a track """
        raise NotImplementedError
