"""Tests that a queued podcast episode or audiobook gets the chapters of its full details."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    ItemMapping,
    MediaItemChapter,
    PodcastEpisode,
    ProviderMapping,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"
# the library book has two copies and streams from the second one
STREAMING_INSTANCE = "audiobookshelf--2"
STREAMING_ITEM_ID = "book_2"


def _episode(chapters: list[MediaItemChapter] | None = None) -> PodcastEpisode:
    episode = PodcastEpisode(
        item_id="feed1 ep1",
        provider="podcastfeed--1",
        name="Episode One",
        position=1,
        podcast=ItemMapping(
            item_id="feed1",
            provider="podcastfeed--1",
            name="Feed One",
            media_type=MediaType.PODCAST,
        ),
        provider_mappings={
            ProviderMapping(
                item_id="feed1 ep1",
                provider_domain="podcastfeed",
                provider_instance="podcastfeed--1",
            )
        },
    )
    episode.metadata.chapters = chapters
    return episode


def _audiobook(chapters: list[MediaItemChapter] | None = None) -> Audiobook:
    """Return the library audiobook as a listing hands it over: without chapters by default."""
    book = Audiobook(
        item_id="7",
        provider="library",
        name="Dune",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="audiobookshelf",
                provider_instance=instance,
            )
            for item_id, instance in (
                ("book_1", "audiobookshelf--1"),
                (STREAMING_ITEM_ID, STREAMING_INSTANCE),
            )
        },
    )
    book.metadata.chapters = chapters
    return book


def _queue_item(media_item: PodcastEpisode | Audiobook) -> QueueItem:
    queue_item = QueueItem.from_media_item(QUEUE_ID, media_item)
    queue_item.streamdetails = StreamDetails(
        provider=STREAMING_INSTANCE if isinstance(media_item, Audiobook) else media_item.provider,
        item_id=STREAMING_ITEM_ID if isinstance(media_item, Audiobook) else media_item.item_id,
        audio_format=AudioFormat(),
        media_type=media_item.media_type,
    )
    return queue_item


def _controller(
    details: PodcastEpisode | Audiobook, queue_item: QueueItem
) -> tuple[PlayerQueuesController, Mock]:
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    signal_update = Mock()
    ctrl.signal_update = signal_update  # type: ignore[method-assign]
    ctrl.logger = Mock()
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=1)
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=queue, items=[queue_item])}
    ctrl.mass = MagicMock()
    ctrl.mass.streams.audio.get_stream_details = AsyncMock(return_value=queue_item.streamdetails)
    ctrl.mass.music.podcasts.episode = AsyncMock(return_value=details)

    async def get_provider_item(item_id: str, provider: str, **_kwargs: Any) -> Audiobook:
        if (item_id, provider) != (STREAMING_ITEM_ID, STREAMING_INSTANCE):
            raise MediaNotFoundError(f"{provider}/{item_id} is not the streaming copy")
        assert isinstance(details, Audiobook)
        return details

    ctrl.mass.music.audiobooks.get_provider_item = AsyncMock(side_effect=get_provider_item)
    return ctrl, signal_update


@pytest.mark.parametrize("build", [_episode, _audiobook], ids=["episode", "audiobook"])
async def test_loading_the_item_fills_in_the_chapters_of_the_full_item(build: Any) -> None:
    """An audiobook played from a listing used to stay without chapters, unlike an episode."""
    chapters = [MediaItemChapter(position=1, name="Intro", start=0)]
    queue_item = _queue_item(build())
    ctrl, signal_update = _controller(build(chapters), queue_item)
    lookups: list[Any] = []
    ctrl.mass.create_task = Mock(  # type: ignore[method-assign]
        side_effect=lambda lookup, **_kwargs: lookups.append(lookup)
    )

    await ctrl._load_item(queue_item)
    for lookup in lookups:
        await lookup

    assert queue_item.media_item is not None
    assert queue_item.media_item.metadata.chapters == chapters
    signal_update.assert_called_once_with(QUEUE_ID, items_changed=True)


@pytest.mark.parametrize("build", [_episode, _audiobook], ids=["episode", "audiobook"])
async def test_item_without_chapters_is_marked_as_looked_up(build: Any) -> None:
    """An item without chapters gets an empty list, so it is not looked up again."""
    queue_item = _queue_item(build())
    ctrl, signal_update = _controller(build(), queue_item)

    await ctrl._load_chapters(queue_item)

    assert queue_item.media_item is not None
    assert queue_item.media_item.metadata.chapters == []
    signal_update.assert_not_called()
