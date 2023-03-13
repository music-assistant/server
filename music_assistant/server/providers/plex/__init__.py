import asyncio
from typing import AsyncGenerator, Callable

from plexapi.library import MusicSection as PlexMusicSection
from plexapi.myplex import MyPlexAccount
from plexapi.audio import Album as PlexAlbum
from plexapi.audio import Track as PlexTrack
from plexapi.audio import Playlist as PlexPlaylist
from plexapi.audio import Artist as PlexArtist

from music_assistant.common.models.enums import ProviderFeature, MediaType, ImageType, ContentType
from music_assistant.common.models.errors import LoginFailed, InvalidDataError, MediaNotFoundError
from music_assistant.common.models.media_items import StreamDetails, Track, Playlist, Radio, Album, Artist, \
    MediaItemType, MediaItemImage, ProviderMapping
from music_assistant.server.models.music_provider import MusicProvider
from plexapi.server import PlexServer


AUTH_TOKEN = "token"
SERVER_NAME = "server"
LIBRARY_NAME = "library"


class PlexProvider(MusicProvider):

    _plex_server: PlexServer = None
    _plex_library: PlexMusicSection = None

    async def setup(self) -> None:
        self._attr_supported_features = (
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.BROWSE,
            ProviderFeature.SEARCH,
            ProviderFeature.ARTIST_ALBUMS,
        )

        if not self.config.get_value(AUTH_TOKEN):
            raise LoginFailed("Invalid login credentials")

        def connect():
            plex_account = MyPlexAccount(token=self.config.get_value(AUTH_TOKEN))
            return plex_account.resource(self.config.get_value(SERVER_NAME)).connect()

        self._plex_server = await self._run_async(connect)
        self._plex_library = await self._run_async(self._plex_server.library.section, self.config.get_value(LIBRARY_NAME))

    @classmethod
    async def _run_async(cls, call: Callable, *args, **kwargs):
        return await asyncio.to_thread(call, *args, **kwargs)

    async def _get_data(self, key, cls=None):
        return await self._run_async(self._plex_library.fetchItem, key, cls)

    async def _parse(self, plex_media):
        if plex_media.type == "artist":
            return await self._parse_artist(plex_media)
        elif plex_media.type == "album":
            return await self._parse_album(plex_media)
        elif plex_media.type == "track":
            return await self._parse_track(plex_media)
        elif plex_media.type == "playlist":
            return await self._parse_playlist(plex_media)
        return None

    async def _search_track(self, search_query, limit):
        return await self._run_async(self._plex_library.searchTracks, title=search_query, limit=limit)

    async def _search_album(self, search_query, limit):
        return await self._run_async(self._plex_library.searchAlbums, title=search_query, limit=limit)

    async def _search_artist(self, search_query, limit):
        return await self._run_async(self._plex_library.searchArtists, title=search_query, limit=limit)

    async def _search_playlist(self, search_query, limit):
        return await self._run_async(self._plex_library.playlists, title=search_query, limit=limit)

    async def _search_track_advanced(self, limit, **kwargs):
        return await self._run_async(self._plex_library.searchTracks, filters=kwargs, limit=limit)

    async def _search_album_advanced(self, limit, **kwargs):
        return await self._run_async(self._plex_library.searchAlbums, filters=kwargs, limit=limit)

    async def _search_artist_advanced(self, limit, **kwargs):
        return await self._run_async(self._plex_library.searchArtists, filters=kwargs, limit=limit)

    async def _search_playlist_advanced(self, limit, **kwargs):
        return await self._run_async(self._plex_library.playlists, filters=kwargs, limit=limit)

    async def _parse_album(self, plex_album: PlexAlbum) -> Album:
        """Parse a Plex Album response to an Album model object."""
        album_id = plex_album.key
        album = Album(
            item_id=album_id,
            name=plex_album.title,
            provider=self.domain,
        )
        if plex_album.year:
            album.year = plex_album.year
        if plex_album.thumbUrl:
            album.metadata.images = [MediaItemImage(ImageType.THUMB, plex_album.thumbUrl, True)]
        if plex_album.summary:
            album.metadata.description = plex_album.summary

        album.artist = await self._parse_artist(await asyncio.to_thread(plex_album.artist))

        album.add_provider_mapping(
            ProviderMapping(
                item_id=str(album_id),
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=plex_album.getWebURL(),
            )
        )
        return album

    async def _parse_artist(self, plex_artist: PlexArtist) -> Artist:
        """Parse a Plex Artist response to Artist model object."""
        artist_id = plex_artist.key
        if not artist_id:
            raise InvalidDataError("Artist does not have a valid ID")
        artist = Artist(item_id=artist_id, name=plex_artist.title, provider=self.domain)
        if plex_artist.summary:
            artist.metadata.description = plex_artist.summary
        if plex_artist.thumbUrl:
            artist.metadata.images = [MediaItemImage(ImageType.THUMB, plex_artist.thumbUrl, True)]
        artist.add_provider_mapping(
            ProviderMapping(
                item_id=str(artist_id),
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=plex_artist.getWebURL(),
            )
        )
        return artist

    async def _parse_playlist(self, plex_playlist: PlexPlaylist) -> Playlist:
        """Parse a Plex Playlist response to a Playlist object."""
        playlist = Playlist(
            item_id=plex_playlist.key, provider=self.domain, name=plex_playlist.title
        )
        if plex_playlist.summary:
            playlist.metadata.description = plex_playlist.summary
        if plex_playlist.thumbUrl:
            playlist.metadata.images = [MediaItemImage(ImageType.THUMB, plex_playlist.thumbUrl, True)]
        playlist.is_editable = True
        playlist.add_provider_mapping(
            ProviderMapping(
                item_id=plex_playlist.key,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=plex_playlist.getWebURL(),
            )
        )
        return playlist

    async def _parse_track(self, plex_track: PlexTrack) -> Track:
        """Parse a Plex Track response to a Track model object."""
        track = Track(item_id=plex_track.key, provider=self.domain, name=plex_track.title)

        track.artist = await self._parse_artist(
            await self._run_async(plex_track.artist)
        )

        # guard that track has valid artists
        if plex_track.thumbUrl:
            track.metadata.images = [MediaItemImage(ImageType.THUMB, plex_track.thumbUrl, True)]
        if plex_album := await self._run_async(plex_track.album):
            track.album = await self._parse_album(plex_album)
        if plex_track.duration:
            track.duration = int(plex_track.duration/1000)
        if plex_track.trackNumber:
            track.track_number = plex_track.trackNumber
        if plex_track.parentIndex:
            track.disc_number = plex_track.parentIndex
        available = False
        content = None

        if plex_track.media:
            available = True
            content = plex_track.media[0].container

        track.add_provider_mapping(
            ProviderMapping(
                item_id=plex_track.key,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                available=available,
                content_type=ContentType.try_parse(content) if content else None,
                url=plex_track.getWebURL(),
            )
        )
        return track

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType] | None = None,
        limit: int = 5,
    ) -> list[MediaItemType]:
        if not media_types:
            media_types = [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK, MediaType.PLAYLIST]

        tasks = []

        for media_type in media_types:
            if media_type == MediaType.ARTIST:
                tasks.append(self._search_artist(search_query, limit))
            if media_type == MediaType.ALBUM:
                tasks.append(self._search_album(search_query, limit))
            if media_type == MediaType.TRACK:
                tasks.append(self._search_track(search_query, limit))
            if media_type == MediaType.PLAYLIST:
                tasks.append(self._search_playlist(search_query, limit))

        search_results = await asyncio.gather(*tasks)

        results = []
        for task_result in search_results:
            for plex_result in task_result:
                results.append(await self._parse(plex_result))

        return results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Plex Music."""
        artists_obj = await self._run_async(
            self._plex_library.all
        )
        for artist in artists_obj:
            yield await self._parse_artist(artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Plex Music."""
        albums_obj = await self._run_async(
            self._plex_library.albums
        )
        for album in albums_obj:
            yield await self._parse_album(album)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlists_obj = await self._run_async(
            self._plex_library.playlists
        )
        for playlist in playlists_obj:
            yield await self._parse_playlist(playlist)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Plex Music."""
        tracks_obj = await self._search_track(
            None, limit=9999
        )
        for track in tracks_obj:
            yield await self._parse_track(track)

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        plex_album = await self._get_data(prov_album_id, PlexAlbum)
        return (
            await self._parse_album(plex_album)
            if plex_album
            else None
        )

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        plex_album = await self._get_data(prov_album_id, PlexAlbum)

        tracks = []
        for plex_track in await self._run_async(plex_album.tracks):
            track = await self._parse_track(plex_track)
            tracks.append(track)
        return tracks

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        plex_artist = await self._get_data(prov_artist_id, PlexArtist)
        return await self._parse_artist(plex_artist) if plex_artist else None

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        plex_track = await self._get_data(prov_track_id, PlexTrack)
        return await self._parse_track(plex_track)

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        plex_playlist = await self._get_data(prov_playlist_id, PlexPlaylist)
        return await self._parse_playlist(plex_playlist)

    async def get_playlist_tracks(self, prov_playlist_id) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        plex_playlist = await self._get_data(prov_playlist_id, PlexPlaylist)

        playlist_items = await self._run_async(
            plex_playlist.items
        )

        if not playlist_items:
            return []
        tracks = []
        for index, track in enumerate(playlist_items):
            track = await self._parse_track(track)
            if track:
                track.position = index
                tracks.append(track)
        return tracks

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of albums for the given artist."""
        plex_artist = await self._get_data(prov_artist_id, PlexArtist)
        plex_albums = await self._run_async(plex_artist.albums)
        if plex_albums:
            albums = []
            for album_obj in plex_albums:
                albums.append(await self._parse_album(album_obj))
            return albums
        return []

    async def get_stream_details(self, item_id: str) -> StreamDetails | None:
        """Get streamdetails for a track."""
        plex_track = await self._get_data(item_id, PlexTrack)
        if not plex_track or not plex_track.media:
            raise MediaNotFoundError(f"track {item_id} not found")

        media = plex_track.media[0]

        return StreamDetails(
            item_id=plex_track.key,
            provider=self.domain,
            content_type=ContentType.try_parse(media.container),
            duration=plex_track.duration,
            channels=media.audioChannels,
            direct=self._plex_server.url(media.parts[0].key, True),
        )
