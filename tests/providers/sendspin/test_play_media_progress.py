"""
Tests for progress metadata being pushed when playback restarts on the same item.

Regression coverage for: seeking within a track did not send a server/state with
metadata.progress. A seek restarts the item that is already playing, so
current_media's identity is unchanged and the debounced media-updated callback
never fired, leaving clients to extrapolate from the position before the seek.
A start that does change the media identity is left to that callback. A resume
behind a protocol parent is pushed once the parent starts playing.
"""

from __future__ import annotations

import time
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.sendspin.player import SendspinPlayer


def _player(
    *,
    media_before: PlayerMedia | None,
    media_after: PlayerMedia | None,
    state_after: PlaybackState = PlaybackState.PLAYING,
) -> tuple[SendspinPlayer, Mock, AsyncMock, Mock]:
    """
    Build a real (but un-__init__'d) SendspinPlayer around a mocked playback session.

    :param media_before: The state's current media when play_media is called.
    :param media_after: The state's current media once update_state has run.
    :param state_after: The state's playback state once update_state has run.
    """
    player = object.__new__(SendspinPlayer)
    player._player_id = "p1"
    player.logger = Mock()
    session = Mock(cancel=AsyncMock(), start=AsyncMock())
    player.playback_session = session
    player._get_cast_bridge_manager = Mock(return_value=None)  # type: ignore[method-assign]
    # update_state() is mocked out (its real implementation needs a fully-initialized
    # player/mass), so the mock swaps in the snapshot it would normally rebuild.
    player._state = SimpleNamespace(current_media=media_before)  # type: ignore[assignment]

    def _update_state() -> None:
        player._state = SimpleNamespace(  # type: ignore[assignment]
            current_media=media_after, playback_state=state_after
        )

    player.update_state = Mock(side_effect=_update_state)  # type: ignore[method-assign,misc]
    player.send_current_media_metadata = AsyncMock()  # type: ignore[method-assign]
    create_task = Mock(side_effect=lambda coro, **_kwargs: coro.close())
    player.mass = Mock(create_task=create_task)
    return player, create_task, player.send_current_media_metadata, session


def _media(item: int = 1, *, elapsed_time: int | None = None) -> PlayerMedia:
    return PlayerMedia(
        uri=f"track-{item}",
        media_type=MediaType.TRACK,
        title=f"Track {item}",
        source_id="queue-1",
        queue_item_id=f"item-{item}",
        elapsed_time=elapsed_time,
    )


def _stopped_media() -> PlayerMedia:
    """Return media whose position a parent that hasn't caught up still dates to the stop."""
    media = _media(elapsed_time=19)
    media.elapsed_time_last_updated = time.time() - 45
    return media


async def _play_media(
    player: SendspinPlayer,
    media: PlayerMedia,
    synced_to: str | None,
    protocol_parent_id: str | None = None,
) -> None:
    with (
        patch.object(
            SendspinPlayer, "synced_to", new_callable=PropertyMock, return_value=synced_to
        ),
        patch.object(
            SendspinPlayer,
            "protocol_parent_id",
            new_callable=PropertyMock,
            return_value=protocol_parent_id,
        ),
        patch.object(SendspinPlayer, "display_name", new_callable=PropertyMock, return_value="P"),
    ):
        await SendspinPlayer.play_media(player, media)


def _on_protocol_parent_updated(
    player: SendspinPlayer, changed_values: dict[str, tuple[Any, Any]]
) -> None:
    with patch.object(SendspinPlayer, "synced_to", new_callable=PropertyMock, return_value=None):
        SendspinPlayer.on_protocol_parent_updated(player, Mock(), changed_values)


_STARTED_PLAYING = {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}


async def test_play_media_same_item_pushes_progress() -> None:
    """Restarting the item that is already playing (a seek) schedules one progress push."""
    player, create_task, send_metadata, session = _player(
        media_before=_media(elapsed_time=14), media_after=_media(elapsed_time=90)
    )

    await _play_media(player, _media(), synced_to=None)

    session.start.assert_awaited_once()
    send_metadata.assert_called_once()
    create_task.assert_called_once()
    assert create_task.call_args.kwargs["task_id"] == "sendspin_metadata_p1"
    assert create_task.call_args.kwargs["abort_existing"] is True


async def test_play_media_resume_leaves_push_to_protocol_parent() -> None:
    """A resume behind a parent that isn't playing yet doesn't push the parent's old position."""
    player, create_task, send_metadata, _session = _player(
        media_before=_stopped_media(), media_after=_stopped_media()
    )
    cast("Mock", player.mass).players.get_player.return_value = Mock(
        state=SimpleNamespace(playback_state=PlaybackState.IDLE)
    )

    await _play_media(player, _media(), synced_to=None, protocol_parent_id="parent")

    send_metadata.assert_not_called()
    create_task.assert_not_called()


def test_protocol_parent_starting_to_play_pushes_its_position() -> None:
    """A resume pushes the position the parent took on playing, not the stopped one."""
    fresh = _media(elapsed_time=19)
    player, create_task, send_metadata, _session = _player(
        media_before=_stopped_media(), media_after=fresh
    )
    pushed_from: list[PlayerMedia | None] = []

    def _create_task(coro: Coroutine[Any, Any, None], **_kwargs: object) -> None:
        pushed_from.append(player.state.current_media)
        coro.close()

    create_task.side_effect = _create_task

    _on_protocol_parent_updated(player, _STARTED_PLAYING)

    send_metadata.assert_called_once()
    assert pushed_from == [fresh]


def test_protocol_parent_position_change_alone_pushes_nothing() -> None:
    """A parent position correction is not forwarded, since the queue briefly reports stale ones."""
    player, create_task, send_metadata, _session = _player(
        media_before=_media(), media_after=_media()
    )

    _on_protocol_parent_updated(player, {"current_media.elapsed_time": (40, 0)})

    send_metadata.assert_not_called()
    create_task.assert_not_called()


def test_protocol_parent_update_while_idle_pushes_nothing() -> None:
    """An idle player sends nothing on a parent update, such as its own stop or another output."""
    player, create_task, send_metadata, _session = _player(
        media_before=_media(), media_after=_media(), state_after=PlaybackState.IDLE
    )

    _on_protocol_parent_updated(player, _STARTED_PLAYING)

    send_metadata.assert_not_called()
    create_task.assert_not_called()


async def test_play_media_different_item_leaves_push_to_media_callback() -> None:
    """A track change is pushed by the media-updated callback, not by play_media."""
    player, create_task, send_metadata, session = _player(
        media_before=_media(1), media_after=_media(2)
    )

    await _play_media(player, _media(2), synced_to=None)

    session.start.assert_awaited_once()
    send_metadata.assert_not_called()
    create_task.assert_not_called()


async def test_play_media_first_item_leaves_push_to_media_callback() -> None:
    """Media appearing on an idle player is pushed by the media-updated callback."""
    player, create_task, send_metadata, session = _player(media_before=None, media_after=_media())

    await _play_media(player, _media(), synced_to=None)

    session.start.assert_awaited_once()
    send_metadata.assert_not_called()
    create_task.assert_not_called()


async def test_play_media_on_synced_follower_pushes_nothing() -> None:
    """Only the leader sends metadata, so a synced player schedules no push."""
    for media_before in (_media(1), _media(2)):
        player, create_task, send_metadata, _session = _player(
            media_before=media_before, media_after=_media(1)
        )

        await _play_media(player, _media(1), synced_to="p0")

        send_metadata.assert_not_called()
        create_task.assert_not_called()
