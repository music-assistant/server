"""Tests for the squeezelite player provider."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioslimproto.cli import SlimCLICommand, parse_args
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import SlimEvent
from music_assistant_models.enums import ContentType, PlayerType
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.media_items import AudioFormat

from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.squeezelite.player import SqueezelitePlayer
from music_assistant.providers.squeezelite.provider import SqueezelitePlayerProvider

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
    provider.mass.players.get_player.side_effect = lambda pid: (
        sync_parent if pid == "leader" else child
    )
    provider.mass.streams.audio.get_output_format = AsyncMock(return_value=output_format)
    provider.mass.streams.audio.get_player_output_plan.return_value = MagicMock(filter_params=[])
    request = MagicMock(method="GET")
    request.query = {"player_id": "leader", "fmt": fmt, "child_player_id": "child"}
    resp = MagicMock(prepare=AsyncMock(), write=AsyncMock())

    with patch("aiohttp.web.StreamResponse", return_value=resp) as stream_response:
        await SqueezelitePlayerProvider._serve_multi_client_stream(provider, request)

    assert stream_response.call_args.kwargs["headers"]["Content-Type"] == expected_content_type


def _synced_member() -> tuple[MagicMock, SqueezelitePlayer, MagicMock]:
    """Return a mocked provider whose registered player 'member' is synced to 'leader'."""
    leader = SqueezelitePlayer.__new__(SqueezelitePlayer)
    leader._player_id = "leader"
    leader._attr_group_members = ["leader", "member"]
    update_state = MagicMock()
    leader.update_state = update_state  # type: ignore[misc,method-assign]
    member = MagicMock(player_id="member", synced_to="leader")
    provider = MagicMock()
    provider.mass.closing = False
    provider.mass.players.get_player.side_effect = {"leader": leader, "member": member}.get
    provider._release_from_sync_leader = partial(
        SqueezelitePlayerProvider._release_from_sync_leader, provider
    )
    return provider, leader, update_state


def test_disconnected_member_leaves_its_sync_leader() -> None:
    """A synced player that disconnects is dropped from its leader, so it returns ungrouped."""
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
    provider.mass.players.get_player.side_effect = {"p1": MagicMock(display_name="Kitchen")}.get

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


def _cli_provider(player_type: PlayerType = PlayerType.PLAYER) -> MagicMock:
    """Return a mocked provider whose only registered player is 'p1', of the given type."""
    provider = MagicMock()
    player = MagicMock(type=player_type, display_name="Kitchen")
    provider.mass.players.get_player.side_effect = {"p1": player}.get
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
    getattr(provider.mass.players, expected_cmd).assert_awaited_once_with("p1")


@pytest.mark.parametrize("raw", [["stop"], ["mode", "stop"]])
async def test_cli_stop_of_a_protocol_device_only_stops_that_device(raw: list[str]) -> None:
    """A protocol-only device also sends stop when another source takes over, so it stops only itself."""
    provider = _cli_provider(PlayerType.PROTOCOL)

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
    member = provider.mass.players.get_player("member")
    member.type = PlayerType.PROTOCOL
    member.protocol_parent_id = "device"
    # the device's own player holds the active group: a leader that is a protocol
    # player itself never reports one
    device = MagicMock(state=MagicMock(active_group=active_group))
    players = {"leader": leader, "member": member, "device": device}
    provider.mass.players.get_player.side_effect = players.get
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
