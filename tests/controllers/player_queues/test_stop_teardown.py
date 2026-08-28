"""
Tests for what a queue stop tears down.

Stopping only the device leaves the queue's session open, so its item buffers keep
producing and a provider serving a live session stays tethered to Music Assistant.
That teardown has to happen even when the device could not be told to stop at all.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue


def _fake_controller() -> MagicMock:
    """Build a MagicMock standing in for the controller, owning one playing queue."""
    queue = MagicMock(
        queue_id="q",
        active=True,
        state=PlaybackState.PLAYING,
        corrected_elapsed_time=42.0,
    )
    fake = MagicMock()
    data = PlayerQueueData(queue=cast("PlayerQueue", queue))
    data.session_id = "sess-1"
    fake._queue_data = {"q": data}
    fake.get = MagicMock(
        side_effect=lambda qid: d.queue if (d := fake._queue_data.get(qid)) else None
    )
    fake.mass.players._handle_cmd_stop = AsyncMock()

    def _close_coro(target: Any, **_kwargs: Any) -> None:
        # the cleanup is handed to create_task as a coroutine; nothing awaits it here
        if hasattr(target, "close"):
            target.close()

    fake.mass.create_task = MagicMock(side_effect=_close_coro)

    @contextlib.asynccontextmanager
    async def _no_lock(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        """Stand in for the playback lock the stop is wrapped in."""
        yield

    fake.mass.players.get_player_lock = _no_lock
    # the play-action wrapper flags the queue while the stop runs
    queue.extra_attributes = {}
    return fake


async def _stop(fake: MagicMock) -> None:
    """Run a stop against the fake controller."""
    await PlayerQueuesController._handle_stop(cast("PlayerQueuesController", fake), "q")


@pytest.mark.asyncio
async def test_stop_ends_the_session_and_clears_the_buffers() -> None:
    """A stop closes the playback session and hands the audio data to the cleanup."""
    fake = _fake_controller()

    await _stop(fake)

    fake.mass.players._handle_cmd_stop.assert_awaited_once_with("q")
    assert fake._queue_data["q"].session_id is None
    fake.mass.streams.audio_processing.clear.assert_called_once_with("q", "sess-1")
    fake._cleanup_queue_audio_data.assert_called_once_with("q", "sess-1")


@pytest.mark.asyncio
async def test_a_player_that_cannot_be_stopped_still_loses_its_session() -> None:
    """
    A device gone unavailable must not keep the queue tethered to its provider.

    Its power was switched off outside MA and it dropped off the network before the
    stop arrived - exactly the case where the session has to be released.
    """
    fake = _fake_controller()
    fake.mass.players._handle_cmd_stop.side_effect = PlayerUnavailableError("gone")

    with pytest.raises(PlayerUnavailableError):
        await _stop(fake)

    assert fake._queue_data["q"].session_id is None
    fake.mass.streams.audio_processing.clear.assert_called_once_with("q", "sess-1")
    fake._cleanup_queue_audio_data.assert_called_once_with("q", "sess-1")


@pytest.mark.asyncio
async def test_a_stop_with_no_session_of_its_own_tears_nothing_down() -> None:
    """
    A stop on a queue that was not playing owns none of the audio it finds.

    The device can still hang for the thirty seconds the playback lock waits, and playback
    that starts in that window must not be taken down by a stop that stopped nothing.
    """
    fake = _fake_controller()
    fake._queue_data["q"].session_id = None

    async def _start_a_session(_queue_id: str) -> None:
        fake._queue_data["q"].session_id = "sess-2"

    fake.mass.players._handle_cmd_stop.side_effect = _start_a_session

    await _stop(fake)

    assert fake._queue_data["q"].session_id == "sess-2"
    fake.mass.streams.audio_processing.clear.assert_not_called()
    fake._cleanup_queue_audio_data.assert_not_called()


@pytest.mark.asyncio
async def test_a_stop_that_lost_the_race_to_a_new_session_leaves_it_alone() -> None:
    """Playback that restarted while the stop ran keeps its own session."""
    fake = _fake_controller()

    async def _restart_the_session(_queue_id: str) -> None:
        fake._queue_data["q"].session_id = "sess-2"

    fake.mass.players._handle_cmd_stop.side_effect = _restart_the_session

    await _stop(fake)

    assert fake._queue_data["q"].session_id == "sess-2"
    # the cleanup is handed the stopped session, so it tears down that session's audio
    # without touching what the replacement already prepared
    fake._cleanup_queue_audio_data.assert_called_once_with("q", "sess-1")
