"""Tests for the squeezelite player provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from aiohttp import web
from aiosendspin.clock import ManualClock
from aioslimproto.cli import SlimCLICommand, parse_args
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import SlimEvent
from music_assistant_models.enums import EventType, ProtocolRole, ProviderFeature
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.event import MassEvent

from music_assistant.helpers.timeline_clock import TimelineClock
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.squeezelite import SUPPORTED_FEATURES
from music_assistant.providers.squeezelite.provider import SqueezelitePlayerProvider
from music_assistant.providers.squeezelite.sendspin_bridge import (
    BRIDGE_STREAM_PATH,
    SendspinBridgeManager,
)

PLAYER_COMMANDS = ("cmd_play", "cmd_pause", "cmd_play_pause", "cmd_stop")


def test_the_provider_syncs_no_players_itself() -> None:
    """Squeezelite players are synced by the Sendspin provider, through their bridges."""
    assert ProviderFeature.SYNC_PLAYERS not in SUPPORTED_FEATURES


def test_a_client_that_disconnects_is_detached_from_its_player() -> None:
    """A client that disconnects has its protocol unregistered."""
    provider = MagicMock()
    provider.mass.closing = False
    provider.get_protocol.return_value = MagicMock(protocol_id="p1")
    event = SlimEvent(type=SlimEventType.PLAYER_DISCONNECTED, player_id="p1")

    SqueezelitePlayerProvider._handle_slimproto_event(provider, event)

    provider.mass.players.unregister.assert_called_once_with("p1")


def test_a_client_that_connects_is_set_up() -> None:
    """A client that connects, also again before its old socket closed, gets a new protocol."""
    provider = MagicMock()
    provider.mass.closing = False
    client = provider.slimproto.get_player.return_value
    event = SlimEvent(type=SlimEventType.PLAYER_CONNECTED, player_id="p1")

    with patch("music_assistant.providers.squeezelite.provider.SqueezelitePlayer") as player_class:
        SqueezelitePlayerProvider._handle_slimproto_event(provider, event)

    player_class.assert_called_once_with(provider, "p1", client)
    provider.mass.create_task.assert_called_once_with(player_class.return_value.setup.return_value)


def test_a_change_of_a_queue_reaches_every_player() -> None:
    """Each player hears of a changed queue: the ones that play it show its repeat and shuffle."""
    provider = MagicMock()
    provider.protocols = [MagicMock(), MagicMock()]

    SqueezelitePlayerProvider._on_queue_updated(
        provider, MassEvent(event=EventType.QUEUE_UPDATED, object_id="queue_1")
    )

    for protocol in provider.protocols:
        protocol.on_queue_updated.assert_called_once_with("queue_1")


def test_the_display_name_is_the_one_set_in_music_assistant() -> None:
    """The name for a client comes from Music Assistant's player, and is None for an unknown one."""
    provider = MagicMock()
    # the CLI names a client by its slimproto id, which is the id of its protocol
    player = MagicMock()
    player.name = "Kitchen"
    provider.mass.players.get_player_by_protocol.side_effect = {"p1": player}.get

    assert SqueezelitePlayerProvider._lookup_display_name(provider, "p1") == "Kitchen"
    assert SqueezelitePlayerProvider._lookup_display_name(provider, "other") is None


def test_a_client_without_a_bridge_reports_its_own_position() -> None:
    """The position in the track is left to a client that has no Sendspin bridge."""
    provider = MagicMock()
    provider.bridge_manager.get_bridge.return_value = None

    assert SqueezelitePlayerProvider._lookup_elapsed(provider, "other") is None


async def test_the_cli_reports_the_name_and_position_music_assistant_shows() -> None:
    """Loading the provider hands its lookups to the CLI, which reports them to the clients."""
    provider = SqueezelitePlayerProvider.__new__(SqueezelitePlayerProvider)
    provider.slimproto = MagicMock(start=AsyncMock())
    provider.mass = MagicMock()

    with patch.object(PlayerProvider, "loaded_in_mass", new=AsyncMock()):
        await provider.loaded_in_mass()

    assert provider.slimproto.cli.display_name_lookup == provider._lookup_display_name
    assert provider.slimproto.cli.elapsed_lookup == provider._lookup_elapsed


