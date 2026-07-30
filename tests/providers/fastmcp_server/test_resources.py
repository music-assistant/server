"""
Tests for ``provider/resources/*`` handler return-value serialisation.

FastMCP's resource read API requires handlers to return ``str | bytes |
list[ResourceContents]``; returning an MA domain object or a provider Brief
dataclass directly raises ``contents must be str, bytes, or list``. These
tests pin the handlers down to JSON-text returns end-to-end via the
in-memory FastMCP Client transport.

All five library resource kinds (artist / album / track / playlist / radio)
are parametrized over a single test body — the handlers are structurally
identical, so a regression in any of them (e.g. dropping a registration or
mis-routing one controller getter) would slip through if only artist were
covered.
"""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc, attr-defined"

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.resources.library_resources import (
    register_library_resources,
)
from music_assistant.providers.fastmcp_server.resources.player_resources import (
    register_player_resources,
)

# (kind, mass-controller-attr, uri-stem) — keyed by library resource kind.
_LIBRARY_KINDS = [
    ("artist", "artists", "library://artist/17"),
    ("album", "albums", "library://album/17"),
    ("track", "tracks", "library://track/17"),
    ("playlist", "playlists", "library://playlist/17"),
    ("radio", "radio", "library://radio/17"),
]


@pytest.mark.parametrize(("kind", "controller_attr", "uri"), _LIBRARY_KINDS)
async def test_library_resource_returns_json_text(
    mock_mass: MagicMock, kind: str, controller_attr: str, uri: str
) -> None:
    """Each library resource kind serialises its existing item to JSON text."""
    payload = {"uri": uri, "name": f"{kind.title()} Name", "kind": kind}
    item = SimpleNamespace(uri=uri, name=payload["name"], to_dict=lambda p=payload: p)
    getattr(mock_mass.music, controller_attr).get_library_item.return_value = item

    mcp: FastMCP = FastMCP(name="t")
    register_library_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        contents = await client.read_resource(uri)

    text_blocks = [c.text for c in contents if hasattr(c, "text")]
    assert text_blocks, f"{kind}: no text content returned"
    parsed = json.loads(text_blocks[0])
    assert parsed["name"] == payload["name"], kind
    assert parsed["uri"] == uri, kind


@pytest.mark.parametrize(("kind", "controller_attr", "uri"), _LIBRARY_KINDS)
async def test_library_resource_returns_null_for_missing(
    mock_mass: MagicMock, kind: str, controller_attr: str, uri: str
) -> None:
    """Every library resource kind renders a missing item as the literal ``"null"``."""
    getattr(mock_mass.music, controller_attr).get_library_item.return_value = None

    mcp: FastMCP = FastMCP(name="t")
    register_library_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        contents = await client.read_resource(uri.replace("/17", "/999"))

    text_blocks = [c.text for c in contents if hasattr(c, "text")]
    assert text_blocks == ["null"], kind


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


async def test_player_resource_reports_synced_state(mock_mass: MagicMock) -> None:
    """
    A sync follower fetched by URI carries the synthesised ``state="synced"``.

    The Connect Wizard / clients reading ``player://{id}`` should see
    the same usability signal that ``list_players`` synthesises — the
    resource and tool share ``to_brief_player``, so any future
    refactor that bypasses the state ladder for the resource path
    would be caught here.
    """
    player = SimpleNamespace(
        player_id="follower",
        display_name="Lenco",
        name="Lenco",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        powered=True,
        current_media=None,
        available=True,
        enabled=True,
        active_group="group-leader",
    )
    mock_mass.players.get_player.return_value = player

    mcp: FastMCP = FastMCP(name="t")
    register_player_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        contents = await client.read_resource("player://follower")

    parsed = json.loads(next(c.text for c in contents if hasattr(c, "text")))
    assert parsed["state"] == "synced"
    assert parsed["active_group"] == "group-leader"


