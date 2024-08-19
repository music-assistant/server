"""The provider class for Open Subsonic."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from libopensonic.connection import Connection as SonicConnection
from libopensonic.errors import (
    AuthError,
    CredentialError,
    DataNotFoundError,
    ParameterError,
    SonicError,
)

from music_assistant.common.models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant.common.models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderPermissionDenied,
)
from music_assistant.common.models.media_items import (
    Album,
    AlbumType,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.constants import (
    CONF_PASSWORD,
    CONF_PATH,
    CONF_PORT,
    CONF_USERNAME,
    UNKNOWN_ARTIST,
)
from music_assistant.server.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from libopensonic.media import Album as SonicAlbum
    from libopensonic.media import AlbumInfo as SonicAlbumInfo
    from libopensonic.media import Artist as SonicArtist
    from libopensonic.media import ArtistInfo as SonicArtistInfo
    from libopensonic.media import Playlist as SonicPlaylist
    from libopensonic.media import PodcastChannel as SonicPodcastChannel
    from libopensonic.media import PodcastEpisode as SonicPodcastEpisode
    from libopensonic.media import Song as SonicSong

CONF_BASE_URL = "baseURL"
CONF_ENABLE_PODCASTS = "enable_podcasts"
CONF_ENABLE_LEGACY_AUTH = "enable_legacy_auth"

UNKNOWN_ARTIST_ID = "fake_artist_unknown"

# We need the following prefix because of the way that Navidrome reports artists for individual
# tracks on Various Artists albums, see the note in the _parse_track() method and the handling
# in get_artist()
NAVI_VARIOUS_PREFIX = "MA-NAVIDROME-"


class OpenSonicProvider(MusicProvider):
    """Provider for Open Subsonic servers."""

    _conn: SonicConnection = None
    _enable_podcasts: bool = True
    _seek_support: bool = False

    async def handle_async_init(self) -> None:
        """Set up the music provider and test the connection."""
        port = self.config.get_value(CONF_PORT)
        if port is None:
            port = 443
        path = self.config.get_value(CONF_PATH)
        if path is None:
            path = ""
        self._conn = SonicConnection(
            self.config.get_value(CONF_BASE_URL),
            username=self.config.get_value(CONF_USERNAME),
            password=self.config.get_value(CONF_PASSWORD),
            legacyAuth=self.config.get_value(CONF_ENABLE_LEGACY_AUTH),
            port=port,
            serverPath=path,
            appName="Music Assistant",
        )
        try:
            success = await self._run_async(self._conn.ping)
            if not success:
                msg = (
                    f"Failed to connect to {self.config.get_value(CONF_BASE_URL)}, "
                    "check your settings."
                )
                raise LoginFailed(msg)
        except (AuthError, CredentialError) as e:
            msg = (
                f"Failed to connect to {self.config.get_value(CONF_BASE_URL)}, check your settings."
            )
            raise LoginFailed(msg) from e
        self._enable_podcasts = self.config.get_value(CONF_ENABLE_PODCASTS)
        try:
            ret = await self._run_async(self._conn.getOpenSubsonicExtensions)
            extensions = ret["openSubsonicExtensions"]
            for entry in extensions:
                if entry["name"] == "transcodeOffset":
                    self._seek_support = True
                    break
        except OSError:
            self.logger.info("Server does not support transcodeOffset, seeking in player provider")

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return a list of supported features."""
        return (
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
            ProviderFeature.BROWSE,
            ProviderFeature.SEARCH,
            ProviderFeature.ARTIST_ALBUMS,
            ProviderFeature.ARTIST_TOPTRACKS,
            ProviderFeature.SIMILAR_TRACKS,
            ProviderFeature.PLAYLIST_TRACKS_EDIT,
            ProviderFeature.PLAYLIST_CREATE,
        )

    @property
    def is_streaming_provider(self) -> bool:
        """
        Return True if the provider is a streaming provider.

        This literally means that the catalog is not the same as the library contents.
        For local based providers (files, plex), the catalog is the same as the library content.
        It also means that data is if this provider is NOT a streaming provider,
        data cross instances is unique, the catalog and library differs per instance.

        Setting this to True will only query one instance of the provider for search and lookups.
        Setting this to False will query all instances of this provider for search and lookups.
        """
        return False

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )

    def _parse_podcast_artist(self, sonic_channel: SonicPodcastChannel) -> Artist:
        artist = Artist(
            item_id=sonic_channel.id,
            name=sonic_channel.title,
            provider=self.instance_id,
            favorite=bool(sonic_channel.starred),
            provider_mappings={
                ProviderMapping(
                    item_id=sonic_channel.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if sonic_channel.description is not None:
            artist.metadata.description = sonic_channel.description
        if sonic_channel.original_image_url:
            artist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sonic_channel.original_image_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        return artist

    def _parse_podcast_album(self, sonic_channel: SonicPodcastChannel) -> Album:
        return Album(
            item_id=sonic_channel.id,
            provider=self.instance_id,
            name=sonic_channel.title,
            provider_mappings={
                ProviderMapping(
                    item_id=sonic_channel.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                )
            },
            album_type=AlbumType.PODCAST,
        )

    def _parse_podcast_episode(
        self, sonic_episode: SonicPodcastEpisode, sonic_channel: SonicPodcastChannel
    ) -> Track:
        return Track(
            item_id=sonic_episode.id,
            provider=self.instance_id,
            name=sonic_episode.title,
            album=self._parse_podcast_album(sonic_channel=sonic_channel),
            artists=[self._parse_podcast_artist(sonic_channel=sonic_channel)],
            duration=sonic_episode.duration if sonic_episode.duration is not None else 0,
            favorite=bool(sonic_episode.starred),
            provider_mappings={
                ProviderMapping(
                    item_id=sonic_episode.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                )
            },
        )

    async def _get_podcast_artists(self) -> list[Artist]:
        if not self._enable_podcasts:
            return []

        sonic_channels = await self._run_async(self._conn.getPodcasts, incEpisodes=False)
        artists = []
        for channel in sonic_channels:
            artists.append(self._parse_podcast_artist(channel))
        return artists

    async def _get_podcasts(self) -> list[SonicPodcastChannel]:
        if not self._enable_podcasts:
            return []
        return await self._run_async(self._conn.getPodcasts, incEpisodes=True)

    def _parse_artist(
        self, sonic_artist: SonicArtist, sonic_info: SonicArtistInfo = None
    ) -> Artist:
        artist = Artist(
            item_id=sonic_artist.id,
            name=sonic_artist.name,
            provider=self.domain,
            favorite=bool(sonic_artist.starred),
            provider_mappings={
                ProviderMapping(
                    item_id=sonic_artist.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

        if sonic_artist.cover_id:
            artist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sonic_artist.cover_id,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            ]
        else:
            artist.metadata.images = []

        if sonic_info:
            if sonic_info.biography:
                artist.metadata.description = sonic_info.biography
            if sonic_info.small_url:
                artist.metadata.images.append(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=sonic_info.small_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                )
        return artist

    def _parse_album(self, sonic_album: SonicAlbum, sonic_info: SonicAlbumInfo = None) -> Album:
        album_id = sonic_album.id
        album = Album(
            item_id=album_id,
            provider=self.domain,
            name=sonic_album.name,
            favorite=bool(sonic_album.starred),
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            year=sonic_album.year,
        )

        if sonic_album.cover_id:
            album.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sonic_album.cover_id,
                    provider=self.instance_id,
                    remotely_accessible=False,
                ),
            ]
        else:
            album.metadata.images = []

        if sonic_album.artist_id:
            album.artists.append(
                self._get_item_mapping(
                    MediaType.ARTIST,
                    sonic_album.artist_id,
                    sonic_album.artist if sonic_album.artist else UNKNOWN_ARTIST,
                )
            )
        else:
            self.logger.info(
                f"Unable to find an artist ID for album '{sonic_album.name}' with "
                f"ID '{sonic_album.id}'."
            )
            album.artists.append(
                Artist(
                    item_id=UNKNOWN_ARTIST_ID,
                    name=UNKNOWN_ARTIST,
                    provider=self.instance_id,
                    provider_mappings={
                        ProviderMapping(
                            item_id=UNKNOWN_ARTIST_ID,
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                        )
                    },
                )
            )

        if sonic_info:
            if sonic_info.small_url:
                album.metadata.images.append(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=sonic_info.small_url,
                        remotely_accessible=False,
                        provider=self.instance_id,
                    )
                )
            if sonic_info.notes:
                album.metadata.description = sonic_info.notes

        return album

    def _parse_track(self, sonic_song: SonicSong) -> Track:
        mapping = None
        if sonic_song.album_id is not None and sonic_song.album is not None:
            mapping = self._get_item_mapping(MediaType.ALBUM, sonic_song.album_id, sonic_song.album)

        track = Track(
            item_id=sonic_song.id,
            provider=self.instance_id,
            name=sonic_song.title,
            album=mapping,
            duration=sonic_song.duration if sonic_song.duration is not None else 0,
            # We are setting disc number to 0 because the standard for what is part of
            # a Open Subsonic Song is not yet set and the implementations I have checked
            # do not contain this field. We should revisit this when the spec is finished
            disc_number=0,
            favorite=bool(sonic_song.starred),
            provider_mappings={
                ProviderMapping(
                    item_id=sonic_song.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(sonic_song.content_type)
                    ),
                )
            },
            track_number=getattr(sonic_song, "track", 0),
        )

        # We need to find an artist for this track but various implementations seem to disagree
        # about where the artist with the valid ID needs to be found. We will add any artist with
        # an ID and only use UNKNOWN if none are found.

        if sonic_song.artist_id:
            track.artists.append(
                self._get_item_mapping(
                    MediaType.ARTIST,
                    sonic_song.artist_id,
                    sonic_song.artist if sonic_song.artist else UNKNOWN_ARTIST,
                )
            )

        for entry in sonic_song.artists:
            if entry.id == sonic_song.artist_id:
                continue
            if entry.id is not None and entry.name is not None:
                track.artists.append(self._get_item_mapping(MediaType.ARTIST, entry.id, entry.name))

        if not track.artists:
            if sonic_song.artist and not sonic_song.artist_id:
                # This is how Navidrome handles tracks from albums which are marked
                # 'Various Artists'. Unfortunately, we cannot lookup this artist independently
                # because it will not have an entry in the artists table so the best we can do it
                # add a 'fake' id with the proper artist name and have get_artist() check for this
                # id and handle it locally.
                artist = Artist(
                    item_id=f"{NAVI_VARIOUS_PREFIX}{sonic_song.artist}",
                    provider=self.domain,
                    name=sonic_song.artist,
                    provider_mappings={
                        ProviderMapping(
                            item_id=UNKNOWN_ARTIST_ID,
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                        )
                    },
                )
            else:
                self.logger.info(
                    f"Unable to find artist ID for track '{sonic_song.title}' with "
                    f"ID '{sonic_song.id}'."
                )
                artist = Artist(
                    item_id=UNKNOWN_ARTIST_ID,
                    name=UNKNOWN_ARTIST,
                    provider=self.instance_id,
                    provider_mappings={
                        ProviderMapping(
                            item_id=UNKNOWN_ARTIST_ID,
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                        )
                    },
                )

            track.artists.append(artist)
        return track

    def _parse_playlist(self, sonic_playlist: SonicPlaylist) -> Playlist:
        playlist = Playlist(
            item_id=sonic_playlist.id,
            provider=self.domain,
            name=sonic_playlist.name,
            is_editable=True,
            favorite=bool(sonic_playlist.starred),
            provider_mappings={
                ProviderMapping(
                    item_id=sonic_playlist.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if sonic_playlist.cover_id:
            playlist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sonic_playlist.cover_id,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            ]
        return playlist

    async def _run_async(self, call: Callable, *args, **kwargs):
        return await self.mass.create_task(call, *args, **kwargs)

    async def resolve_image(self, path: str) -> bytes:
        """Return the image."""

        def _get_cover_art() -> bytes:
            with self._conn.getCoverArt(path) as art:
                return art.content

        return await asyncio.to_thread(_get_cover_art)

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 20
    ) -> SearchResults:
        """Search the sonic library."""
        artists = limit if MediaType.ARTIST in media_types else 0
        albums = limit if MediaType.ALBUM in media_types else 0
        songs = limit if MediaType.TRACK in media_types else 0
        if not (artists or albums or songs):
            return SearchResults()
        answer = await self._run_async(
            self._conn.search3,
            query=search_query,
            artistCount=artists,
            artistOffset=0,
            albumCount=albums,
            albumOffset=0,
            songCount=songs,
            songOffset=0,
            musicFolderId=None,
        )
        return SearchResults(
            artists=[self._parse_artist(entry) for entry in answer["artists"]],
            albums=[self._parse_album(entry) for entry in answer["albums"]],
            tracks=[self._parse_track(entry) for entry in answer["songs"]],
        )

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Provide a generator for reading all artists."""
        indices = await self._run_async(self._conn.getArtists)
        for index in indices:
            for artist in index.artists:
                yield self._parse_artist(artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """
        Provide a generator for reading all artists.

        Note the pagination, the open subsonic docs say that this method is limited to
        returning 500 items per invocation.
        """
        offset = 0
        size = 500
        albums = await self._run_async(
            self._conn.getAlbumList2, ltype="alphabeticalByArtist", size=size, offset=offset
        )
        while albums:
            for album in albums:
                yield self._parse_album(album)
            offset += size
            albums = await self._run_async(
                self._conn.getAlbumList2, ltype="alphabeticalByArtist", size=size, offset=offset
            )

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Provide a generator for library playlists."""
        results = await self._run_async(self._conn.getPlaylists)
        for entry in results:
            yield self._parse_playlist(entry)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """
        Provide a generator for library tracks.

        Note the lack of item count on this method.
        """
        query = ""
        offset = 0
        count = 500
        try:
            results = await self._run_async(
                self._conn.search3,
                query=query,
                artistCount=0,
                albumCount=0,
                songOffset=offset,
                songCount=count,
            )
        except ParameterError:
            # Older Navidrome does not accept an empty string and requires the empty quotes
            query = '""'
            results = await self._run_async(
                self._conn.search3,
                query=query,
                artistCount=0,
                albumCount=0,
                songOffset=offset,
                songCount=count,
            )
        while results["songs"]:
            for entry in results["songs"]:
                yield self._parse_track(entry)
            offset += count
            results = await self._run_async(
                self._conn.search3,
                query=query,
                artistCount=0,
                albumCount=0,
                songOffset=offset,
                songCount=count,
            )

    async def get_album(self, prov_album_id: str) -> Album:
        """Return the requested Album."""
        try:
            sonic_album: SonicAlbum = await self._run_async(self._conn.getAlbum, prov_album_id)
            sonic_info = await self._run_async(self._conn.getAlbumInfo2, aid=prov_album_id)
        except (ParameterError, DataNotFoundError) as e:
            if self._enable_podcasts:
                # This might actually be a 'faked' album from podcasts, try that before giving up
                try:
                    sonic_channel = await self._run_async(
                        self._conn.getPodcasts, incEpisodes=False, pid=prov_album_id
                    )
                    return self._parse_podcast_album(sonic_channel=sonic_channel)
                except SonicError:
                    pass
            msg = f"Album {prov_album_id} not found"
            raise MediaNotFoundError(msg) from e

        return self._parse_album(sonic_album, sonic_info)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Return a list of tracks on the specified Album."""
        try:
            sonic_album: SonicAlbum = await self._run_async(self._conn.getAlbum, prov_album_id)
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Album {prov_album_id} not found"
            raise MediaNotFoundError(msg) from e
        tracks = []
        for sonic_song in sonic_album.songs:
            tracks.append(self._parse_track(sonic_song))
        return tracks

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Return the requested Artist."""
        if prov_artist_id == UNKNOWN_ARTIST_ID:
            return Artist(
                item_id=UNKNOWN_ARTIST_ID,
                name=UNKNOWN_ARTIST,
                provider=self.instance_id,
                provider_mappings={
                    ProviderMapping(
                        item_id=UNKNOWN_ARTIST_ID,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
        elif prov_artist_id.startswith(NAVI_VARIOUS_PREFIX):
            # Special case for handling track artists on various artists album for Navidrome.
            return Artist(
                item_id=prov_artist_id,
                name=prov_artist_id.removeprefix(NAVI_VARIOUS_PREFIX),
                provider=self.instance_id,
                provider_mappings={
                    ProviderMapping(
                        item_id=prov_artist_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )

        try:
            sonic_artist: SonicArtist = await self._run_async(
                self._conn.getArtist, artist_id=prov_artist_id
            )
            sonic_info = await self._run_async(self._conn.getArtistInfo2, aid=prov_artist_id)
        except (ParameterError, DataNotFoundError) as e:
            if self._enable_podcasts:
                # This might actually be a 'faked' artist from podcasts, try that before giving up
                try:
                    sonic_channel = await self._run_async(
                        self._conn.getPodcasts, incEpisodes=False, pid=prov_artist_id
                    )
                    return self._parse_podcast_artist(sonic_channel=sonic_channel[0])
                except SonicError:
                    pass
            msg = f"Artist {prov_artist_id} not found"
            raise MediaNotFoundError(msg) from e
        return self._parse_artist(sonic_artist, sonic_info)

    async def get_track(self, prov_track_id: str) -> Track:
        """Return the specified track."""
        try:
            sonic_song: SonicSong = await self._run_async(self._conn.getSong, prov_track_id)
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Item {prov_track_id} not found"
            raise MediaNotFoundError(msg) from e
        return self._parse_track(sonic_song)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Return a list of all Albums by specified Artist."""
        if prov_artist_id == UNKNOWN_ARTIST_ID or prov_artist_id.startswith(NAVI_VARIOUS_PREFIX):
            return []

        try:
            sonic_artist: SonicArtist = await self._run_async(self._conn.getArtist, prov_artist_id)
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Album {prov_artist_id} not found"
            raise MediaNotFoundError(msg) from e
        albums = []
        for entry in sonic_artist.albums:
            albums.append(self._parse_album(entry))
        return albums

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Return the specified Playlist."""
        try:
            sonic_playlist: SonicPlaylist = await self._run_async(
                self._conn.getPlaylist, prov_playlist_id
            )
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Playlist {prov_playlist_id} not found"
            raise MediaNotFoundError(msg) from e
        return self._parse_playlist(sonic_playlist)

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        if page > 0:
            # paging not supported, we always return the whole list at once
            return result
        try:
            sonic_playlist: SonicPlaylist = await self._run_async(
                self._conn.getPlaylist, prov_playlist_id
            )
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Playlist {prov_playlist_id} not found"
            raise MediaNotFoundError(msg) from e

        # TODO: figure out if subsonic supports paging here
        for index, sonic_song in enumerate(sonic_playlist.songs, 1):
            track = self._parse_track(sonic_song)
            track.position = index
            result.append(track)
        return result

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get the top listed tracks for a specified artist."""
        # We have seen top tracks requested for the UNKNOWN_ARTIST ID, protect against that
        if prov_artist_id == UNKNOWN_ARTIST_ID or prov_artist_id.startswith(NAVI_VARIOUS_PREFIX):
            return []

        try:
            sonic_artist: SonicArtist = await self._run_async(self._conn.getArtist, prov_artist_id)
        except DataNotFoundError as e:
            msg = f"Artist {prov_artist_id} not found"
            raise MediaNotFoundError(msg) from e
        songs: list[SonicSong] = await self._run_async(self._conn.getTopSongs, sonic_artist.name)
        return [self._parse_track(entry) for entry in songs]

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get tracks similar to selected track."""
        songs: list[SonicSong] = await self._run_async(
            self._conn.getSimilarSongs2, iid=prov_track_id, count=limit
        )
        return [self._parse_track(entry) for entry in songs]

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new empty playlist on the server."""
        playlist: SonicPlaylist = await self._run_async(self._conn.createPlaylist, name=name)
        return self._parse_playlist(playlist)

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Append the listed tracks to the selected playlist.

        Note that the configured user must own the playlist to edit this way.
        """
        try:
            await self._run_async(
                self._conn.updatePlaylist, lid=prov_playlist_id, songIdsToAdd=prov_track_ids
            )
        except SonicError:
            msg = f"Failed to add songs to {prov_playlist_id}, check your permissions."
            raise ProviderPermissionDenied(msg)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove selected positions from the playlist."""
        idx_to_remove = [pos - 1 for pos in positions_to_remove]
        try:
            await self._run_async(
                self._conn.updatePlaylist,
                lid=prov_playlist_id,
                songIndexesToRemove=idx_to_remove,
            )
        except SonicError:
            msg = f"Failed to remove songs from {prov_playlist_id}, check your permissions."
            raise ProviderPermissionDenied(msg)

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get the details needed to process a specified track."""
        try:
            sonic_song: SonicSong = await self._run_async(self._conn.getSong, item_id)
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Item {item_id} not found"
            raise MediaNotFoundError(msg) from e

        self.mass.create_task(self._report_playback_started(item_id))

        mime_type = sonic_song.content_type
        if mime_type.endswith("mpeg"):
            mime_type = sonic_song.suffix

        self.logger.debug(
            "Fetching stream details for id %s '%s' with format '%s'",
            sonic_song.id,
            sonic_song.title,
            mime_type,
        )

        return StreamDetails(
            item_id=sonic_song.id,
            provider=self.instance_id,
            can_seek=self._seek_support,
            audio_format=AudioFormat(content_type=ContentType.try_parse(mime_type)),
            stream_type=StreamType.CUSTOM,
            duration=sonic_song.duration if sonic_song.duration is not None else 0,
        )

    async def _report_playback_started(self, item_id: str) -> None:
        await self._run_async(self._conn.scrobble, sid=item_id, submission=False)

    async def on_streamed(self, streamdetails: StreamDetails, seconds_streamed: int) -> None:
        """Handle callback when an item completed streaming."""
        if seconds_streamed >= streamdetails.duration / 2:
            await self._run_async(self._conn.scrobble, sid=streamdetails.item_id, submission=True)

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Provide a generator for the stream data."""
        audio_buffer = asyncio.Queue(1)

        self.logger.debug("Streaming %s", streamdetails.item_id)

        def _streamer() -> None:
            with self._conn.stream(
                streamdetails.item_id, timeOffset=seek_position, estimateContentLength=True
            ) as stream:
                for chunk in stream.iter_content(chunk_size=40960):
                    asyncio.run_coroutine_threadsafe(
                        audio_buffer.put(chunk), self.mass.loop
                    ).result()
            # send empty chunk when we're done
            asyncio.run_coroutine_threadsafe(audio_buffer.put(b"EOF"), self.mass.loop).result()

        # fire up an executor thread to put the audio chunks (threadsafe) on the audio buffer
        streamer_task = self.mass.loop.run_in_executor(None, _streamer)
        try:
            while True:
                # keep reading from the audio buffer until there is no more data
                chunk = await audio_buffer.get()
                if chunk == b"EOF":
                    break
                yield chunk
        finally:
            if not streamer_task.done():
                streamer_task.cancel()

        self.logger.debug("Done streaming %s", streamdetails.item_id)
