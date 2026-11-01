"""
Tests that the idle-resume watcher outlasts the flow-stream lead-out.

The flow stream holds its connection open after the last byte for a lead-out of up
to FLOW_STREAM_LEAD_OUT_MAX_SECONDS. A player that only reports idle once that
connection closes must still have items appended during the hold resumed.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import PlaybackState

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.constants import FLOW_STREAM_LEAD_OUT_MAX_SECONDS

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from music_assistant_models.player_queue import PlayerQueue


def _run_watcher(idle_after_s: int, change_session_at_s: int | None = None) -> MagicMock:
    """Run the resume watcher on a fake clock; return the fake controller."""
    queue = MagicMock(queue_id="q", session_id="sess-1", active=True, state=PlaybackState.PLAYING)
    queue.current_index = 0
    queue.display_name = "Test"
    fake = MagicMock()
    data = PlayerQueueData(queue=cast("PlayerQueue", queue))
    data.session_id = "sess-1"
    fake._queue_data = {"q": data}
    fake.get = MagicMock(return_value=queue)
    # an item was appended after the flow buffer completed
    fake.get_next_item = MagicMock(return_value=MagicMock(queue_item_id="next"))
    fake.index_by_id = MagicMock(return_value=1)
    fake.play_index = AsyncMock()
    captured: list[Coroutine[Any, Any, None]] = []
    fake.mass.create_task = MagicMock(side_effect=lambda coro, **_kw: captured.append(coro))

    PlayerQueuesController.queue_buffer_completed(
        cast("PlayerQueuesController", fake), "q", queue_exhausted=True
    )
    assert len(captured) == 1

    clock = {"t": 0}

    async def _fake_sleep(seconds: float) -> None:
        clock["t"] += int(seconds)
        # the player reports idle only once the held connection is closed
        if clock["t"] >= idle_after_s:
            queue.state = PlaybackState.IDLE
        if change_session_at_s is not None and clock["t"] >= change_session_at_s:
            data.session_id = "sess-2"

    async def _drive() -> None:
        with patch(
            "music_assistant.controllers.player_queues.controller.asyncio.sleep",
            _fake_sleep,
        ):
            await captured[0]

    asyncio.run(_drive())
    return fake


def test_appended_item_resumes_after_short_drain() -> None:
    """Idle within the old 60 s window still resumes the appended item."""
    fake = _run_watcher(idle_after_s=20)
    fake.play_index.assert_awaited_once_with("q", 1)


def test_appended_item_resumes_after_drain_longer_than_60s() -> None:
    """A lead-out longer than 60 s must not outlast the watcher."""
    fake = _run_watcher(idle_after_s=60 + 30)
    fake.play_index.assert_awaited_once_with("q", 1)


def test_appended_item_resumes_after_max_lead_out() -> None:
    """Idle right after the longest possible lead-out still resumes."""
    fake = _run_watcher(idle_after_s=FLOW_STREAM_LEAD_OUT_MAX_SECONDS + 10)
    fake.play_index.assert_awaited_once_with("q", 1)


def test_watcher_stops_on_new_session_during_long_drain() -> None:
    """A new playback session during the hold ends the watcher without resuming."""
    fake = _run_watcher(idle_after_s=150, change_session_at_s=90)
    fake.play_index.assert_not_awaited()
