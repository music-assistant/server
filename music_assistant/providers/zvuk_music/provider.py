"""Zvuk Music provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemType,
    Playlist,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from zvuk_music.enums import Quality
from zvuk_music.exceptions import QualityNotAvailableError, SubscriptionRequiredError

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .api_client import ZvukMusicClient
from .constants import (
    CONF_QUALITY,
    CONF_TOKEN,
    DEFAULT_LIMIT,
    PLAYLIST_TRACKS_PAGE_SIZE,
    QUALITY_LOSSLESS,
)
from .parsers import parse_album, parse_artist, parse_playlist, parse_track

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class ZvukMusicProvider(MusicProvider):
    """Implementation of a Zvuk Music MusicProvider."""

    _client: ZvukMusicClient | None = None

    @property
    def client(self) -> ZvukMusicClient:
        """Return the Zvuk Music client."""
        if self._client is None:
            raise ProviderUnavailableError("Provider not initialized")
        return self._client

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        token = self.config.get_value(CONF_TOKEN)
        if not token:
            raise LoginFailed("No Zvuk Music token provided")

        self._client = ZvukMusicClient(str(token))
        await self._client.connect()
        self.logger.info("Successfully connected to Zvuk Music")

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider.

        :param is_removed: Whether the provider is being removed.
        """
        if self._client:
            await self._client.disconnect()
        self._client = None
        await super().unload(is_removed)

    def get_item_mapping(self, media_type: MediaType | str, key: str, name: str) -> ItemMapping:
        """Create a generic item mapping.

        :param media_type: The media type.
        :param key: The item ID.
        :param name: The item name.
        :return: An ItemMapping instance.
        """
        if isinstance(media_type, str):
            media_type = MediaType(media_type)
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )

    # Search

    @use_cache(3600 * 24 * 14)
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on Zvuk Music.

        :param search_query: The search query.
        :param media_types: List of media types to search for.
        :param limit: Maximum number of results per type.
        :return: SearchResults with found items.
        """
        result = SearchResults()

        search_result = await self.client.search(
            search_query,
            limit=limit,
            search_tracks=MediaType.TRACK in media_types,
            search_artists=MediaType.ARTIST in media_types,
            search_releases=MediaType.ALBUM in media_types,
            search_playlists=MediaType.PLAYLIST in media_types,
        )
        if not search_result:
            return result

        # Parse tracks
        if MediaType.TRACK in media_types and search_result.tracks:
            for track in search_result.tracks.items[:limit]:
                try:
                    result.tracks = [*result.tracks, parse_track(self, track)]
                except InvalidDataError as err:
                    self.logger.debug("Error parsing track: %s", err)

        # Parse albums (Zvuk releases)
        if MediaType.ALBUM in media_types and search_result.releases:
            for release in search_result.releases.items[:limit]:
                try:
                    result.albums = [*result.albums, parse_album(self, release)]
                except InvalidDataError as err:
                    self.logger.debug("Error parsing album: %s", err)

        # Parse artists
        if MediaType.ARTIST in media_types and search_result.artists:
            for artist in search_result.artists.items[:limit]:
                try:
                    result.artists = [*result.artists, parse_artist(self, artist)]
                except InvalidDataError as err:
                    self.logger.debug("Error parsing artist: %s", err)

        # Parse playlists
        if MediaType.PLAYLIST in media_types and search_result.playlists:
            for playlist in search_result.playlists.items[:limit]:
                try:
                    result.playlists = [*result.playlists, parse_playlist(self, playlist)]
                except InvalidDataError as err:
                    self.logger.debug("Error parsing playlist: %s", err)

        return result

    # Get single items

    @use_cache(3600 * 24 * 30)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details by ID.

        :param prov_artist_id: The provider artist ID.
        :return: Artist object.
        :raises MediaNotFoundError: If artist not found.
        """
        artist = await self.client.get_artist(prov_artist_id)
        if not artist:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        return parse_artist(self, artist)

    @use_cache(3600 * 24 * 30)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details by ID.

        :param prov_album_id: The provider album ID.
        :return: Album object.
        :raises MediaNotFoundError: If album not found.
        """
        release = await self.client.get_release(prov_album_id)
        if not release:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return parse_album(self, release)

    @use_cache(3600 * 24 * 30)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details by ID.

        :param prov_track_id: The provider track ID.
        :return: Track object.
        :raises MediaNotFoundError: If track not found.
        """
        track = await self.client.get_track(prov_track_id)
        if not track:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        return parse_track(self, track)

    @use_cache(3600 * 24 * 30)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details by ID.

        :param prov_playlist_id: The provider playlist ID.
        :return: Playlist object.
        :raises MediaNotFoundError: If playlist not found.
        """
        playlist = await self.client.get_playlist(prov_playlist_id)
        if not playlist:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")
        return parse_playlist(self, playlist)

    # Get related items

    @use_cache(3600 * 24 * 30)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks.

        :param prov_album_id: The provider album ID.
        :return: List of Track objects.
        """
        release = await self.client.get_release(prov_album_id)
        if not release or not release.tracks:
            return []

        tracks = []
        for index, track in enumerate(release.tracks):
            try:
                parsed_track = parse_track(self, track)
                parsed_track.disc_number = 1
                parsed_track.track_number = index + 1
                tracks.append(parsed_track)
            except InvalidDataError as err:
                self.logger.debug("Error parsing album track: %s", err)
        return tracks

    @use_cache(3600 * 3)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks.

        :param prov_playlist_id: The provider playlist ID.
        :param page: Page number for pagination.
        :return: List of Track objects.
        """
        offset = page * PLAYLIST_TRACKS_PAGE_SIZE
        simple_tracks = await self.client.get_playlist_tracks(
            prov_playlist_id, limit=PLAYLIST_TRACKS_PAGE_SIZE, offset=offset
        )
        if not simple_tracks:
            return []

        # Fetch full track details from SimpleTrack IDs
        track_ids = [str(t.id) for t in simple_tracks if t.id]
        if not track_ids:
            return []

        full_tracks = await self.client.get_tracks(track_ids)
        tracks = []
        for track in full_tracks:
            try:
                tracks.append(parse_track(self, track))
            except InvalidDataError as err:
                self.logger.debug("Error parsing playlist track: %s", err)
        return tracks

    @use_cache(3600 * 24 * 7)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get artist's albums.

        :param prov_artist_id: The provider artist ID.
        :return: List of Album objects.
        """
        artists = await self.client.get_artist_releases(prov_artist_id, limit=DEFAULT_LIMIT)
        if not artists:
            return []

        result = []
        for artist in artists:
            for release in artist.releases:
                try:
                    result.append(parse_album(self, release))
                except InvalidDataError as err:
                    self.logger.debug("Error parsing artist album: %s", err)
        return result

    @use_cache(3600 * 24 * 7)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get artist's top tracks.

        :param prov_artist_id: The provider artist ID.
        :return: List of Track objects.
        """
        artists = await self.client.get_artist_top_tracks(prov_artist_id, limit=DEFAULT_LIMIT)
        if not artists:
            return []

        result = []
        for artist in artists:
            for track in artist.popular_tracks:
                try:
                    result.append(parse_track(self, track))
                except InvalidDataError as err:
                    self.logger.debug("Error parsing artist track: %s", err)
        return result

    # Library methods

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from Zvuk Music."""
        collection = await self.client.get_collection()
        if not collection or not collection.artists:
            return

        artist_ids = [str(item.id) for item in collection.artists if item.id]
        for i in range(0, len(artist_ids), DEFAULT_LIMIT):
            batch_ids = artist_ids[i : i + DEFAULT_LIMIT]
            artists = await self.client.get_artists(batch_ids)
            for artist in artists:
                try:
                    yield parse_artist(self, artist)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing library artist: %s", err)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from Zvuk Music."""
        collection = await self.client.get_collection()
        if not collection or not collection.releases:
            return

        release_ids = [str(item.id) for item in collection.releases if item.id]
        for i in range(0, len(release_ids), DEFAULT_LIMIT):
            batch_ids = release_ids[i : i + DEFAULT_LIMIT]
            releases = await self.client.get_releases(batch_ids)
            for release in releases:
                try:
                    yield parse_album(self, release)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing library album: %s", err)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Zvuk Music."""
        tracks = await self.client.get_liked_tracks()
        for track in tracks:
            try:
                yield parse_track(self, track)
            except InvalidDataError as err:
                self.logger.debug("Error parsing library track: %s", err)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library playlists from Zvuk Music."""
        collection_items = await self.client.get_user_playlists()
        if not collection_items:
            return

        playlist_ids = [str(item.id) for item in collection_items if item.id]
        for i in range(0, len(playlist_ids), DEFAULT_LIMIT):
            batch_ids = playlist_ids[i : i + DEFAULT_LIMIT]
            playlists = await self.client.get_playlists(batch_ids)
            for playlist in playlists:
                try:
                    yield parse_playlist(self, playlist)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing library playlist: %s", err)

    # Library edit methods

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to library.

        :param item: The media item to add.
        :return: True if successful.
        """
        prov_item_id = self._get_provider_item_id(item)
        if not prov_item_id:
            return False

        if item.media_type == MediaType.TRACK:
            return await self.client.like_track(prov_item_id)
        if item.media_type == MediaType.ALBUM:
            return await self.client.like_release(prov_item_id)
        if item.media_type == MediaType.ARTIST:
            return await self.client.like_artist(prov_item_id)
        if item.media_type == MediaType.PLAYLIST:
            return await self.client.like_playlist(prov_item_id)
        return False

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library.

        :param prov_item_id: The provider item ID.
        :param media_type: The media type.
        :return: True if successful.
        """
        if media_type == MediaType.TRACK:
            return await self.client.unlike_track(prov_item_id)
        if media_type == MediaType.ALBUM:
            return await self.client.unlike_release(prov_item_id)
        if media_type == MediaType.ARTIST:
            return await self.client.unlike_artist(prov_item_id)
        if media_type == MediaType.PLAYLIST:
            return await self.client.unlike_playlist(prov_item_id)
        return False

    def _get_provider_item_id(self, item: MediaItemType) -> str | None:
        """Get provider item ID from media item."""
        for mapping in item.provider_mappings:
            if mapping.provider_instance == self.instance_id:
                return mapping.item_id
        return item.item_id if item.provider == self.instance_id else None

    # Playlist management

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """Create a new playlist.

        :param name: Playlist name.
        :return: The created Playlist object.
        """
        playlist_id = await self.client.create_playlist(name)
        playlist = await self.client.get_playlist(playlist_id)
        if not playlist:
            raise MediaNotFoundError(f"Created playlist {playlist_id} not found")
        return parse_playlist(self, playlist)

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add tracks to a playlist.

        :param prov_playlist_id: The provider playlist ID.
        :param prov_track_ids: List of track IDs to add.
        """
        await self.client.add_tracks_to_playlist(prov_playlist_id, prov_track_ids)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove tracks from a playlist by position.

        :param prov_playlist_id: The provider playlist ID.
        :param positions_to_remove: Tuple of track positions (0-based) to remove.
        """
        # Fetch current tracks and filter out the ones at given positions
        simple_tracks = await self.client.get_playlist_tracks(prov_playlist_id, limit=10000)
        remove_positions = set(positions_to_remove)
        remaining_ids = [
            str(t.id) for i, t in enumerate(simple_tracks) if t.id and i not in remove_positions
        ]
        await self.client.update_playlist(prov_playlist_id, remaining_ids)

    # Streaming

    async def get_stream_details(  # noqa: PLR0915
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails:
        """Get stream details for a track.

        :param item_id: The track ID.
        :param media_type: The media type (should be TRACK).
        :return: StreamDetails for the track.
        :raises MediaNotFoundError: If stream URL cannot be obtained.
        """
        streams = await self.client.get_stream_urls(item_id)
        if not streams:
            raise MediaNotFoundError(f"No stream info available for track {item_id}")

        stream = streams[0]
        quality_pref = self.config.get_value(CONF_QUALITY)
        quality_str = str(quality_pref) if quality_pref is not None else QUALITY_LOSSLESS

        # Select quality with fallback chain
        url: str | None = None
        content_type = ContentType.UNKNOWN
        bitrate = 0

        if quality_str == QUALITY_LOSSLESS:
            # Try FLAC -> HIGH -> MID
            for quality in (Quality.FLAC, Quality.HIGH, Quality.MID):
                try:
                    url = stream.get_url(quality)
                    if quality == Quality.FLAC:
                        content_type = ContentType.FLAC
                        bitrate = 0
                    elif quality == Quality.HIGH:
                        content_type = ContentType.MP3
                        bitrate = 320
                    else:
                        content_type = ContentType.MP3
                        bitrate = 128
                    break
                except (SubscriptionRequiredError, QualityNotAvailableError):
                    continue
        else:
            # High quality: try HIGH -> MID
            for quality in (Quality.HIGH, Quality.MID):
                try:
                    url = stream.get_url(quality)
                    if quality == Quality.HIGH:
                        content_type = ContentType.MP3
                        bitrate = 320
                    else:
                        content_type = ContentType.MP3
                        bitrate = 128
                    break
                except (SubscriptionRequiredError, QualityNotAvailableError):
                    continue

        # Ultimate fallback
        if not url:
            best_quality, url = stream.get_best_available()
            if best_quality == Quality.FLAC:
                content_type = ContentType.FLAC
                bitrate = 0
            elif best_quality == Quality.HIGH:
                content_type = ContentType.MP3
                bitrate = 320
            else:
                content_type = ContentType.MP3
                bitrate = 128

        if not url:
            raise MediaNotFoundError(f"No stream URL available for track {item_id}")

        # zvuk-music Stream model (get_stream_urls) has no duration; only expire and URLs.
        # Fetch track for duration so StreamDetails can expose it (e.g. for progress/seeking).
        track = await self.client.get_track(item_id)
        duration: int | None = None
        if track is not None and getattr(track, "duration", None) is not None:
            duration = int(track.duration)

        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=content_type,
                bit_rate=bitrate,
            ),
            stream_type=StreamType.HTTP,
            path=url,
            duration=duration,
            allow_seek=True,
            can_seek=True,
        )
