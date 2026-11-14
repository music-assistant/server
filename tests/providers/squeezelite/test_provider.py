"""Tests for the squeezelite player provider."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import SlimEvent
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.squeezelite.player import SqueezelitePlayer
from music_assistant.providers.squeezelite.provider import SqueezelitePlayerProvider

FLOW_FORMAT = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=192000, bit_depth=32)


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
