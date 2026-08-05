"""Playlist management for Tidal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp.client_exceptions import ClientError
from music_assistant_models.errors import ResourceTemporarilyUnavailable

from .constants import PLAYLISTS
from .parsers import parse_playlist

if TYPE_CHECKING:
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
            sent = [await self.provider.redirect_cached_id(str(tid)) for tid in prov_track_ids]
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
