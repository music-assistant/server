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
from music_assistant_models.queue_item import QueueItem, QueueItemOrigin

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
ALBUM_ORIGIN = QueueItemOrigin(container=ItemMapping.from_item(LIBRARY_ALBUM))
PLAYLIST_ORIGIN = QueueItemOrigin(container=ItemMapping.from_item(PLAYLIST))
# played from the folder that holds the album, pinned to the file in it
ALBUM_FOLDER_ORIGIN = QueueItemOrigin(
    container=ItemMapping.from_item(LIBRARY_ALBUM),
    provider_instance="filesystem_local--abc",
    item_id="Music/Kind of Blue/01.flac",
)
FOLDER_ORIGIN = QueueItemOrigin(
    container=ItemMapping(
        media_type=MediaType.FOLDER,
        item_id="Music/Mixed",
        provider="filesystem_local--abc",
        name="Mixed",
    ),
    provider_instance="filesystem_local--abc",
    item_id="Music/Mixed/01.flac",
)


def _queue_item(
    item_id: str, album: Album | ItemMapping | None, origin: QueueItemOrigin | None = None
) -> QueueItem:
    """Build a queue item holding a track on the given album, played from the given origin."""
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
        origin=origin,
    )


def _controller(
    items: list[QueueItem],
    enqueued: list[Album | Playlist | Track] | None = None,
    library_track: Track | None = None,
) -> PlayerQueuesController:
    """
    Build a bare controller whose queue holds the given items and enqueued parents.

    :param items: The items the queue holds.
    :param enqueued: The parent media items the user enqueued on it.
    :param library_track: The library track the items' track resolves to while loading.
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
                items=len(items),
            ),
            items=items,
            enqueued_media_items=list(enqueued or []),
        )
    }
    tracks_by_uri = {item.uri: item.media_item for item in items}
    mass = MagicMock()
    mass.music.get_library_item_by_prov_id = AsyncMock(
        side_effect=lambda media_type, *_: library_track if media_type == MediaType.TRACK else None
    )
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


async def test_track_played_from_an_album_uses_album_loudness() -> None:
    """The tracks of an album the user pressed play on are normalized on the album loudness."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM, origin=ALBUM_ORIGIN),
        _queue_item("track-2", PROVIDER_ALBUM, origin=ALBUM_ORIGIN),
    ]
    assert await _prefer_album_loudness(items, 0)


async def test_shuffled_album_still_uses_album_loudness() -> None:
    """An album played on shuffle is still that album, however its tracks end up ordered."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM, origin=ALBUM_ORIGIN),
        _queue_item("track-2", OTHER_PROVIDER_ALBUM),
        _queue_item("track-3", PROVIDER_ALBUM, origin=ALBUM_ORIGIN),
    ]
    assert await _prefer_album_loudness(items, 0)


async def test_adjacent_playlist_tracks_of_one_album_use_track_loudness() -> None:
    """A playlist that happens to place two tracks of one album together is no album play."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM, origin=PLAYLIST_ORIGIN),
        _queue_item("track-2", PROVIDER_ALBUM, origin=PLAYLIST_ORIGIN),
    ]
    assert not await _prefer_album_loudness(items, 0, enqueued=[PLAYLIST])


async def test_track_added_beside_an_enqueued_album_uses_track_loudness() -> None:
    """On a mixed queue only the enqueued album's own tracks play as part of an album."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM, origin=ALBUM_ORIGIN),
        _queue_item("track-2", OTHER_PROVIDER_ALBUM),
    ]
    enqueued: list[Album | Playlist | Track] = [
        LIBRARY_ALBUM,
        cast("Track", items[1].media_item),
    ]
    assert await _prefer_album_loudness(items, 0, enqueued=enqueued)
    assert not await _prefer_album_loudness(items, 1, enqueued=enqueued)


async def test_track_of_an_enqueued_album_played_from_elsewhere_uses_track_loudness() -> None:
    """
    An album the user enqueued does not reach its tracks that are played from elsewhere.

    Such a track comes from a playlist, from autoplay or is played on its own.
    """
    items = [
        _queue_item("track-1", PROVIDER_ALBUM, origin=PLAYLIST_ORIGIN),
        _queue_item("track-2", PROVIDER_ALBUM),
    ]
    enqueued: list[Album | Playlist | Track] = [LIBRARY_ALBUM, PLAYLIST]
    assert not await _prefer_album_loudness(items, 0, enqueued=enqueued)
    assert not await _prefer_album_loudness(items, 1, enqueued=enqueued)


async def test_track_played_from_an_album_folder_uses_album_loudness() -> None:
    """A folder that holds an album is played as that album, whatever the queue enqueued."""
    items = [_queue_item("track-1", None, origin=ALBUM_FOLDER_ORIGIN)]
    assert await _prefer_album_loudness(items, 0)


async def test_track_played_from_a_plain_folder_uses_track_loudness() -> None:
    """A folder of loose files is no album, even when its tracks share one."""
    items = [_queue_item("track-1", PROVIDER_ALBUM, origin=FOLDER_ORIGIN)]
    assert not await _prefer_album_loudness(items, 0)


async def test_repeat_single_ignores_the_album_origin() -> None:
    """A track repeating on its own is not played as part of the album folder it came from."""
    items = [_queue_item("track-1", None, origin=ALBUM_FOLDER_ORIGIN)]
    assert not await _prefer_album_loudness(items, 0, repeat_mode=RepeatMode.ONE)


async def test_origin_survives_swapping_in_the_library_track() -> None:
    """Loading swaps the queued track for its library row; where it was played from stays."""
    items = [_queue_item("track-1", PROVIDER_ALBUM, origin=ALBUM_FOLDER_ORIGIN)]
    library_track = Track(
        item_id="42",
        provider="library",
        name="track-1",
        duration=300,
        provider_mappings={
            ProviderMapping(
                item_id="track-1", provider_domain="spotify", provider_instance="spotify--abc"
            )
        },
    )
    controller = _controller(items, library_track=library_track)

    await controller._load_item(items[0])

    assert items[0].media_item is library_track
    assert items[0].origin == ALBUM_FOLDER_ORIGIN


async def test_next_item_is_loaded_with_the_album_decision() -> None:
    """The preload of the item that plays next takes the same decision as the current one."""
    items = [
        _queue_item("track-1", PROVIDER_ALBUM, origin=ALBUM_ORIGIN),
        _queue_item("track-2", PROVIDER_ALBUM, origin=ALBUM_ORIGIN),
    ]
    controller = _controller(items)
    await controller.load_next_queue_item(QUEUE_ID, items[0].queue_item_id)
    get_stream_details = cast("AsyncMock", controller.mass.streams.audio.get_stream_details)
    assert get_stream_details.call_args.kwargs["prefer_album_loudness"]


async def test_album_play_survives_a_restart() -> None:
    """An album queue restored from cache still plays as an album."""
    items = [_queue_item("track-1", PROVIDER_ALBUM, origin=ALBUM_ORIGIN)]
    controller = _controller(items)
    queue_data = controller._queue_data[QUEUE_ID]
    restored = PlayerQueueData.from_cache(queue_data.to_cache(), queue_data.items_to_cache())

    controller._queue_data[QUEUE_ID] = restored
    await controller._load_item(restored.items[0])

    get_stream_details = cast("AsyncMock", controller.mass.streams.audio.get_stream_details)
    assert get_stream_details.call_args.kwargs["prefer_album_loudness"]
