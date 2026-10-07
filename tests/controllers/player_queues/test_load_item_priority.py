"""Tests for the throttler priority of the requests made while loading a queue item."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.helpers.throttle_retry import (
    RequestPriority,
    current_priority,
    request_priority,
)

QUEUE_ID = "queue-1"


def _queue_item() -> QueueItem:
    """Build a queue item holding a track."""
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id="track-1",
        name="track-1",
        duration=300,
        media_item=Track(
            item_id="track-1",
            provider="spotify--abc",
            name="track-1",
            duration=300,
            provider_mappings={
                ProviderMapping(
                    item_id="track-1",
                    provider_domain="spotify",
                    provider_instance="spotify--abc",
                )
            },
        ),
    )


def _controller(item: QueueItem, seen: list[RequestPriority]) -> PlayerQueuesController:
    """
    Build a bare controller whose queue holds the item.

    :param item: The item the queue holds, not in the library.
    :param seen: Collects the priority the stream details are requested with.
    """

    async def _get_stream_details(**_kwargs: Any) -> MagicMock:
        seen.append(current_priority())
        return MagicMock(duration=None)

    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.logger = MagicMock()
    controller._queue_data = {
        QUEUE_ID: PlayerQueueData(
            queue=PlayerQueue(
                queue_id=QUEUE_ID,
                active=True,
                display_name="Test queue",
                available=True,
                items=1,
            ),
            items=[item],
        )
    }
    mass = MagicMock()
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mass.music.get_item_by_uri = AsyncMock(return_value=item.media_item)
    mass.streams.audio.get_stream_details = AsyncMock(side_effect=_get_stream_details)
    controller.mass = mass
    return controller


async def test_load_item_requests_with_playback_priority() -> None:
    """Loading an item makes its requests with playback priority, the caller keeps its own."""
    item = _queue_item()
    seen: list[RequestPriority] = []
    controller = _controller(item, seen)

    with request_priority(RequestPriority.NORMAL):
        await controller._load_item(item)
        assert current_priority() is RequestPriority.NORMAL

    assert seen == [RequestPriority.HIGH]