async def test_player_resource_reports_needs_setup_state(mock_mass: MagicMock) -> None:
    """An unconfigured player fetched by URI carries ``state="needs_setup"``."""
    player = SimpleNamespace(
        player_id="raw",
        display_name="Unboxed",
        name="Unboxed",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        powered=True,
        current_media=None,
        available=True,
        enabled=True,
        needs_setup=True,
    )
    mock_mass.players.get_player.return_value = player

    mcp: FastMCP = FastMCP(name="t")
    register_player_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        contents = await client.read_resource("player://raw")

    parsed = json.loads(next(c.text for c in contents if hasattr(c, "text")))
    assert parsed["state"] == "needs_setup"
    assert parsed["needs_setup"] is True


async def test_player_resource_reflects_external_source_playback(
    mock_mass: MagicMock,
) -> None:
    """
    ``player://`` resource passes the active queue so external-source state is visible.

    When a player is idle at the MA layer but Yandex Ynison is streaming through
    it (Spotify Connect / AirPlay / Ynison), ``get_active_queue`` returns a queue
    whose ``state`` is ``"playing"`` and whose ``current_item`` is an AUDIO_SOURCE
    item. The resource must surface ``state="playing"``, ``external_source``, and
    ``current_item`` identically to what ``players_get_player`` returns.
    """
    player = SimpleNamespace(
        player_id="ext1",
        display_name="Ynison Player",
        name="Ynison Player",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=80,
        powered=True,
        current_media=None,
        available=True,
        enabled=True,
    )
    mock_mass.players.get_player.return_value = player

    active_queue = SimpleNamespace(
        state=SimpleNamespace(value="playing"),
        current_item=SimpleNamespace(
            name="Yandex Music Connect (Ynison)",
            streamdetails=SimpleNamespace(
                media_type=SimpleNamespace(value="audio_source"),
                provider="yandex_ynison--PL8BnL7a",
                stream_metadata=SimpleNamespace(title="Behind Your Walls"),
            ),
        ),
    )
    mock_mass.player_queues.get_active_queue.return_value = active_queue

    mcp: FastMCP = FastMCP(name="t")
    register_player_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        contents = await client.read_resource("player://ext1")

    parsed = json.loads(next(c.text for c in contents if hasattr(c, "text")))
    assert parsed["state"] == "playing"
    assert parsed["external_source"] == "yandex_ynison--PL8BnL7a"
    assert parsed["current_item"] == "Behind Your Walls"


async def test_queue_resource_returns_json_text_for_brief(mock_mass: MagicMock) -> None:
    """A ``QueueBrief`` returned by the queue handler is JSON-serialised."""
    queue = SimpleNamespace(
        queue_id="q1",
        current_index=2,
        items=10,
        shuffle_enabled=True,
        repeat_mode=SimpleNamespace(value="all"),
    )
    items = [
        SimpleNamespace(queue_item_id=str(i), name=f"track-{i}", duration=180, media_item=None)
        for i in range(3)
    ]
    mock_mass.player_queues.get.return_value = queue
    mock_mass.player_queues.items.return_value = items

    mcp: FastMCP = FastMCP(name="t")
    register_player_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        contents = await client.read_resource("queue://q1")

    text_blocks = [c.text for c in contents if hasattr(c, "text")]
    assert text_blocks, "no text content returned"
    parsed = json.loads(text_blocks[0])
    assert parsed["queue_id"] == "q1"
    assert parsed["current_index"] == 2
    assert parsed["item_count"] == 10  # the canonical int total, not len(items)
    assert parsed["shuffle"] is True
    assert parsed["repeat"] == "all"
    assert len(parsed["items"]) == 3
    # The handler is documented to cap at MA's default page size of 500.
    mock_mass.player_queues.items.assert_called_once_with("q1", limit=500)


async def test_queue_resource_returns_null_for_missing(mock_mass: MagicMock) -> None:
    """A missing queue resolves to the literal ``"null"`` — no follow-up MA calls."""
    mock_mass.player_queues.get.return_value = None
    mock_mass.player_queues.items.reset_mock()

    mcp: FastMCP = FastMCP(name="t")
    register_player_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        contents = await client.read_resource("queue://does-not-exist")

    text_blocks = [c.text for c in contents if hasattr(c, "text")]
    assert text_blocks == ["null"]
    # ``items`` must not be queried when the queue itself is None — otherwise
    # the handler is doing wasted work and racing against a deleted queue.
    mock_mass.player_queues.items.assert_not_called()
