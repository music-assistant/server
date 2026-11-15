"""Tests for the final queue state published when a player leaves the registry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import EventType, PlaybackState
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"


@pytest.mark.parametrize("permanent", [False, True])
def test_removed_queue_is_published_idle(permanent: bool) -> None:
    """Clients get a last QUEUE_UPDATED so a removed playing queue does not stay playing."""
    queue = PlayerQueue(
        queue_id=QUEUE_ID,
        active=True,
        display_name="Kitchen",
        available=True,
        items=0,
        state=PlaybackState.PLAYING,
    )
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=queue)}
    ctrl._managed_pool = MagicMock()
    ctrl.mass = MagicMock()

    ctrl.on_player_remove(QUEUE_ID, permanent=permanent)

    ctrl.mass.signal_event.assert_called_once_with(
        EventType.QUEUE_UPDATED, object_id=QUEUE_ID, data=queue
    )
    assert queue.state == PlaybackState.IDLE
    assert QUEUE_ID not in ctrl._queue_data
