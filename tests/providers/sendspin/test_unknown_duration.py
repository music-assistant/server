"""Tests for the Sendspin metadata of media whose duration the player does not know."""

from __future__ import annotations

import asyncio
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType, RepeatMode
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.sendspin.player import SendspinPlayer


async def test_a_negative_duration_publishes_metadata_without_a_seek_range() -> None:
    """A player reporting -1 for a stream of unknown length still gets its metadata."""
    player = MagicMock()
    player.available = True
    player._content_takeover_pending = False
    player._metadata_lock = asyncio.Lock()
    player._metadata_publish_allowed.return_value = True
    player._player_media = PlayerMedia(
        uri="stream-1",
        media_type=MediaType.RADIO,
        title="Live stream",
        duration=-1,
        source_id="queue-1",
        queue_item_id="item-1",
    )
    player._player_playing = True
    player.mass.player_queues.get.return_value = SimpleNamespace(
        repeat_mode=RepeatMode.OFF, shuffle_enabled=False
    )
    player.mass.player_queues.get_item.return_value = None
    player._send_album_artwork = AsyncMock()
    player._album_artist = AsyncMock(return_value=None)
    player._send_beat_schedule = AsyncMock()
    player._timeline_anchored = lambda metadata, *_args, **_kwargs: (metadata, None)
    player._compute_track_progress_ms.return_value = 5000
    player._color_role = None
    player._build_current_media_metadata = partial(
        SendspinPlayer._build_current_media_metadata, player
    )
    player._queue_repeat_shuffle = SendspinPlayer._queue_repeat_shuffle
    player._publish_repeat_shuffle = partial(SendspinPlayer._publish_repeat_shuffle, player)

    await SendspinPlayer.send_current_media_metadata(player)

    player._controller_role.set_seek_max_ms.assert_called_once_with(None)
    metadata = player._metadata_role.set_metadata.call_args.args[0]
    assert metadata.track_duration == 0
    assert metadata.track_progress == 5000
