"""
End-to-end tests for a queue entering "dynamic mode" through the real queue controller.

Boots a hermetic MusicAssistant (fake `test` provider + demo players) and exercises the
source-recording and finite->dynamic transition: playing a finite source records it as a source
even while the queue stays linear, and introducing a dynamic source rebuilds the upcoming tail
into a bounded, smart-shuffled managed pool over ALL the queue's sources. Shuffle and repeat are
locked (implicit) while the queue is dynamic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.enums import QueueOption, RepeatMode
from music_assistant_models.errors import InvalidCommand
from music_assistant_models.media_items import Playlist

from music_assistant.controllers.player_queues import managed_pool
from music_assistant.controllers.player_queues.constants import MANAGED_POOL_MAX
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.radio_playlist import radio_playlist_uri

from .conftest import demo_players, wait_for

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, ItemMapping, MediaItemType


async def _play_finite_album(mass: MusicAssistant, queue_id: str, album_id: str = "0_0") -> Album:
    """Play a finite album linearly and wait until it is the active (non-dynamic) queue."""
    test_prov = cast("MusicProvider", mass.get_provider("test"))
    assert test_prov is not None
    album = await test_prov.get_album(album_id)  # 20 tracks: <album_id>_0 .. <album_id>_19
    await mass.player_queues.play_media(queue_id, cast("MediaItemType | ItemMapping | str", album))
    assert await wait_for(
        lambda: (q := mass.player_queues.get(queue_id)) is not None and q.current_index is not None
    ), "album never started playing"
    return album


async def _add_dynamic_playlist(mass: MusicAssistant, queue_id: str, seed_id: str = "4_4_0") -> str:
    """Add a dynamic radio playlist (seeded from a track) to the queue and return its uri."""
    test_prov = cast("MusicProvider", mass.get_provider("test"))
    assert test_prov is not None
    seed = await test_prov.get_track(seed_id)
    uri = radio_playlist_uri(seed)
    await mass.player_queues.play_media(queue_id, uri, option=QueueOption.ADD)
    return uri


@pytest.mark.asyncio
async def test_finite_source_recorded_even_while_linear(e2e_mass: MusicAssistant) -> None:
    """Playing a finite album records it as a source, but the queue stays linear (not dynamic)."""
    queue_id = demo_players(e2e_mass)[0].player_id
    album = await _play_finite_album(e2e_mass, queue_id)

    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    # a finite-only queue is NOT dynamic even though its source is recorded ...
    assert queue.is_dynamic is False
    # ... and the whole album is enqueued linearly (no bounded pool yet)
    assert len(e2e_mass.player_queues.items(queue_id)) == 20
    # the finite source is recorded server-side and projected onto the wire `sources`
    data = e2e_mass.player_queues._queue_data[queue_id]
    assert [item.uri for item in data.source_items] == [album.uri]
    assert [mapping.uri for mapping in queue.sources] == [album.uri]


@pytest.mark.asyncio
async def test_source_items_records_finite_and_dynamic(e2e_mass: MusicAssistant) -> None:
    """Adding a dynamic playlist to a finite queue records BOTH the finite and dynamic sources."""
    queue_id = demo_players(e2e_mass)[0].player_id
    album = await _play_finite_album(e2e_mass, queue_id)
    await _add_dynamic_playlist(e2e_mass, queue_id)

    data = e2e_mass.player_queues._queue_data[queue_id]
    source_uris = {item.uri for item in data.source_items}
    # the finite album is still a source, now mixed with the dynamic playlist
    assert album.uri in source_uris
    assert any(isinstance(item, Playlist) and item.is_dynamic for item in data.source_items)


@pytest.mark.asyncio
async def test_finite_then_dynamic_rebuilds_tail_into_bounded_pool(
    e2e_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a dynamic source collapses the finite tail into a bounded mix of all sources."""
    # shrink the pool target so the collapse is visible against a 20-track album
    monkeypatch.setattr(managed_pool, "MANAGED_POOL_TARGET", 10)
    queue_id = demo_players(e2e_mass)[0].player_id
    await _play_finite_album(e2e_mass, queue_id)
    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    assert queue.current_item is not None
    playing_id = queue.current_item.media_item.item_id if queue.current_item.media_item else None
    assert playing_id == "0_0_0"

    await _add_dynamic_playlist(e2e_mass, queue_id)

    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    assert queue.current_index is not None
    # the queue is now a managed pool ...
    assert queue.is_dynamic is True
    items = e2e_mass.player_queues.items(queue_id)
    tail_ids = [
        item.media_item.item_id for item in items[queue.current_index + 1 :] if item.media_item
    ]
    # ... whose upcoming tail is bounded, not the whole 20-track album left in front of the pool
    assert 0 < len(tail_ids) <= MANAGED_POOL_MAX
    all_ids = [item.media_item.item_id for item in items if item.media_item]
    assert len([tid for tid in all_ids if tid.startswith("0_0_")]) < 20
    # the current track is kept playing across the rebuild
    assert queue.current_item is not None
    assert queue.current_item.media_item is not None
    assert queue.current_item.media_item.item_id == "0_0_0"
    # the bounded mix draws from BOTH the finite album (0_0_*) and the dynamic playlist (others)
    assert any(tid.startswith("0_0_") for tid in tail_ids)
    assert any(not tid.startswith("0_0_") for tid in tail_ids)


