"""
Regression tests for the ``playlists/*`` sub-server.

Pin three contracts that were broken before:

1. ``add_tracks`` never claims atomicity (the previous "≤10 atomic" carve-out
   was fiction — Music Assistant's playlist controller offers no atomicity at
   any size).
2. ``playlist_id`` accepts the ``library://playlist/<n>`` URI in addition to
   the bare integer id, normalising both to the integer that MA's controller
   requires.
3. Invalid identifiers raise a clean ``ToolError`` rather than a Python
   ``ValueError`` leaking out of MA.
"""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc, union-attr"

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from music_assistant.providers.fastmcp_server.tools.playlists import (
    _coerce_playlist_db_id,
    build_playlists_server,
)


@pytest.fixture
def server(mock_mass: Any) -> FastMCP:
    """Mount only the playlists sub-server on a fresh root FastMCP."""
    mcp: FastMCP = FastMCP(name="test")
    mcp.mount(build_playlists_server(mock_mass, require_confirmation=False), namespace="playlists")
    return mcp


class TestCoercePlaylistDbId:
    """The helper that normalises ``playlist_id`` to MA's integer form."""

    def test_passes_int_through(self) -> None:
        """Bare integers are returned unchanged."""
        assert _coerce_playlist_db_id(42) == 42

    @pytest.mark.parametrize("value", ["42", "  42 ", "1"])
    def test_accepts_digit_strings(self, value: str) -> None:
        """All-digit strings (with optional whitespace) coerce to int."""
        assert _coerce_playlist_db_id(value) == int(value.strip())

    @pytest.mark.parametrize(
        ("uri", "expected"),
        [
            ("library://playlist/1", 1),
            ("library://playlist/42", 42),
            ("library://playlist/9999", 9999),
        ],
    )
    def test_accepts_library_uri(self, uri: str, expected: int) -> None:
        """``library://playlist/<n>`` URIs unwrap to their integer id."""
        assert _coerce_playlist_db_id(uri) == expected

    @pytest.mark.parametrize(
        "bad",
        [
            "spotify://playlist/37i9dQZF1DXcBWIGoYBM5M",
            "library://playlist/abc",
            "library://album/42",
            "library://playlist/",
            "library://playlist/42/extra",
            "",
            "not-a-uri",
        ],
    )
    def test_rejects_invalid(self, bad: str) -> None:
        """Anything outside the accepted forms raises a clean ``ToolError``."""
        with pytest.raises(ToolError, match="Invalid playlist_id"):
            _coerce_playlist_db_id(bad)


class TestAddTracks:
    """The bulk add path: one call per track, no atomicity claim."""

    async def test_loops_one_by_one_with_progress(self, server: FastMCP, mock_mass: Any) -> None:
        """12 URIs produce 12 calls to ``add_playlist_track`` (not one bulk call)."""
        uris = [f"library://track/{i}" for i in range(12)]
        async with Client(server) as client:
            await client.call_tool(
                "playlists_add_tracks",
                {"playlist_id": 99, "track_uris": uris},
            )

        track_mock: AsyncMock = mock_mass.music.playlists.add_playlist_track
        bulk_mock: AsyncMock = mock_mass.music.playlists.add_playlist_tracks

        assert track_mock.await_count == 12
        assert bulk_mock.await_count == 0, (
            "add_playlist_tracks (bulk) must not be called — it returns a "
            "BackgroundTask without atomicity guarantees; the tool now loops "
            "one-by-one for honest progress reporting."
        )
        for i, call in enumerate(track_mock.await_args_list):
            assert call.args == (99, uris[i]), f"call {i} args drift"

    async def test_normalises_library_uri_to_int(self, server: FastMCP, mock_mass: Any) -> None:
        """A ``library://playlist/<n>`` URI is forwarded to MA as the bare int."""
        async with Client(server) as client:
            await client.call_tool(
                "playlists_add_tracks",
                {"playlist_id": "library://playlist/7", "track_uris": ["library://track/1"]},
            )
        track_mock: AsyncMock = mock_mass.music.playlists.add_playlist_track
        assert track_mock.await_args.args == (7, "library://track/1")

    async def test_docstring_does_not_claim_atomic(self, server: FastMCP) -> None:
        """CI guard against re-introduction of the false 'atomic' claim."""
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}
        desc = (tools["playlists_add_tracks"].description or "").lower()
        assert "atomically" not in desc
        assert "atomic semantics" not in desc

    async def test_rejects_invalid_playlist_id(self, server: FastMCP, mock_mass: Any) -> None:
        """A bad ``playlist_id`` short-circuits before any MA call is made."""
        async with Client(server) as client:
            with pytest.raises(ToolError, match="Invalid playlist_id"):
                await client.call_tool(
                    "playlists_add_tracks",
                    {
                        "playlist_id": "spotify://playlist/abc",
                        "track_uris": ["library://track/1"],
                    },
                )
        assert mock_mass.music.playlists.add_playlist_track.await_count == 0


class TestAddTrack:
    """The singular add path."""

    async def test_normalises_library_uri_to_int(self, server: FastMCP, mock_mass: Any) -> None:
        """URI form is forwarded as the unwrapped integer id."""
        async with Client(server) as client:
            await client.call_tool(
                "playlists_add_track",
                {"playlist_id": "library://playlist/42", "track_uri": "library://track/1"},
            )
        track_mock: AsyncMock = mock_mass.music.playlists.add_playlist_track
        assert track_mock.await_args.args == (42, "library://track/1")

    async def test_accepts_bare_int(self, server: FastMCP, mock_mass: Any) -> None:
        """A bare integer is passed through unchanged."""
        async with Client(server) as client:
            await client.call_tool(
                "playlists_add_track",
                {"playlist_id": 42, "track_uri": "library://track/1"},
            )
        assert mock_mass.music.playlists.add_playlist_track.await_args.args == (
            42,
            "library://track/1",
        )


class TestRemoveTracks:
    """The position-based delete path."""

    async def test_normalises_library_uri_to_int(self, server: FastMCP, mock_mass: Any) -> None:
        """URI form is unwrapped and positions are forwarded as an immutable tuple."""
        async with Client(server) as client:
            await client.call_tool(
                "playlists_remove_tracks",
                {"playlist_id": "library://playlist/5", "positions": [0, 2]},
            )
        remove_mock: AsyncMock = mock_mass.music.playlists.remove_playlist_tracks
        assert remove_mock.await_args.args == (5, (0, 2))


class TestSchemaContract:
    """Pin the user-facing JSON schema so the tool keeps accepting both forms."""

    @pytest.mark.parametrize(
        "tool_name",
        ["playlists_add_track", "playlists_add_tracks", "playlists_remove_tracks"],
    )
    async def test_playlist_id_schema_accepts_int_or_string(
        self, server: FastMCP, tool_name: str
    ) -> None:
        """Expose ``playlist_id`` to LLM callers as a string-or-integer union."""
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}
        prop = tools[tool_name].inputSchema["properties"]["playlist_id"]
        # JSON-schema rendering of ``str | int`` typically uses ``anyOf`` with
        # ``{"type": "integer"}`` and ``{"type": "string"}``; some encoders
        # use ``type: [string, integer]`` instead. Accept either.
        any_of = prop.get("anyOf")
        if any_of is not None:
            types = {entry.get("type") for entry in any_of}
        else:
            raw = prop.get("type")
            types = set(raw) if isinstance(raw, list) else {raw}
        assert {"integer", "string"} <= types, (
            f"{tool_name}: playlist_id schema drifted to {prop!r}; must "
            f"accept both integer and string so callers can pass "
            f"library://playlist/<n>"
        )