async def test_connected_client_without_attached_protocol_is_attached() -> None:
    """A connected client whose player was removed is attached again; attached or disabled not."""
    provider = SqueezelitePlayerProvider.__new__(SqueezelitePlayerProvider)
    attached = MagicMock(player_id="attached")
    removed = MagicMock(player_id="removed")
    disabled = MagicMock(player_id="disabled")
    provider.slimproto = MagicMock(players=[attached, removed, disabled])
    provider.mass = MagicMock()
    provider.mass.players.get_protocol.side_effect = {"attached": MagicMock()}.get
    provider.mass.players.wants_protocol.side_effect = lambda protocol_id: protocol_id != "disabled"

    with patch("music_assistant.providers.squeezelite.provider.SqueezelitePlayer") as player_class:
        player_class.return_value.setup = AsyncMock()
        await provider.discover_players()

    player_class.assert_called_once_with(provider, "removed", removed)
    player_class.return_value.setup.assert_awaited_once()


def test_a_client_of_a_disabled_player_gets_no_commands() -> None:
    """A client that connects while its player is disabled is left alone until enabled."""
    provider = MagicMock()
    provider.mass.closing = False
    provider.get_protocol = MagicMock(return_value=None)
    provider.mass.players.wants_protocol = MagicMock(return_value=False)
    event = SlimEvent(type=SlimEventType.PLAYER_CONNECTED, player_id="disabled")

    with patch("music_assistant.providers.squeezelite.provider.SqueezelitePlayer") as player_class:
        SqueezelitePlayerProvider._handle_slimproto_event(provider, event)

    player_class.assert_not_called()
    provider.mass.players.wants_protocol.assert_called_once_with("disabled")


async def test_the_cli_hands_its_commands_to_the_provider() -> None:
    """The provider gets to handle every CLI command before aioslimproto does."""
    provider = SqueezelitePlayerProvider.__new__(SqueezelitePlayerProvider)
    provider.mass = MagicMock()
    provider.config = MagicMock()
    provider.logger = MagicMock()

    with (
        patch("music_assistant.providers.squeezelite.provider.logging"),
        patch("music_assistant.providers.squeezelite.provider.SlimServer") as slim_server,
        patch.object(provider, "_validate_all_ports", new=AsyncMock()),
    ):
        await provider.handle_async_init()

    assert slim_server.call_args.kwargs["cli_command_handler"] == provider._handle_cli_command


def _cli_provider(role: ProtocolRole = ProtocolRole.NATIVE) -> MagicMock:
    """Return a mocked provider whose protocol 'p1', of the given role, is attached to 'kitchen'."""
    provider = MagicMock()
    protocol = MagicMock(role=role, player=MagicMock(player_id="kitchen"))
    # a protocol is held without a player until it is known which player it belongs to
    provider.get_protocol = {"p1": protocol, "unattached": MagicMock(player=None)}.get
    # no Sendspin bridge plays on the client
    provider.bridge_manager.get_transport_command_target.return_value = None
    for cmd in PLAYER_COMMANDS:
        setattr(provider.mass.players, cmd, AsyncMock())
    return provider


def _cli_command(player_id: str | None, raw: list[str]) -> SlimCLICommand:
    """Return the CLI command aioslimproto parses from the raw tokens a client sends."""
    return SlimCLICommand(player_id, raw[0], *parse_args(list(raw[1:])))


def _awaited_player_commands(provider: MagicMock) -> list[str]:
    """Return the names of the player commands the provider awaited."""
    return [cmd for cmd in PLAYER_COMMANDS if getattr(provider.mass.players, cmd).await_count]


@pytest.mark.parametrize(
    ("raw", "expected_cmd"),
    [
        (["play"], "cmd_play"),
        (["pause", "1"], "cmd_pause"),
        (["pause", "0"], "cmd_play"),
        (["pause"], "cmd_play_pause"),
        (["mode", "play"], "cmd_play"),
        (["mode", "pause"], "cmd_pause"),
        (["mode", "stop"], "cmd_stop"),
        (["stop"], "cmd_stop"),
    ],
)
async def test_cli_transport_commands_control_the_player_group(
    raw: list[str], expected_cmd: str
) -> None:
    """Play, pause and stop from a client go to Music Assistant, which acts on its whole group."""
    provider = _cli_provider()

    await SqueezelitePlayerProvider._handle_cli_command(provider, _cli_command("p1", raw))

    assert _awaited_player_commands(provider) == [expected_cmd]
    getattr(provider.mass.players, expected_cmd).assert_awaited_once_with("kitchen")


@pytest.mark.parametrize("raw", [["stop"], ["mode", "stop"]])
async def test_cli_stop_of_a_protocol_device_only_stops_that_device(raw: list[str]) -> None:
    """A protocol-only device also sends stop when another source takes over, so it stops only itself."""
    provider = _cli_provider(ProtocolRole.AUDIO)

    with pytest.raises(NotImplementedError):
        await SqueezelitePlayerProvider._handle_cli_command(provider, _cli_command("p1", raw))

    assert _awaited_player_commands(provider) == []


