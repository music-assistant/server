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

from music_assistant.providers.fastmcp_server.tools._common import (
    brief_from_uri,
    resolve_typed_uri,
    resolve_uri,
    to_brief_track,
)
from music_assistant.providers.fastmcp_server.tools.library import build_library_server
from music_assistant.providers.fastmcp_server.tools.metadata import build_metadata_server

from .media_fakes import fake_media_item


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


# ── resolve_typed_uri / brief_from_uri media-type assertion ─────────────────


class TestResolveTypedUriRejectsWrongMediaType:
    """Wrong-type URIs must raise with a hint for the resolved type."""

    @pytest.mark.parametrize(
        ("expected", "type_label", "wrong"),
        [
            (MediaType.TRACK, "track", MediaType.ALBUM),
            (MediaType.ALBUM, "album", MediaType.TRACK),
            (MediaType.ARTIST, "artist", MediaType.PLAYLIST),
            (MediaType.PLAYLIST, "playlist", MediaType.RADIO),
            (MediaType.RADIO, "radio", MediaType.TRACK),
        ],
    )
    async def test_rejects_wrong_type_with_matching_hint(
        self,
        mock_mass: Any,
        expected: MediaType,
        type_label: str,
        wrong: MediaType,
    ) -> None:
        """Hint names the tool for the resolved media type, not the caller's tool."""
        mock_mass.music.get_item_by_uri = AsyncMock(
            return_value=fake_media_item(wrong, uri=f"library://{wrong.value}/42")
        )
        with pytest.raises(ToolError, match=rf"is not an? {type_label}") as exc_info:
            await resolve_typed_uri(
                mock_mass,
                f"library://{wrong.value}/42",
                expected,
                type_label=type_label,
            )
        assert wrong.value in str(exc_info.value).lower()

    async def test_album_uri_on_track_tool_mentions_album_not_track(self, mock_mass: Any) -> None:
        """Album URI passed where a track is expected suggests album tools."""
        mock_mass.music.get_item_by_uri = AsyncMock(
            return_value=fake_media_item(MediaType.ALBUM, uri="library://album/7")
        )
        with pytest.raises(ToolError, match="library_get_album_by_uri"):
            await brief_from_uri(
                mock_mass,
                "library://album/7",
                MediaType.TRACK,
                to_brief=to_brief_track,
                type_label="track",
            )


class TestGetByUriIntegration:
    """Smoke-test MCP wiring for URI resolver tools."""

    async def test_get_track_by_uri_happy_path(
        self, library_server: FastMCP, mock_mass: Any
    ) -> None:
        """Track URI returns a brief via the mounted library server."""
        uri = "library://track/1"
        mock_mass.music.get_item_by_uri = AsyncMock(
            return_value=fake_media_item(MediaType.TRACK, uri=uri)
        )
        async with Client(library_server) as client:
            result = await client.call_tool("library_get_track_by_uri", {"uri": uri})
        text_blocks = [c.text for c in result.content if hasattr(c, "text")]
        assert any(uri in t for t in text_blocks)

    async def test_get_album_by_uri_happy_path(
        self, library_server: FastMCP, mock_mass: Any
    ) -> None:
        """Album URI returns a brief via the mounted library server."""
        uri = "library://album/1"
        mock_mass.music.get_item_by_uri = AsyncMock(
            return_value=fake_media_item(MediaType.ALBUM, uri=uri, artist="Artist")
        )
        async with Client(library_server) as client:
            result = await client.call_tool("library_get_album_by_uri", {"uri": uri})
        text_blocks = [c.text for c in result.content if hasattr(c, "text")]
        assert any(uri in t for t in text_blocks)


class TestGetLyricsRejectsNonTracks:
    """Lyrics are track-only; album/playlist URIs raise ``ToolError``."""

    async def test_rejects_album_uri(self, metadata_server: FastMCP, mock_mass: Any) -> None:
        """An album URI raises rather than returning ``None`` silently."""
        mock_mass.music.get_item_by_uri = AsyncMock(
            return_value=fake_media_item(MediaType.ALBUM, uri="library://album/7")
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
        item = fake_media_item(MediaType.TRACK, metadata=metadata)
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=item)

        async with Client(metadata_server) as client:
            result = await client.call_tool(
                "metadata_get_lyrics",
                {"track_uri": "library://track/1"},
            )
        text_blocks = [c.text for c in result.content if hasattr(c, "text")]
        assert any("verse one" in t for t in text_blocks)
