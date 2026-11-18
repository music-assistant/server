"""
End-to-end tests for the bookkeeping that credits an album play once.

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


async def _play(
    mass: MusicAssistant, queue_id: str, media: MediaItemType, **kwargs: object
) -> None:
    """Play (or enqueue) an item and wait until the queue has picked it up."""
    await mass.player_queues.play_media(
        queue_id,
        cast("MediaItemType | ItemMapping | str", media),
        **kwargs,  # type: ignore[arg-type]
    )
    assert await wait_for(
        lambda: (q := mass.player_queues.get(queue_id)) is not None and q.items > 0
    ), "item never reached the queue"


@pytest.mark.asyncio
async def test_album_credit_is_armed_and_rearmed_through_play_media(
    e2e_mass: MusicAssistant,
) -> None:
    """Playing an album arms its credit once; queueing it again arms it for another play."""
    queue_id = demo_players(e2e_mass)[0].player_id
    album, _ = await _album_with_track(e2e_mass, "0_0")
    tracker = e2e_mass.player_queues

    await _play(e2e_mass, queue_id, album)
    queue_data = tracker.queue_data(queue_id)
    first, second = queue_data.items[:2]
    assert queue_data.credited_albums == set()

    # the first of the album's tracks to complete claims the credit
    assert tracker._claim_album_credit(queue_data, first) is not None
    assert queue_data.credited_albums == {album.uri}
    # every later track of the same album finds it already claimed
    assert tracker._claim_album_credit(queue_data, second) is None

    # queueing the same album again is a new play of it
    await _play(e2e_mass, queue_id, album, option=QueueOption.ADD)
    assert queue_data.credited_albums == set()
    assert tracker._claim_album_credit(queue_data, queue_data.items[-1]) is not None


@pytest.mark.asyncio
async def test_album_credit_survives_a_play_that_keeps_its_tracks_queued(
    e2e_mass: MusicAssistant,
) -> None:
    """A track played in between leaves the album's remaining tracks credited."""
    queue_id = demo_players(e2e_mass)[1].player_id
    album, track = await _album_with_track(e2e_mass, "0_0")
    tracker = e2e_mass.player_queues

    await _play(e2e_mass, queue_id, album)
    queue_data = tracker.queue_data(queue_id)
    remaining = queue_data.items[-1]
    assert tracker._claim_album_credit(queue_data, queue_data.items[0]) is not None

    # a play of a single track goes in before the rest of the album
    await _play(e2e_mass, queue_id, track, option=QueueOption.PLAY)
    assert remaining in queue_data.items
    assert tracker._claim_album_credit(queue_data, remaining) is None


@pytest.mark.asyncio
async def test_album_credit_is_dropped_once_its_tracks_left_the_queue(
    e2e_mass: MusicAssistant,
) -> None:
    """A play that replaces the queue lets the next credit drop the previous album's one."""
    queue_id = demo_players(e2e_mass)[2].player_id
    album, _ = await _album_with_track(e2e_mass, "0_0")
    other_album, _ = await _album_with_track(e2e_mass, "1_0")
    tracker = e2e_mass.player_queues

    await _play(e2e_mass, queue_id, album)
    queue_data = tracker.queue_data(queue_id)
    assert tracker._claim_album_credit(queue_data, queue_data.items[0]) is not None

    await _play(e2e_mass, queue_id, other_album, option=QueueOption.REPLACE)
    assert tracker._claim_album_credit(queue_data, queue_data.items[0]) is not None
    assert queue_data.credited_albums == {other_album.uri}
