"""The provider class for Open Subsonic."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from libopensonic.connection import Connection as SonicConnection
from libopensonic.errors import (
    AuthError,
    CredentialError,
    DataNotFoundError,
    ParameterError,
    SonicError,
)
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    ActionUnavailable,
    LoginFailed,
    MediaNotFoundError,
    ProviderPermissionDenied,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    CONF_PASSWORD,
    CONF_PATH,
    CONF_PORT,
    CONF_USERNAME,
    UNKNOWN_ARTIST,
)
from music_assistant.models.music_provider import MusicProvider

from .parsers import parse_album, parse_artist

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from libopensonic.media import Album as SonicAlbum
    from libopensonic.media import Artist as SonicArtist
    from libopensonic.media import Playlist as SonicPlaylist
    from libopensonic.media import PodcastChannel as SonicPodcast
    from libopensonic.media import PodcastEpisode as SonicEpisode
    from libopensonic.media import Song as SonicSong

CONF_BASE_URL = "baseURL"
CONF_ENABLE_PODCASTS = "enable_podcasts"
CONF_ENABLE_LEGACY_AUTH = "enable_legacy_auth"
CONF_OVERRIDE_OFFSET = "override_transcode_offest"

UNKNOWN_ARTIST_ID = "fake_artist_unknown"

# We need the following prefix because of the way that Navidrome reports artists for individual
# tracks on Various Artists albums, see the note in the _parse_track() method and the handling
# in get_artist()
NAVI_VARIOUS_PREFIX = "MA-NAVIDROME-"


# Because of some subsonic API weirdness, we have to lookup any podcast episode by finding it in
# the list of episodes in a channel, to facilitate, we will use both the episode id and the
# channel id concatenated as an episode id to MA
EP_CHAN_SEP = "$!$"


Param = ParamSpec("Param")
RetType = TypeVar("RetType")


class OpenSonicProvider(MusicProvider):
    """Provider for Open Subsonic servers."""

    _conn: SonicConnection = None
    _enable_podcasts: bool = True
    _seek_support: bool = False
    _ignore_offset: bool = False

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
                raise CredentialError
        except (AuthError, CredentialError) as e:
            msg = (
                f"Failed to connect to {self.config.get_value(CONF_BASE_URL)}, check your settings."
            )
            raise LoginFailed(msg) from e
        self._enable_podcasts = bool(self.config.get_value(CONF_ENABLE_PODCASTS))
        self._ignore_offset = bool(self.config.get_value(CONF_OVERRIDE_OFFSET))
        try:
            ret = await self._run_async(self._conn.getOpenSubsonicExtensions)
            extensions = ret["openSubsonicExtensions"]
            for entry in extensions:
                if entry["name"] == "transcodeOffset" and not self._ignore_offset:
                    self._seek_support = True
                    break
        except OSError:
            self.logger.info("Server does not support transcodeOffset, seeking in player provider")

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return a list of supported features."""
        return {
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
            ProviderFeature.LIBRARY_PODCASTS,
            ProviderFeature.LIBRARY_PODCASTS_EDIT,
        }

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

    def _parse_track(
        self, sonic_song: SonicSong, album: Album | ItemMapping | None = None
    ) -> Track:
        # Unfortunately, the Song response type is not defined in the open subsonic spec so we have
        # implementations which disagree about where the album id for this song should be stored.
        # We accept either song.ablum_id or song.parent but prefer album_id.
        if not album:
            if sonic_song.album_id and sonic_song.album:
                album = self._get_item_mapping(
                    MediaType.ALBUM, sonic_song.album_id, sonic_song.album
                )
            elif sonic_song.parent and sonic_song.album:
                album = self._get_item_mapping(MediaType.ALBUM, sonic_song.parent, sonic_song.album)

        track = Track(
            item_id=sonic_song.id,
            provider=self.instance_id,
            name=sonic_song.title,
            album=album,
            duration=sonic_song.duration if sonic_song.duration is not None else 0,
            disc_number=sonic_song.disc_number or 0,
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
                fake_id = f"{NAVI_VARIOUS_PREFIX}{sonic_song.artist}"
                artist = Artist(
                    item_id=fake_id,
                    provider=self.domain,
                    name=sonic_song.artist,
                    provider_mappings={
                        ProviderMapping(
                            item_id=fake_id,
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                        )
                    },
                )
            else:
                self.logger.info(
                    "Unable to find artist ID for track '%s' with ID '%s'.",
                    sonic_song.title,
                    sonic_song.id,
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
            playlist.metadata.images = UniqueList()
            playlist.metadata.images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sonic_playlist.cover_id,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            )
        return playlist

    def _parse_podcast(self, sonic_podcast: SonicPodcast) -> Podcast:
        podcast = Podcast(
            item_id=sonic_podcast.id,
            provider=self.domain,
            name=sonic_podcast.title,
            uri=sonic_podcast.url,
            total_episodes=len(sonic_podcast.episodes),
            provider_mappings={
                ProviderMapping(
                    item_id=sonic_podcast.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

        podcast.metadata.description = sonic_podcast.description
        podcast.metadata.images = UniqueList()

        if sonic_podcast.cover_id:
            podcast.metadata.images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sonic_podcast.cover_id,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            )

        return podcast

    def _parse_epsiode(
        self, sonic_episode: SonicEpisode, sonic_channel: SonicPodcast
    ) -> PodcastEpisode:
        eid = f"{sonic_episode.channel_id}{EP_CHAN_SEP}{sonic_episode.id}"
        pos = 1
        for ep in sonic_channel.episodes:
            if ep.id == sonic_episode.id:
                break
            pos += 1

        episode = PodcastEpisode(
            item_id=eid,
            provider=self.domain,
            name=sonic_episode.title,
            position=pos,
            podcast=self._parse_podcast(sonic_channel),
            provider_mappings={
                ProviderMapping(
                    item_id=eid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            duration=sonic_episode.duration,
        )

        if sonic_episode.description:
            episode.metadata.description = sonic_episode.description

        return episode

    async def _get_podcast_episode(self, eid: str) -> SonicEpisode:
        chan_id, ep_id = eid.split(EP_CHAN_SEP)
        chan = await self._run_async(self._conn.getPodcasts, incEpisodes=True, pid=chan_id)

        for episode in chan[0].episodes:
            if episode.id == ep_id:
                return episode

        msg = f"Can't find episode {ep_id} in podcast {chan_id}"
        raise MediaNotFoundError(msg)

    async def _run_async(
        self, call: Callable[Param, RetType], *args: Param.args, **kwargs: Param.kwargs
    ) -> RetType:
        return await asyncio.to_thread(call, *args, **kwargs)

    async def resolve_image(self, path: str) -> bytes | Any:
        """Return the image."""

        def _get_cover_art() -> bytes | Any:
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
            artists=[parse_artist(self.instance_id, entry) for entry in answer["artists"]],
            albums=[
                parse_album(self.logger, self.instance_id, entry) for entry in answer["albums"]
            ],
            tracks=[self._parse_track(entry) for entry in answer["songs"]],
        )

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Provide a generator for reading all artists."""
        indices = await self._run_async(self._conn.getArtists)
        for index in indices:
            for artist in index.artists:
                yield parse_artist(self.instance_id, artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """
        Provide a generator for reading all artists.

        Note the pagination, the open subsonic docs say that this method is limited to
        returning 500 items per invocation.
        """
        offset = 0
        size = 500
        albums = await self._run_async(
            self._conn.getAlbumList2,
            ltype="alphabeticalByArtist",
            size=size,
            offset=offset,
        )
        while albums:
            for album in albums:
                yield parse_album(self.logger, self.instance_id, album)
            offset += size
            albums = await self._run_async(
                self._conn.getAlbumList2,
                ltype="alphabeticalByArtist",
                size=size,
                offset=offset,
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
            album: Album | None = None
            for entry in results["songs"]:
                aid = entry.album_id if entry.album_id else entry.parent
                if album is None or album.item_id != aid:
                    album = await self.get_album(prov_album_id=aid)
                yield self._parse_track(entry, album=album)
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
            msg = f"Album {prov_album_id} not found"
            raise MediaNotFoundError(msg) from e

        return parse_album(self.logger, self.instance_id, sonic_album, sonic_info)

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
            msg = f"Artist {prov_artist_id} not found"
            raise MediaNotFoundError(msg) from e
        return parse_artist(self.instance_id, sonic_artist, sonic_info)

    async def get_track(self, prov_track_id: str) -> Track:
        """Return the specified track."""
        try:
            sonic_song: SonicSong = await self._run_async(self._conn.getSong, prov_track_id)
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Item {prov_track_id} not found"
            raise MediaNotFoundError(msg) from e
        aid = sonic_song.album_id if sonic_song.album_id else sonic_song.parent
        if not aid:
            self.logger.warning("Unable to find album id for track %s", sonic_song.id)
        album: Album = await self.get_album(prov_album_id=aid)
        return self._parse_track(sonic_song, album=album)

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
            albums.append(parse_album(self.logger, self.instance_id, entry))
        return albums

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return the specified Playlist."""
        try:
            sonic_playlist: SonicPlaylist = await self._run_async(
                self._conn.getPlaylist, prov_playlist_id
            )
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Playlist {prov_playlist_id} not found"
            raise MediaNotFoundError(msg) from e
        return self._parse_playlist(sonic_playlist)

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        podcast_id, _ = prov_episode_id.split(EP_CHAN_SEP)
        async for episode in self.get_podcast_episodes(podcast_id):
            if episode.item_id == prov_episode_id:
                return episode
        msg = f"Episode {prov_episode_id} not found"
        raise MediaNotFoundError(msg)

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all Episodes for given podcast id."""
        if not self._enable_podcasts:
            return
        channels = await self._run_async(
            self._conn.getPodcasts, incEpisodes=True, pid=prov_podcast_id
        )
        channel = channels[0]
        for episode in channel.episodes:
            yield self._parse_epsiode(episode, channel)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full Podcast details by id."""
        if not self._enable_podcasts:
            msg = "Podcasts are currently disabled in the provider configuration"
            raise ActionUnavailable(msg)

        channels = await self._run_async(
            self._conn.getPodcasts, incEpisodes=True, pid=prov_podcast_id
        )

        return self._parse_podcast(channels[0])

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library/subscribed podcasts from the provider."""
        if self._enable_podcasts:
            channels = await self._run_async(self._conn.getPodcasts, incEpisodes=True)

            for channel in channels:
                yield self._parse_podcast(channel)

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

        album: Album | None = None
        for index, sonic_song in enumerate(sonic_playlist.songs, 1):
            aid = sonic_song.album_id if sonic_song.album_id else sonic_song.parent
            if not aid:
                self.logger.warning("Unable to find albumd for track %s", sonic_song.id)
            if not album or album.item_id != aid:
                album = await self.get_album(prov_album_id=aid)
            track = self._parse_track(sonic_song, album=album)
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
        try:
            songs: list[SonicSong] = await self._run_async(
                self._conn.getSimilarSongs, iid=prov_track_id, count=limit
            )
        except DataNotFoundError as e:
            # Subsonic returns an error here instead of an empty list, I don't think this
            # should be an exception but there we are. Return an empty list because this
            # exception means we didn't find anything similar.
            self.logger.info(e)
            return []
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
                self._conn.updatePlaylist,
                lid=prov_playlist_id,
                songIdsToAdd=prov_track_ids,
            )
        except SonicError as ex:
            msg = f"Failed to add songs to {prov_playlist_id}, check your permissions."
            raise ProviderPermissionDenied(msg) from ex

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
        except SonicError as ex:
            msg = f"Failed to remove songs from {prov_playlist_id}, check your permissions."
            raise ProviderPermissionDenied(msg) from ex

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get the details needed to process a specified track."""
        item: SonicSong | SonicEpisode
        if media_type == MediaType.TRACK:
            try:
                item = await self._run_async(self._conn.getSong, item_id)
            except (ParameterError, DataNotFoundError) as e:
                msg = f"Item {item_id} not found"
                raise MediaNotFoundError(msg) from e

            self.logger.debug(
                "Fetching stream details for id %s '%s' with format '%s'",
                item.id,
                item.title,
                item.content_type,
            )

        elif media_type == MediaType.PODCAST_EPISODE:
            item = await self._get_podcast_episode(item_id)

            self.logger.debug(
                "Fetching stream details for podcast episode '%s' with format '%s'",
                item.id,
                item.content_type,
            )
        else:
            msg = f"Unsupported media type encountered '{media_type}'"
            raise UnsupportedFeaturedException(msg)

        mime_type = item.content_type
        # For mp4 or m4a files, better to let ffmpeg detect the codec in use so mark them unknown
        if mime_type.endswith("mp4"):
            self.logger.warning(
                "Due to the streaming method used by the subsonic API, M4A files "
                "may fail. See provider documentation for more information."
            )
            mime_type = "?"
        # For mp3 files, ffmpeg wants to be told 'mp3' instead of 'audio/mpeg'
        elif mime_type.endswith("mpeg"):
            mime_type = item.suffix

        return StreamDetails(
            item_id=item.id,
            provider=self.instance_id,
            allow_seek=True,
            can_seek=self._seek_support,
            media_type=media_type,
            audio_format=AudioFormat(content_type=ContentType.try_parse(mime_type)),
            stream_type=StreamType.CUSTOM,
            duration=item.duration if item.duration else 0,
        )

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """
        Handle callback when a (playable) media item has been played.

        This is called by the Queue controller when;
            - a track has been fully played
            - a track has been stopped (or skipped) after being played
            - every 30s when a track is playing

        Fully played is True when the track has been played to the end.

        Position is the last known position of the track in seconds, to sync resume state.
        When fully_played is set to false and position is 0,
        the user marked the item as unplayed in the UI.

        is_playing is True when the track is currently playing.

        media_item is the full media item details of the played/playing track.
        """
        # Leave this function as the place where we will create a bookmark for podcasts when they
        # are stopped early and delete the bookmark when they are finished.

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Provide a generator for the stream data."""
        audio_buffer: asyncio.Queue[bytes] = asyncio.Queue(1)
        # ignore seek position if the server does not support it
        # in that case we let the core handle seeking
        if not self._seek_support:
            seek_position = 0

        self.logger.debug("Streaming %s", streamdetails.item_id)

        def _streamer() -> None:
            self.logger.debug("starting stream of item '%s'", streamdetails.item_id)
            try:
                with self._conn.stream(
                    streamdetails.item_id,
                    timeOffset=seek_position,
                    estimateContentLength=True,
                ) as stream:
                    for chunk in stream.iter_content(chunk_size=40960):
                        asyncio.run_coroutine_threadsafe(
                            audio_buffer.put(chunk), self.mass.loop
                        ).result()
                # send empty chunk when we're done
                asyncio.run_coroutine_threadsafe(audio_buffer.put(b"EOF"), self.mass.loop).result()
            except DataNotFoundError as err:
                msg = f"Item '{streamdetails.item_id}' not found"
                raise MediaNotFoundError(msg) from err

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
