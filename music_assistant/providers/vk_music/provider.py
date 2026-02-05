"""VK Music provider implementation."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Artist,
    AudioFormat,
    ItemMapping,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .api_client import VKMusicClient
from .constants import CONF_TOKEN, DEFAULT_LIMIT, PLAYLIST_ID_SPLITTER, PLAYLIST_TRACKS_PAGE_SIZE
from .parsers import parse_playlist, parse_track

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class VKMusicProvider(MusicProvider):
    """Implementation of a VK Music MusicProvider."""

    _client: VKMusicClient | None = None

    @property
    def client(self) -> VKMusicClient:
        """Return the VK Music client."""
        if self._client is None:
            raise ProviderUnavailableError("Provider not initialized")
        return self._client

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        token = self.config.get_value(CONF_TOKEN)
        if not token:
            raise LoginFailed("No VK Music token provided")

        self._client = VKMusicClient(str(token))
        await self._client.connect()
        self.logger.info("Successfully connected to VK Music")

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
        """Perform search on VK Music.

        :param search_query: The search query.
        :param media_types: List of media types to search for.
        :param limit: Maximum number of results per type.
        :return: SearchResults with found items.
        """
        result = SearchResults()

        # Search tracks
        if MediaType.TRACK in media_types:
            try:
                songs = await self.client.search_tracks(search_query, count=limit)
                for song in songs[:limit]:
                    try:
                        result.tracks = [*result.tracks, parse_track(self, song)]
                    except InvalidDataError as err:
                        self.logger.debug("Error parsing track: %s", err)
            except ResourceTemporarilyUnavailable as err:
                self.logger.warning("Track search unavailable: %s", err)

        # Search playlists
        if MediaType.PLAYLIST in media_types:
            try:
                playlists = await self.client.search_playlists(search_query, count=limit)
                for playlist in playlists[:limit]:
                    try:
                        result.playlists = [*result.playlists, parse_playlist(self, playlist)]
                    except InvalidDataError as err:
                        self.logger.debug("Error parsing playlist: %s", err)
            except ResourceTemporarilyUnavailable as err:
                self.logger.warning("Playlist search unavailable: %s", err)

        return result

    # Get single items

    @use_cache(3600 * 24 * 30)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details by ID.

        :param prov_track_id: The provider track ID (format: "owner_id_track_id").
        :return: Track object.
        :raises MediaNotFoundError: If track not found.
        """
        song = await self.client.get_track(prov_track_id)
        if not song:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        return parse_track(self, song)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details by ID.

        VK Music does not provide a direct API for fetching artist info.
        We return a minimal Artist object based on the ID (which is the artist name).

        :param prov_artist_id: The provider artist ID (artist name).
        :return: Artist object.
        """
        return Artist(
            item_id=prov_artist_id,
            provider=self.instance_id,
            name=prov_artist_id,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    @use_cache(3600 * 24 * 30)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details by ID.

        :param prov_playlist_id: The provider playlist ID
            (format: "owner_id_playlist_id_access_key").
        :return: Playlist object.
        :raises MediaNotFoundError: If playlist not found.
        """
        # Parse composite ID
        parts = prov_playlist_id.split(PLAYLIST_ID_SPLITTER)
        if len(parts) < 2:
            raise MediaNotFoundError(f"Invalid playlist ID format: {prov_playlist_id}")

        owner_id = int(parts[0])
        playlist_id = int(parts[1])
        access_key = parts[2] if len(parts) > 2 else None

        # VK API doesn't have direct get_playlist - verify it exists by fetching tracks
        await self.client.get_playlist_tracks(owner_id, playlist_id, access_key, count=1, offset=0)

        # Create minimal playlist object
        return Playlist(
            item_id=prov_playlist_id,
            provider=self.instance_id,
            name=f"Playlist {playlist_id}",
            owner="VK Music",
            is_editable=False,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    # Get related items

    @use_cache(3600 * 3)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks.

        :param prov_playlist_id: The provider playlist ID.
        :param page: Page number for pagination.
        :return: List of Track objects.
        """
        # Parse composite ID
        parts = prov_playlist_id.split(PLAYLIST_ID_SPLITTER)
        if len(parts) < 2:
            return []

        owner_id = int(parts[0])
        playlist_id = int(parts[1])
        access_key = parts[2] if len(parts) > 2 else None

        offset = page * PLAYLIST_TRACKS_PAGE_SIZE
        songs = await self.client.get_playlist_tracks(
            owner_id, playlist_id, access_key, count=PLAYLIST_TRACKS_PAGE_SIZE, offset=offset
        )

        tracks = []
        for song in songs:
            try:
                tracks.append(parse_track(self, song))
            except InvalidDataError as err:
                self.logger.debug("Error parsing playlist track: %s", err)
        return tracks

    # Library methods

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from VK Music."""
        offset = 0
        while True:
            songs = await self.client.get_user_tracks(count=DEFAULT_LIMIT, offset=offset)
            if not songs:
                break

            for song in songs:
                try:
                    yield parse_track(self, song)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing library track: %s", err)

            offset += DEFAULT_LIMIT
            if len(songs) < DEFAULT_LIMIT:
                break

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library playlists from VK Music."""
        offset = 0
        while True:
            playlists = await self.client.get_user_playlists(count=DEFAULT_LIMIT, offset=offset)
            if not playlists:
                break

            for playlist in playlists:
                try:
                    yield parse_playlist(self, playlist)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing library playlist: %s", err)

            offset += DEFAULT_LIMIT
            if len(playlists) < DEFAULT_LIMIT:
                break

    # Recommendations

    async def recommendations(self) -> list[RecommendationFolder]:
        """Get VK Music recommendations.

        :return: List of recommendation folders.
        """
        result: list[RecommendationFolder] = []

        # Popular tracks
        try:
            popular = await self.client.get_popular_tracks(count=25)
            popular_tracks = []
            for song in popular:
                with contextlib.suppress(InvalidDataError):
                    popular_tracks.append(parse_track(self, song))
            if popular_tracks:
                result.append(
                    RecommendationFolder(
                        item_id="popular",
                        provider=self.instance_id,
                        path="popular",
                        name="Popular Tracks",
                        items=UniqueList(popular_tracks),
                    )
                )
        except Exception as err:
            self.logger.debug("Error fetching popular tracks: %s", err)

        # User recommendations
        try:
            recs = await self.client.get_recommendations(user_id=self.client.user_id, count=25)
            rec_tracks = []
            for song in recs:
                with contextlib.suppress(InvalidDataError):
                    rec_tracks.append(parse_track(self, song))
            if rec_tracks:
                result.append(
                    RecommendationFolder(
                        item_id="recommended",
                        provider=self.instance_id,
                        path="recommended",
                        name="Recommended For You",
                        items=UniqueList(rec_tracks),
                    )
                )
        except Exception as err:
            self.logger.debug("Error fetching recommendations: %s", err)

        return result

    # Streaming

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails:
        """Get stream details for a track.

        VK Music provides direct HTTP URLs in Song objects.
        URLs may expire, so we always fetch fresh track data.

        :param item_id: The track ID (format: "owner_id_track_id").
        :param media_type: The media type (should be TRACK).
        :return: StreamDetails for the track.
        :raises MediaNotFoundError: If no stream URL available.
        """
        # Always fetch fresh to get valid URL (URLs may expire)
        song = await self.client.get_track(item_id)
        if not song or not song.url:
            raise MediaNotFoundError(f"No stream URL available for track {item_id}")

        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.MP3,
                bit_rate=320,
            ),
            stream_type=StreamType.HTTP,
            path=song.url,
            can_seek=True,
        )
