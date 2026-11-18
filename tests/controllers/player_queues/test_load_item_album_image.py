"""Tests for the image of a track loaded from an album."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.media_items import (
    Album,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "queue-1"
PLAYED_ALBUM_THUMB = MediaItemImage(
    type=ImageType.THUMB, path="http://images/album.jpg", provider="spotify--abc"
)
OTHER_ALBUM_THUMB = MediaItemImage(
    type=ImageType.THUMB, path="http://images/compilation.jpg", provider="spotify--abc"
)
TRACK_THUMB = MediaItemImage(
    type=ImageType.THUMB, path="http://images/track.jpg", provider="spotify--abc"
)
PLAYED_ALBUM = ItemMapping(
    media_type=MediaType.ALBUM,
    item_id="album-prov-1",
    provider="spotify--abc",
    name="Kind of Blue",
    image=PLAYED_ALBUM_THUMB,
)
LIBRARY_PLAYED_ALBUM = Album(
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
    metadata=MediaItemMetadata(images=UniqueList([PLAYED_ALBUM_THUMB])),
)
# the library lists the track on the album with the lowest id, here a compilation
LIBRARY_TRACK = Track(
    item_id="3",
    provider="library",
    name="So What",
    duration=300,
    provider_mappings={
        ProviderMapping(
            item_id="track-1",
            provider_domain="spotify",
            provider_instance="spotify--abc",
        )
    },
    album=ItemMapping(
        media_type=MediaType.ALBUM,
        item_id="2",
        provider="library",
        name="Jazz Compilation",
        image=OTHER_ALBUM_THUMB,
    ),
    metadata=MediaItemMetadata(images=UniqueList([TRACK_THUMB])),
)


def _queue_item() -> QueueItem:
    """Build a queue item holding a track as listed on the album it is played from."""
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id="track-1",
        name="So What",
        duration=300,
        media_item=Track(
            item_id="track-1",
            provider="spotify--abc",
            name="So What",
            duration=300,
            provider_mappings={
                ProviderMapping(
                    item_id="track-1",
                    provider_domain="spotify",
                    provider_instance="spotify--abc",
                )
            },
            album=PLAYED_ALBUM,
        ),
    )


def _controller(item: QueueItem, library_album: Album | None) -> PlayerQueuesController:
    """
    Build a bare controller whose queue holds the item, which is in the library.

    :param item: The item the queue holds.
    :param library_album: The library album the played album resolves to, if any.
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
    mass.music.get_library_item_by_prov_id = AsyncMock(
        side_effect=lambda media_type, *_: (
            Track.from_dict(LIBRARY_TRACK.to_dict())
            if media_type == MediaType.TRACK
            else library_album
        )
    )
    mass.streams.audio.get_stream_details = AsyncMock(return_value=MagicMock(duration=None))
    controller.mass = mass
    return controller


@pytest.mark.parametrize("library_album", [LIBRARY_PLAYED_ALBUM, None])
async def test_track_shows_the_image_of_the_album_it_is_played_from(
    library_album: Album | None,
) -> None:
    """A track played from an album shows that album's image, not the one the library lists."""
    item = _queue_item()
    controller = _controller(item, library_album)

    await controller._load_item(item)

    assert item.media_item is not None
    assert item.media_item.image == PLAYED_ALBUM_THUMB
    assert item.media_item.metadata.images == [TRACK_THUMB]
