"""Tests that autoplay's similar-track refill skips songs already in the queue."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"
LOADER = "music_assistant.controllers.player_queues.queue_loader"


def _track(
    item_id: str, provider: str, mappings: list[tuple[str, str]], domain: str = "apple_music"
) -> Track:
    """Build a track with the given (provider_instance, item_id) mappings."""
    return Track(
        item_id=item_id,
        provider=provider,
        name=item_id,
        provider_mappings={
            ProviderMapping(
                item_id=prov_item_id,
                provider_domain=domain,
                provider_instance=prov_instance,
            )
            for prov_instance, prov_item_id in mappings
        },
    )


async def _similar_tracks(queued: Track, similar: list[Track]) -> list[Track]:
    """Run the similar-tracks refill for a queue holding a single track."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=1)
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = [
        QueueItem(queue_id=QUEUE_ID, queue_item_id="a", name="a", duration=180, media_item=queued)
    ]
    queue_data.source_items = [queued]
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.logger = MagicMock()
    radio_prov = MagicMock()
    radio_prov.get_dynamic_tracks = AsyncMock(return_value=similar)
    ctrl.mass = MagicMock()
    ctrl.mass.get_provider.return_value = radio_prov
    with patch(f"{LOADER}.playback_sources", AsyncMock(return_value=(None, None))):
        return await ctrl._get_similar_tracks(QUEUE_ID, is_initial=True)


async def test_similar_track_matching_a_queued_library_mapping_is_skipped() -> None:
    """A provider track whose id maps to a queued library track must not be queued again."""
    library_track = _track("12", "library", [("apple_music--x", "am-1")])
    same_song = _track("am-1", "apple_music--x", [("apple_music--x", "am-1")])
    new_song = _track("am-2", "apple_music--x", [("apple_music--x", "am-2")])
    tracks = await _similar_tracks(library_track, [same_song, new_song])
    assert [t.item_id for t in tracks] == ["am-2"]


async def test_same_id_on_another_instance_of_the_domain_is_kept() -> None:
    """Item ids are only unique per provider instance, e.g. relative paths on two file shares."""
    queued = _track("song.mp3", "fs--a", [("fs--a", "song.mp3")], domain="filesystem_local")
    other = _track("song.mp3", "fs--b", [("fs--b", "song.mp3")], domain="filesystem_local")
    tracks = await _similar_tracks(queued, [other])
    assert tracks == [other]
