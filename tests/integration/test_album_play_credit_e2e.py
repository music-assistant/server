"""
End-to-end tests for the bookkeeping that credits an enqueued album as played.

Boots a hermetic MusicAssistant (fake `test` provider + demo players) and drives the real
``play_media`` path, so the wiring that arms and re-arms an album's credit is exercised
rather than the decision helper alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.enums import QueueOption

from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

from .conftest import demo_players, wait_for

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, ItemMapping, MediaItemType, Track


async def _album_with_track(mass: MusicAssistant, album_id: str) -> tuple[Album, Track]:
    """Return a fake-provider album together with one of its tracks."""
    test_prov = cast("MusicProvider", mass.get_provider("test"))
    assert test_prov is not None
    album = await test_prov.get_album(album_id)
    tracks = await test_prov.get_album_tracks(album_id)
    return album, tracks[0]


async def _play(mass: MusicAssistant, queue_id: str, album: Album, **kwargs: object) -> None:
    """Play (or enqueue) an album and wait until the queue has picked it up."""
    await mass.player_queues.play_media(
        queue_id,
        cast("MediaItemType | ItemMapping | str", album),
        **kwargs,  # type: ignore[arg-type]
    )
    assert await wait_for(
        lambda: (q := mass.player_queues.get(queue_id)) is not None and q.items > 0
    ), "album never reached the queue"


@pytest.mark.asyncio
async def test_album_credit_is_armed_and_rearmed_through_play_media(
    e2e_mass: MusicAssistant,
) -> None:
    """Playing an album arms its credit once; queueing it again arms it for another play."""
    queue_id = demo_players(e2e_mass)[0].player_id
    album, track = await _album_with_track(e2e_mass, "0_0")
    tracker = e2e_mass.player_queues

    await _play(e2e_mass, queue_id, album)
    queue_data = tracker.queue_data(queue_id)
    assert queue_data.credited_albums == set()

    # the first of the album's tracks to complete claims the credit
    assert tracker._claim_enqueued_album_credit(queue_data, track) is not None
    assert queue_data.credited_albums == {album}
    # every later track of the same album finds it already claimed
    assert tracker._claim_enqueued_album_credit(queue_data, track) is None

    # queueing the same album again is a new play of it
    await _play(e2e_mass, queue_id, album, option=QueueOption.ADD)
    assert queue_data.credited_albums == set()
    assert tracker._claim_enqueued_album_credit(queue_data, track) is not None


@pytest.mark.asyncio
async def test_album_credit_is_cleared_when_a_new_play_replaces_the_queue(
    e2e_mass: MusicAssistant,
) -> None:
    """A play that replaces the queue drops the credits recorded for the previous one."""
    queue_id = demo_players(e2e_mass)[1].player_id
    album, track = await _album_with_track(e2e_mass, "0_0")
    other_album, _ = await _album_with_track(e2e_mass, "1_0")
    tracker = e2e_mass.player_queues

    await _play(e2e_mass, queue_id, album)
    queue_data = tracker.queue_data(queue_id)
    assert tracker._claim_enqueued_album_credit(queue_data, track) is not None
    assert queue_data.credited_albums == {album}

    await _play(e2e_mass, queue_id, other_album)
    assert queue_data.credited_albums == set()


@pytest.mark.asyncio
async def test_album_credit_is_dropped_when_the_album_leaves_the_enqueued_list(
    e2e_mass: MusicAssistant,
) -> None:
    """A credit is forgotten once its album falls off the (capped) enqueued list."""
    queue_id = demo_players(e2e_mass)[2].player_id
    album, track = await _album_with_track(e2e_mass, "0_0")
    tracker = e2e_mass.player_queues

    await _play(e2e_mass, queue_id, album)
    queue_data = tracker.queue_data(queue_id)
    assert tracker._claim_enqueued_album_credit(queue_data, track) is not None
    assert queue_data.credited_albums == {album}

    # push the album out of the enqueued list, which holds the last 10 items
    for index in range(1, 12):
        other, _ = await _album_with_track(e2e_mass, f"{index}_0")
        await _play(e2e_mass, queue_id, other, option=QueueOption.ADD)

    assert album not in queue_data.enqueued_media_items
    assert queue_data.credited_albums == set()
