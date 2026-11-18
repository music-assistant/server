"""Tests for mapping a Plex play queue created from the MA queue to MA queue positions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from plexapi.playqueue import PlayQueue

from music_assistant.providers.plex_connect import queue_sync
from music_assistant.providers.plex_connect.queue_commands import QueueCommandsMixin
from music_assistant.providers.plex_connect.queue_sync import QueueSyncMixin

TRACK_COUNT = 23
# the Plex server answers a play queue create with only the first ~20 items after the selection
CREATE_WINDOW = 21


class _QueueHandler(QueueSyncMixin, QueueCommandsMixin):
    """The queue mixins with the host-class attributes mocked."""

    def __init__(self, current_index: int) -> None:
        provider = Mock()
        provider.instance_id = "plex--instance1"
        provider.mass.player_queues.get = Mock(
            return_value=SimpleNamespace(current_index=current_index)
        )
        provider.mass.player_queues.items = Mock(
            return_value=[SimpleNamespace(media_item=n) for n in range(TRACK_COUNT)]
        )
        self.provider = provider
        self.plex_server = Mock(fetchItem=lambda key: key)
        self._ma_player_id = "player1"
        self.play_queue_id: str | None = None
        self.play_queue_version = 0
        self.play_queue_item_ids: dict[int, int] = {}


def _playqueue(keys: list[str], window_start: int, selected: int) -> SimpleNamespace:
    """Return a play queue response holding the items from window_start onwards."""
    return SimpleNamespace(
        playQueueID=77,
        playQueueVersion=1,
        playQueueTotalCount=len(keys),
        playQueueSelectedItemID=900000 + selected,
        playQueueSelectedItemOffset=selected,
        items=[
            SimpleNamespace(key=key, playQueueItemID=900000 + n)
            for n, key in enumerate(keys)
            if n >= window_start
        ],
    )


async def _create(handler: _QueueHandler, window_start: int, create_end: int) -> None:
    """Create the play queue against a fake server that windows its responses."""
    created: dict[str, Any] = {}

    def fake_create(_server: Any, items: list[str], **kwargs: Any) -> Any:
        created["keys"] = items
        created["selected"] = items.index(kwargs["startItem"])
        response = _playqueue(items, window_start, created["selected"])
        response.items = response.items[: create_end - window_start]
        return response

    def fake_get(_server: Any, window: int, **_kw: Any) -> Any:
        selected = created["selected"]
        return _playqueue(created["keys"], max(0, selected - window), selected)

    with (
        patch.object(queue_sync, "plex_key_for_item", side_effect=lambda n, _id: f"/m/{n}"),
        patch.object(PlayQueue, "create", side_effect=fake_create),
        patch.object(PlayQueue, "get", side_effect=fake_get),
    ):
        await handler._create_plex_playqueue_from_ma()


async def test_every_track_of_a_long_album_gets_its_play_queue_item_id() -> None:
    """Tracks past the create response window still map to their own playQueueItemID."""
    handler = _QueueHandler(current_index=0)

    await _create(handler, window_start=0, create_end=CREATE_WINDOW)

    assert handler.play_queue_item_ids == {n: 900000 + n for n in range(TRACK_COUNT)}


async def test_window_starting_mid_queue_maps_by_queue_position() -> None:
    """A response window that starts after the queue head maps items to their own position."""
    handler = _QueueHandler(current_index=15)

    await _create(handler, window_start=5, create_end=TRACK_COUNT)

    assert handler.play_queue_item_ids[15] == 900015
    assert handler.play_queue_item_ids[22] == 900022
