"""URI-addressable read-only library resources."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..tags import Tag
from ..tools._common import to_resource_text

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def register_library_resources(mcp: Any, mass: MusicAssistant) -> None:
    """Register ``library://*`` resources on the given FastMCP root."""

    @mcp.resource("library://artist/{artist_id}", tags={Tag.QUERY_LIBRARY})  # type: ignore[untyped-decorator, unused-ignore]
    async def artist_resource(artist_id: str) -> str | None:
        """Full artist record by library id."""
        return to_resource_text(await mass.music.artists.get_library_item(artist_id))

    @mcp.resource("library://album/{album_id}", tags={Tag.QUERY_LIBRARY})  # type: ignore[untyped-decorator, unused-ignore]
    async def album_resource(album_id: str) -> str | None:
        """Full album record by library id."""
        return to_resource_text(await mass.music.albums.get_library_item(album_id))

    @mcp.resource("library://track/{track_id}", tags={Tag.QUERY_LIBRARY})  # type: ignore[untyped-decorator, unused-ignore]
    async def track_resource(track_id: str) -> str | None:
        """Full track record by library id."""
        return to_resource_text(await mass.music.tracks.get_library_item(track_id))

    @mcp.resource("library://playlist/{playlist_id}", tags={Tag.QUERY_LIBRARY})  # type: ignore[untyped-decorator, unused-ignore]
    async def playlist_resource(playlist_id: str) -> str | None:
        """Full playlist record by library id."""
        return to_resource_text(await mass.music.playlists.get_library_item(playlist_id))

    @mcp.resource("library://radio/{radio_id}", tags={Tag.QUERY_LIBRARY})  # type: ignore[untyped-decorator, unused-ignore]
    async def radio_resource(radio_id: str) -> str | None:
        """Full radio station record by library id."""
        return to_resource_text(await mass.music.radio.get_library_item(radio_id))
