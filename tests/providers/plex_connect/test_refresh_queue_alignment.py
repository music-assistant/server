"""Tests for aligning Music Assistant queue indices with Plex play queue indices."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

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
