"""Tests for where the Sendspin player publishes the queue's repeat and shuffle state."""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiosendspin.models.types import RepeatMode as SendspinRepeatMode
from music_assistant_models.enums import MediaType, PlaybackState, RepeatMode
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.sendspin.player import SendspinPlayer


async def test_repeat_and_shuffle_reach_clients_through_the_controller_only() -> None:
    """The metadata object carries no repeat/shuffle; the controller state does."""
    player = MagicMock()
    player.available = True
    player.state = SimpleNamespace(
        current_media=PlayerMedia(
            uri="track-1",
            media_type=MediaType.TRACK,
            title="Title",
            duration=180,
            source_id="queue-1",
            queue_item_id="item-1",
        ),
        playback_state=PlaybackState.PLAYING,
    )
    player.mass.player_queues.get.return_value = SimpleNamespace(
        repeat_mode=RepeatMode.ALL, shuffle_enabled=True
    )
    player.mass.player_queues.get_item.return_value = None
    player._send_album_artwork = AsyncMock()
    player._send_beat_schedule = AsyncMock()
    player._compute_track_progress_ms.return_value = 1000
    player._color_role = None
    player._publish_repeat_shuffle = partial(SendspinPlayer._publish_repeat_shuffle, player)

    await SendspinPlayer.send_current_media_metadata(player)

    metadata = player._metadata_role.set_metadata.call_args.args[0]
    assert metadata.title == "Title"
    assert metadata.repeat is None
    assert metadata.shuffle is None
    player._controller_role.set_repeat.assert_called_once_with(SendspinRepeatMode.ALL)
    player._controller_role.set_shuffle.assert_called_once_with(shuffle=True)
