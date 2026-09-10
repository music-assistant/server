"""Tests for aligning Music Assistant queue indices with Plex play queue indices."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import PlaybackState

from music_assistant.providers.plex_connect.queue_commands import QueueCommandsMixin
from music_assistant.providers.plex_connect.queue_sync import QueueSyncMixin


def _fake_track(track_key: str) -> SimpleNamespace:
    """Return a stand-in for the MA track a Plex item resolves to."""
    return SimpleNamespace(key=track_key, name=track_key)


class _QueueHandler(QueueSyncMixin, QueueCommandsMixin):
    """The queue mixins with the host-class attributes mocked."""

    def __init__(self) -> None:
        self.play_media = AsyncMock()
        provider = Mock()
        provider.get_track = AsyncMock(side_effect=_fake_track)
        provider.mass.player_queues.play_media = self.play_media
        self.queue = SimpleNamespace(
            state=PlaybackState.PLAYING, current_index=0, index_in_buffer=0
        )
        provider.mass.player_queues.get = Mock(return_value=self.queue)
        self.provider = provider
        self._ma_player_id = "player1"
        self._updating_from_plex = False
        self.play_queue_id = "1093"
        self.play_queue_item_ids: dict[int, int] = {}

    def queued_keys(self) -> list[str]:
        """Return the Plex keys handed to play_media, in order."""
        await_args = self.play_media.await_args
        assert await_args is not None
        return [track.key for track in await_args.kwargs["media"]]


def _make_playqueue(count: int, selected_index: int) -> Any:
    """
    Build a fake Plex PlayQueue of ``count`` items in album order.

    Item N gets key ``/library/metadata/N`` and playQueueItemID ``1000+N``.
    """
    items = [
        SimpleNamespace(key=f"/library/metadata/{n}", playQueueItemID=1000 + n)
        for n in range(count)
    ]
    return SimpleNamespace(
        items=items,
        playQueueSelectedItemID=1000 + selected_index,
        playQueueSelectedItemOffset=selected_index,
    )


async def test_refresh_does_not_requeue_the_playing_track() -> None:
    """
    A refresh must not put the currently playing track back into the queue.

    MA's queue is the Plex queue rotated to start at the selected item, so MA index 0
    is Plex index 1 here. Slicing Plex at the MA index re-queued Plex item 1.
    """
    handler = _QueueHandler()
    playqueue = _make_playqueue(12, selected_index=1)
    # Playback started at Plex index 1, so that item sits at MA index 0.
    handler.play_queue_item_ids = {0: 1001}

    await handler._replace_remaining_queue("player1", playqueue, 0)

    assert "/library/metadata/1" not in handler.queued_keys()


async def test_refresh_preserves_the_wrapped_queue_order() -> None:
    """Items before the playing track wrap to the tail, as on the initial load."""
    handler = _QueueHandler()
    playqueue = _make_playqueue(12, selected_index=1)
    handler.play_queue_item_ids = {0: 1001}

    await handler._replace_remaining_queue("player1", playqueue, 0)

    expected = [f"/library/metadata/{n}" for n in [*range(2, 12), 0]]
    assert handler.queued_keys() == expected


async def test_refresh_keeps_item_ids_in_ma_index_space() -> None:
    """The playQueueItemID map stays keyed by MA index, not by Plex index."""
    handler = _QueueHandler()
    playqueue = _make_playqueue(12, selected_index=1)
    handler.play_queue_item_ids = {0: 1001}

    await handler._replace_remaining_queue("player1", playqueue, 0)

    # MA index 1 now holds Plex item 2, and the wrapped Plex item 0 lands last.
    assert handler.play_queue_item_ids[0] == 1001
    assert handler.play_queue_item_ids[1] == 1002
    assert handler.play_queue_item_ids[11] == 1000


async def test_refresh_falls_back_to_the_plex_selected_item() -> None:
    """Without a mapping for the MA index, trust the item Plex reports as selected."""
    handler = _QueueHandler()
    playqueue = _make_playqueue(12, selected_index=3)
    handler.play_queue_item_ids = {}

    await handler._replace_remaining_queue("player1", playqueue, 0)

    assert "/library/metadata/3" not in handler.queued_keys()


async def test_refresh_does_not_requeue_the_buffered_track() -> None:
    """
    REPLACE_NEXT keeps the buffered item as well, so it must not be re-queued.

    The player has already been handed MA index 1, so REPLACE_NEXT inserts after it.
    Slicing from the playing index hands that same track over a second time.
    """
    handler = _QueueHandler()
    playqueue = _make_playqueue(12, selected_index=1)
    handler.play_queue_item_ids = {0: 1001, 1: 1002}
    handler.queue.index_in_buffer = 1

    await handler._replace_remaining_queue("player1", playqueue, 0)

    assert "/library/metadata/2" not in handler.queued_keys()


async def test_refresh_replaces_only_what_follows_the_buffered_item() -> None:
    """The buffered item is kept, so the replacement starts one track later."""
    handler = _QueueHandler()
    playqueue = _make_playqueue(12, selected_index=1)
    handler.play_queue_item_ids = {0: 1001, 1: 1002}
    handler.queue.index_in_buffer = 1

    await handler._replace_remaining_queue("player1", playqueue, 0)

    expected = [f"/library/metadata/{n}" for n in [*range(3, 12), 0]]
    assert handler.queued_keys() == expected


async def test_refresh_on_a_windowed_queue_resumes_after_the_current_item() -> None:
    """
    A window that omits MA index 0 must still resume right after the current item.

    The server returns a window centred on the playing track, so on a long queue the
    item MA started from is not in the response and no rotation can be derived from it.
    """
    handler = _QueueHandler()
    items = [
        SimpleNamespace(key=f"/library/metadata/{n}", playQueueItemID=1000 + n)
        for n in range(40, 100)
    ]
    playqueue = SimpleNamespace(
        items=items,
        playQueueTotalCount=200,
        playQueueSelectedItemID=1060,
        playQueueSelectedItemOffset=60,
    )
    # MA started from track 0, which this window does not contain.
    handler.play_queue_item_ids = {0: 1000, 60: 1060}
    handler.queue.current_index = 60
    handler.queue.index_in_buffer = 60

    await handler._replace_remaining_queue("player1", playqueue, 60)

    assert handler.queued_keys() == [f"/library/metadata/{n}" for n in range(61, 100)]


async def test_refresh_after_playback_wrapped_past_the_origin() -> None:
    """Once playback reaches the wrapped tail, only the tracks before the origin remain."""
    handler = _QueueHandler()
    playqueue = _make_playqueue(12, selected_index=4)
    # Playback began at Plex index 4 and has reached MA index 9, which is Plex index 1.
    handler.play_queue_item_ids = {0: 1004, 9: 1001}
    handler.queue.current_index = 9
    handler.queue.index_in_buffer = 9

    await handler._replace_remaining_queue("player1", playqueue, 9)

    assert handler.queued_keys() == ["/library/metadata/2", "/library/metadata/3"]


async def test_refresh_when_the_origin_track_was_removed_from_plex() -> None:
    """
    A removed origin still leaves a wrap: the boundary moves to the next MA item.

    An absent origin in a complete response means the track was deleted from the queue,
    which is not the same as it falling outside a truncated one.
    """
    handler = _QueueHandler()
    # MA holds [1, 2, 3, 0] and is playing item 1; Plex no longer lists item 1.
    items = [
        SimpleNamespace(key=f"/library/metadata/{n}", playQueueItemID=1000 + n) for n in (0, 2, 3)
    ]
    playqueue = SimpleNamespace(
        items=items,
        playQueueTotalCount=3,
        playQueueSelectedItemID=1002,
        playQueueSelectedItemOffset=1,
    )
    handler.play_queue_item_ids = {0: 1001, 1: 1002, 2: 1003, 3: 1000}
    handler.queue.current_index = 1
    handler.queue.index_in_buffer = 1

    await handler._replace_remaining_queue("player1", playqueue, 1)

    assert handler.queued_keys() == ["/library/metadata/3", "/library/metadata/0"]
