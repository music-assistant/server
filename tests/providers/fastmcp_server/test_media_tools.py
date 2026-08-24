"""
Targeted regression tests for the ``media/*`` sub-server.

Focus is the ``play_announcement`` URL-scheme guard and volume clamping —
both newly enforced by the tool to keep an LLM caller (or a malicious
prompt-injection vector) from pointing a player at, say,
``file:///etc/passwd`` or shoving an out-of-range volume into MA.
"""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc, union-attr"

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from music_assistant.providers.fastmcp_server.tools.media import build_media_server


@pytest.fixture
def server(mock_mass: Any) -> FastMCP:
    """Mount only the media sub-server on a fresh root FastMCP."""
    mcp: FastMCP = FastMCP(name="test")
    mcp.mount(build_media_server(mock_mass, require_confirmation=False), namespace="media")
    return mcp


class TestPlayAnnouncementUrlGuard:
    """The URL passed to ``play_announcement`` is user-controlled. Validate the scheme."""

    @pytest.mark.parametrize(
        "bad_url",
        [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "ftp://example.com/clip.mp3",
            "data:audio/mpeg;base64,AAAA",
            "/etc/passwd",
            "not-a-url",
        ],
    )
    async def test_rejects_non_http_url(
        self, server: FastMCP, mock_mass: Any, bad_url: str
    ) -> None:
        """Anything other than http(s) raises ``ToolError`` before reaching MA."""
        async with Client(server) as client:
            with pytest.raises(ToolError, match="http"):
                await client.call_tool(
                    "media_play_announcement",
                    {"player_id": "p1", "url": bad_url},
                )
        # MA's player API must never see the bad URL — short-circuit at the tool.
        mock_mass.players.play_announcement.assert_not_awaited()

    @pytest.mark.parametrize(
        "good_url",
        [
            "http://media.example/clip.mp3",
            "https://media.example/clip.mp3",
            "HTTP://EXAMPLE.COM/clip.mp3",  # scheme case-insensitive
        ],
    )
    async def test_accepts_http_and_https(
        self, server: FastMCP, mock_mass: Any, good_url: str
    ) -> None:
        """Http and https URLs are forwarded to MA verbatim."""
        async with Client(server) as client:
            await client.call_tool(
                "media_play_announcement",
                {"player_id": "p1", "url": good_url},
            )
        mock_mass.players.play_announcement.assert_awaited_once()
        args = mock_mass.players.play_announcement.call_args.args
        assert args[0] == "p1"
        assert args[1] == good_url


class TestPlayAnnouncementVolumeClamp:
    """Out-of-range volume values are clamped before reaching MA."""

    @pytest.mark.parametrize(
        ("given", "clamped"),
        [
            (-5, 0),
            (0, 0),
            (50, 50),
            (100, 100),
            (150, 100),
            (10000, 100),
        ],
    )
    async def test_clamps_to_zero_hundred(
        self, server: FastMCP, mock_mass: Any, given: int, clamped: int
    ) -> None:
        """Match the [0, 100] envelope every other volume tool enforces."""
        async with Client(server) as client:
            await client.call_tool(
                "media_play_announcement",
                {
                    "player_id": "p1",
                    "url": "https://media.example/clip.mp3",
                    "volume_level": given,
                },
            )
        kwargs = mock_mass.players.play_announcement.call_args.kwargs
        assert kwargs["volume_level"] == clamped

    async def test_none_passes_through(self, server: FastMCP, mock_mass: Any) -> None:
        """An omitted volume_level (the common case) reaches MA as None."""
        async with Client(server) as client:
            await client.call_tool(
                "media_play_announcement",
                {"player_id": "p1", "url": "https://media.example/clip.mp3"},
            )
        kwargs = mock_mass.players.play_announcement.call_args.kwargs
        assert kwargs["volume_level"] is None
