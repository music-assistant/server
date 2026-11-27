"""
The position a queue reports around an announcement that stops it.

The default announcement stops the queue's playback and plays the clip instead. For the length of
the clip the queue keeps its playing state, holds its position at the point it resumes from and
carries a marker for clients, and once the announcement is over it follows its player again.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, NamedTuple, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import EventType, PlaybackState, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player_queue import PlayerQueue, PlayLogEntry
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.helpers import CompareState
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.helpers import AnnounceData

QUEUE_ID = "player_1"
TRACK_A = "track-a"
TRACK_B = "track-b"
SAVED_POSITION = 65


class _Update(NamedTuple):
    """What one QUEUE_UPDATED event said about the queue."""

    state: PlaybackState
    elapsed_time: float
    held: bool


def _item(item_id: str) -> QueueItem:
    """Return a queue item."""
    return QueueItem(queue_id=QUEUE_ID, queue_item_id=item_id, name=item_id, duration=200)


def _queue(state: PlaybackState = PlaybackState.PLAYING) -> PlayerQueue:
    """Return a queue in the middle of its second item."""
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Player 1", available=True, items=2
    )
    queue.state = state
    queue.flow_mode = True
    queue.current_index = 1
    queue.current_item = _item(TRACK_B)
    queue.elapsed_time = SAVED_POSITION
    queue.elapsed_time_last_updated = time.time()
    return queue


def _player(*, state: PlaybackState = PlaybackState.PLAYING, announcement: bool = True) -> Any:
    """Return the queue's player."""
    player = MagicMock()
    player.player_id = QUEUE_ID
    player.name = "Player 1"
    player.available = True
    player.type = PlayerType.PLAYER
    player.playback_state = state
    player.active_source = QUEUE_ID
    player.fake_paused = False
    player.corrected_elapsed_time = 0.0
    player.group_members = []
    player.offline_by_power_control = False
    player.synced_to = None
    player.active_group = None
    player.resolve_output_protocol = Mock(return_value=None)
    player.extra_data = {ATTR_ANNOUNCEMENT_IN_PROGRESS: announcement}
    return player


def _mass(player: Any) -> MagicMock:
    """Return a mocked server that records the queue updates it publishes."""
    mass = MagicMock()
    mass.players.get_player = Mock(return_value=player)
    mass.players.get_player_or_protocol_player = Mock(return_value=None)
    mass.players._handle_cmd_stop = AsyncMock()
    # the published queue is the live snapshot, so what an event said is taken as it goes out
    mass.published = []

    def _publish(event: EventType, **kwargs: Any) -> None:
        if event == EventType.QUEUE_UPDATED:
            queue = kwargs["data"]
            held = bool(queue.extra_attributes.get(ATTR_ANNOUNCEMENT_IN_PROGRESS))
            mass.published.append(_Update(queue.state, queue.elapsed_time, held))

    mass.signal_event = Mock(side_effect=_publish)
    return mass


def _controller(queue: PlayerQueue, player: Any) -> PlayerQueuesController:
    """Return a queue controller whose only queue is the given one, on a mocked server."""
    data = PlayerQueueData(queue=queue)
    data.items = [_item(TRACK_A), _item(TRACK_B)]
    # a flow session whose first item was fully streamed before the second one started
    data.flow_mode_stream_log = [
        PlayLogEntry(TRACK_A, seconds_streamed=0.0, seek_position=0),
        PlayLogEntry(TRACK_B, seconds_streamed=None, seek_position=None),
    ]
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl._queue_data = {QUEUE_ID: data}
    ctrl.logger = MagicMock()
    ctrl.mass = _mass(player)
    ctrl.mass.player_queues = ctrl
    return ctrl


def _announcements(ctrl: PlayerQueuesController) -> PlayerController:
    """Return the players controller that announces on the given queue controller."""
    announcements = PlayerController.__new__(PlayerController)
    announcements.mass = ctrl.mass
    announcements.logger = MagicMock()
    announcements.get_active_queue = Mock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(queue_id=QUEUE_ID)
    )
    announcements.get_player = Mock(return_value=None)  # type: ignore[method-assign]
    announcements._wait_for_playback_state = AsyncMock()  # type: ignore[method-assign]
    announcements._unmute_and_set_announcement_volume = AsyncMock()  # type: ignore[method-assign]
    return announcements


def _fallback_announcements(ctrl: PlayerQueuesController) -> PlayerController:
    """Return the players controller, announcing through the default implementation."""
    renderer = ctrl.mass.streams.announcement_renderer
    renderer.register = Mock(  # type: ignore[method-assign]
        return_value=Mock(wait_ready=AsyncMock(return_value=True))
    )
    renderer.get = Mock(return_value=None)  # type: ignore[method-assign]
    renderer.unregister = AsyncMock()  # type: ignore[method-assign]
    ctrl.mass.streams.get_announcement_url = Mock(  # type: ignore[method-assign]
        return_value="http://mass/announcement"
    )
    announcements = _announcements(ctrl)
    announcements._resolve_ready_announce_player = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    announcements._handle_play_media = AsyncMock()  # type: ignore[method-assign]
    announcements._handle_cmd_resume = AsyncMock()  # type: ignore[method-assign]
    return announcements