@pytest.mark.asyncio
async def test_entering_dynamic_mode_enables_smart_shuffle(e2e_mass: MusicAssistant) -> None:
    """Transitioning into dynamic mode auto-enables (implicit) shuffle + smart shuffle."""
    queue_id = demo_players(e2e_mass)[0].player_id
    await _play_finite_album(e2e_mass, queue_id)
    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    # a plain linear album starts with shuffle off and smart shuffle inactive
    assert queue.shuffle_enabled is False
    assert queue.smart_shuffle_active is False

    await _add_dynamic_playlist(e2e_mass, queue_id)

    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    assert queue.is_dynamic is True
    # dynamic mode is an always-on smart mix; the state reflects that
    assert queue.shuffle_enabled is True
    assert queue.smart_shuffle_active is True


@pytest.mark.asyncio
async def test_replace_next_dynamic_keeps_current_track_playing(e2e_mass: MusicAssistant) -> None:
    """REPLACE_NEXT with a dynamic source rebuilds the tail without interrupting the current track."""
    queue_id = demo_players(e2e_mass)[0].player_id
    await _play_finite_album(e2e_mass, queue_id)
    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    assert queue.current_item is not None
    assert queue.current_item.media_item is not None
    playing_before = queue.current_item.media_item.item_id

    test_prov = cast("MusicProvider", e2e_mass.get_provider("test"))
    assert test_prov is not None
    seed = await test_prov.get_track("4_4_0")
    await e2e_mass.player_queues.play_media(
        queue_id, radio_playlist_uri(seed), option=QueueOption.REPLACE_NEXT
    )

    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    assert queue.is_dynamic is True
    # the current track keeps playing across the tail rebuild (REPLACE_NEXT does not jump playback)
    assert queue.current_item is not None
    assert queue.current_item.media_item is not None
    assert queue.current_item.media_item.item_id == playing_before
    # the upcoming tail was replaced by the managed pool
    assert queue.current_index is not None
    assert len(e2e_mass.player_queues.items(queue_id)) > queue.current_index + 1


@pytest.mark.asyncio
async def test_shuffle_and_repeat_locked_while_dynamic(e2e_mass: MusicAssistant) -> None:
    """Shuffle and repeat toggles are rejected while the queue is in dynamic mode."""
    queue_id = demo_players(e2e_mass)[0].player_id
    await _play_finite_album(e2e_mass, queue_id)
    await _add_dynamic_playlist(e2e_mass, queue_id)
    queue = e2e_mass.player_queues.get(queue_id)
    assert queue is not None
    assert queue.is_dynamic is True

    with pytest.raises(InvalidCommand):
        await e2e_mass.player_queues.set_shuffle(queue_id, False)
    with pytest.raises(InvalidCommand):
        await e2e_mass.player_queues.set_repeat(queue_id, RepeatMode.ALL)
