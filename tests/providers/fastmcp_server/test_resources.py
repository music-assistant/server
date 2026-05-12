"""Tests for ``provider/resources/*`` handler return-value serialisation.

FastMCP's resource read API requires handlers to return ``str | bytes |
list[ResourceContents]``; returning an MA domain object or a provider Brief
dataclass directly raises ``contents must be str, bytes, or list``. These
tests pin the handlers down to JSON-text returns end-to-end via the
in-memory FastMCP Client transport.
"""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc, attr-defined"

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.resources.library_resources import (
    register_library_resources,
)
from music_assistant.providers.fastmcp_server.resources.player_resources import (
    register_player_resources,
)


async def test_library_artist_resource_returns_json_text(mock_mass: MagicMock) -> None:
    """An existing artist is serialised to JSON text in the response contents."""
    artist = SimpleNamespace(
        uri="library://artist/17",
        name="7Б",
        to_dict=lambda: {"uri": "library://artist/17", "name": "7Б"},
    )
    mock_mass.music.artists.get_library_item.return_value = artist

    mcp: FastMCP = FastMCP(name="t")
    register_library_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        contents = await client.read_resource("library://artist/17")

    text_blocks = [c.text for c in contents if hasattr(c, "text")]
    assert text_blocks, "no text content returned"
    parsed = json.loads(text_blocks[0])
    assert parsed["name"] == "7Б"
    assert parsed["uri"] == "library://artist/17"


async def test_library_artist_resource_returns_null_for_missing(mock_mass: MagicMock) -> None:
    """A missing library item resolves to ``None`` handler-side, rendered as ``"null"``."""
    mock_mass.music.artists.get_library_item.return_value = None

    mcp: FastMCP = FastMCP(name="t")
    register_library_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        contents = await client.read_resource("library://artist/999")

    text_blocks = [c.text for c in contents if hasattr(c, "text")]
    assert text_blocks == ["null"]


async def test_player_resource_returns_json_text_for_brief(mock_mass: MagicMock) -> None:
    """A ``PlayerBrief`` returned by the player handler is JSON-serialised."""
    player = SimpleNamespace(
        player_id="p1",
        display_name="P1",
        name="P1",
        playback_state=SimpleNamespace(value="playing"),
        volume_level=100,
        powered=True,
        current_media=None,
        state=SimpleNamespace(powered=True, current_media=None),
    )
    mock_mass.players.get_player.return_value = player

    mcp: FastMCP = FastMCP(name="t")
    register_player_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        contents = await client.read_resource("player://p1")

    text_blocks = [c.text for c in contents if hasattr(c, "text")]
    assert text_blocks, "no text content returned"
    parsed = json.loads(text_blocks[0])
    assert parsed["player_id"] == "p1"
    assert parsed["state"] == "playing"
    assert parsed["powered"] is True
