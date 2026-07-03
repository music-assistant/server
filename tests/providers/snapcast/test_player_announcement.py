"""Tests for Snapcast announcement timing (issue #4377)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.snapcast.player import SnapCastPlayer

if TYPE_CHECKING:
    from music_assistant.providers.snapcast.snap_cntrl_proto import SnapclientProto


def _make_player(
    events: list[str],
    *,
    use_builtin_server: bool,
    buffer_size: int = 1000,
    volume_level: int = 25,
) -> SnapCastPlayer:
    """Build a SnapCastPlayer with a mocked provider that records call order in events."""
    player = SnapCastPlayer.__new__(SnapCastPlayer)
    player._player_id = "player-1"
    player._attr_volume_level = volume_level

    async def set_volume(level: int) -> None:
        events.append(f"volume_set:{level}")
        player._attr_volume_level = level

    # group.name == player_id -> player is its own group leader (not synced)
    snap_group = SimpleNamespace(name="player-1", stream="default", clients=["client-1"])
    player.snap_client = cast(
        "SnapclientProto",
        SimpleNamespace(group=snap_group, set_volume=set_volume, identifier="client-1"),
    )

    ma_stream = MagicMock()
    ma_stream.stream_id = "announce-1"
    ma_stream.start_stream = AsyncMock(side_effect=lambda: events.append("start_stream"))
    ma_stream.wait_for_stopped = AsyncMock(side_effect=lambda: events.append("wait_for_stopped"))

    player_group = MagicMock()
    player_group.set_stream = AsyncMock(
        side_effect=lambda stream_id: events.append(f"set_stream:{stream_id}")
    )

    provider = MagicMock()
    provider._use_builtin_server = use_builtin_server
    provider._snapcast_server_buffer_size = buffer_size
    provider.get_snapcast_media_stream = AsyncMock(return_value=ma_stream)
    provider.ensure_player_owned_group = AsyncMock(return_value=player_group)
    player._provider = provider
    return player


def _make_announcement() -> PlayerMedia:
    return PlayerMedia(uri="http://localhost/announce.mp3", media_type=MediaType.ANNOUNCEMENT)


async def _run_announcement(player: SnapCastPlayer, events: list[str], **kwargs: int) -> None:
    async def fake_sleep(delay: float) -> None:
        events.append(f"sleep:{delay}")

    with patch("music_assistant.providers.snapcast.player.asyncio.sleep", side_effect=fake_sleep):
        await player.play_announcement(_make_announcement(), **kwargs)


@pytest.mark.parametrize("use_builtin_server", [True, False])
async def test_volume_restore_waits_for_client_buffer(use_builtin_server: bool) -> None:
    """
    Volume restore and stream switch-back must wait for the client buffer to play out.

    Snapclients play buffer_ms behind the server; restoring the volume as soon
    as the server finished receiving the announcement reverts it ~buffer_ms
    before the audio finishes on the speakers (issue #4377).
    """
    events: list[str] = []
    player = _make_player(events, use_builtin_server=use_builtin_server, buffer_size=1000)
    await _run_announcement(player, events, volume_level=80)

    assert events == [
        "set_stream:announce-1",
        "sleep:1.0",
        "volume_set:80",
        "start_stream",
        "wait_for_stopped",
        "sleep:1.0",
        "volume_set:25",
        "set_stream:default",
    ]


async def test_switch_back_waits_for_client_buffer_without_volume() -> None:
    """The buffered tail must also play out before switching streams back (clipped endings)."""
    events: list[str] = []
    player = _make_player(events, use_builtin_server=False, buffer_size=500)
    await _run_announcement(player, events)

    assert events == [
        "set_stream:announce-1",
        "sleep:0.5",
        "start_stream",
        "wait_for_stopped",
        "sleep:0.5",
        "set_stream:default",
    ]
