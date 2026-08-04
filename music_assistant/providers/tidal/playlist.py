"""Playlist management for Tidal."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
            for mapping in playlist.provider_mappings:
                mapping.is_unique = True
            return playlist
        except ClientError as err:
            raise ResourceTemporarilyUnavailable("Failed to create playlist") from err

    async def add_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add tracks to playlist."""
        try:
            sent = [await self.provider.redirect_cached_id(str(tid)) for tid in prov_track_ids]
            body = {"data": [{"type": "tracks", "id": i} for i in sent]}
            result = await self.api.write_jsonapi(
                "POST", f"playlists/{prov_playlist_id}/relationships/items", body
            )
            if "data" not in result:
                # No per-item feedback (e.g. a 204 response): the write succeeded
                # and there's nothing to diff against.
                return
            added = {d["id"] for d in result.get("data", [])}
            stale_originals = [
                str(orig) for orig, s in zip(prov_track_ids, sent, strict=True) if s not in added
            ]
            resolved = [
                live
                for orig in stale_originals
                if (live := await self.provider.resolve_live_track_id(orig))
            ]
            if resolved:
                retry_body = {"data": [{"type": "tracks", "id": i} for i in resolved]}
                await self.api.write_jsonapi(
                    "POST", f"playlists/{prov_playlist_id}/relationships/items", retry_body
                )
        except ClientError as err:
            raise ResourceTemporarilyUnavailable("Failed to add tracks") from err

    async def remove_tracks(self, prov_playlist_id: str, positions: tuple[int, ...]) -> None:
        """Remove tracks from playlist."""
        if not positions:
            return
        try:
            max_needed = max(positions)
            entries: list[dict[str, Any]] = []
            async for doc in self.api.paginate_jsonapi(
                f"playlists/{prov_playlist_id}/relationships/items"
            ):
                # Videos share this relationship, but MA's positions come from
                # the track listing, so counting anything else here would shift
                # the index and delete the wrong track.
                entries.extend(item for item in doc.data_list if item.get("type") == "tracks")
                if len(entries) >= max_needed:
                    break

            # The official DELETE requires the per-occurrence meta.itemId (a UUID),
            # not just the track id, since the same track can appear at multiple
            # positions. Resolve MA's 1-based positions to those entries.
            selected = []
            for pos in positions:
                index = pos - 1
                if 0 <= index < len(entries):
                    entry = entries[index]
                    item_uuid = (entry.get("meta") or {}).get("itemId")
                    if not item_uuid:
                        continue
                    selected.append(
                        {
                            "type": entry["type"],
                            "id": entry["id"],
                            "meta": {"itemId": item_uuid},
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
