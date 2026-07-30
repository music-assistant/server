"""
Tests for progress being frozen before the push stream is torn down (support#5813).

Pausing a Sendspin group falls back to STOP, and group.stop() can only snapshot
the live position while the group still has an active stream. See the ordering
comment in SendspinPlayer.stop() for why cancelling the session first breaks it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.sendspin.player import SendspinPlayer


def _player_mock() -> MagicMock:
    """Create a mock with the real method under test bound to it."""
    mock = MagicMock()
    mock.playback_session.cancel = AsyncMock()
    mock.api.group.stop = AsyncMock()
    return mock


async def test_stop_freezes_progress_while_transport_is_still_live() -> None:
    """group.stop() must be awaited before the push stream is torn down."""
    mock = _player_mock()
    transport = SimpleNamespace(stream_active=True)
    stream_active_at_freeze: list[bool] = []

    async def group_stop() -> None:
        stream_active_at_freeze.append(transport.stream_active)
        transport.stream_active = False

    async def session_cancel(_reason: str) -> None:
        transport.stream_active = False

    mock.api.group.stop = group_stop
    mock.playback_session.cancel = session_cancel

    await SendspinPlayer.stop(mock)

    assert stream_active_at_freeze == [True]


async def test_stop_still_cancels_the_playback_session() -> None:
    """Freezing first must not drop the session teardown."""
    mock = _player_mock()

    await SendspinPlayer.stop(mock)

    mock.playback_session.cancel.assert_awaited_once_with("stop command")
    mock.api.group.stop.assert_awaited_once()


async def test_session_is_cancelled_even_if_the_group_stop_fails() -> None:
    """A failing group stop must not strand the playback task and its pipelines."""
    mock = _player_mock()
    mock.api.group.stop = AsyncMock(side_effect=RuntimeError("transport gone"))

    with pytest.raises(RuntimeError):
        await SendspinPlayer.stop(mock)

    mock.playback_session.cancel.assert_awaited_once_with("stop command")
