"""Playlist management for Tidal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp.client_exceptions import ClientError
from music_assistant_models.errors import ResourceTemporarilyUnavailable

from .jsonapi import JsonApiDocument
from .parsers_v2 import parse_playlist as parse_playlist_v2

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
            body = {
                "data": {
                    "type": "playlists",
                    "attributes": {"name": name, "description": "", "accessType": "UNLISTED"},
                }
            }
            result = await self.api.write_jsonapi("POST", "playlists", body)
            doc = JsonApiDocument(result)
            playlist = parse_playlist_v2(self.provider, doc, doc.data)
            # The create response doesn't include the "owners" relationship, so
            # parse_playlist_v2 can't infer editability from it. The authenticated
            # user is by definition the owner of a playlist they just created.
            playlist.is_editable = True
            playlist.owner = (
                self.provider.auth.user.profile_name
                or self.provider.auth.user.user_name
                or str(self.provider.auth.user_id)
            )
            return playlist
        except ClientError as err:
            raise ResourceTemporarilyUnavailable("Failed to create playlist") from err

    async def add_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add tracks to playlist."""
        try:
            body = {"data": [{"type": "tracks", "id": str(tid)} for tid in prov_track_ids]}
            await self.api.write_jsonapi(
                "POST", f"playlists/{prov_playlist_id}/relationships/items", body
            )
        except ClientError as err:
            raise ResourceTemporarilyUnavailable("Failed to add tracks") from err

    async def remove_tracks(self, prov_playlist_id: str, positions: tuple[int, ...]) -> None:
        """Remove tracks from playlist."""
        try:
            entries = []
            async for doc in self.api.paginate_jsonapi(
                f"playlists/{prov_playlist_id}/relationships/items"
            ):
                entries.extend(doc.data_list)

            # The official DELETE requires the per-occurrence meta.itemId (a UUID),
            # not just the track id, since the same track can appear at multiple
            # positions. Resolve MA's 1-based positions to those entries.
            selected = []
            for pos in positions:
                index = pos - 1
                if 0 <= index < len(entries):
                    entry = entries[index]
                    selected.append(
                        {
                            "type": entry["type"],
                            "id": entry["id"],
                            "meta": {"itemId": entry["meta"]["itemId"]},
                        }
                    )

            if not selected:
                return

            body = {"data": selected}
            await self.api.write_jsonapi(
                "DELETE", f"playlists/{prov_playlist_id}/relationships/items", body
            )
        except ClientError as err:
            raise ResourceTemporarilyUnavailable("Failed to remove tracks") from err
