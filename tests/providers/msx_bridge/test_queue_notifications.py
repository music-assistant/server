"""Behavior preserved when removing the obsolete queue notification state."""

from __future__ import annotations

from unittest.mock import Mock, call, patch

from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.providers.msx_bridge.player import MSXPlayer


async def test_queue_track_changes_reload_playlist_at_current_index(
    player: MSXPlayer, mass_mock: Mock
) -> None:
    """Successive queue selections reload the native playlist at the selected item."""
    queue = PlayerQueue(
        queue_id="msx_test", active=True, display_name="Test queue", available=True, items=5
    )
    mass_mock.player_queues.get.return_value = queue
    with patch.object(player.provider, "notify_play_playlist") as notify_playlist:
        for index in (1, 3):
            queue.current_index = index
            await player.play_media(
                PlayerMedia(
                    uri=f"http://ma.local/stream/{index}",
                    source_id=queue.queue_id,
                    queue_item_id=f"qi{index}",
                )
            )

    assert notify_playlist.call_args_list == [
        call("msx_test", 1, queue_id="msx_test"),
        call("msx_test", 3, queue_id="msx_test"),
    ]
