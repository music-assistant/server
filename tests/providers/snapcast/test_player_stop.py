"""Tests for stopping playback on a Snapcast player."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.snapcast.player import SnapCastPlayer


async def _stop(other_groups: list[SimpleNamespace]) -> tuple[SimpleNamespace, MagicMock]:
    """Stop a player whose group plays stream music-1; return its group and the stream."""
    ma_stream = MagicMock(stream_id="music-1")
    group = SimpleNamespace(stream="music-1")

    async def set_stream(stream_id: str) -> None:
        group.stream = stream_id

    group.set_stream = set_stream
    provider = MagicMock()
    provider.ensure_player_owned_group = AsyncMock(return_value=group)
    provider.get_snap_ma_stream = lambda name: ma_stream if name == "music-1" else None
    provider._snapserver.groups = [group, *other_groups]
    player = SnapCastPlayer.__new__(SnapCastPlayer)
    player._player_id = "player_1"
    player._provider = provider
    player.snap_client = SimpleNamespace(group=group)
    player.poke_player_update = MagicMock()  # type: ignore[method-assign]

    await player.stop()
    return group, ma_stream


async def test_stop_stops_the_stream_the_group_was_playing() -> None:
    """
    Regression: stop must stop the stream, not only move the group to the default stream.

    A stream left running keeps playing the old session, and a replay that reuses it
    plays that audio before the new one.
    """
    group, ma_stream = await _stop(other_groups=[])

    assert group.stream == "default"
    ma_stream.request_stop_stream.assert_called_once()


async def test_stop_keeps_a_stream_other_groups_still_play() -> None:
    """A stream shared with other groups, e.g. a universal group, keeps playing for them."""
    _, ma_stream = await _stop(other_groups=[SimpleNamespace(stream="music-1")])

    ma_stream.request_stop_stream.assert_not_called()
