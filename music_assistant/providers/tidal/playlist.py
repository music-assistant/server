"""Playlist management for Tidal."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp.client_exceptions import ClientError
from music_assistant_models.errors import ResourceTemporarilyUnavailable

from music_assistant.helpers.playlists import validate_playlist_block_move

from .constants import PLAYLISTS
from .parsers import parse_playlist

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.media_items import Playlist

    from .provider import TidalProvider


class TidalPlaylistManager:
    """Manages Tidal playlist operations."""

    def __init__(self, provider: TidalProvider):
        """Initialize playlist manager."""
        self.provider = provider
        self.api = provider.api
        self.auth = provider.auth
        self.logger = provider.logger

    async def create(self, name: str) -> Playlist:
        """Create a new playlist."""
        try:
            result = await self.api.post(
                f"users/{self.auth.user_id}/playlists",
                data={"title": name, "description": ""},
                as_form=True,
            )
            playlist = parse_playlist(self.provider, result)
            # A freshly created playlist is by definition owned and editable by the
            # authenticated user, regardless of what the create response echoes back.
            playlist.is_editable = True
            playlist.owner = (
                self.auth.user.profile_name or self.auth.user.user_name or str(self.auth.user_id)
            )
            for mapping in playlist.provider_mappings:
                mapping.is_unique = True
            return playlist
        except ClientError as err:
            raise ResourceTemporarilyUnavailable("Failed to create playlist") from err

    async def add_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add tracks to playlist."""
        # v1 appends at the end and skips dead/duplicate ids server-side
        # (onArtifactNotFound / onDupes = SKIP), so no reactive healing round-trip
        # is needed; the cache-only redirect still maps ids the churn cache already
        # knows are stale onto their live equivalents before they would be skipped.
        # The playlist ETag guards the write against a concurrent modification.
        try:
            sent = await asyncio.gather(
                *(self.provider.redirect_cached_id(str(tid)) for tid in prov_track_ids)
            )
            playlist_obj, etag = await self.api.get_with_etag(f"{PLAYLISTS}/{prov_playlist_id}")
            data = {
                "onArtifactNotFound": "SKIP",
                "trackIds": ",".join(sent),
                "toIndex": playlist_obj.get("numberOfTracks", 0),
                "onDupes": "SKIP",
            }
            headers = {"If-None-Match": etag} if etag else {}
            await self.api.post(
                f"{PLAYLISTS}/{prov_playlist_id}/items", data=data, as_form=True, headers=headers
            )
        except ClientError as err:
            raise ResourceTemporarilyUnavailable("Failed to add tracks") from err

    async def remove_tracks(self, prov_playlist_id: str, positions: tuple[int, ...]) -> None:
        """Remove tracks from playlist."""
        # MA's 1-based positions come from the same v1 track listing get_playlist_tracks
        # returns, and v1 deletes by 0-based index, so the positions map straight onto the
        # delete with no cross-API resolution. The ETag guards against a lost update.
        if not positions:
            return
        try:
            _, etag = await self.api.get_with_etag(f"{PLAYLISTS}/{prov_playlist_id}")
            indices = ",".join(str(pos - 1) for pos in positions)
            headers = {"If-None-Match": etag} if etag else {}
            await self.api.delete(
                f"{PLAYLISTS}/{prov_playlist_id}/items/{indices}", headers=headers
            )
        except ClientError as err:
            raise ResourceTemporarilyUnavailable("Failed to remove tracks") from err

    async def move_track(self, prov_playlist_id: str, from_position: int, to_position: int) -> None:
        """
        Move one track of a playlist to another position.

        :param prov_playlist_id: The Tidal playlist uuid.
        :param from_position: The current position of the track, 1-based.
        :param to_position: The position the track ends up at after the move, 1-based.
        :raises InvalidDataError: A position is outside the playlist.
        """
        await self.move_tracks(prov_playlist_id, [from_position], to_position)

    async def move_tracks(
        self, prov_playlist_id: str, positions: Sequence[int], to_position: int | None = None
    ) -> None:
        """
        Move tracks of a playlist as one block to another position.

        The moved tracks keep their order.

        :param prov_playlist_id: The Tidal playlist uuid.
        :param positions: The current positions of the tracks, 1-based.
        :param to_position: The position the block's first track ends up at, 1-based. None
            moves the tracks to the end of the playlist.
        :raises InvalidDataError: A position is outside the playlist, or the block does not fit.
        """
        # v1 moves any set of 0-based indices in one call, and toIndex is the final index of
        # the first moved item (tidalapi's live tests: index 0 -> toIndex n-2 lands at n-2,
        # and indices 0-3 -> toIndex n land at the end in order).
        # The ETag guards against a lost update, as for add and remove.
        try:
            playlist_obj, etag = await self.api.get_with_etag(f"{PLAYLISTS}/{prov_playlist_id}")
            num_tracks = playlist_obj.get("numberOfTracks", 0)
            if to_position is None:
                to_position = num_tracks - len(positions) + 1
            selected = validate_playlist_block_move(num_tracks, positions, to_position)
            indices = ",".join(str(position - 1) for position in selected)
            headers = {"If-None-Match": etag} if etag else {}
            await self.api.post(
                f"{PLAYLISTS}/{prov_playlist_id}/items/{indices}",
                data={"toIndex": to_position - 1},
                as_form=True,
                headers=headers,
            )
        except ClientError as err:
            raise ResourceTemporarilyUnavailable("Failed to move tracks") from err
