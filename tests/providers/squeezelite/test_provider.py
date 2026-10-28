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
