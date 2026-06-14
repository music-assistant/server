"""Regression tests for media-item URI resolution.

Two robustness fixes are pinned here:

1. ``_resolve_uri`` now translates Music Assistant's distinct error classes
   to distinct ``ToolError`` messages. Previously every failure (typo,
   provider offline, malformed URI) flattened to the same string, so the
   LLM caller couldn't decide whether to retry, fix the URI, or surface
   the outage to the user.

2. ``get_track_by_uri`` and ``get_lyrics`` now refuse non-track URIs
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

from music_assistant.providers.fastmcp_server.tools.library import build_library_server
from music_assistant.providers.fastmcp_server.tools.media import _resolve_uri, build_media_server
from music_assistant.providers.fastmcp_server.tools.metadata import build_metadata_server


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
    """Mount only the media sub-server (for ``_resolve_uri`` integration use)."""
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_media_server(mock_mass, require_confirmation=False), namespace="media")
    return mcp


# ── _resolve_uri narrow-exception handling ───────────────────────────────────


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
            await _resolve_uri(mock_mass, "library://track/1")

    async def test_unknown_exception_propagates(self, mock_mass: Any) -> None:
        """Unrecognised errors are not silently flattened — they propagate."""
        mock_mass.music.get_item_by_uri = AsyncMock(side_effect=RuntimeError("?!"))
        with pytest.raises(RuntimeError, match=r"\?!"):
            await _resolve_uri(mock_mass, "library://track/1")


# ── get_track_by_uri / get_lyrics media-type assertion ───────────────────────


def _fake_item(media_type: MediaType, **kwargs: Any) -> MagicMock:
    """Build a stub item exposing ``media_type`` and the usual brief fields."""
    item = MagicMock(
        spec_set=["media_type", "uri", "name", "artists", "album", "duration", "metadata"]
    )
    item.media_type = media_type
    item.uri = kwargs.get("uri", "library://track/1")
    item.name = kwargs.get("name", "Track Name")
    item.artists = kwargs.get("artists", [])
    item.album = kwargs.get("album")
    item.duration = kwargs.get("duration", 180)
    item.metadata = kwargs.get("metadata")
    return item


class TestGetTrackByUriRejectsNonTracks:
    """A non-track URI must raise rather than silently coerce."""

    @pytest.mark.parametrize(
        "wrong_type",
        [MediaType.ALBUM, MediaType.PLAYLIST, MediaType.ARTIST, MediaType.RADIO],
    )
    async def test_rejects_non_track_media_type(
        self, library_server: FastMCP, mock_mass: Any, wrong_type: MediaType
    ) -> None:
        """Passing e.g. an album URI raises ``ToolError`` (not a garbage TrackBrief)."""
        mock_mass.music.get_item_by_uri = AsyncMock(
            return_value=_fake_item(wrong_type, uri=f"library://{wrong_type.value}/42")
        )
        async with Client(library_server) as client:
            with pytest.raises(ToolError, match="is not a track"):
                await client.call_tool(
                    "library_get_track_by_uri",
                    {"uri": f"library://{wrong_type.value}/42"},
                )

    async def test_accepts_track_media_type(self, library_server: FastMCP, mock_mass: Any) -> None:
        """A genuine track URI returns the brief as before."""
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=_fake_item(MediaType.TRACK))
        async with Client(library_server) as client:
            result = await client.call_tool(
                "library_get_track_by_uri",
                {"uri": "library://track/1"},
            )
        text_blocks = [c.text for c in result.content if hasattr(c, "text")]
        assert any("library://track/1" in t for t in text_blocks)


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
