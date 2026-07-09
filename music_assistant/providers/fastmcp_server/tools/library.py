"""Library: search, list, and get tools (read-only)."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from music_assistant_models.enums import MediaType

from ..models import (
    AlbumBrief,
    AlbumTracksResult,
    ArtistAlbumsResult,
    ArtistBrief,
    PlaylistBrief,
    RadioBrief,
    TrackBrief,
)
from ..tags import Tag
from ._common import (
    TIMEOUT_QUERY,
    album_tracks_from_uri,
    artist_albums_from_uri,
    brief_from_uri,
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


def _register_uri_tools(sub: FastMCP, mass: MusicAssistant) -> None:
    """Register URI resolver and drill-down library tools."""

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
        return await brief_from_uri(
            mass,
            uri,
            MediaType.TRACK,
            to_brief=to_brief_track,
            type_label="track",
        )

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("Get album by URI"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def get_album_by_uri(uri: str) -> AlbumBrief:
        """
        Resolve an album by its Music Assistant URI to a brief summary.

        Returns the same ``AlbumBrief`` shape that search and list tools emit.
        Raises ``ToolError`` if the URI does not resolve, or if it resolves
        to a non-album. Use ``search_albums`` first if you only have a
        name or partial identifier. For the track listing, use
        ``get_album_tracks`` with the same URI.

        :param uri: A Music Assistant album URI of the form
            ``<provider>://album/<id>`` (e.g. as found on ``AlbumBrief.uri``).
        """
        return await brief_from_uri(
            mass,
            uri,
            MediaType.ALBUM,
            to_brief=to_brief_album,
            type_label="album",
        )

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("Get artist by URI"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def get_artist_by_uri(uri: str) -> ArtistBrief:
        """
        Resolve an artist by its Music Assistant URI to a brief summary.

        Returns the same ``ArtistBrief`` shape that search and list tools emit.
        Raises ``ToolError`` if the URI does not resolve, or if it resolves
        to a non-artist. Use ``search_artists`` first if you only have a
        name or partial identifier. For the artist's albums, use
        ``get_artist_albums`` with the same URI.

        :param uri: A Music Assistant artist URI of the form
            ``<provider>://artist/<id>`` (e.g. as found on ``ArtistBrief.uri``).
        """
        return await brief_from_uri(
            mass,
            uri,
            MediaType.ARTIST,
            to_brief=to_brief_artist,
            type_label="artist",
        )

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("Get artist albums"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def get_artist_albums(uri: str, ctx: Context | None = None) -> ArtistAlbumsResult:
        """
        List albums by an artist, newest first.

        Returns an ``ArtistAlbumsResult`` with a brief artist header and one
        ``AlbumBrief`` per album (including ``uri`` for drill-down or playback).
        This is a summary — not full metadata (genres, artwork URLs, etc.).

        Typical flow: ``search_artists`` → ``get_artist_albums`` →
        ``get_album_tracks`` or ``playback_play_media``.

        Raises ``ToolError`` if the URI does not resolve or is not an artist.

        :param uri: A Music Assistant artist URI of the form
            ``<provider>://artist/<id>`` (e.g. as found on ``ArtistBrief.uri``).
        """
        if ctx is not None:
            await ctx.info(f"Fetching albums for artist {uri!r}")
        return await artist_albums_from_uri(mass, uri)

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("Get playlist by URI"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def get_playlist_by_uri(uri: str) -> PlaylistBrief:
        """
        Resolve a playlist by its Music Assistant URI to a brief summary.

        Returns the same ``PlaylistBrief`` shape that list tools emit.
        Raises ``ToolError`` if the URI does not resolve, or if it resolves
        to a non-playlist.

        :param uri: A Music Assistant playlist URI of the form
            ``<provider>://playlist/<id>`` (e.g. as found on
            ``PlaylistBrief.uri``).
        """
        return await brief_from_uri(
            mass,
            uri,
            MediaType.PLAYLIST,
            to_brief=to_brief_playlist,
            type_label="playlist",
        )

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("Get radio by URI"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def get_radio_by_uri(uri: str) -> RadioBrief:
        """
        Resolve a radio station by its Music Assistant URI to a brief summary.

        Returns the same ``RadioBrief`` shape that list tools emit.
        Raises ``ToolError`` if the URI does not resolve, or if it resolves
        to a non-radio station.

        :param uri: A Music Assistant radio URI of the form
            ``<provider>://radio/<id>`` (e.g. as found on ``RadioBrief.uri``).
        """
        return await brief_from_uri(
            mass,
            uri,
            MediaType.RADIO,
            to_brief=to_brief_radio,
            type_label="radio",
        )

    @sub.tool(
        tags={Tag.QUERY_LIBRARY},
        annotations=_readonly("Get album tracks"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def get_album_tracks(uri: str, ctx: Context | None = None) -> AlbumTracksResult:
        """
        List the tracks on an album, in disc and track order.

        Returns an ``AlbumTracksResult`` with a brief album header and one
        ``TrackBrief`` per track (including ``uri`` for playback). This is a
        summary — not full album metadata (genres, artwork URLs, etc.).

        Typical flow: ``search_albums`` → ``get_album_tracks`` → pick a track
        URI or pass the album URI to ``playback_play_media`` for the full album.

        Raises ``ToolError`` if the URI does not resolve or is not an album.

        :param uri: A Music Assistant album URI of the form
            ``<provider>://album/<id>`` (e.g. as found on ``AlbumBrief.uri``).
        """
        if ctx is not None:
            await ctx.info(f"Fetching tracks for album {uri!r}")
        return await album_tracks_from_uri(mass, uri)


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
        items = await mass.music.tracks.library_items(limit=limit, offset=offset, summary=False)
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
        items = await mass.music.albums.library_items(limit=limit, offset=offset, summary=False)
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
        items = await mass.music.artists.library_items(limit=limit, offset=offset, summary=False)
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
        items = await mass.music.playlists.library_items(limit=limit, offset=offset, summary=False)
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
        items = await mass.music.radio.library_items(limit=limit, offset=offset, summary=False)
        return [to_brief_radio(r) for r in items]

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

    _register_uri_tools(sub, mass)

    return sub
