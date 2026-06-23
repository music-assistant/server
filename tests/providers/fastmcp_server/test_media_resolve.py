"""
Regression tests for media-item URI resolution.

Two robustness fixes are pinned here:

1. ``resolve_uri`` now translates Music Assistant's distinct error classes
   to distinct ``ToolError`` messages. Previously every failure (typo,
   provider offline, malformed URI) flattened to the same string, so the
   LLM caller couldn't decide whether to retry, fix the URI, or surface
   the outage to the user.

2. ``get_*_by_uri`` and ``get_lyrics`` now refuse wrong-type URIs
   with a clean ``ToolError``. Previously ``to_brief_track`` would
   happily coerce an album or playlist into a garbage ``TrackBrief``,
   and ``get_lyrics`` would silently return ``None`` rather than
   surface the type confusion.
"""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc, union-attr"

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    InvalidProviderURI,
    MediaNotFoundError,
    ProviderUnavailableError,
)

from music_assistant.providers.fastmcp_server.tools._common import resolve_uri
from music_assistant.providers.fastmcp_server.tools.library import build_library_server
from music_assistant.providers.fastmcp_server.tools.media import build_media_server
from music_assistant.providers.fastmcp_server.tools.metadata import build_metadata_server

_URI_TOOLS: list[tuple[MediaType, str, str]] = [
    (MediaType.TRACK, "library_get_track_by_uri", "track"),
    (MediaType.ALBUM, "library_get_album_by_uri", "album"),
    (MediaType.ARTIST, "library_get_artist_by_uri", "artist"),
    (MediaType.PLAYLIST, "library_get_playlist_by_uri", "playlist"),
    (MediaType.RADIO, "library_get_radio_by_uri", "radio"),
]

_ALL_LIBRARY_TYPES = [
    MediaType.TRACK,
    MediaType.ALBUM,
    MediaType.ARTIST,
    MediaType.PLAYLIST,
    MediaType.RADIO,
]


@pytest.fixture
def library_server(mock_mass: Any) -> FastMCP:
    """Mount only the library sub-server."""
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_library_server(mock_mass), namespace="library")
    return mcp


@pytest.fixture
def metadata_server(mock_mass: Any) -> FastMCP:
    """Mount only the metadata sub-server."""
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_metadata_server(mock_mass), namespace="metadata")
    return mcp


@pytest.fixture
def media_server(mock_mass: Any) -> FastMCP:
    """Mount only the media sub-server (for ``resolve_uri`` integration use)."""
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_media_server(mock_mass, require_confirmation=False), namespace="media")
    return mcp


# ── resolve_uri narrow-exception handling ───────────────────────────────────


class TestResolveUriNarrowsExceptions:
    """Each MA error class maps to its own ToolError message."""

    @pytest.mark.parametrize(
        ("exc", "expected_fragment"),
        [
            (MediaNotFoundError("nope"), "not found"),
            (InvalidProviderURI("bad uri"), "Malformed"),
            (ProviderUnavailableError("offline"), "offline or unreachable"),
        ],
    )
    async def test_each_error_class_distinct_message(
        self, mock_mass: Any, exc: Exception, expected_fragment: str
    ) -> None:
        """The LLM caller sees a distinct, actionable message per failure class."""
        mock_mass.music.get_item_by_uri = AsyncMock(side_effect=exc)
        with pytest.raises(ToolError, match=expected_fragment):
            await resolve_uri(mock_mass, "library://track/1")

    async def test_unknown_exception_propagates(self, mock_mass: Any) -> None:
        """Unrecognised errors are not silently flattened — they propagate."""
        mock_mass.music.get_item_by_uri = AsyncMock(side_effect=RuntimeError("?!"))
        with pytest.raises(RuntimeError, match=r"\?!"):
            await resolve_uri(mock_mass, "library://track/1")


# ── get_*_by_uri / get_lyrics media-type assertion ───────────────────────────


