"""
End-to-end tests for the bounded managed radio pool through the real queue controller + DB.

Boots a hermetic MusicAssistant (fake `test` music provider + demo players) and exercises:
- a radio-flagged enqueue turning the whole queue into a small bounded pool (not the whole library);
- a refill drawing from the queue's dynamic sources, weighted per source (multiplicity) and
  hard-gated against the playlog recency windows.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.controllers.player_queues.constants import (
    MANAGED_POOL_MAX,
    MANAGED_POOL_TARGET,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

from .conftest import demo_players, wait_for

if TYPE_CHECKING:
    from music_assistant_models.media_items import ItemMapping, MediaItemType

HOUR = 3600
DAY = 24 * HOUR
TEST_USER = "e2e-user"


async def _insert_play(mass: MusicAssistant, item_id: str, timestamp: int, userid: str) -> None:
    """Insert a fully-played track row into the playlog for the given user."""
    await mass.music.database.insert(
        DB_TABLE_PLAYLOG,
        {
            "item_id": item_id,
            "provider": "test",
            "media_type": MediaType.TRACK.value,
            "name": f"Track {item_id}",
            "timestamp": timestamp,
            "fully_played": True,
            "seconds_played": 60,
            "userid": userid,
            "user_initiated": True,
        },
    )


@pytest.mark.asyncio
async def test_radio_enqueue_builds_bounded_pool(e2e_mass: MusicAssistant) -> None:
    """A radio-flagged enqueue turns the queue into a small bounded pool, not the whole library."""
    queue_id = demo_players(e2e_mass)[0].player_id
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    assert test_prov is not None
    seed = await test_prov.get_track("0_0_0")

    await e2e_mass.player_queues.play_media(
        queue_id, cast("MediaItemType | ItemMapping | str", seed), radio_mode=True
    )

    assert await wait_for(lambda: len(e2e_mass.player_queues.items(queue_id)) > 1), (
        "radio pool never populated"
    )
    items = e2e_mass.player_queues.items(queue_id)
    # bounded: a single-track radio yields ~target items, never the full 500-track library
    assert 1 < len(items) <= MANAGED_POOL_MAX
    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    assert queue.radio_source  # the seed is kept as a dynamic source


@pytest.mark.asyncio
async def test_refill_gates_recent_and_weights_by_multiplicity(e2e_mass: MusicAssistant) -> None:
    """A managed-pool refill excludes recently-played tracks and favours the higher-multiplicity source."""
    queue_id = demo_players(e2e_mass)[0].player_id
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    assert test_prov is not None
    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    queue.userid = TEST_USER

    # two albums (20 tracks each) as TRACKS sources; the second is added twice (multiplicity 2)
    album_single = await test_prov.get_album("0_0")  # tracks 0_0_0 .. 0_0_19
    album_double = await test_prov.get_album("1_0")  # tracks 1_0_0 .. 1_0_19
    queue.radio_source = cast("list[MediaItemType]", [album_single, album_double, album_double])
    e2e_mass.player_queues._managed_pool.register(queue_id, queue.radio_source, set(), replace=True)

    # singleton album: 5 tracks played a day ago -> within the 1-week song window -> hard-gated
    now = int(time.time())
    gated = [f"0_0_{i}" for i in range(5)]
    for item_id in gated:
        await _insert_play(e2e_mass, item_id, now - DAY, TEST_USER)
    # duplicated album: 5 tracks played 5h ago -> outside the 3h repeat-gap -> allowed
    for i in range(5):
        await _insert_play(e2e_mass, f"1_0_{i}", now - 5 * HOUR, TEST_USER)

    pool = await e2e_mass.player_queues._managed_pool.fill(queue_id, is_initial=True)
    ids = [track.item_id for track in pool]

    assert 0 < len(pool) <= MANAGED_POOL_TARGET
    # recency hard gate: the within-window singleton tracks are excluded entirely
    assert not any(item_id in ids for item_id in gated)
    # per-base quota: the album added twice contributes more than the one added once
    single_count = sum(1 for tid in ids if tid.startswith("0_0_"))
    double_count = sum(1 for tid in ids if tid.startswith("1_0_"))
    assert double_count > single_count


@pytest.mark.asyncio
async def test_refill_dedupes_only_active_tail_not_played_history(e2e_mass: MusicAssistant) -> None:
    """A track in the played history can return on refill; only the current+unplayed tail is deduped."""
    queue_id = demo_players(e2e_mass)[0].player_id
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    assert test_prov is not None
    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    queue.userid = TEST_USER

    album = await test_prov.get_album("0_0")  # tracks 0_0_0 .. 0_0_19
    queue.radio_source = cast("list[MediaItemType]", [album])
    e2e_mass.player_queues._managed_pool.register(queue_id, queue.radio_source, set(), replace=True)

    # simulate a queue with played history: 0_0_0..0_0_4 already played, 0_0_5 is the current track
    queue_items = [
        QueueItem.from_media_item(queue_id, await test_prov.get_track(f"0_0_{i}")) for i in range(6)
    ]
    e2e_mass.player_queues._queue_items[queue_id] = queue_items
    queue.current_index = 5

    pool = await e2e_mass.player_queues._managed_pool.fill(queue_id, is_initial=False)
    ids = [track.item_id for track in pool]

    # the active (current) track isn't duplicated into the upcoming pool
    assert "0_0_5" not in ids
    # but already-played history is not permanently excluded (no recency block in play here)
    assert any(f"0_0_{i}" in ids for i in range(5))
