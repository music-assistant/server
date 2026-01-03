"""The provider class for Open Subsonic."""

from __future__ import annotations

from asyncio import TaskGroup
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from libopensonic import AsyncConnection as SonicConnection
from libopensonic.errors import (
    AuthError,
    CredentialError,
    DataNotFoundError,
    ParameterError,
    SonicError,
)
from music_assistant_models.enums import ContentType, MediaType, StreamType
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
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    CONF_PASSWORD,
    CONF_PATH,
    CONF_PORT,
    CONF_USERNAME,
    UNKNOWN_ARTIST,
)
from music_assistant.models.music_provider import MusicProvider

from .parsers import (
    EP_CHAN_SEP,
    NAVI_VARIOUS_PREFIX,
    UNKNOWN_ARTIST_ID,
    parse_album,
    parse_artist,
    parse_epsiode,
    parse_playlist,
    parse_podcast,
    parse_track,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from libopensonic.media import AlbumID3 as SonicAlbum
    from libopensonic.media import ArtistID3 as SonicArtist
    from libopensonic.media import Bookmark as SonicBookmark
    from libopensonic.media import Child as SonicItem
    from libopensonic.media import OpenSubsonicExtension, PodcastChannel
    from libopensonic.media import Playlist as SonicPlaylist
    from libopensonic.media import PodcastEpisode as SonicEpisode


CONF_BASE_URL = "baseURL"
CONF_ENABLE_PODCASTS = "enable_podcasts"
CONF_ENABLE_LEGACY_AUTH = "enable_legacy_auth"
CONF_OVERRIDE_OFFSET = "override_transcode_offest"
CONF_RECO_FAVES = "recommend_favorites"
CONF_NEW_ALBUMS = "recommend_new"
CONF_PLAYED_ALBUMS = "recommend_played"
CONF_RECO_SIZE = "recommendation_count"
CONF_PAGE_SIZE = "pagination_size"

CACHE_CATEGORY_PODCAST_CHANNEL = 1
CACHE_CATEGORY_PODCAST_EPISODES = 2

Param = ParamSpec("Param")
RetType = TypeVar("RetType")


class OpenSonicProvider(MusicProvider):
    """Provider for Open Subsonic servers."""

    conn: SonicConnection
    _enable_podcasts: bool = True
    _seek_support: bool = False
    _ignore_offset: bool = False
    _show_faves: bool = True
    _show_new: bool = True
    _show_played: bool = True
    _reco_limit: int = 10
    _pagination_size: int = 200

    async def handle_async_init(self) -> None:
        """Set up the music provider and test the connection."""
        port = self.config.get_value(CONF_PORT)
        port = int(str(port)) if port is not None else 443
        path = self.config.get_value(CONF_PATH)
        if path is None:
            path = ""
        self.conn = SonicConnection(
            str(self.config.get_value(CONF_BASE_URL)),
            username=str(self.config.get_value(CONF_USERNAME)),
            password=str(self.config.get_value(CONF_PASSWORD)),
            legacy_auth=bool(self.config.get_value(CONF_ENABLE_LEGACY_AUTH)),
            port=port,
            server_path=str(path),
            app_name="Music Assistant",
        )
        try:
            success = await self.conn.ping()
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
            extensions: list[OpenSubsonicExtension] = await self.conn.get_open_subsonic_extensions()
            for entry in extensions:
                if entry.name == "transcodeOffset" and not self._ignore_offset:
                    self._seek_support = True
                    break
        except OSError:
            self.logger.info("Server does not support transcodeOffset, seeking in player provider")
        self._show_faves = bool(self.config.get_value(CONF_RECO_FAVES))
        self._show_new = bool(self.config.get_value(CONF_NEW_ALBUMS))
        self._show_played = bool(self.config.get_value(CONF_PLAYED_ALBUMS))
        self._reco_limit = int(str(self.config.get_value(CONF_RECO_SIZE)))
        self._pagination_size = int(str(self.config.get_value(CONF_PAGE_SIZE)))
        self._pagination_size = min(self._pagination_size, 500)

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

    async def _get_podcast_episode(self, eid: str) -> SonicEpisode:
        chan_id, ep_id = eid.split(EP_CHAN_SEP)
        chan = await self.conn.get_podcasts(inc_episodes=True, pid=chan_id)

        if not chan[0].episode:
            raise MediaNotFoundError(f"Missing episode list for podcast channel '{chan[0].id}'")

        for episode in chan[0].episode:
            if episode.id == ep_id:
                return episode

        msg = f"Can't find episode {ep_id} in podcast {chan_id}"
        raise MediaNotFoundError(msg)

    def _set_loudness(self, item: SonicItem) -> None:
        if item.replay_gain and item.replay_gain.track_gain is not None:
            # Convert ReplayGain values (gain in dB) to integrated loudness (LUFS)
            track_loudness = -18 - item.replay_gain.track_gain
            album_loudness = (
                -18 - item.replay_gain.album_gain
                if item.replay_gain.album_gain is not None
                else None
            )
            self.mass.create_task(
                self.mass.music.set_loudness(
                    item.id,
                    self.instance_id,
                    track_loudness,
                    album_loudness,
                )
            )

    async def resolve_image(self, path: str) -> bytes | Any:
        """Return the image."""
        self.logger.debug("Requesting cover art for '%s'", path)

        try:
            art = await self.conn.get_cover_art(path)
            return await art.content.read()
        except DataNotFoundError:
            self.logger.warning("Unable to locate a cover image for %s", path)
            return None

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 20
    ) -> SearchResults:
        """Search the sonic library."""
        artists = limit if MediaType.ARTIST in media_types else 0
        albums = limit if MediaType.ALBUM in media_types else 0
        songs = limit if MediaType.TRACK in media_types else 0
        if not (artists or albums or songs):
            return SearchResults()
        answer = await self.conn.search3(
            query=search_query,
            artist_count=artists,
            artist_offset=0,
            album_count=albums,
            album_offset=0,
            song_count=songs,
            song_offset=0,
        )

        if answer.artist:
            ar = [parse_artist(self.instance_id, entry) for entry in answer.artist]
        else:
            ar = []

        if answer.album:
            al = [parse_album(self.logger, self.instance_id, entry) for entry in answer.album]
        else:
            al = []

        if answer.song:
            tr = []
            for entry in answer.song:
                self._set_loudness(entry)
                tr.append(parse_track(self.logger, self.instance_id, entry))
        else:
            tr = []

        return SearchResults(artists=ar, albums=al, tracks=tr)

    async def set_favorite(self, prov_item_id: str, media_type: MediaType, favorite: bool) -> None:
        """Set or clear favorite on the server."""
        # The subsonic spec does not support favorite-ing anything but artists, albums, and tracks
        if media_type not in (MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK):
            return

        track_ids: list[str] = []
        album_ids: list[str] = []
        artist_ids: list[str] = []

        if media_type == MediaType.ARTIST:
            artist_ids.append(prov_item_id)
        elif media_type == MediaType.ALBUM:
            album_ids.append(prov_item_id)
        elif media_type == MediaType.TRACK:
            track_ids.append(prov_item_id)

        if favorite:
            await self.conn.star(sids=track_ids, album_ids=album_ids, artist_ids=artist_ids)
        else:
            await self.conn.unstar(sids=track_ids, album_ids=album_ids, artist_ids=artist_ids)

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Provide a generator for reading all artists."""
        artists = await self.conn.get_artists()

        if not artists.index:
            return

        for index in artists.index:
            if not index.artist:
                continue

            for artist in index.artist:
                yield parse_artist(self.instance_id, artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """
        Provide a generator for reading all artists.

        Note the pagination, the open subsonic docs say that this method is limited to
        returning 500 items per invocation.
        """
        offset = 0
        size = self._pagination_size
        albums = await self.conn.get_album_list2(
            ltype="alphabeticalByArtist",
            size=size,
            offset=offset,
        )
        while albums:
            for album in albums:
                yield parse_album(self.logger, self.instance_id, album)
            offset += size
            albums = await self.conn.get_album_list2(
                ltype="alphabeticalByArtist",
                size=size,
                offset=offset,
            )

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Provide a generator for library playlists."""
        results = await self.conn.get_playlists()
        for entry in results:
            yield parse_playlist(self.instance_id, entry)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """
        Provide a generator for library tracks.

        Note the lack of item count on this method.
        """
        query = ""
        offset = 0
        count = self._pagination_size
        try:
            results = await self.conn.search3(
                query=query,
                artist_count=0,
                album_count=0,
                song_offset=offset,
                song_count=count,
            )
        except ParameterError:
            # Older Navidrome does not accept an empty string and requires the empty quotes
            query = '""'
            results = await self.conn.search3(
                query=query,
                artist_count=0,
                album_count=0,
                song_offset=offset,
                song_count=count,
            )
        while results.song:
            album: Album | None = None
            for entry in results.song:
                aid = entry.album_id if entry.album_id else entry.parent
                if aid is not None and (album is None or album.item_id != aid):
                    album = await self.get_album(prov_album_id=aid)
                self._set_loudness(entry)
                yield parse_track(self.logger, self.instance_id, entry, album=album)
            offset += count
            results = await self.conn.search3(
                query=query,
                artist_count=0,
                album_count=0,
                song_offset=offset,
                song_count=count,
            )

    async def get_album(self, prov_album_id: str) -> Album:
        """Return the requested Album."""
        try:
            sonic_album: SonicAlbum = await self.conn.get_album(prov_album_id)
            sonic_info = await self.conn.get_album_info2(aid=prov_album_id)
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Album {prov_album_id} not found"
            raise MediaNotFoundError(msg) from e

        return parse_album(self.logger, self.instance_id, sonic_album, sonic_info)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Return a list of tracks on the specified Album."""
        try:
            sonic_album: SonicAlbum = await self.conn.get_album(prov_album_id)
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Album {prov_album_id} not found"
            raise MediaNotFoundError(msg) from e
        tracks = []
        if sonic_album.song:
            for sonic_song in sonic_album.song:
                self._set_loudness(sonic_song)
                tracks.append(parse_track(self.logger, self.instance_id, sonic_song))
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
            sonic_artist: SonicArtist = await self.conn.get_artist(artist_id=prov_artist_id)
            sonic_info = await self.conn.get_artist_info2(aid=prov_artist_id)
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Artist {prov_artist_id} not found"
            raise MediaNotFoundError(msg) from e
        return parse_artist(self.instance_id, sonic_artist, sonic_info)

    async def get_track(self, prov_track_id: str) -> Track:
        """Return the specified track."""
        try:
            sonic_song: SonicItem = await self.conn.get_song(prov_track_id)
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Item {prov_track_id} not found"
            raise MediaNotFoundError(msg) from e
        aid = sonic_song.album_id if sonic_song.album_id else sonic_song.parent
        album: Album | None = None
        if not aid:
            self.logger.warning("Unable to find album id for track %s", sonic_song.id)
        else:
            album = await self.get_album(prov_album_id=aid)
        self._set_loudness(sonic_song)
        return parse_track(self.logger, self.instance_id, sonic_song, album=album)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Return a list of all Albums by specified Artist."""
        if prov_artist_id == UNKNOWN_ARTIST_ID or prov_artist_id.startswith(NAVI_VARIOUS_PREFIX):
            return []

        try:
            sonic_artist: SonicArtist = await self.conn.get_artist(prov_artist_id)
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Album {prov_artist_id} not found"
            raise MediaNotFoundError(msg) from e
        albums = []
        if sonic_artist.album:
            for entry in sonic_artist.album:
                albums.append(parse_album(self.logger, self.instance_id, entry))
        return albums

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return the specified Playlist."""
        try:
            sonic_playlist: SonicPlaylist = await self.conn.get_playlist(prov_playlist_id)
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Playlist {prov_playlist_id} not found"
            raise MediaNotFoundError(msg) from e
        return parse_playlist(self.instance_id, sonic_playlist)

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
        channels = await self.conn.get_podcasts(inc_episodes=True, pid=prov_podcast_id)
        channel = channels[0]
        if not channel.episode:
            return

        for episode in channel.episode:
            self._set_loudness(episode)
            yield parse_epsiode(self.instance_id, episode, channel)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full Podcast details by id."""
        if not self._enable_podcasts:
            msg = "Podcasts are currently disabled in the provider configuration"
            raise ActionUnavailable(msg)

        channels = await self.conn.get_podcasts(inc_episodes=True, pid=prov_podcast_id)

        return parse_podcast(self.instance_id, channels[0])

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library/subscribed podcasts from the provider."""
        if self._enable_podcasts:
            channels = await self.conn.get_podcasts(inc_episodes=True)

            for channel in channels:
                yield parse_podcast(self.instance_id, channel)

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        if page > 0:
            # paging not supported, we always return the whole list at once
            return result
        try:
            sonic_playlist: SonicPlaylist = await self.conn.get_playlist(prov_playlist_id)
        except (ParameterError, DataNotFoundError) as e:
            msg = f"Playlist {prov_playlist_id} not found"
            raise MediaNotFoundError(msg) from e

        if not sonic_playlist.entry:
            return result

        album: Album | None = None
        for index, sonic_song in enumerate(sonic_playlist.entry, 1):
            aid = sonic_song.album_id if sonic_song.album_id else sonic_song.parent
            if not aid:
                self.logger.warning("Unable to find album for track %s", sonic_song.id)
            if aid is not None and (not album or album.item_id != aid):
                album = await self.get_album(prov_album_id=aid)
            self._set_loudness(sonic_song)
            track = parse_track(self.logger, self.instance_id, sonic_song, album=album)
            track.position = index
            result.append(track)
        return result

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get the top listed tracks for a specified artist."""
        # We have seen top tracks requested for the UNKNOWN_ARTIST ID, protect against that
        if prov_artist_id == UNKNOWN_ARTIST_ID or prov_artist_id.startswith(NAVI_VARIOUS_PREFIX):
            return []

        try:
            sonic_artist: SonicArtist = await self.conn.get_artist(prov_artist_id)
        except DataNotFoundError as e:
            msg = f"Artist {prov_artist_id} not found"
            raise MediaNotFoundError(msg) from e
        songs: list[SonicItem] = await self.conn.get_top_songs(sonic_artist.name)
        tracks = []
        for entry in songs:
            self._set_loudness(entry)
            tracks.append(parse_track(self.logger, self.instance_id, entry))
        return tracks

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get tracks similar to selected track."""
        try:
            songs: list[SonicItem] = await self.conn.get_similar_songs(
                iid=prov_track_id, count=limit
            )
        except DataNotFoundError as e:
            # Subsonic returns an error here instead of an empty list, I don't think this
            # should be an exception but there we are. Return an empty list because this
            # exception means we didn't find anything similar.
            self.logger.info(e)
            return []
        tracks = []
        for entry in songs:
            self._set_loudness(entry)
            tracks.append(parse_track(self.logger, self.instance_id, entry))
        return tracks

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new empty playlist on the server."""
        if not await self.conn.create_playlist(name=name):
            raise ProviderPermissionDenied(
                "Please ensure you have permission to create playlists on your server"
            )
        pls: list[SonicPlaylist] = await self.conn.get_playlists()
        for pl in pls:
            if pl.name == name:
                return parse_playlist(self.instance_id, pl)
        raise MediaNotFoundError(f"Failed to create podcast with name '{name}'")

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Append the listed tracks to the selected playlist.

        Note that the configured user must own the playlist to edit this way.
        """
        try:
            await self.conn.update_playlist(
                lid=prov_playlist_id,
                song_ids_to_add=prov_track_ids,
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
            await self.conn.update_playlist(
                lid=prov_playlist_id,
                song_indices_to_remove=idx_to_remove,
            )
        except SonicError as ex:
            msg = f"Failed to remove songs from {prov_playlist_id}, check your permissions."
            raise ProviderPermissionDenied(msg) from ex

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get the details needed to process a specified track."""
        item: SonicItem | SonicEpisode
        if media_type == MediaType.TRACK:
            try:
                item = await self.conn.get_song(item_id)
            except (ParameterError, DataNotFoundError) as e:
                msg = f"Item {item_id} not found"
                raise MediaNotFoundError(msg) from e

            mime_type = item.transcoded_content_type or item.content_type

            self.logger.debug(
                "Fetching stream details for id %s '%s' with format '%s'",
                item.id,
                item.title,
                mime_type,
            )

        elif media_type == MediaType.PODCAST_EPISODE:
            item = await self._get_podcast_episode(item_id)

            mime_type = item.transcoded_content_type or item.content_type

            self.logger.debug(
                "Fetching stream details for podcast episode '%s' with format '%s'",
                item.id,
                item.content_type,
            )
        else:
            msg = f"Unsupported media type encountered '{media_type}'"
            raise UnsupportedFeaturedException(msg)

        if mime_type and mime_type.endswith("mp4"):
            self.logger.warning(
                "Due to the streaming method used by the subsonic API, M4A files "
                "may fail. See provider documentation for more information."
            )

        # We believe that reporting the container type here is causing playback problems and ffmpeg
        # should be capable of guessing the correct container type for any media supported by
        # OpenSubsonic servers. Better to let ffmpeg figure things out than tell it something
        # confusing. We still go through the effort of figuring out what the server thinks the
        # container is to warn about M4A files.
        mime_type = "?"

        return StreamDetails(
            item_id=item.id,
            provider=self.instance_id,
            allow_seek=True,
            can_seek=self._seek_support,
            media_type=media_type,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(mime_type),
                sample_rate=item.sampling_rate if item.sampling_rate else 44100,
                bit_depth=item.bit_depth if item.bit_depth else 16,
                channels=item.channel_count if item.channel_count else 2,
            ),
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
        if media_type != MediaType.PODCAST_EPISODE:
            # We don't handle audio books in this provider so this is the only resummable media
            # type we should see.
            return

        _, ep_id = prov_item_id.split(EP_CHAN_SEP)

        if fully_played:
            # We completed the episode and should delete our bookmark
            try:
                await self.conn.delete_bookmark(mid=ep_id)
            except DataNotFoundError:
                # We probably raced with something else deleting this bookmark, not really a problem
                self.logger.info("Bookmark for item '%s' has already been deleted.", ep_id)
            return

        # Otherwise, create a new bookmark for this item or update the existing one
        # MA provides a position in seconds but expects it back in milliseconds
        await self.conn.create_bookmark(
            mid=ep_id,
            position=position * 1000,
            comment="Music Assistant Bookmark",
        )

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """
        Get progress (resume point) details for the given Audiobook or Podcast episode.

        This is a separate call from the regular get_item call to ensure the resume position
        is always up-to-date and because a lot providers have this info present on a dedicated
        endpoint.

        Will be called right before playback starts to ensure the resume position is correct.

        Returns a boolean with the fully_played status
        and an integer with the resume position in ms.
        """
        if media_type != MediaType.PODCAST_EPISODE:
            raise NotImplementedError("AudioBooks are not supported by the Open Subsonic provider")

        _, ep_id = item_id.split(EP_CHAN_SEP)

        bookmarks: list[SonicBookmark] = await self.conn.get_bookmarks()

        for mark in bookmarks:
            if mark.entry.id == ep_id:
                return (False, mark.position)
        # If we get here, there is no bookmark
        return (False, 0)

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Provide a generator for the stream data."""
        # ignore seek position if the server does not support it
        # in that case we let the core handle seeking
        if not self._seek_support:
            seek_position = 0

        self.logger.debug("Streaming %s", streamdetails.item_id)
        try:
            resp = await self.conn.stream(
                streamdetails.item_id, time_offset=seek_position, estimate_length=True
            )
        except DataNotFoundError as err:
            msg = f"Item '{streamdetails.item_id}' not found"
            raise MediaNotFoundError(msg) from err
        self.logger.debug("starting stream of item '%s'", streamdetails.item_id)
        async with resp:
            async for chunk in resp.content.iter_chunked(40960):
                yield bytes(chunk)

        self.logger.debug("Done streaming %s", streamdetails.item_id)

    async def _get_podcast_channel_async(self, chan_id: str) -> PodcastChannel | None:
        if cache := await self.mass.cache.get(
            key=chan_id,
            provider=self.instance_id,
            category=CACHE_CATEGORY_PODCAST_CHANNEL,
        ):
            return cache
        if channels := await self.conn.get_podcasts(inc_episodes=True, pid=chan_id):
            channel = channels[0]
            await self.mass.cache.set(
                key=chan_id,
                data=channel,
                provider=self.instance_id,
                expiration=600,
                category=CACHE_CATEGORY_PODCAST_CHANNEL,
            )
            return channel
        return None

    async def _podcast_recommendations(self) -> RecommendationFolder:
        podcasts: RecommendationFolder = RecommendationFolder(
            item_id="subsonic_newest_podcasts",
            provider=self.domain,
            name="Newest Podcast Episodes",
        )
        sonic_episodes = await self.conn.get_newest_podcasts(count=self._reco_limit)
        for ep in sonic_episodes:
            if channel_info := await self._get_podcast_channel_async(ep.channel_id):
                self._set_loudness(ep)
                podcasts.items.append(parse_epsiode(self.instance_id, ep, channel_info))
        return podcasts

    async def _favorites_recommendation(self) -> RecommendationFolder:
        faves: RecommendationFolder = RecommendationFolder(
            item_id="subsonic_starred_albums", provider=self.domain, name="Starred Items"
        )
        starred = await self.conn.get_starred2()
        if starred.album:
            for sonic_album in starred.album[: self._reco_limit]:
                faves.items.append(parse_album(self.logger, self.instance_id, sonic_album))
        if starred.artist:
            for sonic_artist in starred.artist[: self._reco_limit]:
                faves.items.append(parse_artist(self.instance_id, sonic_artist))
        if starred.song:
            for sonic_song in starred.song[: self._reco_limit]:
                self._set_loudness(sonic_song)
                faves.items.append(parse_track(self.logger, self.instance_id, sonic_song))
        return faves

    async def _new_recommendations(self) -> RecommendationFolder:
        new_stuff: RecommendationFolder = RecommendationFolder(
            item_id="subsonic_new_albums", provider=self.domain, name="New Albums"
        )
        new_albums = await self.conn.get_album_list2(ltype="newest", size=self._reco_limit)
        for sonic_album in new_albums:
            new_stuff.items.append(parse_album(self.logger, self.instance_id, sonic_album))
        return new_stuff

    async def _played_recommendations(self) -> RecommendationFolder:
        recent: RecommendationFolder = RecommendationFolder(
            item_id="subsonic_most_played", provider=self.domain, name="Most Played Albums"
        )
        albums = await self.conn.get_album_list2(ltype="frequent", size=self._reco_limit)
        for sonic_album in albums:
            recent.items.append(parse_album(self.logger, self.instance_id, sonic_album))
        return recent

    async def recommendations(self) -> list[RecommendationFolder]:
        """Provide recommendations.

        These can provide favorited items, recently added albums, newest podcast episodes,
        and most played albums.  What is included is configured with the provider.
        """
        recos: list[RecommendationFolder] = []

        podcasts = None
        faves = None
        new_stuff = None
        played = None
        async with TaskGroup() as grp:
            if self._enable_podcasts:
                podcasts = grp.create_task(self._podcast_recommendations())
            if self._show_faves:
                faves = grp.create_task(self._favorites_recommendation())
            if self._show_new:
                new_stuff = grp.create_task(self._new_recommendations())
            if self._show_played:
                played = grp.create_task(self._played_recommendations())

        if podcasts:
            recos.append(podcasts.result())
        if faves:
            recos.append(faves.result())
        if new_stuff:
            recos.append(new_stuff.result())
        if played:
            recos.append(played.result())

        return recos
