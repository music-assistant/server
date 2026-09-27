"""Tests that a queue advancing on its own resumes an audiobook at its saved position."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import RepeatMode
from music_assistant_models.media_items import Audiobook, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "queue-1"


def _mappings(item_id: str) -> set[ProviderMapping]:
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain="filesystem_local",
            provider_instance="filesystem_local--1",
        )
    }


def _book(item_id: str, resume_position_ms: int = 0) -> QueueItem:
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=item_id,
        name=item_id,
        duration=120,
        media_item=Audiobook(
            item_id=item_id,
            provider="filesystem_local--1",
            name=item_id,
            duration=120,
            resume_position_ms=resume_position_ms,
            provider_mappings=_mappings(item_id),
        ),
    )


def _track(item_id: str) -> QueueItem:
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=item_id,
        name=item_id,
        duration=120,
        media_item=Track(
            item_id=item_id,
            provider="filesystem_local--1",
            name=item_id,
            duration=120,
            provider_mappings=_mappings(item_id),
        ),
    )


def _controller(
    items: list[QueueItem], repeat_mode: RepeatMode = RepeatMode.OFF
) -> tuple[PlayerQueuesController, AsyncMock]:
    """Build a bare controller holding the given items, returning its stream details mock."""
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.logger = MagicMock()
    controller._queue_data = {
        QUEUE_ID: PlayerQueueData(
            queue=PlayerQueue(
                queue_id=QUEUE_ID,
                active=True,
                display_name="Test queue",
                available=True,
                items=len(items),
                repeat_mode=repeat_mode,
            ),
            items=items,
        )
    }
    mass = MagicMock()
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mass.music.get_item_by_uri = AsyncMock(
        side_effect=lambda uri: next(i.media_item for i in items if i.uri == uri)
    )
    get_stream_details = AsyncMock(return_value=MagicMock(duration=None))
    mass.streams.audio.get_stream_details = get_stream_details
    controller.mass = mass
    return controller, get_stream_details


async def test_next_audiobook_starts_at_its_resume_point() -> None:
    """The next audiobook starts where it was left off, not at 0:00."""
    items = [_book("book-a"), _book("book-b", resume_position_ms=60000)]
    controller, get_stream_details = _controller(items)

    await controller.load_next_queue_item(QUEUE_ID, "book-a")

    assert get_stream_details.call_args.kwargs["seek_position"] == 59


async def test_repeated_audiobook_starts_from_the_beginning() -> None:
    """Repeat all wraps around to the start of the first audiobook, not its old bookmark."""
    items = [_book("book-a", resume_position_ms=60000), _book("book-b")]
    controller, get_stream_details = _controller(items, repeat_mode=RepeatMode.ALL)

    await controller.load_next_queue_item(QUEUE_ID, "book-b")

    assert get_stream_details.call_args.kwargs["seek_position"] == 0


async def test_next_track_starts_from_the_beginning() -> None:
    """A music track that follows an audiobook plays from its start."""
    items = [_book("book-a", resume_position_ms=60000), _track("track-b")]
    controller, get_stream_details = _controller(items)

    await controller.load_next_queue_item(QUEUE_ID, "book-a")

    assert get_stream_details.call_args.kwargs["seek_position"] == 0
