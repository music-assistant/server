"""Tests that PlayerMedia sent to a player names the podcast for a podcast episode."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    ItemMapping,
    PodcastEpisode,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"


def _controller() -> PlayerQueuesController:
    """Build a bare controller with a single queue."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue_data = PlayerQueueData(queue=MagicMock())
    queue_data.session_id = "session1"
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.mass = MagicMock()
    return ctrl


def _queue_item(media_item: PodcastEpisode | Track) -> QueueItem:
    """Wrap a media item in a queue item."""
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id="item1",
        name=media_item.name,
        duration=300,
        media_item=media_item,
    )


def _mappings(item_id: str) -> set[ProviderMapping]:
    """Return a single provider mapping for a test item."""
    return {ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")}


async def test_podcast_episode_uses_podcast_name_as_artist() -> None:
    """A podcast episode has no artists, so the podcast name fills the artist only."""
    episode = PodcastEpisode(
        item_id="ep1",
        provider="test",
        name="Episode 1",
        position=1,
        duration=300,
        podcast=ItemMapping(
            media_type=MediaType.PODCAST, item_id="pod1", provider="test", name="My Podcast"
        ),
        provider_mappings=_mappings("ep1"),
    )

    media = await _controller().player_media_from_queue_item(_queue_item(episode))

    assert media.title == "Episode 1"
    assert media.artist == "My Podcast"
    assert media.album == ""


async def test_track_keeps_its_own_artist_and_album() -> None:
    """A track still reports its own artists and album."""
    track = Track(
        item_id="t1",
        provider="test",
        name="Song",
        duration=300,
        artists=UniqueList(
            [ItemMapping(media_type=MediaType.ARTIST, item_id="a1", provider="test", name="Band")]
        ),
        album=ItemMapping(media_type=MediaType.ALBUM, item_id="al1", provider="test", name="LP"),
        provider_mappings=_mappings("t1"),
    )

    media = await _controller().player_media_from_queue_item(_queue_item(track))

    assert media.artist == "Band"
    assert media.album == "LP"
