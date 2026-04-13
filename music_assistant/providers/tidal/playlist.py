"""Playlist management for Tidal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp.client_exceptions import ClientError
from music_assistant_models.errors import ResourceTemporarilyUnavailable

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
            data = {"title": name, "description": ""}
            result = await self.api.post(
                f"users/{self.auth.user_id}/playlists", data=data, as_form=True
            )
            return parse_playlist(self.provider, result)
        except ClientError as err:
            raise ResourceTemporarilyUnavailable("Failed to create playlist") from err

    async def add_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add tracks to playlist."""
        try:
            # Get ETag first
            api_result = await self.api.get(f"playlists/{prov_playlist_id}", return_etag=True)
            playlist_obj = api_result[0] if isinstance(api_result, tuple) else api_result
            etag = api_result[1] if isinstance(api_result, tuple) else None

            data = {
                "onArtifactNotFound": "SKIP",
                "trackIds": ",".join(map(str, prov_track_ids)),
                "toIndex": playlist_obj.get("numberOfTracks", 0),
                "onDupes": "SKIP",
            }
            headers = {"If-None-Match": etag} if etag else {}

            await self.api.post(
                f"playlists/{prov_playlist_id}/items", data=data, as_form=True, headers=headers
            )
        except ClientError as err:
            raise ResourceTemporarilyUnavailable("Failed to add tracks") from err

    async def remove_tracks(self, prov_playlist_id: str, positions: tuple[int, ...]) -> None:
        """Remove tracks from playlist."""
        try:
            # Get ETag first
            api_result = await self.api.get(f"playlists/{prov_playlist_id}", return_etag=True)
            etag = api_result[1] if isinstance(api_result, tuple) else None

            # Tidal uses 0-based indices in URL path
            indices = ",".join(str(pos - 1) for pos in positions)
            headers = {"If-None-Match": etag} if etag else {}

            await self.api.delete(f"playlists/{prov_playlist_id}/items/{indices}", headers=headers)
        except ClientError as err:
            raise ResourceTemporarilyUnavailable("Failed to remove tracks") from err