@pytest.mark.parametrize(
    ("player_id", "raw"),
    [
        ("p1", ["power", "1"]),
        ("p1", ["mode", "?"]),
        ("other", ["play"]),
        ("unattached", ["play"]),
        (None, ["play"]),
    ],
)
async def test_cli_leaves_other_commands_to_aioslimproto(
    player_id: str | None, raw: list[str]
) -> None:
    """Other commands, and commands for a player Music Assistant does not know, are not handled."""
    provider = _cli_provider()

    with pytest.raises(NotImplementedError):
        await SqueezelitePlayerProvider._handle_cli_command(provider, _cli_command(player_id, raw))

    assert _awaited_player_commands(provider) == []


async def test_cli_command_that_fails_is_logged() -> None:
    """A player command that fails is logged instead of failing the client's request."""
    provider = _cli_provider()
    provider.mass.players.cmd_pause.side_effect = PlayerUnavailableError("unavailable")

    await SqueezelitePlayerProvider._handle_cli_command(
        provider, _cli_command("p1", ["pause", "1"])
    )

    provider.logger.warning.assert_called_once()


# ---------------------------------------------------------------- Sendspin bridge


async def test_the_slimproto_server_runs_on_the_clock_of_sendspin() -> None:
    """Every server time of aioslimproto is on the clock the Sendspin timestamps are on."""
    provider = SqueezelitePlayerProvider.__new__(SqueezelitePlayerProvider)
    provider.mass = MagicMock()
    provider.mass.timeline_clock = TimelineClock()
    provider.config = MagicMock()
    provider.logger = MagicMock()

    with (
        patch("music_assistant.providers.squeezelite.provider.logging"),
        patch("music_assistant.providers.squeezelite.provider.SlimServer") as slim_server,
        patch.object(provider, "_validate_all_ports", new=AsyncMock()),
    ):
        await provider.handle_async_init()

    # the timeline clock of the server, which Sendspin runs on too
    assert provider.clock is provider.mass.timeline_clock
    assert isinstance(provider.bridge_manager, SendspinBridgeManager)
    server_time = slim_server.call_args.kwargs["clock"]
    provider.clock = ManualClock(now_us_value=5_000_000_250_000)
    assert server_time() == 5_000_000.25


async def test_a_bridge_stream_pins_the_rate_of_the_timeline_clock() -> None:
    """While a bridge streams to its player, the rate of the timeline clock waits."""
    provider = SqueezelitePlayerProvider.__new__(SqueezelitePlayerProvider)
    provider.mass = MagicMock()
    provider.mass.timeline_clock = clock = TimelineClock()
    provider.config = MagicMock()
    provider.logger = MagicMock()
    with (
        patch("music_assistant.providers.squeezelite.provider.logging"),
        patch("music_assistant.providers.squeezelite.provider.SlimServer"),
        patch.object(provider, "_validate_all_ports", new=AsyncMock()),
    ):
        await provider.handle_async_init()
    bridge = MagicMock(owns_stream=False, stop=AsyncMock())
    provider.bridge_manager._bridges["sq01"] = bridge

    pinned = [clock.rate_pinned]
    bridge.owns_stream = True
    pinned.append(clock.rate_pinned)
    await provider.unload()
    provider.bridge_manager._bridges["sq01"] = bridge
    pinned.append(clock.rate_pinned)

    # an unloaded provider holds nothing back
    assert pinned == [False, True, False]


async def test_the_bridge_stream_route_follows_the_provider() -> None:
    """The route of the bridge streams is there while the provider is loaded."""
    provider = SqueezelitePlayerProvider.__new__(SqueezelitePlayerProvider)
    provider.slimproto = MagicMock(start=AsyncMock(), stop=AsyncMock())
    provider.mass = MagicMock()
    provider.bridge_manager = bridge_manager = MagicMock(close=AsyncMock())

    with patch.object(PlayerProvider, "loaded_in_mass", new=AsyncMock()):
        await provider.loaded_in_mass()
    provider.mass.streams.register_dynamic_route.assert_any_call(
        BRIDGE_STREAM_PATH, provider._serve_bridge_stream
    )
    # the stream of a bridge is the only audio the provider serves itself
    routes = [route.args[0] for route in provider.mass.streams.register_dynamic_route.mock_calls]
    assert routes == [BRIDGE_STREAM_PATH, "/jsonrpc.js"]

    await provider.unload()

    bridge_manager.close.assert_awaited_once()
    provider.mass.streams.unregister_dynamic_route.assert_any_call(BRIDGE_STREAM_PATH)
    removed = [route.args[0] for route in provider.mass.streams.unregister_dynamic_route.mock_calls]
    assert removed == routes


