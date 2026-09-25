"""Tests for queue resume while a fallback announcement is in progress."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "player_1"
ITEM_ID = "track-1"


def _controller(
    *,
    queue_state: PlaybackState,
    resume_pos: int,
    elapsed_time: float,
    announcement_in_progress: bool,
    duration: int = 200,
) -> tuple[PlayerQueuesController, PlayerQueue]:
    """Build a controller with one queue item and a configurable player announce flag."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Player 1", available=True, items=1
    )
    item = QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=ITEM_ID,
        name="Track",
        duration=duration,
    )
    queue.current_item = item
    queue.current_index = 0
    queue.state = queue_state
    queue.resume_pos = resume_pos
    queue.elapsed_time = elapsed_time
    # Far enough back that wall-clock corrected_elapsed_time would overshoot.
    queue.elapsed_time_last_updated = time.time() - 30
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = [item]
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.get_item = Mock(return_value=item)  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    player = MagicMock()
    player.state = MagicMock(playback_state=PlaybackState.IDLE)
    player.extra_data = {ATTR_ANNOUNCEMENT_IN_PROGRESS: announcement_in_progress}
    ctrl.mass.players.get_player = Mock(return_value=player)
    ctrl.logger = MagicMock()
    return ctrl, queue


async def test_resume_during_announcement_uses_parked_resume_pos() -> None:
    """Resume during announce uses parked resume_pos, not wall clock."""
    ctrl, queue = _controller(
        queue_state=PlaybackState.PLAYING,
        resume_pos=90,
        elapsed_time=90,
        announcement_in_progress=True,
    )
    assert queue.corrected_elapsed_time > 110

    await ctrl.resume(QUEUE_ID)

    ctrl.play_index.assert_awaited_once_with(QUEUE_ID, ITEM_ID, 90, False)  # type: ignore[attr-defined]


async def test_resume_while_playing_without_announcement_uses_live_clock() -> None:
    """Resume while playing without announce still uses the live clock."""
    ctrl, queue = _controller(
        queue_state=PlaybackState.PLAYING,
        resume_pos=90,
        elapsed_time=90,
        announcement_in_progress=False,
    )
    live_pos = int(queue.corrected_elapsed_time)
    assert live_pos > 110

    await ctrl.resume(QUEUE_ID)

    ctrl.play_index.assert_awaited_once_with(QUEUE_ID, ITEM_ID, live_pos, False)  # type: ignore[attr-defined]
