"""Tests for the album-loudness decision taken while loading a queue item."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType, RepeatMode
from music_assistant_models.media_items import Album, ItemMapping, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "queue-1"

PROVIDER_ALBUM = ItemMapping(
    media_type=MediaType.ALBUM,
    item_id="album-prov-1",
    provider="spotify--abc",
    name="Kind of Blue",
)
OTHER_PROVIDER_ALBUM = ItemMapping(
    media_type=MediaType.ALBUM,
    item_id="album-prov-2",
    provider="spotify--abc",
    name="Sketches of Spain",
)
LIBRARY_ALBUM = Album(
    item_id="7",
    provider="library",
    name="Kind of Blue",
    provider_mappings={
        ProviderMapping(
            item_id="album-prov-1",
            provider_domain="spotify",
            provider_instance="spotify--abc",
        )
    },
)


def _queue_item(item_id: str, album: Album | ItemMapping) -> QueueItem:
    """Build a queue item holding a track on the given album."""
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=item_id,
        name=item_id,
        duration=300,
        media_item=Track(
            item_id=item_id,
            provider="spotify--abc",
            name=item_id,
            duration=300,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain="spotify",
                    provider_instance="spotify--abc",
                )
            },
            album=album,
        ),
    )


def _controller(items: list[QueueItem]) -> PlayerQueuesController:
    """Build a bare controller whose queue holds the given items."""
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
            ),
            items=items,
        )
    }
    tracks_by_uri = {item.uri: item.media_item for item in items}
    mass = MagicMock()
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mass.music.get_item_by_uri = AsyncMock(side_effect=lambda uri: tracks_by_uri[uri])
    mass.streams.audio.get_stream_details = AsyncMock(return_value=MagicMock(duration=None))
    controller.mass = mass
    return controller


async def _prefer_album_loudness(
    items: list[QueueItem], current_index: int, repeat_mode: RepeatMode = RepeatMode.OFF
) -> bool:
    """Load the item after the given index the way a player asks for it, and read the decision."""
    controller = _controller(items)
    controller._queue_data[QUEUE_ID].queue.repeat_mode = repeat_mode
    await controller.load_next_queue_item(QUEUE_ID, items[current_index].queue_item_id)
    get_stream_details = cast("AsyncMock", controller.mass.streams.audio.get_stream_details)
    return cast("bool", get_stream_details.call_args.kwargs["prefer_album_loudness"])


async def test_standalone_track_uses_track_loudness() -> None:
    """A track surrounded by other albums is normalized on its own loudness."""
    items = [
        _queue_item("track-1", OTHER_PROVIDER_ALBUM),
        _queue_item("track-2", PROVIDER_ALBUM),
    ]
    assert not await _prefer_album_loudness(items, current_index=0)


async def test_second_track_of_an_album_uses_album_loudness() -> None:
    """The track right after the first one of an album is still part of that album."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM),
        _queue_item("track-2", PROVIDER_ALBUM),
    ]
    assert await _prefer_album_loudness(items, current_index=0)


async def test_library_album_matches_provider_album_of_previous_item() -> None:
    """
    A loaded previous item carries the library album while the item being loaded has the provider one.

    Both describe the same album, so the album loudness applies.
    """
    items = [
        _queue_item("track-1", LIBRARY_ALBUM),
        _queue_item("track-2", PROVIDER_ALBUM),
    ]
    assert await _prefer_album_loudness(items, current_index=0)


async def test_library_album_matches_provider_album_of_next_item() -> None:
    """The same album seen from both representations is recognised on the next-item side too."""
    items = [
        _queue_item("track-1", OTHER_PROVIDER_ALBUM),
        _queue_item("track-2", PROVIDER_ALBUM),
        _queue_item("track-3", LIBRARY_ALBUM),
    ]
    assert await _prefer_album_loudness(items, current_index=0)


async def test_different_albums_use_track_loudness() -> None:
    """Neighbouring tracks from genuinely different albums do not form an album."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM),
        _queue_item("track-2", OTHER_PROVIDER_ALBUM),
        _queue_item("track-3", PROVIDER_ALBUM),
    ]
    assert not await _prefer_album_loudness(items, current_index=0)


async def test_first_item_of_the_queue_has_no_previous_item() -> None:
    """Repeating the queue wraps back to its first item, which nothing plays before."""
    items = [
        _queue_item("track-1", OTHER_PROVIDER_ALBUM),
        _queue_item("track-2", PROVIDER_ALBUM),
        _queue_item("track-3", OTHER_PROVIDER_ALBUM),
    ]
    assert not await _prefer_album_loudness(items, current_index=2, repeat_mode=RepeatMode.ALL)


async def test_track_on_repeat_single_is_not_an_album() -> None:
    """A track repeating on its own is not played as part of its album."""
    items = [_queue_item("track-1", PROVIDER_ALBUM)]
    assert not await _prefer_album_loudness(items, current_index=0, repeat_mode=RepeatMode.ONE)


async def test_repeat_single_ignores_the_album_around_it() -> None:
    """Repeating one track of an album does not play the rest of that album."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM),
        _queue_item("track-2", PROVIDER_ALBUM),
        _queue_item("track-3", PROVIDER_ALBUM),
    ]
    assert not await _prefer_album_loudness(items, current_index=1, repeat_mode=RepeatMode.ONE)
