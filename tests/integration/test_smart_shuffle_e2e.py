"""
End-to-end test for smart shuffle through the real queue controller + DB.

Boots a hermetic MusicAssistant (fake `test` music provider + demo players), enqueues a set of
deterministic tracks, marks some as recently played for the queue's user in the playlog, enables the
per-queue smart-shuffle config, and asserts that toggling shuffle pushes the recently-played tracks
behind the fresh ones — exercising config read -> recency engine query -> arrange -> queue order.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.constants import CONF_PLAYER_QUEUES, CONF_VALUE_ENABLED, DB_TABLE_PLAYLOG
from music_assistant.controllers.player_queues.constants import (
    CONF_SMART_SHUFFLE_ARTIST_RECENCY,
    CONF_SMART_SHUFFLE_DUPLICATE_GAP,
    CONF_SMART_SHUFFLE_ENABLED,
    CONF_SMART_SHUFFLE_SONG_RECENCY,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

from .conftest import demo_players, wait_for

if TYPE_CHECKING:
    from music_assistant_models.media_items import ItemMapping, MediaItemType

WEEK = 7 * 24 * 3600
TEST_USER = "e2e-user"


@pytest.mark.asyncio
async def test_smart_shuffle_pushes_recently_played_to_back(e2e_mass: MusicAssistant) -> None:
    """With smart shuffle on, recently-played tracks are ordered behind fresh ones."""
    queue_id = demo_players(e2e_mass)[0].player_id
    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    assert test_prov is not None

    # 9 distinct tracks (distinct artists -> artist tier won't interfere)
    tracks = [await test_prov.get_track(f"{i}_0_0") for i in range(9)]
    await e2e_mass.player_queues.play_media(
        queue_id, cast("list[MediaItemType | ItemMapping | str]", tracks)
    )

    def _playing() -> bool:
        queue = e2e_mass.player_queues.get(queue_id)
        return queue is not None and queue.current_index is not None

    assert await wait_for(_playing), "queue never started playing"

    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    # scope the recency window to a known user (no session user exists under test)
    e2e_mass.player_queues.queue_data(queue_id).userid = TEST_USER

    # mark the later half of the tracks as recently played for this user
    recent_tracks = tracks[5:]
    fresh_tracks = tracks[:5]
    now = int(time.time())
    for track in recent_tracks:
        await e2e_mass.music.database.insert(
            DB_TABLE_PLAYLOG,
            {
                "item_id": track.item_id,
                "provider": track.provider,
                "media_type": MediaType.TRACK.value,
                "name": track.name,
                "timestamp": now,
                "fully_played": True,
                "seconds_played": track.duration or 180,
                "userid": TEST_USER,
                "user_initiated": True,
            },
        )

    # recency windows are a global (queue controller) setting: long song window, artist/duplicate
    # windows off so only song recency drives the ordering
    await e2e_mass.config.save_core_config(
        CONF_PLAYER_QUEUES,
        {
            CONF_SMART_SHUFFLE_SONG_RECENCY: str(WEEK),
            CONF_SMART_SHUFFLE_ARTIST_RECENCY: "0",
            CONF_SMART_SHUFFLE_DUPLICATE_GAP: "0",
        },
    )
    # enable smart shuffle on the queue
    await e2e_mass.config.save_player_queue_config(
        queue_id,
        {CONF_SMART_SHUFFLE_ENABLED: CONF_VALUE_ENABLED},
    )

    await e2e_mass.player_queues.set_shuffle(queue_id, True)

    # only the items after the playing/buffered index are reordered
    cur = queue.index_in_buffer if queue.index_in_buffer is not None else queue.current_index
    assert cur is not None
    order = {
        item.media_item.item_id: index
        for index, item in enumerate(e2e_mass.player_queues.items(queue_id))
        if item.media_item is not None
    }
    recent_positions = [order[t.item_id] for t in recent_tracks if order.get(t.item_id, -1) > cur]
    fresh_positions = [order[t.item_id] for t in fresh_tracks if order.get(t.item_id, -1) > cur]

    assert recent_positions, "recently-played tracks missing from the reordered tail"
    assert fresh_positions, "fresh tracks missing from the reordered tail"
    # every recently-played track sits behind every fresh track in the reordered region
    assert max(fresh_positions) < min(recent_positions)