def _announce_data() -> AnnounceData:
    """Return the announcement to play."""
    return AnnounceData(
        announcement_url="http://mass/clip.mp3",
        pre_announce=False,
        pre_announce_url="",
        announce_player_id=None,
    )


async def _prepare(announcements: PlayerController, player: Any, prev_state: PlaybackState) -> None:
    """Free up the player for an announcement, as the default announcement does."""
    await announcements._prepare_for_announcement(
        player,
        volume_level=None,
        prev_state=prev_state,
        prev_synced_to=None,
        prev_group=None,
        prev_members=[],
        prev_media_name=None,
        prev_volumes={},
        prev_muted=set(),
    )


def _prev_state(queue: PlayerQueue) -> CompareState:
    """Return the state the queue reported before an announcement took the player over."""
    return CompareState(
        queue_id=QUEUE_ID,
        state=PlaybackState.PLAYING,
        current_item_id=TRACK_B,
        next_item_id=None,
        current_item=queue.current_item,
        elapsed_time=SAVED_POSITION,
        last_playing_elapsed_time=SAVED_POSITION,
        stream_title=None,
        codec_type=None,
        output_player_ids=[QUEUE_ID],
    )


def _held(ctrl: PlayerQueuesController) -> bool:
    """Return whether the queue carries the announcement marker."""
    queue = ctrl._queue_data[QUEUE_ID].queue
    return bool(queue.extra_attributes.get(ATTR_ANNOUNCEMENT_IN_PROGRESS))


def _queue_updates(mass: Any) -> list[_Update]:
    """Return the QUEUE_UPDATED events the server published, as they went out."""
    return cast("list[_Update]", mass.published)


async def test_the_announcement_holds_a_playing_queue_at_the_position_its_stop_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queue keeps playing state, carries the marker and holds the saved position."""
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    queue = _queue()
    queue.elapsed_time_last_updated = now
    player = _player()
    ctrl = _controller(queue, player)

    async def _device_stop(_queue_id: str) -> None:
        # a slow device: the wall clock runs on while the stop travels to it
        nonlocal now
        now += 24.0

    ctrl.mass.players._handle_cmd_stop = AsyncMock(  # type: ignore[method-assign]
        side_effect=_device_stop
    )

    await _prepare(_announcements(ctrl), player, PlaybackState.PLAYING)

    assert queue.state == PlaybackState.PLAYING
    assert _held(ctrl)
    assert queue.elapsed_time == SAVED_POSITION
    assert queue.elapsed_time_last_updated == now
    assert _queue_updates(ctrl.mass)[-1] == _Update(PlaybackState.PLAYING, SAVED_POSITION, True)


async def test_the_announcement_does_not_hold_a_queue_the_user_paused() -> None:
    """A paused queue keeps its own position and carries no marker."""
    queue = _queue(PlaybackState.PAUSED)
    queue.elapsed_time = 7
    player = _player(state=PlaybackState.PAUSED)
    ctrl = _controller(queue, player)

    await _prepare(_announcements(ctrl), player, PlaybackState.PAUSED)

    assert not _held(ctrl)
    assert queue.elapsed_time == 7


async def test_the_queue_follows_its_player_again_when_the_announcement_ends() -> None:
    """After the clip the marker is gone and the queue plays on at its player's position."""
    queue = _queue()
    player = _player(announcement=False)
    ctrl = _controller(queue, player)
    ctrl._queue_data[QUEUE_ID].prev_state = _prev_state(queue)
    announcements = _fallback_announcements(ctrl)

    async def _resume(*_args: Any) -> None:
        # the device plays the item on; its updates are ignored until the announcement is over
        player.playback_state = PlaybackState.PLAYING
        player.corrected_elapsed_time = SAVED_POSITION + 3

    announcements._handle_cmd_resume = AsyncMock(side_effect=_resume)  # type: ignore[method-assign]

    await announcements._run_announcement(player, _announce_data(), None)

    assert not _held(ctrl)
    assert queue.state == PlaybackState.PLAYING
    assert queue.elapsed_time == SAVED_POSITION + 3
    assert _queue_updates(ctrl.mass)[-1] == _Update(
        PlaybackState.PLAYING, SAVED_POSITION + 3, False
    )


async def test_a_failing_announcement_releases_the_queue() -> None:
    """An announcement that fails after the stop leaves no marker behind."""
    queue = _queue()
    player = _player(announcement=False)
    ctrl = _controller(queue, player)
    announcements = _fallback_announcements(ctrl)
    announcements._handle_play_media = AsyncMock(  # type: ignore[method-assign]
        side_effect=PlayerCommandFailed("unreachable")
    )

    with pytest.raises(PlayerCommandFailed):
        await announcements._run_announcement(player, _announce_data(), None)

    assert not _held(ctrl)
    assert not _queue_updates(ctrl.mass)[-1].held


def test_the_end_of_an_announcement_that_held_nothing_publishes_nothing() -> None:
    """An announcement that did not stop the queue, as a native one, leaves it as it is."""
    ctrl = _controller(_queue(), _player(announcement=False))

    ctrl.release_after_announcement(QUEUE_ID)

    assert not _queue_updates(ctrl.mass)
