"""
Tests that a Plex-authored queue order survives the load into Music Assistant.

Plex owns the order of a play queue - a shuffled one arrives already shuffled - and the
index-to-playQueueItemID map is built against that order, so the Music Assistant queue's own
shuffle must never reorder the tracks on their way in. Plex's shuffle flag is mirrored onto the
queue separately, once the items have landed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import PlaybackState, QueueOption

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


def _make_playqueue(count: int) -> Any:
    """Build a fake Plex PlayQueue of ``count`` items in the order Plex wants them played."""
    items = [
        SimpleNamespace(key=f"/library/metadata/{n}", playQueueItemID=1000 + n)
        for n in range(count)
    ]
    return SimpleNamespace(items=items, playQueueSelectedItemID=1000, playQueueSelectedItemOffset=0)


async def test_replacing_the_queue_refuses_to_let_shuffle_reorder_plex() -> None:
    """
    The whole-queue load asks for an unshuffled queue, whatever the queue's own toggle says.

    The tracks arrive in the order Plex wants them played, and play_queue_item_ids is keyed on
    that order - a shuffle applied on the way in would start the wrong track and make every
    position MA reports back to Plexamp wrong.
    """
    handler = _QueueHandler()

    await handler._replace_entire_queue("player1", _make_playqueue(5))

    call = handler.play_media.await_args
    assert call is not None
    assert call.kwargs["option"] == QueueOption.REPLACE
    assert call.kwargs["shuffle"] is False
    # and the tracks themselves go over in Plex's order
    assert [track.key for track in call.kwargs["media"]] == [
        f"/library/metadata/{n}" for n in range(5)
    ]
    assert handler.play_queue_item_ids == {n: 1000 + n for n in range(5)}
