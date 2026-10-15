"""Tests for standalone Sendspin display Play routing."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from aiosendspin.models import MediaCommand
from aiosendspin.models.controller import ControllerCommandPayload
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryServerPairingStore
from aiosendspin.server import SendspinServer
from aiosendspin.server.client import SendspinClient
from aiosendspin.server.group import SendspinGroup
from aiosendspin.server.roles import ControllerPlayEvent
from music_assistant_models.enums import PlayerType

from music_assistant.providers.sendspin.player import (
    SUPPORTED_GROUP_COMMANDS,
    SendspinPlayer,
    SendspinVisualizerPlayer,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def display() -> AsyncGenerator[SendspinVisualizerPlayer]:
    """Create a display bound to a real protocol group."""
    server = SendspinServer(
        asyncio.get_running_loop(),
        Identity.generate(),
        "test",
        pairing_store=InMemoryServerPairingStore(),
    )
    client = SendspinClient(server, "display")
    group = SendspinGroup(server, client)
    client._group = group
    player = cast("Any", object.__new__(SendspinVisualizerPlayer))
    player._player_id = "display"
    player.api = client
    player._provider = SimpleNamespace(instance_id="sendspin")
    player.logger = logging.getLogger(__name__)
    player.unsub_event_cb = None
    player.unsub_group_event_cb = None
    candidates: list[Any] = []
    tasks: list[asyncio.Task[None]] = []

    def iter_players(**kwargs: Any) -> list[Any]:
        return [
            item
            for item in candidates
            if (kwargs.get("return_unavailable", True) or item.available)
            and (kwargs.get("return_disabled", True) or item.enabled)
        ]

    def create_task(coro: Any) -> None:
        tasks.append(asyncio.create_task(coro))

    player.mass = SimpleNamespace(
        players=SimpleNamespace(iter_players=iter_players, cmd_play=AsyncMock()),
        create_task=create_task,
        candidates=candidates,
        tasks=tasks,
    )
    yield player
    player._unsubscribe_client_callbacks()
    await asyncio.gather(*tasks)
    await server.close()


def _target(player_id: str = "group", **kwargs: Any) -> SimpleNamespace:
    """Create a configured playback target."""
    return SimpleNamespace(
        player_id=player_id,
        type=kwargs.get("type", PlayerType.GROUP),
        available=kwargs.get("available", True),
        enabled=kwargs.get("enabled", True),
        group_members=[],
        state=SimpleNamespace(static_group_members=kwargs.get("members", ["display"])),
    )


async def test_play_acceptance_and_routing(display: Any) -> None:
    """Advertisement allows the real controller event to play the configured group."""
    role = cast("Any", display._controller_role)
    display.mass.candidates.append(_target())
    events: list[Any] = []
    display.api.group.add_event_listener(lambda _group, event: events.append(event))
    command = ControllerCommandPayload(command=MediaCommand.PLAY)
    role.handle_command(command)
    assert not events
    display._subscribe_client_callbacks()
    role.handle_command(command)
    assert len(events) == 1
    assert isinstance(events[0], ControllerPlayEvent)
    await asyncio.gather(*display.mass.tasks)
    display.mass.players.cmd_play.assert_awaited_once_with("group")
    assert role._supported_commands == [MediaCommand.PLAY]


@pytest.mark.parametrize("synced", [False, True])
async def test_grouped_display_preserves_audio_controls(display: Any, synced: bool) -> None:
    """A display follower neither replaces controls nor dispatches a duplicate Play."""
    audio = SendspinClient(display.api._server, "audio")
    group = SendspinGroup(display.api._server, audio)
    audio._group = group
    await group.add_client(display.api)
    role = cast("Any", group.group_role("controller"))
    role.set_supported_commands(SUPPORTED_GROUP_COMMANDS)
    with patch.object(
        SendspinVisualizerPlayer, "synced_to", property(lambda _: "audio" if synced else None)
    ):
        display._subscribe_client_callbacks()
        with patch.object(display, "_schedule_membership_sync"):
            display._on_group_changed(group)
        assert role._supported_commands == SUPPORTED_GROUP_COMMANDS
        leader = cast("Any", object.__new__(SendspinPlayer))
        leader._player_id = "audio"
        leader.mass = display.mass
        leader._provider = display.provider
        leader._handle_controller_event = AsyncMock()
        group.add_event_listener(leader.group_event_cb)
        role.handle_command(ControllerCommandPayload(command=MediaCommand.PLAY))
        await asyncio.gather(*display.mass.tasks)
        leader._handle_controller_event.assert_awaited_once()
        display.mass.players.cmd_play.assert_not_awaited()


@pytest.mark.parametrize("count", [0, 2])
async def test_missing_or_ambiguous_target(
    display: Any, count: int, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing and ambiguous configured targets refuse Play with useful diagnostics."""
    display.mass.candidates.extend(_target(f"group-{index}") for index in range(count))
    await display._play_static_group()
    display.mass.players.cmd_play.assert_not_awaited()
    assert "display" in caplog.text
    assert ("group-0, group-1" if count else "none") in caplog.text


async def test_only_enabled_available_static_groups(display: Any) -> None:
    """Disabled, unavailable, dynamic, and non-group players cannot become targets."""
    display.mass.candidates.extend(
        [
            _target("disabled", enabled=False),
            _target("unavailable", available=False),
            _target("dynamic", members=[]),
            _target("audio", type=PlayerType.PLAYER),
            _target("valid"),
        ]
    )
    await display._play_static_group()
    display.mass.players.cmd_play.assert_awaited_once_with("valid")


async def test_solo_group_change_and_stale_callback(display: SendspinVisualizerPlayer) -> None:
    """A new solo group advertises Play and callbacks from the old group cannot route."""
    old_group = display.api.group
    new_group = SendspinGroup(display.api._server, display.api)
    display.api._group = new_group
    with patch.object(display, "_schedule_membership_sync"):
        display._on_group_changed(old_group)
        assert cast("Any", new_group.group_role("controller"))._supported_commands == []
        display._on_group_changed(new_group)
    assert cast("Any", display._controller_role)._supported_commands == [MediaCommand.PLAY]
    display.group_event_cb(old_group, ControllerPlayEvent())
    assert not display.mass.tasks


async def test_no_controller_role(display: SendspinVisualizerPlayer) -> None:
    """A group without a controller role remains unchanged."""
    with patch.object(SendspinVisualizerPlayer, "_controller_role", property(lambda _: None)):
        display._subscribe_client_callbacks()
        with patch.object(display, "_schedule_membership_sync"):
            display._on_group_changed(display.api.group)
    assert cast("Any", display.api.group.group_role("controller"))._supported_commands == []
