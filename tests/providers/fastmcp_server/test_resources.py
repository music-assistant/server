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
from functools import partial
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import McpError
from music_assistant_models.access import PlaylistAccess
from music_assistant_models.auth import UserRole
from music_assistant_models.enums import PlaybackState, ProviderSharing
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.music.media.playlists import PlaylistController
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.webserver.helpers import auth_middleware
from music_assistant.providers.fastmcp_server.resource_helpers import (
    to_brief_player,
    to_brief_queue,
    to_resource_text,
)
from music_assistant.providers.fastmcp_server.resources.library_resources import (
    register_library_resources,
)
from music_assistant.providers.fastmcp_server.resources.player_resources import (
    register_player_resources,
)
from tests.common import MockProvider, attach_mock, make_player_state, mock_player_mass

# (kind, mass-controller-attr, uri-stem) — keyed by library resource kind.
_LIBRARY_KINDS = [
    ("artist", "artists", "library://artist/17"),
    ("album", "albums", "library://album/17"),
    ("track", "tracks", "library://track/17"),
    ("playlist", "playlists", "library://playlist/17"),
    ("radio", "radio", "library://radio/17"),
]


def test_resource_helpers_have_a_tool_independent_public_module() -> None:
    """Resources retain their converters after the custom tool package is removed."""
    assert all(callable(helper) for helper in (to_resource_text, to_brief_player, to_brief_queue))


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
    provider = MockProvider("test", mass=mock_player_mass())
    player, protocol = attach_mock(provider, "p1", "P1")
    protocol._attr_playback_state = PlaybackState.PLAYING
    protocol._attr_volume_level = 100
    player.update_state(force_update=True, signal_event=False)
    mock_mass.players.get_player.return_value = player
    mock_mass.player_queues.get_active_queue.return_value = None

    mcp: FastMCP = FastMCP(name="t")
    register_player_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        contents = await client.read_resource("player://pl_p1")

    text_blocks = [c.text for c in contents if hasattr(c, "text")]
    assert text_blocks, "no text content returned"
    parsed = json.loads(text_blocks[0])
    assert parsed["player_id"] == "pl_p1"
    assert parsed["state"] == "playing"
    assert parsed["volume_level"] == 100
    assert parsed["powered"] is True


async def test_player_resource_does_not_hide_queue_controller_failures(
    mock_mass: MagicMock,
) -> None:
    """An unexpected queue-controller failure surfaces instead of a plausible snapshot."""
    mock_mass.players.get_player.return_value = SimpleNamespace(
        player_id="p1", name="P1", state=None
    )
    mock_mass.player_queues.get_active_queue.side_effect = RuntimeError("queue controller bug")

    mcp: FastMCP = FastMCP(name="t")
    register_player_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        with pytest.raises(McpError):
            await client.read_resource("player://p1")


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
        name="Lenco",
        state=make_player_state("pl_follower", name="Lenco", active_group="pl_group"),
    )
    mock_mass.players.get_player.return_value = player

    mcp: FastMCP = FastMCP(name="t")
    register_player_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        contents = await client.read_resource("player://follower")

    parsed = json.loads(next(c.text for c in contents if hasattr(c, "text")))
    assert parsed["state"] == "synced"
    assert parsed["active_group"] == "pl_group"


async def test_player_resource_reports_needs_setup_state(mock_mass: MagicMock) -> None:
    """An unconfigured player fetched by URI carries ``state="needs_setup"``."""
    player = SimpleNamespace(
        name="Unboxed", state=make_player_state("pl_raw", name="Unboxed", needs_setup=True)
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
        name="Ynison Player",
        state=make_player_state("pl_ext1", name="Ynison Player", volume_level=80),
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


async def test_queue_resource_takes_a_legacy_id_of_the_player(mock_mass: MagicMock) -> None:
    """A queue URI naming the player by a legacy id returns that player's queue and items."""
    queues = PlayerQueuesController.__new__(PlayerQueuesController)
    queues.mass = SimpleNamespace(
        players=SimpleNamespace(
            resolve_player_id=lambda player_id: {"old-q1": "q1"}.get(player_id, player_id)
        )
    )
    queues._queue_data = {
        "q1": PlayerQueueData(
            queue=PlayerQueue(
                queue_id="q1", active=True, display_name="Kitchen", available=True, items=2
            ),
            items=[
                QueueItem(queue_id="q1", queue_item_id=str(i), name=f"track-{i}", duration=180)
                for i in range(2)
            ],
        )
    }
    mock_mass.player_queues = queues

    mcp: FastMCP = FastMCP(name="t")
    register_player_resources(mcp, mock_mass)
    async with Client(mcp) as client:
        contents = await client.read_resource("queue://old-q1")

    parsed = json.loads(next(c.text for c in contents if hasattr(c, "text")))
    assert parsed["queue_id"] == "q1"
    assert [item["name"] for item in parsed["items"]] == ["track-0", "track-1"]


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


@pytest.mark.parametrize(
    ("access", "visible"),
    [
        (PlaylistAccess(owner="member-1"), True),
        (PlaylistAccess(owner="member-2"), False),
        (PlaylistAccess(owner="member-2", sharing=ProviderSharing.MEMBERS), True),
        (PlaylistAccess(owner="member-2", sharing=ProviderSharing.SELECTED), False),
        (None, True),
    ],
)
async def test_playlist_resource_hides_playlists_the_member_may_not_see(
    mock_mass: MagicMock, access: PlaylistAccess | None, visible: bool
) -> None:
    """A personal playlist reads as ``"null"`` for a member it is not shared with."""
    playlist = SimpleNamespace(
        uri="library://playlist/17",
        access=access,
        to_dict=lambda: {"uri": "library://playlist/17", "name": "Private"},
    )
    mock_mass.music.playlists.get_library_item.return_value = playlist
    mock_mass.music.playlists.visible_to_caller = partial(
        PlaylistController.visible_to_caller, mock_mass.music.playlists
    )
    member = SimpleNamespace(user_id="member-1", role=UserRole.USER)

    mcp: FastMCP = FastMCP(name="t")
    register_library_resources(mcp, mock_mass)
    context_token = auth_middleware.current_user.set(member)
    try:
        async with Client(mcp) as client:
            contents = await client.read_resource("library://playlist/17")
    finally:
        auth_middleware.current_user.reset(context_token)

    text_blocks = [c.text for c in contents if hasattr(c, "text")]
    assert (text_blocks != ["null"]) is visible
