"""Tests for the Snapcast player's flow-mode elapsed time anchor."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.providers.snapcast.player import SnapCastPlayer

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.snapcast.provider import SnapCastProvider
    from music_assistant.providers.snapcast.snap_cntrl_proto import SnapclientProto


def _make_player(ma_stream: MagicMock) -> SnapCastPlayer:
    player = SnapCastPlayer.__new__(SnapCastPlayer)
    player._last_tracked_state = None
    player._snap_ma_stream = None
    player._extra_data = {}
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
    provider.get_snap_ma_stream.return_value = ma_stream
    player._provider = cast("SnapCastProvider", provider)
    player.mass = cast("MusicAssistant", MagicMock())
    return player


@pytest.mark.asyncio
async def test_playback_anchor_becoming_known_triggers_update() -> None:
    """
    Regression: the anchor must be published once the stream's buffer delay has passed.

    Without it the elapsed time stays unanchored for the whole flow stream and the
    queue index never advances.
    """
    ma_stream = MagicMock(playback_started_at=None)
    player = _make_player(ma_stream)

    assert await player._process_snapcast_client_state() is True
    assert player._attr_elapsed_time_last_updated is None

    ma_stream.playback_started_at = 1234.5

    assert await player._process_snapcast_client_state() is True
    assert player._attr_elapsed_time == 0
    assert player._attr_elapsed_time_last_updated == 1234.5


@pytest.mark.asyncio
async def test_announcement_keeps_music_anchor() -> None:
    """The announcement stream's anchor must not replace the music stream's anchor."""
    music_stream = MagicMock(playback_started_at=1234.5)
    player = _make_player(music_stream)
    await player._process_snapcast_client_state()

    player.extra_data[ATTR_ANNOUNCEMENT_IN_PROGRESS] = True
    player.snap_provider.get_snap_ma_stream.return_value = MagicMock(  # type: ignore[attr-defined]
        playback_started_at=2000.0
    )
    await player._process_snapcast_client_state()

    assert player._attr_elapsed_time == 0
    assert player._attr_elapsed_time_last_updated == 1234.5
