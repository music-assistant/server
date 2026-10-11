"""
Tests for progress metadata being pushed when playback (re)starts.

Regression coverage for: seeking within a track did not send a server/state with
metadata.progress. A seek restarts the item that is already playing, so
current_media's identity is unchanged and the debounced media-updated callback
never fired, leaving clients to extrapolate from the position before the seek.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from music_assistant_models.enums import MediaType
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.sendspin.player import SendspinPlayer


def _player() -> tuple[SendspinPlayer, Mock, AsyncMock, Mock]:
    """Build a real (but un-__init__'d) SendspinPlayer around a mocked playback session."""
    player = object.__new__(SendspinPlayer)
    player._player_id = "p1"
    player.logger = Mock()
    session = Mock(cancel=AsyncMock(), start=AsyncMock())
    player.playback_session = session
    player._get_cast_bridge_manager = Mock(return_value=None)  # type: ignore[method-assign]
    player.update_state = Mock()  # type: ignore[method-assign,misc]
    player.send_current_media_metadata = AsyncMock()  # type: ignore[method-assign]
    create_task = Mock(side_effect=lambda coro, **_kwargs: coro.close())
    player.mass = Mock(create_task=create_task)
    return player, create_task, player.send_current_media_metadata, session


def _media() -> PlayerMedia:
    return PlayerMedia(uri="track-1", media_type=MediaType.TRACK, queue_item_id="item-1")


async def test_play_media_pushes_progress() -> None:
    """Starting playback schedules a progress push, also when the same item restarts."""
    player, create_task, send_metadata, session = _player()

    with (
        patch.object(SendspinPlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(SendspinPlayer, "display_name", new_callable=PropertyMock, return_value="P"),
    ):
        await SendspinPlayer.play_media(player, _media())

    session.start.assert_awaited_once()
    send_metadata.assert_called_once()
    create_task.assert_called_once()
    assert create_task.call_args.kwargs["task_id"] == "sendspin_metadata_p1"
    assert create_task.call_args.kwargs["abort_existing"] is True


async def test_play_media_on_synced_follower_pushes_nothing() -> None:
    """Only the leader sends metadata, so a synced player schedules no push."""
    player, create_task, send_metadata, _session = _player()

    with (
        patch.object(SendspinPlayer, "synced_to", new_callable=PropertyMock, return_value="p0"),
        patch.object(SendspinPlayer, "display_name", new_callable=PropertyMock, return_value="P"),
    ):
        await SendspinPlayer.play_media(player, _media())

    send_metadata.assert_not_called()
    create_task.assert_not_called()
