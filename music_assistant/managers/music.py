"""MusicManager: Orchestrates all data from music providers and sync to internal database."""

import asyncio
import logging
from typing import List, Set, Tuple

from music_assistant.constants import (
    EVENT_ALBUM_ADDED,
    EVENT_ARTIST_ADDED,
    EVENT_PLAYLIST_ADDED,
    EVENT_RADIO_ADDED,
    EVENT_TRACK_ADDED,
)
from music_assistant.helpers.cache import cached
from music_assistant.helpers.compare import (
    compare_album,
    compare_artists,
    compare_strings,
    compare_track,
)
from music_assistant.helpers.musicbrainz import MusicBrainz
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.web import api_route
from music_assistant.managers.tasks import TaskInfo
from music_assistant.models.media_types import (
    Album,
    AlbumType,
    Artist,
    FullAlbum,
    ItemMapping,
    MediaItem,
    MediaType,
    Playlist,
    Radio,
    SearchResult,
    Track,
)
from music_assistant.models.provider import MusicProvider, ProviderType

LOGGER = logging.getLogger("music_manager")


class MusicManager:
    """Several helpers around the musicproviders."""

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.mass = mass
        self.cache = mass.cache
        self.musicbrainz = MusicBrainz(mass)
        self._db_add_progress = set()

    async def setup(self):
        """Async initialize of module."""

    @property
    def providers(self) -> Tuple[MusicProvider]:
        """Return all providers of type musicprovider."""
        return self.mass.get_providers(ProviderType.MUSIC_PROVIDER)

    ################ GET MediaItem(s) by id and provider #################

    @api_route("artists/{provider_id}/{item_id}")
    async def get_artist(
        self, item_id: str, provider_id: str, refresh: bool = False, lazy: bool = True
    ) -> Artist:
        """Return artist details for the given provider artist id."""
        if provider_id == "database" and not refresh:
            return await self.mass.database.get_artist(item_id)
        db_item = await self.mass.database.get_artist_by_prov_id(provider_id, item_id)
        if db_item and refresh:
            provider_id, item_id = await self.__get_provider_id(db_item)
        elif db_item:
            return db_item
        artist = await self._get_provider_artist(item_id, provider_id)
        if not lazy:
            return await self.add_artist(artist)
        self.mass.tasks.add(
            f"Add artist {artist.uri} to database", self.add_artist, artist
        )
        return db_item if db_item else artist

    async def _get_provider_artist(self, item_id: str, provider_id: str) -> Artist:
        """Return artist details for the given provider artist id."""
        provider = self.mass.get_provider(provider_id)
        if not provider or not provider.available:
            raise Exception("Provider %s is not available!" % provider_id)
        cache_key = f"{provider_id}.get_artist.{item_id}"
        artist = await cached(self.cache, cache_key, provider.get_artist, item_id)
        if not artist:
            raise Exception(
                "Artist %s not found on provider %s" % (item_id, provider_id)
            )
        return artist

    @api_route("albums/{provider_id}/{item_id}")
    async def get_album(
        self, item_id: str, provider_id: str, refresh: bool = False, lazy: bool = True
    ) -> Album:
        """Return album details for the given provider album id."""
        if provider_id == "database" and not refresh:
            return await self.mass.database.get_album(item_id)
        db_item = await self.mass.database.get_album_by_prov_id(provider_id, item_id)
        if db_item and refresh:
            provider_id, item_id = await self.__get_provider_id(db_item)
        elif db_item:
            return db_item
        album = await self._get_provider_album(item_id, provider_id)
        if not lazy:
            return await self.add_album(album)
        self.mass.tasks.add(f"Add album {album.uri} to database", self.add_album, album)
        return db_item if db_item else album

    async def _get_provider_album(self, item_id: str, provider_id: str) -> Album:
        """Return album details for the given provider album id."""
        provider = self.mass.get_provider(provider_id)
        if not provider or not provider.available:
            raise Exception("Provider %s is not available!" % provider_id)
        cache_key = f"{provider_id}.get_album.{item_id}"
        album = await cached(self.cache, cache_key, provider.get_album, item_id)
        if not album:
            raise Exception(
                "Album %s not found on provider %s" % (item_id, provider_id)
            )
        return album

    @api_route("tracks/{provider_id}/{item_id}")
    async def get_track(
        self,
        item_id: str,
        provider_id: str,
        track_details: Track = None,
        album_details: Album = None,
        refresh: bool = False,
        lazy: bool = True,
    ) -> Track:
        """Return track details for the given provider track id."""
        if provider_id == "database" and not refresh:
            return await self.mass.database.get_track(item_id)
        db_item = await self.mass.database.get_track_by_prov_id(provider_id, item_id)
        if db_item and refresh:
            provider_id, item_id = await self.__get_provider_id(db_item)
        elif db_item:
            return db_item
        if not track_details:
            track_details = await self._get_provider_track(item_id, provider_id)
        if album_details:
            track_details.album = album_details
        if not lazy:
            return await self.add_track(track_details)
        self.mass.tasks.add(
            f"Add track {track_details.uri} to database", self.add_track, track_details
        )
        return db_item if db_item else track_details

    async def _get_provider_track(self, item_id: str, provider_id: str) -> Track:
        """Return track details for the given provider track id."""
        provider = self.mass.get_provider(provider_id)
        if not provider or not provider.available:
            raise Exception("Provider %s is not available!" % provider_id)
        cache_key = f"{provider_id}.get_track.{item_id}"
        track = await cached(self.cache, cache_key, provider.get_track, item_id)
        if not track:
            raise Exception(
                "Track %s not found on provider %s" % (item_id, provider_id)
            )
        return track

    @api_route("playlists/{provider_id}/{item_id}")
    async def get_playlist(
        self, item_id: str, provider_id: str, refresh: bool = False, lazy: bool = True
    ) -> Playlist:
        """Return playlist details for the given provider playlist id."""
        assert item_id and provider_id
        db_item = await self.mass.database.get_playlist_by_prov_id(provider_id, item_id)
        if db_item and refresh:
            provider_id, item_id = await self.__get_provider_id(db_item)
        elif db_item:
            return db_item
        playlist = await self._get_provider_playlist(item_id, provider_id)
        if not lazy:
            return await self.add_playlist(playlist)
        self.mass.tasks.add(
            f"Add playlist {playlist.name} to database", self.add_playlist, playlist
        )
        return db_item if db_item else playlist

    async def _get_provider_playlist(self, item_id: str, provider_id: str) -> Playlist:
        """Return playlist details for the given provider playlist id."""
        provider = self.mass.get_provider(provider_id)
        if not provider or not provider.available:
            raise Exception("Provider %s is not available!" % provider_id)
        cache_key = f"{provider_id}.get_playlist.{item_id}"
        playlist = await cached(
            self.cache,
            cache_key,
            provider.get_playlist,
            item_id,
            expires=86400 * 2,
        )
        if not playlist:
            raise Exception(
                "Playlist %s not found on provider %s" % (item_id, provider_id)
            )
        return playlist

    @api_route("radios/{provider_id}/{item_id}")
    async def get_radio(
        self, item_id: str, provider_id: str, refresh: bool = False, lazy: bool = True
    ) -> Radio:
        """Return radio details for the given provider radio id."""
        assert item_id and provider_id
        db_item = await self.mass.database.get_radio_by_prov_id(provider_id, item_id)
        if db_item and refresh:
            provider_id, item_id = await self.__get_provider_id(db_item)
        elif db_item:
            return db_item
        radio = await self._get_provider_radio(item_id, provider_id)
        if not lazy:
            return await self.add_radio(radio)
        self.mass.tasks.add(
            f"Add radio station {radio.name} to database", self.add_radio, radio
        )
        return db_item if db_item else radio

    async def _get_provider_radio(self, item_id: str, provider_id: str) -> Radio:
        """Return radio details for the given provider playlist id."""
        provider = self.mass.get_provider(provider_id)
        if not provider or not provider.available:
            raise Exception("Provider %s is not available!" % provider_id)
        cache_key = f"{provider_id}.get_radio.{item_id}"
        radio = await cached(self.cache, cache_key, provider.get_radio, item_id)
        if not radio:
            raise Exception(
                "Radio %s not found on provider %s" % (item_id, provider_id)
            )
        return radio

    @api_route("albums/{provider_id}/{item_id}/tracks")
    async def get_album_tracks(self, item_id: str, provider_id: str) -> List[Track]:
        """Return album tracks for the given provider album id."""
        assert item_id and provider_id
        album = await self.get_album(item_id, provider_id)
        if album.provider == "database":
            # album tracks are not stored in db, we always fetch them (cached) from the provider.
            prov_id = next(iter(album.provider_ids))
            provider_id = prov_id.provider
            item_id = prov_id.item_id
        provider = self.mass.get_provider(provider_id)
        cache_key = f"{provider_id}.album_tracks.{item_id}"
        all_prov_tracks = await cached(
            self.cache, cache_key, provider.get_album_tracks, item_id
        )
        # retrieve list of db items
        db_tracks = await self.mass.database.get_tracks_from_provider_ids(
            {x.provider for x in album.provider_ids},
            {x.item_id for x in all_prov_tracks},
        )
        # combine provider tracks with db tracks
        return [
            await self.__process_item(
                item,
                db_tracks,
                album=album,
                disc_number=item.disc_number,
                track_number=item.track_number,
            )
            for item in all_prov_tracks
        ]

    @api_route("albums/{provider_id}/{item_id}/versions")
    async def get_album_versions(self, item_id: str, provider_id: str) -> Set[Album]:
        """Return all versions of an album we can find on all providers."""
        album = await self.get_album(item_id, provider_id)
        provider_ids = {
            item.id for item in self.mass.get_providers(ProviderType.MUSIC_PROVIDER)
        }
        search_query = f"{album.artist.name} {album.name}"
        return {
            prov_item
            for prov_items in await asyncio.gather(
                *[
                    self.search_provider(search_query, prov_id, [MediaType.ALBUM], 25)
                    for prov_id in provider_ids
                ]
            )
            for prov_item in prov_items.albums
            if compare_strings(prov_item.artist.name, album.artist.name)
        }

    @api_route("tracks/{provider_id}/{item_id}/versions")
    async def get_track_versions(self, item_id: str, provider_id: str) -> Set[Track]:
        """Return all versions of a track we can find on all providers."""
        track = await self.get_track(item_id, provider_id)
        provider_ids = {
            item.id for item in self.mass.get_providers(ProviderType.MUSIC_PROVIDER)
        }
        first_artist = next(iter(track.artists))
        search_query = f"{first_artist.name} {track.name}"
        return {
            prov_item
            for prov_items in await asyncio.gather(
                *[
                    self.search_provider(search_query, prov_id, [MediaType.TRACK], 25)
                    for prov_id in provider_ids
                ]
            )
            for prov_item in prov_items.tracks
            if compare_artists(prov_item.artists, track.artists)
        }

    @api_route("playlists/{provider_id}/{item_id}/tracks")
    async def get_playlist_tracks(self, item_id: str, provider_id: str) -> List[Track]:
        """Return playlist tracks for the given provider playlist id."""
        assert item_id and provider_id
        if provider_id == "database":
            # playlist tracks are not stored in db, we always fetch them (cached) from the provider.
            playlist = await self.mass.database.get_playlist(item_id)
            prov_id = next(iter(playlist.provider_ids))
            provider_id = prov_id.provider
            item_id = prov_id.item_id
            provider = self.mass.get_provider(provider_id)
        else:
            provider = self.mass.get_provider(provider_id)
            playlist = await provider.get_playlist(item_id)
        cache_checksum = playlist.checksum
        cache_key = f"{provider_id}.playlist_tracks.{item_id}"
        return await cached(
            self.cache,
            cache_key,
            provider.get_playlist_tracks,
            item_id,
            checksum=cache_checksum,
        )

    async def __process_item(
        self,
        item,
        db_items,
        index=None,
        album=None,
        disc_number=None,
        track_number=None,
    ):
        """Return combined result of provider item and db result."""
        for db_item in db_items:
            if item.item_id in {x.item_id for x in db_item.provider_ids}:
                item = db_item
                break
        if index is not None and not item.position:
            item.position = index
        if album is not None:
            item.album = album
        if disc_number is not None:
            item.disc_number = disc_number
        if track_number is not None:
            item.track_number = track_number
        return item

    @api_route("artists/{provider_id}/{item_id}/tracks")
    async def get_artist_toptracks(self, item_id: str, provider_id: str) -> Set[Track]:
        """Return top tracks for an artist."""
        if provider_id != "database":
            return await self._get_provider_artist_toptracks(item_id, provider_id)

        # db artist: get results from all providers
        artist = await self.get_artist(item_id, provider_id)
        all_prov_tracks = {
            track
            for prov_tracks in await asyncio.gather(
                *[
                    self._get_provider_artist_toptracks(item.item_id, item.provider)
                    for item in artist.provider_ids
                ]
            )
            for track in prov_tracks
        }
        # retrieve list of db items
        db_tracks = await self.mass.database.get_tracks_from_provider_ids(
            {x.provider for x in artist.provider_ids},
            {x.item_id for x in all_prov_tracks},
        )
        # combine provider tracks with db tracks and filter duplicate itemid's
        return {await self.__process_item(item, db_tracks) for item in all_prov_tracks}

    async def _get_provider_artist_toptracks(
        self, item_id: str, provider_id: str
    ) -> List[Track]:
        """Return top tracks for an artist on given provider."""
        provider = self.mass.get_provider(provider_id)
        if not provider or not provider.available:
            LOGGER.error("Provider %s is not available", provider_id)
            return []
        cache_key = f"{provider_id}.artist_toptracks.{item_id}"
        return await cached(
            self.cache,
            cache_key,
            provider.get_artist_toptracks,
            item_id,
        )

    @api_route("artists/{provider_id}/{item_id}/albums")
    async def get_artist_albums(self, item_id: str, provider_id: str) -> Set[Album]:
        """Return (all) albums for an artist."""
        if provider_id != "database":
            return await self._get_provider_artist_albums(item_id, provider_id)
        # db artist: get results from all providers
        artist = await self.get_artist(item_id, provider_id)
        all_prov_albums = {
            album
            for prov_albums in await asyncio.gather(
                *[
                    self._get_provider_artist_albums(item.item_id, item.provider)
                    for item in artist.provider_ids
                ]
            )
            for album in prov_albums
        }
        # retrieve list of db items
        db_tracks = await self.mass.database.get_albums_from_provider_ids(
            [x.provider for x in artist.provider_ids],
            [x.item_id for x in all_prov_albums],
        )
        # combine provider tracks with db tracks and filter duplicate itemid's
        return {await self.__process_item(item, db_tracks) for item in all_prov_albums}

    async def _get_provider_artist_albums(
        self, item_id: str, provider_id: str
    ) -> List[Album]:
        """Return albums for an artist on given provider."""
        provider = self.mass.get_provider(provider_id)
        if not provider or not provider.available:
            LOGGER.error("Provider %s is not available", provider_id)
            return []
        cache_key = f"{provider_id}.artistalbums.{item_id}"
        return await cached(
            self.cache,
            cache_key,
            provider.get_artist_albums,
            item_id,
        )

    @api_route("search/{provider_id}")
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
            return await self.mass.database.search(search_query, media_types)
        provider = self.mass.get_provider(provider_id)
        cache_key = f"{provider_id}.search.{search_query}.{media_types}.{limit}"
        return await cached(
            self.cache,
            cache_key,
            provider.search,
            search_query,
            media_types,
            limit,
        )

    @api_route("search")
    async def global_search(
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
        provider_ids = ["database"] + [
            item.id for item in self.mass.get_providers(ProviderType.MUSIC_PROVIDER)
        ]
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

    @api_route("items/by_uri")
    async def get_item_by_uri(self, uri: str) -> MediaItem:
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

    @api_route("items/{media_type}/{provider_id}/{item_id}")
    async def get_item(
        self,
        item_id: str,
        provider_id: str,
        media_type: MediaType,
        refresh: bool = False,
        lazy: bool = True,
    ) -> MediaItem:
        """Get single music item by id and media type."""
        if media_type == MediaType.ARTIST:
            return await self.get_artist(
                item_id, provider_id, refresh=refresh, lazy=lazy
            )
        if media_type == MediaType.ALBUM:
            return await self.get_album(
                item_id, provider_id, refresh=refresh, lazy=lazy
            )
        if media_type == MediaType.TRACK:
            return await self.get_track(
                item_id, provider_id, refresh=refresh, lazy=lazy
            )
        if media_type == MediaType.PLAYLIST:
            return await self.get_playlist(
                item_id, provider_id, refresh=refresh, lazy=lazy
            )
        if media_type == MediaType.RADIO:
            return await self.get_radio(
                item_id, provider_id, refresh=refresh, lazy=lazy
            )
        return None

    @api_route("items/refresh", method="PUT")
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
        except Exception:  # pylint:disable=broad-except
            pass
        searchresult: SearchResult = await self.global_search(
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

    ################ ADD MediaItem(s) to database helpers ################

    async def add_artist(self, artist: Artist) -> Artist:
        """Add artist to local db and return the database item."""
        if not artist.musicbrainz_id:
            artist.musicbrainz_id = await self._get_artist_musicbrainz_id(artist)
        # grab additional metadata
        artist.metadata = await self.mass.metadata.get_artist_metadata(
            artist.musicbrainz_id, artist.metadata
        )
        db_item = await self.mass.database.add_artist(artist)
        # also fetch same artist on all providers
        await self.match_artist(db_item)
        db_item = await self.mass.database.get_artist(db_item.item_id)
        self.mass.eventbus.signal(EVENT_ARTIST_ADDED, db_item)
        return db_item

    async def add_album(self, album: Album) -> Album:
        """Add album to local db and return the database item."""
        # make sure we have an artist
        assert album.artist
        db_item = await self.mass.database.add_album(album)
        # also fetch same album on all providers
        await self.match_album(db_item)
        db_item = await self.mass.database.get_album(db_item.item_id)
        self.mass.eventbus.signal(EVENT_ALBUM_ADDED, db_item)
        return db_item

    async def add_track(self, track: Track) -> Track:
        """Add track to local db and return the new database item."""
        # make sure we have artists
        assert track.artists
        # make sure we have an album
        assert track.album or track.albums
        db_item = await self.mass.database.add_track(track)
        # also fetch same track on all providers (will also get other quality versions)
        await self.match_track(db_item)
        db_item = await self.mass.database.get_track(db_item.item_id)
        self.mass.eventbus.signal(EVENT_TRACK_ADDED, db_item)
        return db_item

    async def add_playlist(self, playlist: Playlist) -> Playlist:
        """Add playlist to local db and return the new database item."""
        db_item = await self.mass.database.add_playlist(playlist)
        self.mass.eventbus.signal(EVENT_PLAYLIST_ADDED, db_item)
        return db_item

    async def add_radio(self, radio: Radio) -> Radio:
        """Add radio to local db and return the new database item."""
        db_item = await self.mass.database.add_radio(radio)
        self.mass.eventbus.signal(EVENT_RADIO_ADDED, db_item)
        return db_item

    async def _get_artist_musicbrainz_id(self, artist: Artist):
        """Fetch musicbrainz id by performing search using the artist name, albums and tracks."""
        # try with album first
        for lookup_album in await self._get_provider_artist_albums(
            artist.item_id, artist.provider
        ):
            if not lookup_album:
                continue
            if artist.name != lookup_album.artist.name:
                continue
            musicbrainz_id = await self.musicbrainz.get_mb_artist_id(
                artist.name,
                albumname=lookup_album.name,
                album_upc=lookup_album.upc,
            )
            if musicbrainz_id:
                return musicbrainz_id
        # fallback to track
        for lookup_track in await self._get_provider_artist_toptracks(
            artist.item_id, artist.provider
        ):
            if not lookup_track:
                continue
            musicbrainz_id = await self.musicbrainz.get_mb_artist_id(
                artist.name,
                trackname=lookup_track.name,
                track_isrc=lookup_track.isrc,
            )
            if musicbrainz_id:
                return musicbrainz_id
        # lookup failed, use the shitty workaround to use the name as id.
        LOGGER.warning("Unable to get musicbrainz ID for artist %s !", artist.name)
        return artist.name

    async def match_artist(self, db_artist: Artist):
        """
        Try to find matching artists on all providers for the provided (database) item_id.

        This is used to link objects of different providers together.
        """
        assert (
            db_artist.provider == "database"
        ), "Matching only supported for database items!"
        cur_providers = [item.provider for item in db_artist.provider_ids]
        for provider in self.mass.get_providers(ProviderType.MUSIC_PROVIDER):
            if provider.id in cur_providers:
                continue
            if MediaType.ARTIST not in provider.supported_mediatypes:
                continue
            if not await self._match_prov_artist(db_artist, provider):
                LOGGER.debug(
                    "Could not find match for Artist %s on provider %s",
                    db_artist.name,
                    provider.name,
                )

    async def _match_prov_artist(self, db_artist: Artist, provider: MusicProvider):
        """Try to find matching artists on given provider for the provided (database) artist."""
        LOGGER.debug(
            "Trying to match artist %s on provider %s", db_artist.name, provider.name
        )
        # try to get a match with some reference tracks of this artist
        for ref_track in await self.get_artist_toptracks(
            db_artist.item_id, db_artist.provider
        ):
            # make sure we have a full track
            if isinstance(ref_track.album, ItemMapping):
                ref_track = await self.get_track(ref_track.item_id, ref_track.provider)
            searchstr = "%s %s" % (db_artist.name, ref_track.name)
            search_results = await self.search_provider(
                searchstr, provider.id, [MediaType.TRACK], limit=25
            )
            for search_result_item in search_results.tracks:
                if compare_track(search_result_item, ref_track):
                    # get matching artist from track
                    for search_item_artist in search_result_item.artists:
                        if compare_strings(db_artist.name, search_item_artist.name):
                            # 100% album match
                            # get full artist details so we have all metadata
                            prov_artist = await self._get_provider_artist(
                                search_item_artist.item_id, search_item_artist.provider
                            )
                            await self.mass.database.update_artist(
                                db_artist.item_id, prov_artist
                            )
                            return True
        # try to get a match with some reference albums of this artist
        artist_albums = await self.get_artist_albums(
            db_artist.item_id, db_artist.provider
        )
        for ref_album in artist_albums:
            if ref_album.album_type == AlbumType.COMPILATION:
                continue
            searchstr = "%s %s" % (db_artist.name, ref_album.name)
            search_result = await self.search_provider(
                searchstr, provider.id, [MediaType.ALBUM], limit=25
            )
            for search_result_item in search_result.albums:
                # artist must match 100%
                if not compare_strings(db_artist.name, search_result_item.artist.name):
                    continue
                if compare_album(search_result_item, ref_album):
                    # 100% album match
                    # get full artist details so we have all metadata
                    prov_artist = await self._get_provider_artist(
                        search_result_item.artist.item_id,
                        search_result_item.artist.provider,
                    )
                    await self.mass.database.update_artist(
                        db_artist.item_id, prov_artist
                    )
                    return True
        return False

    async def match_album(self, db_album: Album):
        """
        Try to find matching album on all providers for the provided (database) album_id.

        This is used to link objects of different providers/qualities together.
        """
        assert (
            db_album.provider == "database"
        ), "Matching only supported for database items!"
        if not isinstance(db_album, FullAlbum):
            # matching only works if we have a full album object
            db_album = await self.mass.database.get_album(db_album.item_id)

        async def find_prov_match(provider):
            LOGGER.debug(
                "Trying to match album %s on provider %s", db_album.name, provider.name
            )
            match_found = False
            searchstr = "%s %s" % (db_album.artist.name, db_album.name)
            if db_album.version:
                searchstr += " " + db_album.version
            search_result = await self.search_provider(
                searchstr, provider.id, [MediaType.ALBUM], limit=25
            )
            for search_result_item in search_result.albums:
                if not search_result_item.available:
                    continue
                if not compare_album(search_result_item, db_album):
                    continue
                # we must fetch the full album version, search results are simplified objects
                prov_album = await self._get_provider_album(
                    search_result_item.item_id, search_result_item.provider
                )
                if compare_album(prov_album, db_album):
                    # 100% match, we can simply update the db with additional provider ids
                    await self.mass.database.update_album(db_album.item_id, prov_album)
                    match_found = True
                    # while we're here, also match the artist
                    if db_album.artist.provider == "database":
                        prov_artist = await self._get_provider_artist(
                            prov_album.artist.item_id, prov_album.artist.provider
                        )
                        await self.mass.database.update_artist(
                            db_album.artist.item_id, prov_artist
                        )

            # no match found
            if not match_found:
                LOGGER.debug(
                    "Could not find match for Album %s on provider %s",
                    db_album.name,
                    provider.name,
                )

        # try to find match on all providers
        providers = self.mass.get_providers(ProviderType.MUSIC_PROVIDER)
        for provider in providers:
            if MediaType.ALBUM in provider.supported_mediatypes:
                await find_prov_match(provider)

    async def match_track(self, db_track: Track):
        """
        Try to find matching track on all providers for the provided (database) track_id.

        This is used to link objects of different providers/qualities together.
        """
        assert (
            db_track.provider == "database"
        ), "Matching only supported for database items!"
        if isinstance(db_track.album, ItemMapping):
            # matching only works if we have a full track object
            db_track = await self.mass.database.get_track(db_track.item_id)
        for provider in self.mass.get_providers(ProviderType.MUSIC_PROVIDER):
            if MediaType.TRACK not in provider.supported_mediatypes:
                continue
            LOGGER.debug(
                "Trying to match track %s on provider %s", db_track.name, provider.name
            )
            match_found = False
            for db_track_artist in db_track.artists:
                if match_found:
                    break
                searchstr = "%s %s" % (db_track_artist.name, db_track.name)
                if db_track.version:
                    searchstr += " " + db_track.version
                search_result = await self.search_provider(
                    searchstr, provider.id, [MediaType.TRACK], limit=25
                )
                for search_result_item in search_result.tracks:
                    if not search_result_item.available:
                        continue
                    if compare_track(search_result_item, db_track):
                        # 100% match, we can simply update the db with additional provider ids
                        match_found = True
                        await self.mass.database.update_track(
                            db_track.item_id, search_result_item
                        )
                        # while we're here, also match the artist
                        if db_track_artist.provider == "database":
                            for artist in search_result_item.artists:
                                if not compare_strings(
                                    db_track_artist.name, artist.name
                                ):
                                    continue
                                prov_artist = await self._get_provider_artist(
                                    artist.item_id, artist.provider
                                )
                                await self.mass.database.update_artist(
                                    db_track_artist.item_id, prov_artist
                                )

            if not match_found:
                LOGGER.debug(
                    "Could not find match for Track %s on provider %s",
                    db_track.name,
                    provider.name,
                )

    async def __get_provider_id(self, media_item: MediaItem) -> tuple:
        """Return provider and item id."""
        if media_item.provider == "database":
            media_item = await self.mass.database.get_item_by_prov_id(
                "database", media_item.item_id, media_item.media_type
            )
            for prov in media_item.provider_ids:
                if prov.available and self.mass.get_provider(prov.provider):
                    provider = self.mass.get_provider(prov.provider)
                    if provider and provider.available:
                        return (prov.provider, prov.item_id)
        else:
            provider = self.mass.get_provider(media_item.provider)
            if provider and provider.available:
                return (media_item.provider, media_item.item_id)
        return None, None
