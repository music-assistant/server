"""Tests for a pause and a resume of the AirPlay client of a receiver."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.airplay_receiver import AirPlayReceiverProvider
from music_assistant.providers.airplay_receiver import provider as provider_module

PLAYER_ID = "player1"
SOURCE_ID = "receiver1"


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Create a mock provider with the real pause handling bound to it, streaming to a player."""
    monkeypatch.setattr(provider_module, "FLUSH_PAUSE_DELAY", 0.05)
    mock = MagicMock()
    mock.instance_id = "airplay_receiver"
    mock.mass.players.pause_audio_source = AsyncMock()
    mock.mass.player_queues.play_media = AsyncMock()
    daemon = MagicMock()
    daemon.player_id = SOURCE_ID
    daemon.in_use_by_player = PLAYER_ID
    daemon.active_player_id = PLAYER_ID
    daemon.pending_stop_task = None
    daemon.pending_start_task = None
    daemon.pending_pause_task = None
    daemon.paused_player_id = None
    daemon.audio_source.uri = "airplay_receiver://audio_source/receiver1"
    mock.daemon = daemon

    def _create_task(coro: Any) -> asyncio.Task[Any]:
        return asyncio.get_event_loop().create_task(coro)

    mock.mass.create_task.side_effect = _create_task
    for name in ("_pause_after_flush", "_cancel_pending_pause", "_start_playback"):
        method = getattr(AirPlayReceiverProvider, name)
        getattr(mock, name).side_effect = lambda *args, _method=method: _method(mock, *args)

    def _clear_active_player(cleared: MagicMock) -> None:
        cleared.active_player_id = None
        cleared.in_use_by_player = None

    mock._clear_active_player.side_effect = _clear_active_player
    mock._write_silence_to_unblock_stream = AsyncMock()
    return mock


def _handle(mock: MagicMock, play_state: str) -> None:
    AirPlayReceiverProvider._handle_play_state_change(mock, mock.daemon, play_state)


async def _paused(mock: MagicMock) -> None:
    """Let the AirPlay client pause, until the player is paused and its stream ended."""
    _handle(mock, "flushed")
    await asyncio.sleep(0.1)
    mock.mass.players.pause_audio_source.assert_awaited_once_with(
        PLAYER_ID, SOURCE_ID, "airplay_receiver"
    )
    # the held player ends the stream, which releases the claim of the player
    mock.daemon.in_use_by_player = None


async def test_a_pause_of_the_client_pauses_the_player(provider: MagicMock) -> None:
    """A flush that is not followed by a resume shows the player as paused."""
    await _paused(provider)

    assert provider.daemon.paused_player_id == PLAYER_ID
    # the stream of the silenced player ends instead of waiting for audio
    provider._write_silence_to_unblock_stream.assert_awaited_once_with(provider.daemon)


async def test_a_seek_of_the_client_does_not_pause_the_player(provider: MagicMock) -> None:
    """A flush that is followed by a resume at once, as on a seek, changes nothing."""
    _handle(provider, "flushed")
    _handle(provider, "resumed")
    await asyncio.sleep(0.1)

    provider.mass.players.pause_audio_source.assert_not_awaited()
    provider.mass.player_queues.play_media.assert_not_awaited()


async def test_a_resume_of_the_client_plays_on_the_paused_player(provider: MagicMock) -> None:
    """A resume after a pause plays the source on the player again."""
    await _paused(provider)

    _handle(provider, "resumed")
    await asyncio.sleep(0)

    provider.mass.player_queues.play_media.assert_awaited_once_with(
        PLAYER_ID, "airplay_receiver://audio_source/receiver1"
    )
    assert provider.daemon.paused_player_id is None


async def test_a_resume_at_the_start_of_a_session_starts_nothing(provider: MagicMock) -> None:
    """The client resumes once as it starts to stream, which is no resume of a pause."""
    _handle(provider, "resumed")
    await asyncio.sleep(0)

    provider.mass.player_queues.play_media.assert_not_awaited()


async def test_a_session_that_ends_while_paused_lets_the_player_go(
    provider: MagicMock,
) -> None:
    """A client that leaves during the pause releases the source, and a later resume is none."""
    await _paused(provider)

    _handle(provider, "stopped")
    provider._clear_active_player.assert_called_once_with(provider.daemon)
    _handle(provider, "resumed")
    await asyncio.sleep(0)

    provider.mass.player_queues.play_media.assert_not_awaited()


async def test_a_flush_without_a_player_streaming_does_nothing(provider: MagicMock) -> None:
    """A client that flushes while no player streams the source pauses no player."""
    provider.daemon.in_use_by_player = None

    _handle(provider, "flushed")
    await asyncio.sleep(0.1)

    provider.mass.players.pause_audio_source.assert_not_awaited()
