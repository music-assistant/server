"""Tests for the Snapcast player stopping its queue when the snapclient drops."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.snapcast.player import SnapCastPlayer

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.snapcast.provider import SnapCastProvider
    from music_assistant.providers.snapcast.snap_cntrl_proto import SnapclientProto


def _make_player(queue_active: bool) -> SnapCastPlayer:
    player = SnapCastPlayer.__new__(SnapCastPlayer)
    player._player_id = "kitchen"
    player._last_tracked_state = None
    player._snap_ma_stream = None
    player._announcement_clip_playing = False
    player._extra_data = {}
    player._attr_available = True
    player._is_alive = lambda: True  # type: ignore[method-assign]
    group = SimpleNamespace(stream="ma-stream", clients=[], name="group")
    player.snap_client = cast(
        "SnapclientProto",
        SimpleNamespace(
            group=group, friendly_name="Kitchen", volume=50, muted=False, connected=True
        ),
    )
    provider = MagicMock()
    provider._snapserver.stream.return_value = SimpleNamespace(status="playing")
    provider.get_snap_ma_stream.return_value = MagicMock(playback_started_at=1234.5)
    player._provider = cast("SnapCastProvider", provider)
    mass = MagicMock()
    mass.player_queues.get.return_value = SimpleNamespace(
        active=queue_active, state=PlaybackState.PLAYING
    )
    mass.player_queues.stop = AsyncMock()
    player.mass = cast("MusicAssistant", mass)
    return player


def _drop_client(player: SnapCastPlayer) -> None:
    player._is_alive = lambda: False  # type: ignore[method-assign]
    player.snap_client.connected = False  # type: ignore[misc]


@pytest.mark.asyncio
async def test_dropped_client_stops_its_playing_queue() -> None:
    """A playing player whose client drops stops its queue while it can still be stopped."""
    player = _make_player(queue_active=True)
    await player._process_snapcast_client_state()
    available_at_stop: list[bool] = []
    player.mass.player_queues.stop.side_effect = lambda _: available_at_stop.append(  # type: ignore[attr-defined]
        player._attr_available
    )

    _drop_client(player)
    await player._process_snapcast_client_state()

    player.mass.player_queues.stop.assert_awaited_once_with(player.player_id)  # type: ignore[attr-defined]
    assert available_at_stop == [True]
    assert player._attr_available is False


@pytest.mark.asyncio
async def test_dropped_group_member_leaves_queue_alone() -> None:
    """A dropped member has no active queue of its own, so the leader keeps playing."""
    player = _make_player(queue_active=False)
    await player._process_snapcast_client_state()

    _drop_client(player)
    await player._process_snapcast_client_state()

    player.mass.player_queues.stop.assert_not_awaited()  # type: ignore[attr-defined]
