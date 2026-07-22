"""Regression tests for delayed next-track enqueueing in the player queue stream feeder."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


async def test_enqueue_next_item_waits_for_playing_player_update() -> None:
    """The queued next item is enqueued after the player update reports PLAYING."""
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.logger = MagicMock()

    wait_entered = asyncio.Event()
    release_wait = asyncio.Event()

    @asynccontextmanager
    async def wait_for_player_update(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
        wait_entered.set()
        await release_wait.wait()
        yield

    player_state = SimpleNamespace(
        playback_state=PlaybackState.IDLE,
        active_source="q1",
    )
    player = SimpleNamespace(state=player_state)

    mass = MagicMock()
    mass.players = MagicMock()
    mass.players.wait_for_player_update = MagicMock(side_effect=wait_for_player_update)
    mass.players.get_player = MagicMock(return_value=player)
    mass.players.enqueue_next_media = AsyncMock()
    controller.mass = mass

    current_item = _make_queue_item("q1", "current")
    next_item = _make_queue_item("q1", "next")
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=2,
        state=PlaybackState.IDLE,
        current_index=0,
        index_in_buffer=0,
        current_item=current_item,
    )
    controller._queue_data = {
        "q1": PlayerQueueData(
            queue=queue,
            items=[current_item, next_item],
            session_id="session-1",
        )
    }

    controller._enqueue_next_item("q1", next_item)
    enqueue_callback = mass.call_later.call_args.args[1]
    enqueue_task = asyncio.create_task(enqueue_callback(next_item))
    await asyncio.sleep(0)

    assert wait_entered.is_set()
    mass.players.enqueue_next_media.assert_not_awaited()

    player_state.playback_state = PlaybackState.PLAYING
    release_wait.set()
    await enqueue_task

    mass.players.wait_for_player_update.assert_called_once_with(
        "q1",
        attribute_name="playback_state",
        attribute_value=PlaybackState.PLAYING,
    )
    mass.players.enqueue_next_media.assert_awaited_once()
    assert mass.players.enqueue_next_media.await_args.kwargs["player_id"] == "q1"
    assert mass.players.enqueue_next_media.await_args.kwargs["media"].queue_item_id == "next"
    assert controller._queue_data["q1"].next_item_id_enqueued == "next"


class _BlockingWaitForPlayerUpdate:
    """Async context manager that blocks until the test releases it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.released = asyncio.Event()

    async def __aenter__(self) -> None:
        self.entered.set()
        await self.released.wait()

    async def __aexit__(self, *args: object) -> bool:
        return False


def _make_queue_item(queue_id: str, item_id: str) -> QueueItem:
    """Build a minimal playable queue item."""
    return QueueItem(
        queue_id=queue_id,
        queue_item_id=item_id,
        name=item_id,
        duration=60,
    )
