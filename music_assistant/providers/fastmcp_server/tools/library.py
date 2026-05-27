"""Library: search, list, and get tools (read-only)."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
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
        """
        Search for tracks by free-text query across all enabled music providers.

        Returns ``TrackBrief`` items with ``uri``, ``name``, ``artists``, ``album``,
        and ``duration``. Use ``list_library_tracks`` instead to enumerate only
        tracks already saved to the user's library.

        :param query: Free-text search string.
        :param limit: Max results to return (clamped to ``[1, 200]``).
        """
        _, limit = page_args(0, limit)
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
        """
        Search for albums by free-text query across all enabled music providers.

        Returns ``AlbumBrief`` items with ``uri``, ``name``, ``artists`` and
        ``year``. Use ``list_library_albums`` to enumerate only albums already
        saved to the user's library.

        :param query: Free-text search string.
        :param limit: Max results to return (clamped to ``[1, 200]``).
        """
        _, limit = page_args(0, limit)
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
        """
        Search for artists by free-text query across all enabled music providers.

        Returns ``ArtistBrief`` items with ``uri`` and ``name``. Use
        ``list_library_artists`` to enumerate only artists already saved to the
        user's library.

        :param query: Free-text search string.
        :param limit: Max results to return (clamped to ``[1, 200]``).
        """
        _, limit = page_args(0, limit)
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
        """
        List tracks already saved to the user's library, paginated.

        Returns ``TrackBrief`` items in library order. Does not query external
        providers — use ``search_tracks`` for that.

        :param offset: Zero-based start position (clamped to ``>= 0``).
        :param limit: Page size (clamped to ``[1, 200]``).
        """
        offset, limit = page_args(offset, limit)
        items = await mass.music.tracks.library_items(limit=limit, offset=offset)
        return [to_brief_track(t) for t in items]

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("List library albums"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def list_library_albums(offset: int = 0, limit: int = 50) -> list[AlbumBrief]:
        """
        List albums already saved to the user's library, paginated.

        Returns ``AlbumBrief`` items in library order. Does not query external
        providers — use ``search_albums`` for that.

        :param offset: Zero-based start position (clamped to ``>= 0``).
        :param limit: Page size (clamped to ``[1, 200]``).
        """
        offset, limit = page_args(offset, limit)
        items = await mass.music.albums.library_items(limit=limit, offset=offset)
        return [to_brief_album(a) for a in items]

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("List library artists"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def list_library_artists(offset: int = 0, limit: int = 50) -> list[ArtistBrief]:
        """
        List artists already saved to the user's library, paginated.

        Returns ``ArtistBrief`` items in library order. Does not query external
        providers — use ``search_artists`` for that.

        :param offset: Zero-based start position (clamped to ``>= 0``).
        :param limit: Page size (clamped to ``[1, 200]``).
        """
        offset, limit = page_args(offset, limit)
        items = await mass.music.artists.library_items(limit=limit, offset=offset)
        return [to_brief_artist(a) for a in items]

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("List library playlists"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def list_library_playlists(offset: int = 0, limit: int = 50) -> list[PlaylistBrief]:
        """
        List playlists already saved to the user's library, paginated.

        Returns ``PlaylistBrief`` items in library order.

        :param offset: Zero-based start position (clamped to ``>= 0``).
        :param limit: Page size (clamped to ``[1, 200]``).
        """
        offset, limit = page_args(offset, limit)
        items = await mass.music.playlists.library_items(limit=limit, offset=offset)
        return [to_brief_playlist(p) for p in items]

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("List library radio"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def list_library_radio(offset: int = 0, limit: int = 50) -> list[RadioBrief]:
        """
        List radio stations already saved to the user's library, paginated.

        Returns ``RadioBrief`` items in library order.

        :param offset: Zero-based start position (clamped to ``>= 0``).
        :param limit: Page size (clamped to ``[1, 200]``).
        """
        offset, limit = page_args(offset, limit)
        items = await mass.music.radio.library_items(limit=limit, offset=offset)
        return [to_brief_radio(r) for r in items]

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("Get track by URI"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def get_track_by_uri(uri: str) -> TrackBrief:
        """
        Resolve a track by its Music Assistant URI to a brief summary.

        Returns the same ``TrackBrief`` shape that search and list tools emit.
        Raises ``ToolError`` if the URI does not resolve, or if it resolves
        to a non-track (album, playlist, …) — otherwise the brief would
        silently carry the wrong shape and downstream tools would
        misinterpret it. Use ``search_tracks`` first if you only have a
        name or partial identifier.

        :param uri: A Music Assistant track URI of the form
            ``<provider>://track/<id>`` (e.g. as found on
            ``TrackBrief.uri``).
        """
        item = await mass.music.get_item_by_uri(uri)
        media_type = getattr(item, "media_type", None)
        if media_type != MediaType.TRACK:
            raise ToolError(
                f"URI {uri!r} is not a track (got media_type={media_type!r}); "
                f"pass a `library://track/<n>` URI instead."
            )
        return to_brief_track(item)

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("Recently added tracks"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def recently_added_tracks(limit: int = 10) -> list[TrackBrief]:
        """
        Return tracks most recently added to the user's library, newest first.

        Returns ``TrackBrief`` items. Does not paginate further than the first
        page — pick a higher ``limit`` if more history is needed.

        :param limit: Max results to return (clamped to ``[1, 200]``).
        """
        _, limit = page_args(0, limit)
        items = await mass.music.recently_added_tracks(limit=limit)
        return [to_brief_track(t) for t in items]

    return sub
