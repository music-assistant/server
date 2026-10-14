"""Tests for fetching the full track details while loading a queue item."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import (
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "queue-1"


def _queue_item() -> QueueItem:
    """Build a queue item holding a track without an image."""
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


def _controller(item: QueueItem, fetch_error: Exception) -> PlayerQueuesController:
    """
    Build a bare controller whose queue holds the item and whose full item fetch fails.

    :param item: The item the queue holds, not in the library.
    :param fetch_error: The error fetching the full item raises.
    """
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
    mass.music.get_item_by_uri = AsyncMock(side_effect=fetch_error)
    mass.streams.audio.get_stream_details = AsyncMock(return_value=MagicMock(duration=None))
    controller.mass = mass
    return controller


@pytest.mark.parametrize(
    "fetch_error",
    [RetriesExhausted("rate limited"), ResourceTemporarilyUnavailable("unavailable")],
)
async def test_temporary_fetch_failure_plays_the_item_as_listed(fetch_error: Exception) -> None:
    """A provider that is temporarily unavailable does not stop the item from playing."""
    item = _queue_item()
    original_track = item.media_item
    controller = _controller(item, fetch_error)

    await controller._load_item(item)

    assert item.media_item is original_track
    get_stream_details = cast("AsyncMock", controller.mass.streams.audio.get_stream_details)
    get_stream_details.assert_awaited_once()


async def test_unavailable_item_still_fails_to_load() -> None:
    """An item the provider no longer has still fails to load."""
    item = _queue_item()
    controller = _controller(item, MediaNotFoundError("gone"))

    with pytest.raises(MediaNotFoundError):
        await controller._load_item(item)
    get_stream_details = cast("AsyncMock", controller.mass.streams.audio.get_stream_details)
    get_stream_details.assert_not_awaited()
