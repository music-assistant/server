"""Tests for the stop→play race in AirPlay Receiver session handling."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.airplay_receiver import AirPlayReceiverProvider


@pytest.fixture
def provider() -> MagicMock:
    """Create a mock provider with the real play-state handling bound to it."""
    mock = MagicMock()
    mock.logger = MagicMock()
    mock._first_volume_event_received = False
    # queue_id == player_id in MA: the stop and the follow-up play target the same
    # player, which is the same-player reconnect scenario the race can actually break.
    mock._in_use_by_player = "player1"
    mock._active_player_id = "player1"
    mock._active_session_id = "session1"
    mock._pending_stop_task = None
    mock._get_target_player_id.return_value = "player1"
    mock._audio_source.uri = "airplay://source"
    mock._write_silence_to_unblock_stream = AsyncMock()

    def _clear_active_player() -> None:
        mock._active_player_id = None
        mock._in_use_by_player = None
        mock._active_session_id = None

    mock._clear_active_player.side_effect = _clear_active_player

    def _create_task(coro: Any) -> asyncio.Task[Any]:
        return asyncio.get_event_loop().create_task(coro)

    mock.mass.create_task.side_effect = _create_task

    def _start_playback(player_id: str) -> Any:
        return AirPlayReceiverProvider._start_playback(mock, player_id)

    mock._start_playback.side_effect = _start_playback
    return mock


def _handle(mock: MagicMock, play_state: str) -> None:
    AirPlayReceiverProvider._handle_play_state_change(mock, play_state)


async def test_play_waits_for_inflight_stop(provider: MagicMock) -> None:
    """A quick stop→play bounce must not let the late stop kill the new stream."""
    events: list[str] = []
    stop_release = asyncio.Event()

    async def slow_stop(_player_id: str) -> None:
        await stop_release.wait()
        events.append("stop_done")

    async def play_media(_player_id: str, _media: str) -> None:
        events.append("play")

    provider.mass.players.cmd_stop = slow_stop
    provider.mass.player_queues.play_media = play_media

    _handle(provider, "stopped")
    _handle(provider, "playing")

    for _ in range(5):
        await asyncio.sleep(0)
    assert events == []

    stop_release.set()
    await asyncio.sleep(0.01)
    assert events == ["stop_done", "play"]
    assert provider._pending_stop_task is None


async def test_play_without_pending_stop_starts_immediately(provider: MagicMock) -> None:
    """A plain session start plays right away."""
    provider._in_use_by_player = None
    provider.mass.player_queues.play_media = AsyncMock()

    _handle(provider, "playing")
    await asyncio.sleep(0.01)

    provider.mass.player_queues.play_media.assert_awaited_once_with("player1", "airplay://source")


async def test_play_proceeds_when_stop_fails(provider: MagicMock) -> None:
    """A failing stop must not block the new playback from starting."""
    provider.mass.players.cmd_stop = AsyncMock(side_effect=PlayerCommandFailed("boom"))
    provider.mass.player_queues.play_media = AsyncMock()

    _handle(provider, "stopped")
    _handle(provider, "playing")
    await asyncio.sleep(0.01)

    provider.mass.player_queues.play_media.assert_awaited_once_with("player1", "airplay://source")


async def test_play_proceeds_when_stop_fails_unexpectedly(provider: MagicMock) -> None:
    """An unexpected (non-PlayerCommandFailed) stop error must not abort the new session."""
    provider.mass.players.cmd_stop = AsyncMock(side_effect=RuntimeError("kaboom"))
    provider.mass.player_queues.play_media = AsyncMock()

    _handle(provider, "stopped")
    _handle(provider, "playing")
    await asyncio.sleep(0.01)

    provider.mass.player_queues.play_media.assert_awaited_once_with("player1", "airplay://source")
    assert provider._pending_stop_task is None


async def test_concurrent_starts_all_wait_for_pending_stop(provider: MagicMock) -> None:
    """Rapid duplicate starts must all await the pending stop, none racing past it."""
    events: list[str] = []
    stop_release = asyncio.Event()

    async def slow_stop() -> None:
        await stop_release.wait()
        events.append("stop_done")

    async def play_media(_player_id: str, _media: str) -> None:
        events.append("play")

    provider._pending_stop_task = asyncio.get_event_loop().create_task(slow_stop())
    provider.mass.player_queues.play_media = play_media

    # two starts before on_source_selected claims _in_use_by_player
    starts = [
        asyncio.ensure_future(AirPlayReceiverProvider._start_playback(provider, "player1"))
        for _ in range(2)
    ]
    for _ in range(5):
        await asyncio.sleep(0)
    # neither start may play until the pending stop has finished
    assert events == []

    stop_release.set()
    await asyncio.gather(*starts)

    assert events[0] == "stop_done"
    assert events.count("play") == 2
    assert provider._pending_stop_task is None
