"""Tests for the album-loudness decision taken while loading a queue item."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType, RepeatMode
from music_assistant_models.media_items import (
    Album,
    ItemMapping,
    Playlist,
    ProviderMapping,
    Track,
)
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
OTHER_LIBRARY_ALBUM = Album(
    item_id="8",
    provider="library",
    name="Sketches of Spain",
    provider_mappings={
        ProviderMapping(
            item_id="album-prov-2",
            provider_domain="spotify",
            provider_instance="spotify--abc",
        )
    },
)
PLAYLIST = Playlist(
    item_id="playlist-1",
    provider="spotify--abc",
    name="Jazz essentials",
    provider_mappings={
        ProviderMapping(
            item_id="playlist-1",
            provider_domain="spotify",
            provider_instance="spotify--abc",
        )
    },
)


def _queue_item(item_id: str, album: Album | ItemMapping | None) -> QueueItem:
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


def _controller(
    items: list[QueueItem], enqueued: list[Album | Playlist | Track] | None = None
) -> PlayerQueuesController:
    """Build a bare controller whose queue holds the given items and enqueued parents."""
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
            enqueued_media_items=list(enqueued or []),
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
    items: list[QueueItem],
    index: int,
    enqueued: list[Album | Playlist | Track] | None = None,
    repeat_mode: RepeatMode = RepeatMode.OFF,
) -> bool:
    """Load the item at the given index and read the loudness decision taken for it."""
    controller = _controller(items, enqueued)
    controller._queue_data[QUEUE_ID].queue.repeat_mode = repeat_mode
    await controller._load_item(items[index])
    get_stream_details = cast("AsyncMock", controller.mass.streams.audio.get_stream_details)
    return cast("bool", get_stream_details.call_args.kwargs["prefer_album_loudness"])


async def test_track_of_an_enqueued_album_uses_album_loudness() -> None:
    """The tracks of an album the user pressed play on are normalized on the album loudness."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM),
        _queue_item("track-2", PROVIDER_ALBUM),
    ]
    assert await _prefer_album_loudness(items, 0, enqueued=[LIBRARY_ALBUM])


async def test_shuffled_album_still_uses_album_loudness() -> None:
    """An album played on shuffle is still that album, however its tracks end up ordered."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM),
        _queue_item("track-2", OTHER_PROVIDER_ALBUM),
        _queue_item("track-3", PROVIDER_ALBUM),
    ]
    assert await _prefer_album_loudness(items, 0, enqueued=[LIBRARY_ALBUM])


async def test_adjacent_playlist_tracks_of_one_album_use_track_loudness() -> None:
    """A playlist that happens to place two tracks of one album together is no album play."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM),
        _queue_item("track-2", PROVIDER_ALBUM),
    ]
    assert not await _prefer_album_loudness(items, 0, enqueued=[PLAYLIST])


async def test_track_added_beside_an_enqueued_album_uses_track_loudness() -> None:
    """On a mixed queue only the enqueued album's own tracks play as part of an album."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM),
        _queue_item("track-2", OTHER_PROVIDER_ALBUM),
    ]
    enqueued: list[Album | Playlist | Track] = [
        LIBRARY_ALBUM,
        cast("Track", items[1].media_item),
    ]
    assert await _prefer_album_loudness(items, 0, enqueued=enqueued)
    assert not await _prefer_album_loudness(items, 1, enqueued=enqueued)


async def test_a_different_enqueued_album_does_not_apply() -> None:
    """Only the album a track actually belongs to counts, not any album on the queue."""
    items = [_queue_item("track-1", PROVIDER_ALBUM)]
    assert not await _prefer_album_loudness(items, 0, enqueued=[OTHER_LIBRARY_ALBUM])


async def test_queue_without_an_enqueued_album_uses_track_loudness() -> None:
    """A queue that records no album parent (a browsed folder, a restored queue) is no album play."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM),
        _queue_item("track-2", PROVIDER_ALBUM),
    ]
    assert not await _prefer_album_loudness(items, 0)


async def test_library_album_enqueued_matches_the_provider_album_on_the_item() -> None:
    """The enqueued album and the queue's tracks may hold different shapes of the same album."""
    items = [_queue_item("track-1", PROVIDER_ALBUM)]
    assert await _prefer_album_loudness(items, 0, enqueued=[LIBRARY_ALBUM])


async def test_provider_album_enqueued_matches_the_library_album_on_the_item() -> None:
    """The same album seen from both representations is recognised the other way around too."""
    items = [_queue_item("track-1", LIBRARY_ALBUM)]
    provider_album = Album(
        item_id="album-prov-1",
        provider="spotify--abc",
        name="Kind of Blue",
        provider_mappings={
            ProviderMapping(
                item_id="album-prov-1",
                provider_domain="spotify",
                provider_instance="spotify--abc",
            )
        },
    )
    assert await _prefer_album_loudness(items, 0, enqueued=[provider_album])


async def test_item_without_an_album_uses_track_loudness() -> None:
    """An item that carries no album at all has no album loudness to prefer."""
    items = [_queue_item("track-1", None)]
    assert not await _prefer_album_loudness(items, 0, enqueued=[LIBRARY_ALBUM])


async def test_repeat_single_ignores_the_enqueued_album() -> None:
    """A track repeating on its own is not played as part of the album it was enqueued with."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM),
        _queue_item("track-2", PROVIDER_ALBUM),
    ]
    assert not await _prefer_album_loudness(
        items, 0, enqueued=[LIBRARY_ALBUM], repeat_mode=RepeatMode.ONE
    )


async def test_next_item_is_loaded_with_the_album_decision() -> None:
    """The preload of the item that plays next takes the same decision as the current one."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM),
        _queue_item("track-2", PROVIDER_ALBUM),
    ]
    controller = _controller(items, enqueued=[LIBRARY_ALBUM])
    await controller.load_next_queue_item(QUEUE_ID, items[0].queue_item_id)
    get_stream_details = cast("AsyncMock", controller.mass.streams.audio.get_stream_details)
    assert get_stream_details.call_args.kwargs["prefer_album_loudness"]
