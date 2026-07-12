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
from music_assistant_models.media_items import Playlist
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.controllers.player_queues import managed_pool
from music_assistant.controllers.player_queues.constants import (
    MANAGED_POOL_MAX,
    MANAGED_POOL_TARGET,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.radio_playlist import radio_playlist_uri

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
    assert queue.sources  # the seed is kept as a dynamic source


@pytest.mark.asyncio
async def test_refill_gates_recent_and_weights_by_multiplicity(e2e_mass: MusicAssistant) -> None:
    """A managed-pool refill excludes recently-played tracks and favours the higher-multiplicity source."""
    queue_id = demo_players(e2e_mass)[0].player_id
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    assert test_prov is not None
    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    e2e_mass.player_queues.queue_data(queue_id).userid = TEST_USER

    # two albums (20 tracks each) as TRACKS sources; the second is added twice (multiplicity 2)
    album_single = await test_prov.get_album("0_0")  # tracks 0_0_0 .. 0_0_19
    album_double = await test_prov.get_album("1_0")  # tracks 1_0_0 .. 1_0_19
    sources = cast("list[MediaItemType]", [album_single, album_double, album_double])
    e2e_mass.player_queues.queue_data(queue_id).source_items = sources

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
    e2e_mass.player_queues.queue_data(queue_id).userid = TEST_USER

    album = await test_prov.get_album("0_0")  # tracks 0_0_0 .. 0_0_19
    sources = cast("list[MediaItemType]", [album])
    e2e_mass.player_queues.queue_data(queue_id).source_items = sources

    # simulate a queue with played history: 0_0_0..0_0_4 already played, 0_0_5 is the current track
    queue_items = [
        QueueItem.from_media_item(queue_id, await test_prov.get_track(f"0_0_{i}")) for i in range(6)
    ]
    e2e_mass.player_queues.queue_data(queue_id).items = queue_items
    queue.current_index = 5

    pool = await e2e_mass.player_queues._managed_pool.fill(queue_id, is_initial=False)
    ids = [track.item_id for track in pool]

    # the active (current) track isn't duplicated into the upcoming pool
    assert "0_0_5" not in ids
    # but already-played history is not permanently excluded (no recency block in play here)
    assert any(f"0_0_{i}" in ids for i in range(5))


@pytest.mark.asyncio
async def test_finite_source_materializes_and_plays_through_once(
    e2e_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finite source is materialized once and dequeued progressively until it is exhausted."""
    # shrink the per-refill target so a single 20-track album is dequeued over several refills
    monkeypatch.setattr(managed_pool, "MANAGED_POOL_TARGET", 5)
    queue_id = demo_players(e2e_mass)[0].player_id
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    assert test_prov is not None
    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    e2e_mass.player_queues.queue_data(queue_id).userid = TEST_USER

    album = await test_prov.get_album("0_0")  # tracks 0_0_0 .. 0_0_19
    e2e_mass.player_queues.queue_data(queue_id).source_items = cast("list[MediaItemType]", [album])

    played: list[str] = []
    fills = 0
    # generous guard; a 20-track album drains in ~4 refills at the patched target of 5
    while e2e_mass.player_queues.queue_data(queue_id).source_items and fills < 20:
        pool = await e2e_mass.player_queues._managed_pool.fill(queue_id, is_initial=(fills == 0))
        played += [track.item_id for track in pool]
        fills += 1

    # progressive: it took several refills, not one dump of the whole album
    assert fills > 1
    # plays through once: every album track exactly once, never recycled as tracks age out
    assert sorted(played) == sorted(f"0_0_{i}" for i in range(20))
    # exhausted: the source is retired from the queue and its materialized state is released
    assert not e2e_mass.player_queues.queue_data(queue_id).source_items
    assert queue_id not in e2e_mass.player_queues._managed_pool._materialized


@pytest.mark.asyncio
async def test_recency_denied_track_rotates_to_back_not_dropped(e2e_mass: MusicAssistant) -> None:
    """A recency-denied finite-source track is kept for a fair second chance, not dropped."""
    queue_id = demo_players(e2e_mass)[0].player_id
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    assert test_prov is not None
    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    e2e_mass.player_queues.queue_data(queue_id).userid = TEST_USER

    album = await test_prov.get_album("0_0")  # tracks 0_0_0 .. 0_0_19
    uri = album.uri
    assert uri is not None
    e2e_mass.player_queues.queue_data(queue_id).source_items = cast("list[MediaItemType]", [album])

    # 5 tracks played a day ago -> within the 1-week song window -> recency-denied this round
    now = int(time.time())
    denied = [f"0_0_{i}" for i in range(5)]
    for item_id in denied:
        await _insert_play(e2e_mass, item_id, now - DAY, TEST_USER)

    pool = await e2e_mass.player_queues._managed_pool.fill(queue_id, is_initial=True)
    ids = [track.item_id for track in pool]

    # the denied tracks are not handed to the queue this round ...
    assert not any(item_id in ids for item_id in denied)
    mat = e2e_mass.player_queues._managed_pool._materialized[queue_id][uri]
    remaining = [track.item_id for track in mat.tracks]
    # ... but they are kept (rotated to the back of the deque), not dropped
    assert remaining[-len(denied) :] == denied
    # the 15 playable tracks were dispatched; the source is not yet exhausted
    assert mat.dispatched == {f"0_0_{i}" for i in range(5, 20)}
    assert e2e_mass.player_queues.queue_data(queue_id).source_items

    # once the recency block lifts, the held tracks get their second chance and the source exhausts
    await e2e_mass.music.database.delete(
        DB_TABLE_PLAYLOG, {"provider": "test", "userid": TEST_USER}
    )
    pool2 = await e2e_mass.player_queues._managed_pool.fill(queue_id, is_initial=False)
    ids2 = sorted(track.item_id for track in pool2)

    assert ids2 == sorted(denied)
    assert not e2e_mass.player_queues.queue_data(queue_id).source_items


@pytest.mark.asyncio
async def test_large_finite_source_is_paged_and_bounded(
    e2e_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source larger than the per-source cap is paged in as it drains, bounding internal state."""
    # a cap far below the album size forces paging: the deque must never hold the whole album
    monkeypatch.setattr(managed_pool, "MANAGED_POOL_SOURCE_CAP", 5)
    queue_id = demo_players(e2e_mass)[0].player_id
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    assert test_prov is not None
    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    e2e_mass.player_queues.queue_data(queue_id).userid = TEST_USER

    album = await test_prov.get_album("0_0")  # 20 tracks, well over the patched cap of 5
    uri = album.uri
    assert uri is not None
    e2e_mass.player_queues.queue_data(queue_id).source_items = cast("list[MediaItemType]", [album])

    played: list[str] = []
    max_held = 0
    fills = 0
    while e2e_mass.player_queues.queue_data(queue_id).source_items and fills < 20:
        pool = await e2e_mass.player_queues._managed_pool.fill(queue_id, is_initial=False)
        played += [track.item_id for track in pool]
        if mat := e2e_mass.player_queues._managed_pool._materialized.get(queue_id, {}).get(uri):
            max_held = max(max_held, len(mat.tracks))
        fills += 1

    # bounded: the materialized deque never held more than the per-source cap at once
    assert max_held <= 5
    # paged correctly: every track played exactly once despite being fetched in chunks
    assert sorted(played) == sorted(f"0_0_{i}" for i in range(20))
    assert not e2e_mass.player_queues.queue_data(queue_id).source_items


@pytest.mark.asyncio
async def test_dynamic_source_is_not_materialized(e2e_mass: MusicAssistant) -> None:
    """A dynamic-playlist source self-manages and is never materialized; only finite ones are."""
    queue_id = demo_players(e2e_mass)[0].player_id
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    assert test_prov is not None
    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    e2e_mass.player_queues.queue_data(queue_id).userid = TEST_USER

    # a dynamic radio playlist (self-managing) mixed with a finite album (materialized)
    seed = await test_prov.get_track("4_4_0")
    radio = await e2e_mass.music.get_item_by_uri(radio_playlist_uri(seed))
    assert isinstance(radio, Playlist)
    assert radio.is_dynamic
    album = await test_prov.get_album("0_0")
    e2e_mass.player_queues.queue_data(queue_id).source_items = cast(
        "list[MediaItemType]", [radio, album]
    )

    pool = await e2e_mass.player_queues._managed_pool.fill(queue_id, is_initial=False)

    assert pool  # both sources feed the bounded pool
    materialized = e2e_mass.player_queues._managed_pool._materialized.get(queue_id, {})
    assert album.uri in materialized  # the finite source is materialized into a deque
    assert radio.uri not in materialized  # the dynamic playlist is left to self-manage


@pytest.mark.asyncio
async def test_progressive_loading_returns_first_batch_then_fills(
    e2e_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Progressive loading (use_first_batch=True) returns initial candidates without blocking on full target."""
    monkeypatch.setattr(managed_pool, "MANAGED_POOL_SOURCE_CAP", 10)
    queue_id = demo_players(e2e_mass)[0].player_id
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    assert test_prov is not None
    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    e2e_mass.player_queues.queue_data(queue_id).userid = TEST_USER

    album = await test_prov.get_album("0_0")  # 20 tracks
    e2e_mass.player_queues.queue_data(queue_id).source_items = cast("list[MediaItemType]", [album])

    # use_first_batch=True returns available candidates capped at SOURCE_CAP (10), not TARGET (25)
    first_batch = await e2e_mass.player_queues._managed_pool.fill(
        queue_id, is_initial=False, use_first_batch=True
    )
    assert len(first_batch) == 10  # limited by MANAGED_POOL_SOURCE_CAP

    # normal fill would return more if the source allows
    e2e_mass.player_queues._managed_pool.forget(queue_id)
    normal_fill = await e2e_mass.player_queues._managed_pool.fill(
        queue_id, is_initial=True, use_first_batch=False
    )
    # with is_initial=True and SOURCE_CAP=10, we get up to 10 (still capped by source cap)
    assert len(normal_fill) == 10
