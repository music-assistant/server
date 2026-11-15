"""
Tests for an ended queue that plays again while its dynamic source is fetched.

A queue that ran out of items is refilled from the dynamic source it was started from, or
taken for ended when that source has nothing left. A play that starts the queue while the
source is fetched has the queue: it is neither refilled nor taken for ended.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.controllers.player_queues.playback_tracker import PlaybackTrackerMixin

from .test_autoplay_dispatch import _dynamic_playlist, _prov_track, _stop_states, _tracker

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track
    from music_assistant_models.player_queue import PlayerQueue


@pytest.mark.parametrize(
    "fetched",
    [[_prov_track("fresh")], [], ProviderUnavailableError("gone")],
    ids=["tracks", "nothing", "an error"],
)
async def test_a_queue_that_plays_again_while_its_source_is_fetched_is_left_alone(
    fetched: list[Track] | Exception,
) -> None:
    """
    Whatever the dynamic source hands back, a queue that plays by then keeps what it plays.

    :param fetched: The tracks the dynamic source returns, or the error it fails with.
    """
    tracker = _tracker()
    tracker._queue_data["q1"].userid = None
    tracker.load = AsyncMock()
    tracker.play_index = AsyncMock()
    queue = cast(
        "PlayerQueue",
        SimpleNamespace(
            queue_id="q1",
            next_item=None,
            flow_mode=False,
            state=PlaybackState.IDLE,
            current_index=None,
            display_name="Q1",
        ),
    )
    prev_state, new_state = _stop_states(
        SimpleNamespace(media_type=MediaType.TRACK, streamdetails=None, duration=3600)
    )
    PlaybackTrackerMixin._handle_end_of_queue(tracker, queue, prev_state, new_state)
    settle_coro = tracker.mass.create_task.call_args.args[0]

    async def _fetch_while_a_play_starts(*_args: Any, **_kwargs: Any) -> list[Track]:
        queue.state = PlaybackState.PLAYING
        if isinstance(fetched, Exception):
            raise fetched
        return fetched

    tracker._media_resolver.get_dynamic_source_tracks = AsyncMock(
        side_effect=_fetch_while_a_play_starts
    )
    tracker.mass.music.favorites.disliked_track_keys = AsyncMock(return_value=(set(), set()))
    tracker.mass.music.favorites.in_disliked_genres = AsyncMock(return_value=set())
    with (
        patch(
            "music_assistant.controllers.player_queues.playback_tracker.asyncio.sleep",
            AsyncMock(),
        ),
        patch(
            "music_assistant.controllers.player_queues.playback_tracker.find_dynamic_source",
            return_value=_dynamic_playlist(),
        ),
    ):
        await settle_coro

    cast("MagicMock", tracker._media_resolver.get_dynamic_source_tracks).assert_awaited_once()
    tracker.load.assert_not_called()
    tracker.play_index.assert_not_called()
    tracker._finish_queue.assert_not_called()
