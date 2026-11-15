"""Tests for the squeezelite player provider."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from aiohttp import web
from aiosendspin.clock import ManualClock, RawMonotonicClock
from aioslimproto.cli import SlimCLICommand, parse_args
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import SlimEvent
from music_assistant_models.enums import ContentType, ProtocolRole
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.media_items import AudioFormat

from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.squeezelite.player import SqueezelitePlayer
from music_assistant.providers.squeezelite.provider import SqueezelitePlayerProvider
from music_assistant.providers.squeezelite.sendspin_bridge import (
    BRIDGE_STREAM_PATH,
    SendspinBridgeManager,
)

FLOW_FORMAT = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=192000, bit_depth=32)
PLAYER_COMMANDS = ("cmd_play", "cmd_pause", "cmd_play_pause", "cmd_stop")


@pytest.mark.parametrize(
    ("fmt", "output_format", "expected_content_type"),
    [
        (
            "wav",
            AudioFormat(content_type=ContentType.WAV, sample_rate=192000, bit_depth=24),
            "audio/wav;rate=192000;bitrate=24;channels=2",
        ),
        (
            "pcm",
            AudioFormat(content_type=ContentType.PCM_S24LE, sample_rate=96000, bit_depth=24),
            "audio/pcm;codec=pcm;rate=96000;bitrate=24;channels=2",
        ),
    ],
)
async def test_multi_client_stream_content_type_describes_output_format(
    fmt: str, output_format: AudioFormat, expected_content_type: str
) -> None:
    """Squeezelite derives the PCM params from the Content-Type, so it must match the stream."""

    async def _stream(**_kwargs: Any) -> AsyncGenerator[bytes]:
        yield b"\x00" * 16

    stream = MagicMock(done=False, audio_format=FLOW_FORMAT, queue_id="q", session_id="s")
    stream.get_stream = _stream
    sync_parent = MagicMock(multi_client_stream=stream)
    child = MagicMock(display_name="Child")
    provider = MagicMock()
    # the sync leader and its child are both protocol objects of this provider
    provider.get_protocol = {"leader": sync_parent, "child": child}.get
    provider.mass.players.get_player.return_value = None
    provider.mass.streams.audio.get_output_format = AsyncMock(return_value=output_format)
    provider.mass.streams.audio.get_player_output_plan.return_value = MagicMock(filter_params=[])
    request = MagicMock(method="GET")
    request.query = {"player_id": "leader", "fmt": fmt, "child_player_id": "child"}
    resp = MagicMock(prepare=AsyncMock(), write=AsyncMock())

    with patch("aiohttp.web.StreamResponse", return_value=resp) as stream_response:
        await SqueezelitePlayerProvider._serve_multi_client_stream(provider, request)

    assert stream_response.call_args.kwargs["headers"]["Content-Type"] == expected_content_type


def _synced_member() -> tuple[MagicMock, SqueezelitePlayer, MagicMock]:
    """Return a mocked provider whose attached protocol 'member' is synced to 'leader'."""
    leader = SqueezelitePlayer.__new__(SqueezelitePlayer)
    leader._protocol_id = "leader"
    leader._attr_group_members = ["leader", "member"]
    update_state = MagicMock()
    leader.update_state = update_state  # type: ignore[misc,method-assign]
    member = MagicMock(protocol_id="member", synced_to="leader")
    provider = MagicMock()
    provider.mass.closing = False
    provider.get_protocol = MagicMock(side_effect={"leader": leader, "member": member}.get)
    provider._release_from_sync_leader = partial(
        SqueezelitePlayerProvider._release_from_sync_leader, provider
    )
    return provider, leader, update_state


def test_disconnected_member_leaves_its_sync_leader() -> None:
    """A synced player that disconnects is dropped from its leader and detached from its player."""
    provider, leader, update_state = _synced_member()
    event = SlimEvent(type=SlimEventType.PLAYER_DISCONNECTED, player_id="member")

    SqueezelitePlayerProvider._handle_slimproto_event(provider, event)

    assert leader.group_members == []
    update_state.assert_called_once()
    provider.mass.players.unregister.assert_called_once_with("member")


def test_reconnected_member_leaves_its_sync_leader() -> None:
    """A synced player that reconnects before its old socket closed is dropped from its leader."""
    provider, leader, _ = _synced_member()
    event = SlimEvent(type=SlimEventType.PLAYER_CONNECTED, player_id="member")

    with patch("music_assistant.providers.squeezelite.provider.SqueezelitePlayer"):
        SqueezelitePlayerProvider._handle_slimproto_event(provider, event)

    assert leader.group_members == []


def test_the_display_name_is_the_one_set_in_music_assistant() -> None:
    """The name for a client comes from Music Assistant's player, and is None for an unknown one."""
    provider = MagicMock()
    # the CLI names a client by its slimproto id, which is the id of its protocol
    player = MagicMock()
    player.name = "Kitchen"
    provider.mass.players.get_player_by_protocol.side_effect = {"p1": player}.get

    assert SqueezelitePlayerProvider._lookup_display_name(provider, "p1") == "Kitchen"
    assert SqueezelitePlayerProvider._lookup_display_name(provider, "other") is None