def _fake_item(media_type: MediaType, **kwargs: Any) -> MagicMock:
    """Build a stub item exposing ``media_type`` and the usual brief fields."""
    item = MagicMock(
        spec_set=[
            "media_type",
            "uri",
            "name",
            "artists",
            "artist",
            "album",
            "duration",
            "year",
            "track_count",
            "owner",
            "description",
            "metadata",
        ]
    )
    item.media_type = media_type
    item.uri = kwargs.get("uri", f"library://{media_type.value}/1")
    item.name = kwargs.get("name", f"{media_type.value.title()} Name")
    item.artists = kwargs.get("artists", [])
    item.artist = kwargs.get("artist")
    item.album = kwargs.get("album")
    item.duration = kwargs.get("duration", 180)
    item.year = kwargs.get("year")
    item.track_count = kwargs.get("track_count")
    item.owner = kwargs.get("owner")
    item.description = kwargs.get("description")
    item.metadata = kwargs.get("metadata")
    return item


class TestGetByUriRejectsWrongMediaType:
    """A wrong-type URI must raise rather than silently coerce."""

    @pytest.mark.parametrize(("expected_type", "tool_name", "type_label"), _URI_TOOLS)
    @pytest.mark.parametrize("wrong_type", _ALL_LIBRARY_TYPES)
    async def test_rejects_wrong_media_type(
        self,
        library_server: FastMCP,
        mock_mass: Any,
        expected_type: MediaType,
        tool_name: str,
        type_label: str,
        wrong_type: MediaType,
    ) -> None:
        """Passing e.g. an album URI to get_track_by_uri raises ``ToolError``."""
        if wrong_type == expected_type:
            pytest.skip("same type is the happy path")
        mock_mass.music.get_item_by_uri = AsyncMock(
            return_value=_fake_item(wrong_type, uri=f"library://{wrong_type.value}/42")
        )
        async with Client(library_server) as client:
            with pytest.raises(ToolError, match=f"is not a {type_label}"):
                await client.call_tool(
                    tool_name,
                    {"uri": f"library://{wrong_type.value}/42"},
                )

    @pytest.mark.parametrize(("expected_type", "tool_name", "_type_label"), _URI_TOOLS)
    async def test_accepts_matching_media_type(
        self,
        library_server: FastMCP,
        mock_mass: Any,
        expected_type: MediaType,
        tool_name: str,
        _type_label: str,
    ) -> None:
        """A genuine URI of the expected type returns the brief."""
        uri = f"library://{expected_type.value}/1"
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=_fake_item(expected_type, uri=uri))
        async with Client(library_server) as client:
            result = await client.call_tool(tool_name, {"uri": uri})
        text_blocks = [c.text for c in result.content if hasattr(c, "text")]
        assert any(uri in t for t in text_blocks)


class TestGetLyricsRejectsNonTracks:
    """Lyrics are track-only; album/playlist URIs raise ``ToolError``."""

    async def test_rejects_album_uri(self, metadata_server: FastMCP, mock_mass: Any) -> None:
        """An album URI raises rather than returning ``None`` silently."""
        mock_mass.music.get_item_by_uri = AsyncMock(
            return_value=_fake_item(MediaType.ALBUM, uri="library://album/7")
        )
        async with Client(metadata_server) as client:
            with pytest.raises(ToolError, match="is not a track"):
                await client.call_tool(
                    "metadata_get_lyrics",
                    {"track_uri": "library://album/7"},
                )

    async def test_returns_lyrics_for_real_track(
        self, metadata_server: FastMCP, mock_mass: Any
    ) -> None:
        """A real track URI surfaces ``metadata.lyrics`` if present."""
        metadata = MagicMock()
        metadata.lyrics = "verse one\nverse two"
        item = _fake_item(MediaType.TRACK, metadata=metadata)
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=item)

        async with Client(metadata_server) as client:
            result = await client.call_tool(
                "metadata_get_lyrics",
                {"track_uri": "library://track/1"},
            )
        text_blocks = [c.text for c in result.content if hasattr(c, "text")]
        assert any("verse one" in t for t in text_blocks)
