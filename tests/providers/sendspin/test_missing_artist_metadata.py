"""Tests for Sendspin metadata with stale library references."""

from unittest.mock import PropertyMock, patch

from music_assistant_models.media_items import Artist, Track
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.mass import MusicAssistant
from music_assistant.providers.sendspin.player import SendspinPlayer


async def test_missing_library_artist_is_ignored(mass: MusicAssistant) -> None:
    """A stale artist reference does not fail a Sendspin metadata task."""
    player = object.__new__(SendspinPlayer)
    player.mass = mass
    player.last_sent_artist_artwork_url = None
    artist = Artist(
        item_id="27",
        provider="library",
        name="Missing artist",
        provider_mappings=set(),
    )
    track = Track(
        item_id="track",
        provider="library",
        name="Track",
        provider_mappings=set(),
        artists=UniqueList([artist]),
    )
    queue_item = QueueItem.from_media_item("queue", track)

    with patch.object(SendspinPlayer, "_artwork_role", new_callable=PropertyMock) as artwork_role:
        artwork_role.return_value = None
        task = mass.create_task(player._send_artist_artwork(queue_item))
        await task
