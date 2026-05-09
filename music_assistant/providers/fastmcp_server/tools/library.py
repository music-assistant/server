"""Library: search, list, and get tools (read-only)."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from music_assistant_models.enums import MediaType

from ..models import (
    AlbumBrief,
    ArtistBrief,
    PlaylistBrief,
    RadioBrief,
    TrackBrief,
)
from ..tags import Tag
from ._common import (
    TIMEOUT_QUERY,
    page_args,
    to_brief_album,
    to_brief_artist,
    to_brief_playlist,
    to_brief_radio,
    to_brief_track,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _readonly(title: str) -> ToolAnnotations:
    """Read-only library tool annotations with the supplied UI title."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def build_library_server(mass: MusicAssistant) -> FastMCP:
    """Construct the ``library/*`` sub-server."""
    sub: FastMCP = FastMCP(name="library")

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=ToolAnnotations(
            title="Search tracks",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def search_tracks(
        query: str, limit: int = 25, ctx: Context | None = None
    ) -> list[TrackBrief]:
        """Search for tracks by free-text query across all enabled providers."""
        if ctx is not None:
            await ctx.info(f"Searching MA for tracks matching {query!r} (limit={limit})")
        results = await mass.music.search(query, [MediaType.TRACK], limit=limit)
        return [to_brief_track(t) for t in (results.tracks or [])]

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=ToolAnnotations(
            title="Search albums",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def search_albums(
        query: str, limit: int = 25, ctx: Context | None = None
    ) -> list[AlbumBrief]:
        """Search for albums by free-text query."""
        if ctx is not None:
            await ctx.info(f"Searching MA for albums matching {query!r} (limit={limit})")
        results = await mass.music.search(query, [MediaType.ALBUM], limit=limit)
        return [to_brief_album(a) for a in (results.albums or [])]

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=ToolAnnotations(
            title="Search artists",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def search_artists(
        query: str, limit: int = 25, ctx: Context | None = None
    ) -> list[ArtistBrief]:
        """Search for artists by free-text query."""
        if ctx is not None:
            await ctx.info(f"Searching MA for artists matching {query!r} (limit={limit})")
        results = await mass.music.search(query, [MediaType.ARTIST], limit=limit)
        return [to_brief_artist(a) for a in (results.artists or [])]

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("List library tracks"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def list_library_tracks(offset: int = 0, limit: int = 50) -> list[TrackBrief]:
        """List tracks already in the user's library, paginated."""
        offset, limit = page_args(offset, limit)
        items = await mass.music.tracks.library_items(limit=limit, offset=offset)
        return [to_brief_track(t) for t in items]

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("List library albums"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def list_library_albums(offset: int = 0, limit: int = 50) -> list[AlbumBrief]:
        """List albums already in the user's library, paginated."""
        offset, limit = page_args(offset, limit)
        items = await mass.music.albums.library_items(limit=limit, offset=offset)
        return [to_brief_album(a) for a in items]

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("List library artists"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def list_library_artists(offset: int = 0, limit: int = 50) -> list[ArtistBrief]:
        """List artists already in the user's library, paginated."""
        offset, limit = page_args(offset, limit)
        items = await mass.music.artists.library_items(limit=limit, offset=offset)
        return [to_brief_artist(a) for a in items]

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("List library playlists"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def list_library_playlists(offset: int = 0, limit: int = 50) -> list[PlaylistBrief]:
        """List playlists already in the user's library, paginated."""
        offset, limit = page_args(offset, limit)
        items = await mass.music.playlists.library_items(limit=limit, offset=offset)
        return [to_brief_playlist(p) for p in items]

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("List library radio"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def list_library_radio(offset: int = 0, limit: int = 50) -> list[RadioBrief]:
        """List radio stations already in the user's library, paginated."""
        offset, limit = page_args(offset, limit)
        items = await mass.music.radio.library_items(limit=limit, offset=offset)
        return [to_brief_radio(r) for r in items]

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("Get track by URI"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def get_track_by_uri(uri: str) -> TrackBrief:
        """Resolve a track by its MA URI to a brief summary."""
        item = await mass.music.get_item_by_uri(uri)
        return to_brief_track(item)

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("Recently added tracks"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def recently_added_tracks(limit: int = 10) -> list[TrackBrief]:
        """Return tracks recently added to the library."""
        items = await mass.music.recently_added_tracks(limit=limit)
        return [to_brief_track(t) for t in items]

    return sub
