"""API client wrapper for VK Music."""

from __future__ import annotations

import logging
from typing import Any

from music_assistant_models.errors import (
    LoginFailed,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from vkpymusic import Service
from vkpymusic.vk_api import VkApiException

from .constants import DEFAULT_LIMIT, DEFAULT_USER_AGENT

# vkpymusic lacks proper type stubs, so we use Any for its types
type VKSong = Any
type VKPlaylist = Any
type VKUserInfo = Any

LOGGER = logging.getLogger(__name__)


class VKMusicClient:
    """Wrapper around vkpymusic Service with async support."""

    def __init__(self, token: str, user_agent: str | None = None) -> None:
        """Initialize the VK Music client.

        :param token: VK Music access token.
        :param user_agent: Custom user agent string.
        """
        self._token = token
        self._user_agent = user_agent or DEFAULT_USER_AGENT
        self._service: Service | None = None
        self._user_id: int | None = None

    @property
    def user_id(self) -> int:
        """Return the user ID."""
        if self._user_id is None:
            raise ProviderUnavailableError("Client not initialized, call connect() first")
        return self._user_id

    async def connect(self) -> bool:
        """Initialize the service and verify token validity.

        :return: True if connection was successful.
        :raises LoginFailed: If the token is invalid.
        """
        try:
            self._service = Service(self._user_agent, self._token)
            is_valid = await self._service.is_token_valid_async()
            if not is_valid:
                raise LoginFailed("Invalid VK Music token")
            user_info: VKUserInfo = await self._service.get_user_info_async()
            self._user_id = user_info.id
            LOGGER.debug("Connected to VK Music as user %s", self._user_id)
            return True
        except VkApiException as err:
            if "invalid" in str(err).lower() or "token" in str(err).lower():
                raise LoginFailed("Invalid VK Music token") from err
            msg = "Error connecting to VK Music"
            raise ResourceTemporarilyUnavailable(msg) from err

    async def disconnect(self) -> None:
        """Disconnect the client."""
        self._service = None
        self._user_id = None

    def _ensure_connected(self) -> Service:
        """Ensure the service is connected and return it."""
        if self._service is None:
            raise ProviderUnavailableError("Client not connected, call connect() first")
        return self._service

    # Library methods

    async def get_user_tracks(self, count: int = DEFAULT_LIMIT, offset: int = 0) -> list[VKSong]:
        """Get user's library tracks.

        :param count: Maximum number of tracks to return.
        :param offset: Offset for pagination.
        :return: List of song objects.
        """
        service = self._ensure_connected()
        try:
            return await service.get_songs_by_userid_async(  # type: ignore[no-any-return]
                self.user_id, count, offset
            )
        except VkApiException as err:
            LOGGER.error("Error fetching user tracks: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch user tracks") from err

    async def get_user_playlists(
        self, count: int = DEFAULT_LIMIT, offset: int = 0
    ) -> list[VKPlaylist]:
        """Get user's playlists.

        :param count: Maximum number of playlists to return.
        :param offset: Offset for pagination.
        :return: List of playlist objects.
        """
        service = self._ensure_connected()
        try:
            return await service.get_playlists_by_userid_async(  # type: ignore[no-any-return]
                self.user_id, count, offset
            )
        except VkApiException as err:
            LOGGER.error("Error fetching playlists: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch playlists") from err

    # Search

    async def search_tracks(
        self, query: str, count: int = DEFAULT_LIMIT, offset: int = 0
    ) -> list[VKSong]:
        """Search for tracks.

        :param query: Search query string.
        :param count: Maximum number of results.
        :param offset: Offset for pagination.
        :return: List of song objects.
        """
        service = self._ensure_connected()
        try:
            return await service.search_songs_by_text_async(  # type: ignore[no-any-return]
                query, count, offset
            )
        except VkApiException as err:
            LOGGER.error("Search tracks error: %s", err)
            raise ResourceTemporarilyUnavailable("Search failed") from err

    async def search_playlists(
        self, query: str, count: int = DEFAULT_LIMIT, offset: int = 0
    ) -> list[VKPlaylist]:
        """Search for playlists.

        :param query: Search query string.
        :param count: Maximum number of results.
        :param offset: Offset for pagination.
        :return: List of playlist objects.
        """
        service = self._ensure_connected()
        try:
            return await service.search_playlists_by_text_async(  # type: ignore[no-any-return]
                query, count, offset
            )
        except VkApiException as err:
            LOGGER.error("Search playlists error: %s", err)
            raise ResourceTemporarilyUnavailable("Search failed") from err

    async def search_albums(
        self, query: str, count: int = DEFAULT_LIMIT, offset: int = 0
    ) -> list[VKPlaylist]:
        """Search for albums (returns Playlist objects in VK).

        :param query: Search query string.
        :param count: Maximum number of results.
        :param offset: Offset for pagination.
        :return: List of playlist objects (albums are playlists in VK).
        """
        service = self._ensure_connected()
        try:
            return await service.search_albums_by_text_async(  # type: ignore[no-any-return]
                query, count, offset
            )
        except VkApiException as err:
            LOGGER.error("Search albums error: %s", err)
            raise ResourceTemporarilyUnavailable("Search failed") from err

    # Get single items

    async def get_track(self, track_id: str) -> VKSong | None:
        """Get a single track by ID.

        :param track_id: Track ID in format 'owner_id_track_id'.
        :return: Song object or None if not found.
        """
        service = self._ensure_connected()
        try:
            tracks = await service.get_songs_by_id_async([track_id])
            return tracks[0] if tracks else None
        except VkApiException as err:
            LOGGER.error("Error fetching track %s: %s", track_id, err)
            return None

    async def get_tracks(self, track_ids: list[str]) -> list[VKSong]:
        """Get multiple tracks by IDs.

        :param track_ids: List of track IDs in format 'owner_id_track_id'.
        :return: List of song objects.
        """
        service = self._ensure_connected()
        try:
            return await service.get_songs_by_id_async(  # type: ignore[no-any-return]
                track_ids
            )
        except VkApiException as err:
            LOGGER.error("Error fetching tracks: %s", err)
            return []

    async def get_playlist_tracks(
        self,
        owner_id: int,
        playlist_id: int,
        access_key: str | None,
        count: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[VKSong]:
        """Get tracks from a playlist.

        :param owner_id: Playlist owner ID.
        :param playlist_id: Playlist ID.
        :param access_key: Access key for private playlists.
        :param count: Maximum number of tracks to return.
        :param offset: Offset for pagination.
        :return: List of song objects.
        """
        service = self._ensure_connected()
        try:
            return await service.get_songs_by_playlist_id_async(  # type: ignore[no-any-return]
                owner_id, playlist_id, access_key or "", count, offset
            )
        except VkApiException as err:
            LOGGER.error("Error fetching playlist tracks %s_%s: %s", owner_id, playlist_id, err)
            raise ResourceTemporarilyUnavailable("Failed to fetch playlist tracks") from err

    # Recommendations

    async def get_popular_tracks(self, count: int = DEFAULT_LIMIT, offset: int = 0) -> list[VKSong]:
        """Get popular tracks.

        :param count: Maximum number of tracks to return.
        :param offset: Offset for pagination.
        :return: List of song objects.
        """
        service = self._ensure_connected()
        try:
            return await service.get_popular_async(  # type: ignore[no-any-return]
                count, offset
            )
        except VkApiException as err:
            LOGGER.error("Error fetching popular tracks: %s", err)
            return []

    async def get_recommendations(
        self,
        user_id: int | None = None,
        song_id: int | None = None,
        count: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[VKSong]:
        """Get personalized recommendations.

        :param user_id: User ID for user-based recommendations.
        :param song_id: Song ID for song-based recommendations.
        :param count: Maximum number of tracks to return.
        :param offset: Offset for pagination.
        :return: List of song objects.
        """
        service = self._ensure_connected()
        try:
            return await service.get_recommendations_async(  # type: ignore[no-any-return]
                user_id, song_id, count, offset
            )
        except VkApiException as err:
            LOGGER.error("Error fetching recommendations: %s", err)
            return []
