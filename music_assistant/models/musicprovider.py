#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
from typing import List
from ..utils import run_periodic, LOGGER, compare_strings
from ..cache import use_cache
from ..constants import CONF_ENABLED
from .media_types import Album, Artist, Track, Playlist, MediaType, Radio


class MusicProvider():
    ''' 
        Model for a Musicprovider
        Common methods usable for every provider
        Provider specific get methods shoud be overriden in the provider specific implementation
        Uses a form of lazy provisioning to local db as cache
    '''

    name = 'My great Music provider'  # display name
    prov_id = 'my_provider'  # used as id
    icon = ''

    def __init__(self, mass, conf):
        self.mass = mass
        self.cache = mass.cache

    async def setup(self):
        ''' async initialize of module '''
        pass

    ### Common methods and properties ####

    async def artist(self, prov_item_id, lazy=True, ref_album=None, ref_track=None) -> Artist:
        ''' return artist details for the given provider artist id '''
        item_id = await self.mass.db.get_database_id(self.prov_id, prov_item_id, MediaType.Artist)
        if not item_id:
            # artist not yet in local database so fetch details
            artist_details = await self.get_artist(prov_item_id)
            if not artist_details:
                raise Exception('artist not found: %s' % prov_item_id)
            if lazy:
                asyncio.create_task(self.add_artist(
                    artist_details))
                artist_details.is_lazy = True
                return artist_details
            item_id = await self.add_artist(artist_details, ref_album=ref_album, ref_track=ref_track)
        return await self.mass.db.artist(item_id)

    async def add_artist(self, artist_details, ref_album=None, ref_track=None) -> int:
        ''' add artist to local db and return the new database id'''
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
            new_artist_toptracks = [item async for item in self.get_artist_toptracks(artist_details.item_id)]
        if ref_album:
            new_artist_albums = [ref_album]
        else:
            new_artist_albums = [item async for item in  self.get_artist_albums(artist_details.item_id)]
        if new_artist_toptracks or new_artist_albums:
            item_provider_keys = [item['provider']
                                    for item in new_artist.provider_ids]
            for prov_id, provider in self.mass.music.providers.items():
                if not prov_id in item_provider_keys:
                    self.mass.event_loop.create_task(
                        provider.match_artist(new_artist, new_artist_albums, new_artist_toptracks))
        return item_id

    async def get_artist_musicbrainz_id(self, artist_details: Artist, ref_album=None, ref_track=None):
        ''' fetch musicbrainz id by performing search with both the artist and one of it's albums or tracks '''
        musicbrainz_id = ""
        # try with album first
        if ref_album:
            lookup_albums = [ref_album]
        else:
            lookup_albums = [item async for item in self.get_artist_albums(artist_details.item_id)]
        for lookup_album in lookup_albums[:10]:
            lookup_album_upc = None
            if not lookup_album:
                continue
            for item in lookup_album.external_ids:
                if item.get("upc"):
                    lookup_album_upc = item["upc"]
                    break
            musicbrainz_id = await self.mass.metadata.get_mb_artist_id(artist_details.name,
                                                                       albumname=lookup_album.name, album_upc=lookup_album_upc)
            if musicbrainz_id:
                break
        # fallback to track
        if not musicbrainz_id:
            if ref_track:
                lookup_tracks = [ref_track]
            else:
                lookup_tracks = [item async for item in self.get_artist_toptracks(artist_details.item_id)]
            for lookup_track in lookup_tracks[:25]:
                if not lookup_track:
                    continue
                lookup_track_isrc = None
                for item in lookup_track.external_ids:
                    if item.get("isrc"):
                        lookup_track_isrc = item["isrc"]
                        break
                musicbrainz_id = await self.mass.metadata.get_mb_artist_id(artist_details.name,
                                                                           trackname=lookup_track.name, track_isrc=lookup_track_isrc)
                if musicbrainz_id:
                    break
        if not musicbrainz_id:
            LOGGER.warning(
                "Unable to get musicbrainz ID for artist %s !" % artist_details.name)
            musicbrainz_id = artist_details.name
        return musicbrainz_id

    async def album(self, prov_item_id, lazy=True, album_details=None) -> Album:
        ''' return album details for the given provider album id'''
        item_id = await self.mass.db.get_database_id(self.prov_id, prov_item_id, MediaType.Album)
        if not item_id:
            # album not yet in local database so fetch details
            if not album_details:
                album_details = await self.get_album(prov_item_id)
            if not album_details:
                raise Exception('album not found: %s' % prov_item_id)
            if lazy:
                asyncio.create_task(self.add_album(
                    album_details))
                album_details.is_lazy = True
                return album_details
            item_id = await self.add_album(album_details)
        return await self.mass.db.album(item_id)

    async def add_album(self, album_details) -> int:
        ''' add album to local db and return the new database id'''
        # we need to fetch album artist too
        db_album_artist = await self.artist(album_details.artist.item_id, lazy=False, ref_album=album_details)
        album_details.artist = db_album_artist
        item_id = await self.mass.db.add_album(album_details)
        # also fetch same album on all providers
        new_album = await self.mass.db.album(item_id)
        item_provider_keys = [item['provider']
                                for item in new_album.provider_ids]
        for prov_id, provider in self.mass.music.providers.items():
            if not prov_id in item_provider_keys:
                self.mass.event_loop.create_task(
                        provider.match_album(new_album))
        return item_id

    async def track(self, prov_item_id, lazy=True, track_details=None) -> Track:
        ''' return track details for the given provider track id '''
        item_id = await self.mass.db.get_database_id(self.prov_id, prov_item_id, MediaType.Track)
        if not item_id:
            # track not yet in local database so fetch details
            if not track_details:
                track_details = await self.get_track(prov_item_id)
            if not track_details:
                raise Exception('track not found: %s' % prov_item_id)
            if lazy:
                asyncio.create_task(self.add_track(
                    track_details))
                track_details.is_lazy = True
                return track_details
            item_id = await self.add_track(track_details)
        return await self.mass.db.track(item_id)

    async def add_track(self, track_details, prov_album_id=None) -> int:
        ''' add track to local db and return the new database id'''
        track_artists = []
        # we need to fetch track artists too
        for track_artist in track_details.artists:
            db_track_artist = await self.artist(track_artist.item_id, lazy=False, ref_track=track_details)
            if db_track_artist:
                track_artists.append(db_track_artist)
        track_details.artists = track_artists
        if not prov_album_id:
            prov_album_id = track_details.album.item_id
        track_details.album = await self.album(prov_album_id, lazy=False)
        item_id = await self.mass.db.add_track(track_details)
        # also fetch same track on all providers (will also get other quality versions)
        new_track = await self.mass.db.track(item_id)
        item_provider_keys = [item['provider']
                                for item in new_track.provider_ids]
        for prov_id, provider in self.mass.music.providers.items():
            if not prov_id in item_provider_keys:
                self.mass.event_loop.create_task(
                        provider.match_track(new_track))
        return item_id

    async def playlist(self, prov_playlist_id) -> Playlist:
        ''' return playlist details for the given provider playlist id '''
        db_id = await self.mass.db.get_database_id(self.prov_id, prov_playlist_id, MediaType.Playlist)
        if db_id:
            # synced playlist, return database details
            return await self.mass.db.playlist(db_id)
        else:
            return await self.get_playlist(prov_playlist_id)

    async def radio(self, prov_radio_id) -> Radio:
        ''' return radio details for the given provider playlist id '''
        db_id = await self.mass.db.get_database_id(self.prov_id, prov_radio_id, MediaType.Radio)
        if db_id:
            # synced radio, return database details
            return await self.mass.db.radio(db_id)
        else:
            return await self.get_radio(prov_radio_id)

    async def album_tracks(self, prov_album_id) -> List[Track]:
        ''' return album tracks for the given provider album id'''
        album = await self.get_album(prov_album_id)
        async for prov_track in self.get_album_tracks(prov_album_id):
            if prov_track:
                # lazy load to database
                if not prov_track.album:
                    prov_track.album = album
                db_track = await self.track(prov_track.item_id, lazy=True, track_details=prov_track)
                db_track.disc_number = prov_track.disc_number
                db_track.track_number = prov_track.track_number
                yield db_track

    async def playlist_tracks(self, prov_playlist_id, limit=100, offset=0) -> List[Track]:
        ''' return playlist tracks for the given provider playlist id'''
        pos = offset
        async for prov_track in self.get_playlist_tracks(prov_playlist_id, limit=limit, offset=offset):
            db_id = await self.mass.db.get_database_id(prov_track.provider, prov_track.item_id, MediaType.Track)
            if db_id:
                # return database track instead if we have a match
                prov_track = await self.mass.db.track(db_id)
            prov_track.position = pos
            pos += 1
            yield prov_track

    async def artist_toptracks(self, prov_item_id) -> List[Track]:
        ''' return top tracks for an artist '''
        async for prov_track in self.get_artist_toptracks(prov_item_id):
            if prov_track:
                db_id = await self.mass.db.get_database_id(self.prov_id, prov_track.item_id, MediaType.Track)
                if db_id:
                    # return database track instead if we have a match
                    yield self.mass.db.track(db_id)
                else:
                    yield prov_track

    async def artist_albums(self, prov_item_id) -> List[Track]:
        ''' return (all) albums for an artist '''
        async for prov_album in self.get_artist_albums(prov_item_id):
            db_id = await self.mass.db.get_database_id(self.prov_id, prov_album.item_id, MediaType.Album)
            if db_id:
                # return database album instead if we have a match
                yield await self.mass.db.album(db_id)
            else:
                yield prov_album

    async def match_artist(self, searchartist: Artist, searchalbums: List[Album], searchtracks: List[Track]):
        ''' try to match artist in this provider by supplying db artist '''
        for searchalbum in searchalbums:
            searchstr = "%s - %s" % (searchartist.name, searchalbum.name)
            search_results = await self.search(searchstr, [MediaType.Album], limit=5)
            for strictness in [True, False]:
                for item in search_results["albums"]:
                    if (item and compare_strings(item.name, searchalbum.name, strict=strictness)):
                        # double safety check - artist must match exactly !
                        if compare_strings(item.artist.name, searchartist.name, strict=strictness):
                            # just load this item in the database where it will be strictly matched
                            await self.artist(item.artist.item_id, lazy=strictness)
                            return
        for searchtrack in searchtracks:
            searchstr = "%s - %s" % (searchartist.name, searchtrack.name)
            search_results = await self.search(searchstr, [MediaType.Track], limit=5)
            for strictness in [True, False]:
                for item in search_results["tracks"]:
                    if (item and compare_strings(item.name, searchtrack.name, strict=strictness)):
                        # double safety check - artist must match exactly !
                        for artist in item.artists:
                            if compare_strings(artist.name, searchartist.name, strict=strictness):
                                # just load this item in the database where it will be strictly matched
                                # we set skip matching to false to prevent endless recursive matching
                                await self.artist(artist.item_id, lazy=False)
                                return

    async def match_album(self, searchalbum: Album):
        ''' try to match album in this provider by supplying db album '''
        searchstr = "%s - %s" % (searchalbum.artist.name,
                                    searchalbum.name)
        if searchalbum.version:
            searchstr += ' ' + searchalbum.version
        search_results = await self.search(searchstr, [MediaType.Album], limit=5)
        for item in search_results["albums"]:
            if (item and (item.name in searchalbum.name or searchalbum.name in item.name) and
                    compare_strings(item.artist.name, searchalbum.artist.name, strict=False)):
                # some providers mess up versions in the title, try to fix that situation
                if (searchalbum.version and not item.version and 
                        searchalbum.name in item.name and searchalbum.version in item.name):
                    item.name = searchalbum.name
                    item.version = searchalbum.version
                # just load this item in the database where it will be strictly matched
                # we set skip matching to false to prevent endless recursive matching
                await self.album(item.item_id, lazy=False, album_details=item)

    async def match_track(self, searchtrack: Track):
        ''' try to match track in this provider by supplying db track '''
        searchstr = "%s - %s" % (searchtrack.artists[0].name, searchtrack.name)
        if searchtrack.version:
            searchstr += ' ' + searchtrack.version
        searchartists = [item.name for item in searchtrack.artists]
        search_results = await self.search(searchstr, [MediaType.Track], limit=5)
        for item in search_results["tracks"]:
            if not item or not item.album:
                continue
            if ((item.name in searchtrack.name or searchtrack.name in item.name) and
                    item.album and item.album.name == searchtrack.album.name):
                # some providers mess up versions in the title, try to fix that situation
                if (searchtrack.version and not item.version and 
                        searchtrack.name in item.name and searchtrack.version in item.name):
                    item.name = searchtrack.name
                    item.version = searchtrack.version
                # double safety check - artist must match exactly !
                for artist in item.artists:
                    for searchartist in searchartists:
                        if compare_strings(artist.name, searchartist, strict=False):
                            # just load this item in the database where it will be strictly matched
                            await self.track(item.item_id, lazy=False, track_details=item)
                            break

    ### Provider specific implementation #####

    async def search(self, searchstring, media_types=List[MediaType], limit=5):
        ''' perform search on the provider '''
        return {
            "artists": [],
            "albums": [],
            "tracks": [],
            "playlists": []
        }

    async def get_library_artists(self) -> List[Artist]:
        ''' retrieve library artists from the provider '''
        # iterator !
        return
        yield

    async def get_library_albums(self) -> List[Album]:
        ''' retrieve library albums from the provider '''
        # iterator !
        return
        yield

    async def get_library_tracks(self) -> List[Track]:
        ''' retrieve library tracks from the provider '''
        # iterator !
        return
        yield

    async def get_playlists(self) -> List[Playlist]:
        ''' retrieve library/subscribed playlists from the provider '''
        # iterator !
        return
        yield

    async def get_radios(self) -> List[Radio]:
        ''' retrieve library/subscribed radio stations from the provider '''
        # iterator !
        return
        yield

    async def get_artist(self, prov_item_id) -> Artist:
        ''' get full artist details by id '''
        raise NotImplementedError

    async def get_artist_albums(self, prov_item_id) -> List[Album]:
        ''' get a list of albums for the given artist '''
        # iterator !
        return
        yield

    async def get_artist_toptracks(self, prov_item_id) -> List[Track]:
        ''' get a list of most popular tracks for the given artist '''
        # iterator !
        return
        yield

    async def get_album(self, prov_item_id) -> Album:
        ''' get full album details by id '''
        raise NotImplementedError

    async def get_track(self, prov_item_id) -> Track:
        ''' get full track details by id '''
        raise NotImplementedError

    async def get_playlist(self, prov_item_id) -> Playlist:
        ''' get full playlist details by id '''
        raise NotImplementedError

    async def get_radio(self, prov_item_id) -> Radio:
        ''' get full radio details by id '''
        raise NotImplementedError

    async def get_album_tracks(self, prov_album_id, limit=100, offset=0) -> List[Track]:
        ''' get album tracks for given album id '''
        # iterator !
        return
        yield

    async def get_playlist_tracks(self, prov_playlist_id, limit=100, offset=0) -> List[Track]:
        ''' get playlist tracks for given playlist id '''
        # iterator !
        return
        yield

    async def add_library(self, prov_item_id, media_type: MediaType):
        ''' add item to library '''
        raise NotImplementedError

    async def remove_library(self, prov_item_id, media_type: MediaType):
        ''' remove item from library '''
        raise NotImplementedError

    async def add_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        ''' add track(s) to playlist '''
        raise NotImplementedError

    async def remove_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        ''' remove track(s) from playlist '''
        raise NotImplementedError

    async def get_stream_details(self, track_id):
        ''' get streamdetails for a track '''
        raise NotImplementedError
