"""
Tests for what a play request naming a live audio source does to the queue.

A live source is not queue content: it plays on the player while the queue keeps
its own items, so selecting it is the real operation and a play request naming one
is forwarded there. Replaces the queue-release tests, which covered a source
leaving a queue it can no longer be in.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import QueueOption
from music_assistant_models.media_items import AudioSource, Track
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"
SOURCE_URI = "spotify_connect--test://audio_source/main"


def _track() -> Track:
    return Track(
        item_id="t1",
        provider="test",
        name="A track the user queued",
        artists=UniqueList(),
        provider_mappings={
            ProviderMapping(item_id="t1", provider_domain="test", provider_instance="test")
        },
    )


def _audio_source() -> AudioSource:
    return AudioSource(
        item_id="main",
        provider="spotify_connect--test",
        name="Spotify Connect",
        provider_mappings={
            ProviderMapping(
                item_id="main",
                provider_domain="spotify_connect",
                provider_instance="spotify_connect--test",
            )
        },
    )


def _controller(*items: QueueItem) -> Any:
    """Build a bare controller holding a queue with the given items."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = MagicMock()
    ctrl.logger = MagicMock()
    ctrl.mass.players.select_source = AsyncMock()
    queue = PlayerQueue(
        queue_id=QUEUE_ID,
        active=True,
        display_name="Q1",
        available=True,
        items=len(items),
    )
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=queue, items=list(items))}
    ctrl._check_player_permission = MagicMock()  # type: ignore[method-assign]
    ctrl.get = MagicMock(return_value=queue)  # type: ignore[method-assign]
    ctrl._handle_play_media = AsyncMock()  # type: ignore[method-assign]
    return ctrl


async def test_a_source_uri_is_selected_and_the_queue_is_left_alone() -> None:
    """
    The source is selected on the player and nothing is enqueued.

    That is the whole point of the source living on the player: the queue keeps the
    items the user had, so they are still there to resume once the source is done.
    """
    existing = QueueItem.from_media_item(QUEUE_ID, _track())
    ctrl = _controller(existing)

    await ctrl.play_media(QUEUE_ID, SOURCE_URI, QueueOption.REPLACE)

    ctrl.mass.players.select_source.assert_awaited_once_with(QUEUE_ID, SOURCE_URI)
    ctrl._handle_play_media.assert_not_awaited()
    assert ctrl._queue_data[QUEUE_ID].items == [existing]


async def test_a_source_media_item_is_selected_too() -> None:
    """A play request carrying the media item rather than its uri behaves the same."""
    ctrl = _controller()

    await ctrl.play_media(QUEUE_ID, _audio_source(), QueueOption.PLAY)

    ctrl.mass.players.select_source.assert_awaited_once_with(QUEUE_ID, SOURCE_URI)
    ctrl._handle_play_media.assert_not_awaited()


async def test_ordinary_media_still_goes_to_the_queue() -> None:
    """Anything that is not a live source is enqueued as before."""
    ctrl = _controller()

    await ctrl.play_media(QUEUE_ID, _track(), QueueOption.PLAY)

    ctrl.mass.players.select_source.assert_not_awaited()
    ctrl._handle_play_media.assert_awaited_once()


async def test_a_source_named_among_other_media_is_not_forwarded() -> None:
    """
    A batch is left to the ordinary path.

    A live source has no meaning lined up behind or alongside other media, so it is
    not quietly turned into a source selection - it fails as the unplayable item it is.
    """
    ctrl = _controller()

    await ctrl.play_media(QUEUE_ID, [SOURCE_URI, "library://track/1"], QueueOption.PLAY)

    ctrl.mass.players.select_source.assert_not_awaited()
    ctrl._handle_play_media.assert_awaited_once()