async def test_the_cli_reports_the_display_name_set_in_music_assistant() -> None:
    """Loading the provider hands the lookup to the CLI, which reports it to the clients."""
    provider = SqueezelitePlayerProvider.__new__(SqueezelitePlayerProvider)
    provider.slimproto = MagicMock(start=AsyncMock())
    provider.mass = MagicMock()

    with patch.object(PlayerProvider, "loaded_in_mass", new=AsyncMock()):
        await provider.loaded_in_mass()

    assert provider.slimproto.cli.display_name_lookup == provider._lookup_display_name


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
    ("active_group", "expected_members"),
    [
        (None, ["leader", "other"]),
        # a sync group player would add the member back right away
        ("syncgroup_1", ["leader", "member", "other"]),
    ],
)
async def test_cli_stop_of_a_synced_protocol_device_leaves_its_group(
    active_group: str | None, expected_members: list[str]
) -> None:
    """A synced protocol-only device that stops leaves its group, unless a sync group holds it."""
    provider, leader, update_state = _synced_member()
    leader._attr_group_members.append("other")
    member = provider.get_protocol("member")
    member.role = ProtocolRole.AUDIO
    # the player of the device reports the group that holds it, its squeezelite
    # protocol does not
    member.player = MagicMock(active_group=active_group)
    for cmd in PLAYER_COMMANDS:
        setattr(provider.mass.players, cmd, AsyncMock())

    # the device sends its stop twice
    for _ in range(2):
        with pytest.raises(NotImplementedError):
            await SqueezelitePlayerProvider._handle_cli_command(
                provider, _cli_command("member", ["stop"])
            )

    assert _awaited_player_commands(provider) == []
    assert leader.group_members == expected_members
    assert update_state.call_count == (0 if active_group else 1)


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
    provider.config = MagicMock()
    provider.logger = MagicMock()

    with (
        patch("music_assistant.providers.squeezelite.provider.logging"),
        patch("music_assistant.providers.squeezelite.provider.SlimServer") as slim_server,
        patch.object(provider, "_validate_all_ports", new=AsyncMock()),
    ):
        await provider.handle_async_init()

    # a clock of its own, of the kind Sendspin makes for itself
    assert provider.clock == RawMonotonicClock()
    assert isinstance(provider.bridge_manager, SendspinBridgeManager)
    server_time = slim_server.call_args.kwargs["clock"]
    provider.clock = ManualClock(now_us_value=5_000_000_250_000)
    assert server_time() == 5_000_000.25


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

    await provider.unload()

    bridge_manager.close.assert_awaited_once()
    provider.mass.streams.unregister_dynamic_route.assert_any_call(BRIDGE_STREAM_PATH)


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
