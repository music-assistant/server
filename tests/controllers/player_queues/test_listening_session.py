"""Tests that the wall-clock time audiobooks and podcasts play is recorded as sessions."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.media_items import (
    Audiobook,
    ItemMapping,
    PodcastEpisode,
    ProviderMapping,
    Track,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "queue_1"
PROVIDER = "test--1"


def _mapping(item_id: str) -> set[ProviderMapping]:
    return {ProviderMapping(item_id=item_id, provider_domain="test", provider_instance=PROVIDER)}


def _episode(item_id: str) -> PodcastEpisode:
    return PodcastEpisode(
        item_id=item_id,
        provider=PROVIDER,
        name=item_id,
        duration=3600,
        position=1,
        podcast=ItemMapping(
            item_id="podcast_1", provider=PROVIDER, media_type=MediaType.PODCAST, name="Podcast"
        ),
        provider_mappings=_mapping(item_id),
    )


MEDIA_ITEMS: dict[str, Audiobook | PodcastEpisode | Track] = {
    "book_1": Audiobook(
        item_id="book_1",
        provider=PROVIDER,
        name="Book",
        duration=30000,
        provider_mappings=_mapping("book_1"),
    ),
    "episode_1": _episode("episode_1"),
    "episode_2": _episode("episode_2"),
    "track_1": Track(
        item_id="track_1",
        provider=PROVIDER,
        name="Track",
        duration=300,
        provider_mappings=_mapping("track_1"),
    ),
}


def _controller(userid: str | None, served: bool) -> PlayerQueuesController:
    items = [
        QueueItem(
            queue_id=QUEUE_ID,
            queue_item_id=item_id,
            name=media_item.name,
            duration=media_item.duration,
            media_item=media_item,
        )
        for item_id, media_item in MEDIA_ITEMS.items()
    ]
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Queue", available=True, items=len(items)
    )
    ctrl._queue_data = {
        QUEUE_ID: PlayerQueueData(
            queue=queue,
            items=items,
            userid=userid,
            served_item_ids=dict.fromkeys(MEDIA_ITEMS) if served else {},
        )
    }
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.streams.is_smart_fades_active.return_value = False
    return ctrl


def _play(
    ctrl: PlayerQueuesController,
    reports: tuple[tuple[int, PlaybackState, str, bool], ...],
) -> list[tuple[str, str, int, int]]:
    """
    Feed player reports as (clock offset, state, queue item, available).

    Returns the written sessions as (item_id, media_type, start offset, seconds).
    """
    start = time.time()
    clock = Mock(return_value=start)
    with patch("music_assistant.controllers.player_queues.playback_tracker.time.time", clock):
        for elapsed, (offset, state, queue_item_id, available) in enumerate(reports):
            clock.return_value = start + offset
            player = SimpleNamespace(
                player_id=QUEUE_ID,
                resolve_output_protocol=lambda queue_item_id=queue_item_id: SimpleNamespace(
                    current_media=SimpleNamespace(
                        source_id=QUEUE_ID, queue_item_id=queue_item_id, uri=None
                    )
                ),
                name="Queue",
                available=available,
                playback_state=state,
                # changes with every report, like a playing player's position does
                corrected_elapsed_time=elapsed,
                group_members=[],
            )
            ctrl._update_queue_from_player(player)  # type: ignore[arg-type]
    upsert = cast("Mock", ctrl.mass.music.database.upsert)
    return [
        (
            call.args[1]["item_id"],
            call.args[1]["media_type"],
            round(call.args[1]["timestamp"] - start),
            call.args[1]["seconds"],
        )
        for call in upsert.call_args_list
    ]


@pytest.mark.parametrize(
    ("userid", "served", "expected"),
    [
        (
            "user_1",
            True,
            [(0, 31), (0, 45), (100, 30), (100, 65), (6000, 20), (6200, 40)],
        ),
        (None, True, []),
        ("user_1", False, []),
    ],
)
def test_audiobook_playing_time_is_written_per_session(
    userid: str | None, served: bool, expected: list[tuple[int, int]]
) -> None:
    """Only time the player plays and reports counts, and a clock set back takes none off."""
    playing, paused = PlaybackState.PLAYING, PlaybackState.PAUSED
    sessions = _play(
        _controller(userid, served),
        (
            (0, playing, "book_1", True),
            (10, playing, "book_1", True),
            (31, playing, "book_1", True),
            (45, paused, "book_1", True),
            (100, playing, "book_1", True),
            (130, playing, "book_1", True),
            # the player stopped reporting while playing and only reports again hours later
            (5000, paused, "book_1", True),
            (6000, playing, "book_1", True),
            # an offline player keeps reporting that it plays
            (6020, playing, "book_1", False),
            (6100, playing, "book_1", False),
            (6200, playing, "book_1", True),
            # the system clock is set back
            (6150, playing, "book_1", True),
            (6170, playing, "book_1", True),
            (6190, paused, "book_1", True),
        ),
    )

    assert [(start, seconds) for _, _, start, seconds in sessions] == expected
    assert {(item_id, media_type) for item_id, media_type, _, _ in sessions} <= {
        ("book_1", "audiobook")
    }


def test_episodes_count_for_their_podcast_and_tracks_not_at_all() -> None:
    """Episodes of one podcast played in a row form one session of the podcast."""
    playing = PlaybackState.PLAYING
    sessions = _play(
        _controller("user_1", served=True),
        (
            (0, playing, "episode_1", True),
            (20, playing, "episode_2", True),
            (40, playing, "track_1", True),
            (100, playing, "track_1", True),
        ),
    )

    assert sessions == [("podcast_1", "podcast", 0, 40)]
