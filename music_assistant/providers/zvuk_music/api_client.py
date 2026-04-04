"""API client wrapper for Zvuk Music."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar, cast

from music_assistant_models.errors import (
    LoginFailed,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from zvuk_music import Artist as ZvukArtist
from zvuk_music import ClientAsync, Collection
from zvuk_music import CollectionItem as ZvukCollectionItem
from zvuk_music import Playlist as ZvukPlaylist
from zvuk_music import Release as ZvukRelease
from zvuk_music import Search as ZvukSearch
from zvuk_music import SimpleTrack as ZvukSimpleTrack
from zvuk_music import Stream as ZvukStream
from zvuk_music import Track as ZvukTrack
from zvuk_music.exceptions import (
    BadRequestError,
    BotDetectedError,
    GraphQLError,
    NetworkError,
    NotFoundError,
    TimedOutError,
    UnauthorizedError,
)

from .constants import DEFAULT_LIMIT

LOGGER = logging.getLogger(__name__)

_P = ParamSpec("_P")
_R = TypeVar("_R")
_NOT_FOUND_SENTINEL: Any = object()


def handle_zvuk_errors(
    not_found_return: Any = _NOT_FOUND_SENTINEL,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Decorate async methods to map Zvuk API exceptions to MA errors.

    :param not_found_return: Value to return on NotFoundError (e.g. None or []).
        If not provided, NotFoundError is not caught.
    """

    def decorator(func: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                return await func(*args, **kwargs)
            except UnauthorizedError as err:
                raise LoginFailed("Invalid Zvuk Music token") from err
            except (NetworkError, TimedOutError) as err:
                LOGGER.error("Zvuk API error: %s", err)
                raise ResourceTemporarilyUnavailable("Zvuk Music request failed") from err
            except (BadRequestError, GraphQLError) as err:
                LOGGER.error("Zvuk API error: %s", err)
                raise ResourceTemporarilyUnavailable("Zvuk Music request failed") from err
            except BotDetectedError as err:
                raise ProviderUnavailableError("Bot detected by Zvuk") from err
            except NotFoundError:
                if not_found_return is _NOT_FOUND_SENTINEL:
                    raise
                return cast("_R", not_found_return)

        return wrapper

    return decorator


class ZvukMusicClient:
    """Wrapper around zvuk-music ClientAsync."""

    def __init__(self, token: str) -> None:
        """Initialize the Zvuk Music client.

        :param token: Zvuk Music X-Auth-Token.
        """
        self._token = token
        self._client: ClientAsync | None = None
        self._user_id: str | None = None

    @property
    def user_id(self) -> str:
        """Return the user ID."""
        if self._user_id is None:
            raise ProviderUnavailableError("Client not initialized, call connect() first")
        return self._user_id

    async def connect(self) -> None:
        """Initialize the client and verify token validity.

        :raises LoginFailed: If the token is invalid.
        :raises ResourceTemporarilyUnavailable: If there is a network error.
        """
        try:
            self._client = await ClientAsync(token=self._token).init()
            if not await self._client.is_authorized():
                raise LoginFailed("Invalid Zvuk Music token")
            profile = await self._client.get_profile()
            if profile and profile.result:
                self._user_id = str(profile.result.id)
            LOGGER.debug("Connected to Zvuk Music as user %s", self._user_id)
        except UnauthorizedError as err:
            raise LoginFailed("Invalid Zvuk Music token") from err
        except (NetworkError, TimedOutError) as err:
            msg = "Network error connecting to Zvuk Music"
            raise ResourceTemporarilyUnavailable(msg) from err

    async def disconnect(self) -> None:
        """Disconnect the client."""
        self._client = None
        self._user_id = None

    def _ensure_connected(self) -> ClientAsync:
        """Ensure the client is connected and return it."""
        if self._client is None:
            raise ProviderUnavailableError("Client not connected, call connect() first")
        return self._client

    # Search

    @handle_zvuk_errors(not_found_return=None)
    async def search(
        self,
        query: str,
        limit: int = DEFAULT_LIMIT,
        *,
        search_tracks: bool = True,
        search_artists: bool = True,
        search_releases: bool = True,
        search_playlists: bool = True,
    ) -> ZvukSearch | None:
        """Search for tracks, albums, artists, or playlists.

        :param query: Search query string.
        :param limit: Maximum number of results per type.
        :param search_tracks: Whether to search for tracks.
        :param search_artists: Whether to search for artists.
        :param search_releases: Whether to search for releases.
        :param search_playlists: Whether to search for playlists.
        :return: Search results object or None.
        """
        client = self._ensure_connected()
        return await client.search(
            query,
            limit=limit,
            tracks=search_tracks,
            artists=search_artists,
            releases=search_releases,
            playlists=search_playlists,
            podcasts=False,
            episodes=False,
            profiles=False,
            books=False,
        )

    # Get single items

    @handle_zvuk_errors(not_found_return=None)
    async def get_track(self, track_id: str) -> ZvukTrack | None:
        """Get a single track by ID.

        :param track_id: Track ID.
        :return: Track object or None if not found.
        """
        client = self._ensure_connected()
        return await client.get_track(track_id)

    @handle_zvuk_errors(not_found_return=[])
    async def get_tracks(self, track_ids: list[str]) -> list[ZvukTrack]:
        """Get multiple tracks by IDs.

        :param track_ids: List of track IDs.
        :return: List of track objects.
        """
        client = self._ensure_connected()
        ids: list[str | int] = list(track_ids)
        return await client.get_tracks(ids)

    @handle_zvuk_errors(not_found_return=None)
    async def get_release(self, release_id: str) -> ZvukRelease | None:
        """Get a single release (album) by ID.

        :param release_id: Release ID.
        :return: Release object or None if not found.
        """
        client = self._ensure_connected()
        return await client.get_release(release_id)

    @handle_zvuk_errors(not_found_return=[])
    async def get_releases(self, release_ids: list[str]) -> list[ZvukRelease]:
        """Get multiple releases by IDs.

        :param release_ids: List of release IDs.
        :return: List of release objects.
        """
        client = self._ensure_connected()
        ids: list[str | int] = list(release_ids)
        return await client.get_releases(ids)

    @handle_zvuk_errors(not_found_return=None)
    async def get_artist(self, artist_id: str) -> ZvukArtist | None:
        """Get a single artist by ID.

        :param artist_id: Artist ID.
        :return: Artist object or None if not found.
        """
        client = self._ensure_connected()
        return await client.get_artist(artist_id, with_description=True)

    @handle_zvuk_errors(not_found_return=[])
    async def get_artists(self, artist_ids: list[str]) -> list[ZvukArtist]:
        """Get multiple artists by IDs.

        :param artist_ids: List of artist IDs.
        :return: List of artist objects.
        """
        client = self._ensure_connected()
        ids: list[str | int] = list(artist_ids)
        return await client.get_artists(ids)

    @handle_zvuk_errors(not_found_return=[])
    async def get_artist_releases(
        self, artist_id: str, limit: int = DEFAULT_LIMIT
    ) -> list[ZvukArtist]:
        """Get artist's releases.

        :param artist_id: Artist ID.
        :param limit: Maximum number of releases.
        :return: List of artist objects with populated releases.
        """
        client = self._ensure_connected()
        return await client.get_artists([artist_id], with_releases=True, releases_limit=limit)

    @handle_zvuk_errors(not_found_return=[])
    async def get_artist_top_tracks(
        self, artist_id: str, limit: int = DEFAULT_LIMIT
    ) -> list[ZvukArtist]:
        """Get artist's top tracks.

        :param artist_id: Artist ID.
        :param limit: Maximum number of tracks.
        :return: List of artist objects with populated popular_tracks.
        """
        client = self._ensure_connected()
        return await client.get_artists([artist_id], with_popular_tracks=True, tracks_limit=limit)

    # Playlists

    @handle_zvuk_errors(not_found_return=None)
    async def get_playlist(self, playlist_id: str) -> ZvukPlaylist | None:
        """Get a playlist by ID.

        :param playlist_id: Playlist ID.
        :return: Playlist object or None if not found.
        """
        client = self._ensure_connected()
        return await client.get_playlist(playlist_id)

    @handle_zvuk_errors(not_found_return=[])
    async def get_playlists(self, playlist_ids: list[str]) -> list[ZvukPlaylist]:
        """Get multiple playlists by IDs.

        :param playlist_ids: List of playlist IDs.
        :return: List of playlist objects.
        """
        client = self._ensure_connected()
        ids: list[str | int] = list(playlist_ids)
        return await client.get_playlists(ids)

    @handle_zvuk_errors(not_found_return=[])
    async def get_playlist_tracks(
        self, playlist_id: str, limit: int = 50, offset: int = 0
    ) -> list[ZvukSimpleTrack]:
        """Get playlist tracks.

        :param playlist_id: Playlist ID.
        :param limit: Maximum number of tracks.
        :param offset: Offset for pagination.
        :return: List of SimpleTrack objects.
        """
        client = self._ensure_connected()
        return await client.get_playlist_tracks(playlist_id, limit=limit, offset=offset)

    # Streaming

    @handle_zvuk_errors(not_found_return=[])
    async def get_stream_urls(self, track_id: str) -> list[ZvukStream]:
        """Get stream URLs for a track.

        :param track_id: Track ID.
        :return: List of Stream objects.
        """
        client = self._ensure_connected()
        return await client.get_stream_urls(track_id)

    # Collection (Library)

    @handle_zvuk_errors()
    async def get_collection(self) -> Collection | None:
        """Get user's collection (liked items).

        :return: Collection object or None.
        """
        client = self._ensure_connected()
        return await client.get_collection()

    @handle_zvuk_errors(not_found_return=[])
    async def get_liked_tracks(self) -> list[ZvukTrack]:
        """Get user's liked tracks.

        :return: List of full Track objects.
        """
        client = self._ensure_connected()
        return await client.get_liked_tracks()

    @handle_zvuk_errors(not_found_return=[])
    async def get_user_playlists(self) -> list[ZvukCollectionItem]:
        """Get user's playlists.

        :return: List of CollectionItem objects with playlist IDs.
        """
        client = self._ensure_connected()
        return await client.get_user_playlists()

    # Library modifications

    async def like_track(self, track_id: str) -> bool:
        """Add a track to liked tracks.

        :param track_id: Track ID.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            return await client.like_track(track_id)
        except (BadRequestError, NetworkError, GraphQLError) as err:
            LOGGER.error("Error liking track %s: %s", track_id, err)
            return False

    async def unlike_track(self, track_id: str) -> bool:
        """Remove a track from liked tracks.

        :param track_id: Track ID.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            return await client.unlike_track(track_id)
        except (BadRequestError, NetworkError, GraphQLError) as err:
            LOGGER.error("Error unliking track %s: %s", track_id, err)
            return False

    async def like_release(self, release_id: str) -> bool:
        """Add a release to liked releases.

        :param release_id: Release ID.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            return await client.like_release(release_id)
        except (BadRequestError, NetworkError, GraphQLError) as err:
            LOGGER.error("Error liking release %s: %s", release_id, err)
            return False

    async def unlike_release(self, release_id: str) -> bool:
        """Remove a release from liked releases.

        :param release_id: Release ID.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            return await client.unlike_release(release_id)
        except (BadRequestError, NetworkError, GraphQLError) as err:
            LOGGER.error("Error unliking release %s: %s", release_id, err)
            return False

    async def like_artist(self, artist_id: str) -> bool:
        """Add an artist to liked artists.

        :param artist_id: Artist ID.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            return await client.like_artist(artist_id)
        except (BadRequestError, NetworkError, GraphQLError) as err:
            LOGGER.error("Error liking artist %s: %s", artist_id, err)
            return False

    async def unlike_artist(self, artist_id: str) -> bool:
        """Remove an artist from liked artists.

        :param artist_id: Artist ID.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            return await client.unlike_artist(artist_id)
        except (BadRequestError, NetworkError, GraphQLError) as err:
            LOGGER.error("Error unliking artist %s: %s", artist_id, err)
            return False

    async def like_playlist(self, playlist_id: str) -> bool:
        """Add a playlist to liked playlists.

        :param playlist_id: Playlist ID.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            return await client.like_playlist(playlist_id)
        except (BadRequestError, NetworkError, GraphQLError) as err:
            LOGGER.error("Error liking playlist %s: %s", playlist_id, err)
            return False

    async def unlike_playlist(self, playlist_id: str) -> bool:
        """Remove a playlist from liked playlists.

        :param playlist_id: Playlist ID.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            return await client.unlike_playlist(playlist_id)
        except (BadRequestError, NetworkError, GraphQLError) as err:
            LOGGER.error("Error unliking playlist %s: %s", playlist_id, err)
            return False

    # Playlist management

    @handle_zvuk_errors()
    async def create_playlist(self, name: str, track_ids: list[str] | None = None) -> str:
        """Create a new playlist.

        :param name: Playlist name.
        :param track_ids: Optional list of track IDs to add.
        :return: New playlist ID.
        """
        client = self._ensure_connected()
        return await client.create_playlist(name, track_ids=track_ids)

    async def delete_playlist(self, playlist_id: str) -> bool:
        """Delete a playlist.

        :param playlist_id: Playlist ID.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            return await client.delete_playlist(playlist_id)
        except (BadRequestError, NetworkError, GraphQLError) as err:
            LOGGER.error("Error deleting playlist %s: %s", playlist_id, err)
            return False

    async def add_tracks_to_playlist(self, playlist_id: str, track_ids: list[str]) -> bool:
        """Add tracks to a playlist.

        :param playlist_id: Playlist ID.
        :param track_ids: List of track IDs to add.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            return await client.add_tracks_to_playlist(playlist_id, track_ids)
        except (BadRequestError, NetworkError, GraphQLError) as err:
            LOGGER.error("Error adding tracks to playlist %s: %s", playlist_id, err)
            return False

    async def update_playlist(self, playlist_id: str, track_ids: list[str]) -> bool:
        """Update playlist tracks (used for removing tracks by providing remaining ones).

        :param playlist_id: Playlist ID.
        :param track_ids: Complete list of track IDs the playlist should contain.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            return await client.update_playlist(playlist_id, track_ids)
        except (BadRequestError, NetworkError, GraphQLError) as err:
            LOGGER.error("Error updating playlist %s: %s", playlist_id, err)
            return False