async def test_the_provider_follows_queue_changes_while_loaded() -> None:
    """Queue changes reach the players while the provider is loaded, and no longer after."""
    provider = SqueezelitePlayerProvider.__new__(SqueezelitePlayerProvider)
    provider.slimproto = MagicMock(start=AsyncMock(), stop=AsyncMock())
    provider.mass = MagicMock()
    provider.bridge_manager = MagicMock(close=AsyncMock())

    with patch.object(PlayerProvider, "loaded_in_mass", new=AsyncMock()):
        await provider.loaded_in_mass()

    provider.mass.subscribe.assert_called_once_with(
        provider._on_queue_updated, EventType.QUEUE_UPDATED
    )
    unsubscribe = provider.mass.subscribe.return_value
    unsubscribe.assert_not_called()

    await provider.unload()

    unsubscribe.assert_called_once_with()


async def test_a_provider_that_failed_to_load_unloads_without_a_bridge_manager() -> None:
    """An unload after a failed setup finds no bridge manager."""
    provider = SqueezelitePlayerProvider.__new__(SqueezelitePlayerProvider)
    provider.mass = MagicMock()

    await provider.unload()

    provider.mass.streams.unregister_dynamic_route.assert_any_call(BRIDGE_STREAM_PATH)


async def test_a_bridge_stream_is_served_by_the_bridge_of_its_player() -> None:
    """The request names its player; the bridge of that player decides about the session."""
    provider = MagicMock()
    response = MagicMock()
    bridge = MagicMock(serve_stream=AsyncMock(return_value=response))
    provider.bridge_manager.get_bridge.side_effect = {"p1": bridge}.get
    request = MagicMock(query={"player_id": "p1", "sid": "abc"})

    assert await SqueezelitePlayerProvider._serve_bridge_stream(provider, request) is response
    bridge.serve_stream.assert_awaited_once_with(request)

    for query in ({"player_id": "other", "sid": "abc"}, {"sid": "abc"}):
        with pytest.raises(web.HTTPNotFound):
            await SqueezelitePlayerProvider._serve_bridge_stream(provider, MagicMock(query=query))


@pytest.mark.parametrize("bridged", [True, False])
def test_an_event_of_a_player_goes_to_its_bridge_first(bridged: bool) -> None:
    """What the bridge does with an event decides what is left of it for the player."""
    provider = MagicMock()
    provider.mass.closing = False
    player = MagicMock(sendspin_bridge=MagicMock() if bridged else None)
    provider.get_protocol.return_value = player
    steps = MagicMock()
    steps.attach_mock(player.handle_slim_event, "player")
    if bridged:
        steps.attach_mock(player.sendspin_bridge.handle_slim_event, "bridge")
    event = SlimEvent(type=SlimEventType.PLAYER_BUFFER_READY, player_id="p1")

    SqueezelitePlayerProvider._handle_slimproto_event(provider, event)

    expected = [call.bridge(event), call.player(event)] if bridged else [call.player(event)]
    assert steps.mock_calls == expected


@pytest.mark.parametrize(
    ("raw", "expected_cmd"),
    [(["play"], "cmd_play"), (["pause", "1"], "cmd_pause"), (["pause"], "cmd_play_pause")],
)
async def test_cli_commands_of_a_bridged_client_go_to_its_sendspin_group(
    raw: list[str], expected_cmd: str
) -> None:
    """A client that plays for its Sendspin bridge controls the player that leads its group."""
    provider = _cli_provider()
    provider.get_protocol("p1").protocol_id = "p1"
    provider.bridge_manager.get_transport_command_target.return_value = "living_room"

    await SqueezelitePlayerProvider._handle_cli_command(provider, _cli_command("p1", raw))

    provider.bridge_manager.get_transport_command_target.assert_called_once_with("p1")
    assert _awaited_player_commands(provider) == [expected_cmd]
    getattr(provider.mass.players, expected_cmd).assert_awaited_once_with("living_room")


async def test_cli_stop_of_a_protocol_device_takes_it_out_of_its_sendspin_stream() -> None:
    """A device that stops itself is not a group stop, and its bridge must not play on."""
    provider = _cli_provider(ProtocolRole.AUDIO)
    provider.get_protocol("p1").protocol_id = "p1"
    provider.bridge_manager.get_transport_command_target.return_value = "living_room"

    with pytest.raises(NotImplementedError):
        await SqueezelitePlayerProvider._handle_cli_command(provider, _cli_command("p1", ["stop"]))

    provider.bridge_manager.stop_streaming.assert_called_once_with("p1")
    assert _awaited_player_commands(provider) == []
