"""
Tests for progress metadata being pushed on playback-state transitions.

Regression coverage for: resuming a paused Sendspin group did not send a
server/state with metadata.progress, because current_media's identity is
unchanged across pause/resume, so the debounced media-updated callback never
fired. Also covers the stale-anchor bug this uncovered: the player's own
elapsed-time anchor (_attr_elapsed_time_last_updated) is only advanced by the
audio commit loop, which stops while paused, so it must be fast-forwarded on
resume - and the snapshot the metadata push actually reads (self.state.current_media)
must be patched directly, since the queue controller that rebuilds it only
refreshes its own cached anchor via a 500ms-debounced callback.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from aiosendspin.models.types import PlaybackStateType
from aiosendspin.server.events import GroupStateChangedEvent
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.sendspin.player import SendspinPlayer


def _player(
    *, playback_state: PlaybackState, current_media: PlayerMedia | None
) -> tuple[SendspinPlayer, Mock, AsyncMock]:
    """Build a real (but un-__init__'d) SendspinPlayer so super() calls resolve correctly."""
    player = object.__new__(SendspinPlayer)
    player._player_id = "p1"
    player._attr_playback_state = playback_state
    player._attr_current_media = current_media
    player._attr_elapsed_time_last_updated = 1.0
    # update_state() is mocked out (its real implementation needs a fully-initialized
    # player/mass), so the snapshot it would normally rebuild is seeded directly here.
    # The fix under test reads/mutates this via the `state` property, not update_state().
    player._state = SimpleNamespace(current_media=current_media)  # type: ignore[assignment]
    player.update_state = Mock()  # type: ignore[method-assign,misc]
    player.send_current_media_metadata = AsyncMock()  # type: ignore[method-assign]
    create_task = Mock(side_effect=lambda coro, **_kwargs: coro.close())
    player.mass = Mock(create_task=create_task)
    return player, create_task, player.send_current_media_metadata


def test_resume_from_pause_pushes_progress_and_refreshes_anchors() -> None:
    """Resuming the same track refreshes both anchors and schedules a progress push."""
    stale_media = PlayerMedia(
        uri="track-1", media_type=MediaType.TRACK, elapsed_time=14, elapsed_time_last_updated=1.0
    )
    player, create_task, send_metadata = _player(
        playback_state=PlaybackState.PAUSED, current_media=stale_media
    )

    with patch.object(SendspinPlayer, "synced_to", new_callable=PropertyMock) as synced_to:
        synced_to.return_value = None
        before = time.time()
        SendspinPlayer.group_event_cb(
            player, Mock(), GroupStateChangedEvent(state=PlaybackStateType.PLAYING)
        )
        after = time.time()

    # the player's own timing base (what the queue controller uses to recompute
    # its cached anchor) must be fast-forwarded
    player_anchor = player._attr_elapsed_time_last_updated
    assert player_anchor is not None
    assert before <= player_anchor <= after
    # the snapshot object the imminent metadata push actually reads must also be
    # patched directly, since the queue controller's own refresh is debounced
    refreshed = stale_media.elapsed_time_last_updated
    assert refreshed is not None
    assert before <= refreshed <= after
    send_metadata.assert_called_once()
    create_task.assert_called_once()
    assert create_task.call_args.kwargs["task_id"] == "sendspin_metadata_p1"
    assert create_task.call_args.kwargs["abort_existing"] is True


def test_pause_pushes_progress_without_touching_anchors() -> None:
    """Pausing schedules a progress push but does not rewrite either elapsed-time anchor."""
    media = PlayerMedia(
        uri="track-1", media_type=MediaType.TRACK, elapsed_time=14, elapsed_time_last_updated=1.0
    )
    player, _create_task, send_metadata = _player(
        playback_state=PlaybackState.PLAYING, current_media=media
    )

    with patch.object(SendspinPlayer, "synced_to", new_callable=PropertyMock) as synced_to:
        synced_to.return_value = None
        SendspinPlayer.group_event_cb(
            player, Mock(), GroupStateChangedEvent(state=PlaybackStateType.PAUSED)
        )

    assert player._attr_elapsed_time_last_updated == 1.0
    assert media.elapsed_time_last_updated == 1.0
    send_metadata.assert_called_once()


def test_synced_follower_does_not_push_progress() -> None:
    """A synced (non-leader) player never sends metadata itself."""
    media = PlayerMedia(
        uri="track-1", media_type=MediaType.TRACK, elapsed_time=14, elapsed_time_last_updated=1.0
    )
    player, _create_task, send_metadata = _player(
        playback_state=PlaybackState.PAUSED, current_media=media
    )

    with patch.object(SendspinPlayer, "synced_to", new_callable=PropertyMock) as synced_to:
        synced_to.return_value = "leader-1"
        SendspinPlayer.group_event_cb(
            player, Mock(), GroupStateChangedEvent(state=PlaybackStateType.PLAYING)
        )

    send_metadata.assert_not_called()


def test_fresh_play_from_idle_does_not_refresh_anchors() -> None:
    """A first play (not a pause->play resume) leaves the fresh media's anchor untouched."""
    fresh_media = PlayerMedia(
        uri="track-1", media_type=MediaType.TRACK, elapsed_time=0, elapsed_time_last_updated=42.0
    )
    player, _create_task, send_metadata = _player(
        playback_state=PlaybackState.IDLE, current_media=fresh_media
    )

    with patch.object(SendspinPlayer, "synced_to", new_callable=PropertyMock) as synced_to:
        synced_to.return_value = None
        SendspinPlayer.group_event_cb(
            player, Mock(), GroupStateChangedEvent(state=PlaybackStateType.PLAYING)
        )

    assert player._attr_elapsed_time_last_updated == 1.0
    assert fresh_media.elapsed_time_last_updated == 42.0
    send_metadata.assert_called_once()
