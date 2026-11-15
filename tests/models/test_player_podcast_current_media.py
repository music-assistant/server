"""Tests that a player's current media names the podcast once for a podcast episode."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, PodcastEpisode, ProviderMapping
from music_assistant_models.queue_item import QueueItem

from music_assistant.models.player import Player
from tests.common import MockProvider, attach_mock, mock_player_mass

PLAYER_ID = "player_1"


def _player_playing(queue_item: QueueItem) -> Player:
    """Create a player whose MA queue is playing the given item."""
    mass = mock_player_mass()
    queue = MagicMock()
    queue.queue_id = PLAYER_ID
    queue.current_item = queue_item
    queue.elapsed_time = 0
    queue.elapsed_time_last_updated = 0
    mass.player_queues.get = MagicMock(return_value=queue)
    mass.metadata.get_image_url = MagicMock(return_value=None)
    player, _ = attach_mock(MockProvider("test_provider", mass=mass), PLAYER_ID, "Player 1")
    player.set_active_mass_source(PLAYER_ID)
    return player


def test_podcast_episode_reports_podcast_as_artist_only() -> None:
    """The podcast name fills the artist and is not repeated as the album."""
    episode = PodcastEpisode(
        item_id="ep1",
        provider="test",
        name="Episode 1",
        position=1,
        duration=300,
        podcast=ItemMapping(
            media_type=MediaType.PODCAST, item_id="pod1", provider="test", name="My Podcast"
        ),
        provider_mappings={
            ProviderMapping(item_id="ep1", provider_domain="test", provider_instance="test")
        },
    )
    queue_item = QueueItem(
        queue_id=PLAYER_ID,
        queue_item_id="item1",
        name=episode.name,
        duration=300,
        media_item=episode,
    )
    player = _player_playing(queue_item)

    player.update_state(signal_event=False)

    media = player.current_media
    assert media is not None
    assert media.title == "Episode 1"
    assert media.artist == "My Podcast"
    assert media.album is None
