"""
The position a queue reports around a device announcement, as the live report showed it.

A device announcement stops the queue's playback: the player plays the clip instead. The queue
itself then has nothing playing, so it has to keep reporting the position it resumes from - and
the position must not be taken from the clip's own stream, which is where the live report's jump
came from.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import EventType, PlaybackState, PlayerType
from music_assistant_models.player_queue import PlayerQueue, PlayLogEntry
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.helpers import CompareState
from music_assistant.controllers.player_queues.playback_tracker import PlaybackTrackerMixin
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from tests.common import use_real_create_task

if TYPE_CHECKING:
    from collections.abc import Sequence

QUEUE_ID = "player_1"
TRACK_A = "track-a"
TRACK_B = "track-b"
PARKED_POSITION = 42
# the live report: the item stood at 65.2s while the clip's own stream was at 41.0s
LIVE_POSITION = 65
LIVE_CLIP_POSITION = 41


class _Update(NamedTuple):
    """What one QUEUE_UPDATED event said about the queue."""

    state: PlaybackState
    elapsed_time: float


def _item(item_id: str, *, seek_position: int | None = None) -> QueueItem:
    """Return a queue item that may carry a seek offset of its stream."""
    item = QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=item_id,
        name=item_id,
        duration=200,
    )
    if seek_position is not None:
        item.streamdetails = SimpleNamespace(seek_position=seek_position)  # type: ignore[assignment]
    return item


def _queue(state: PlaybackState = PlaybackState.PLAYING) -> PlayerQueue:
    """Return a queue parked in the middle of its second item."""
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Player 1", available=True, items=2
    )
    queue.state = state
    queue.flow_mode = True
    queue.current_index = 1
    queue.current_item = _item(TRACK_B)
    queue.elapsed_time = PARKED_POSITION
    queue.elapsed_time_last_updated = time.time()
    return queue


def _flow_log(*, first_item_streamed: float = 200.0) -> list[PlayLogEntry]:
    """Return the play log of a flow session, with the second item being the one that plays."""
    return [
        PlayLogEntry(TRACK_A, seconds_streamed=first_item_streamed, seek_position=0),
        PlayLogEntry(TRACK_B, seconds_streamed=None, seek_position=None),
    ]


def _player(
    *,
    state: PlaybackState = PlaybackState.PLAYING,
    elapsed: float = 0.0,
    announcement: bool = True,
    player_type: PlayerType = PlayerType.PLAYER,
) -> Any:
    """Return the queue's player, playing an announcement at the start of the clip."""
    player = MagicMock()
    player.player_id = QUEUE_ID
    player.name = "Player 1"
    player.available = True
    player.type = player_type
    player.playback_state = state
    player.active_source = QUEUE_ID
    player.corrected_elapsed_time = elapsed
    player.group_members = []
    player.offline_by_power_control = False
    player.extra_data = {ATTR_ANNOUNCEMENT_IN_PROGRESS: announcement}
    return player


def _mass(player: Any) -> MagicMock:
    """Return a mocked server that records the queue updates it publishes."""
    mass = MagicMock()
    mass.players.get_player = Mock(return_value=player)
    mass.players.get_player_or_protocol_player = Mock(return_value=None)
    mass.players._handle_cmd_stop = AsyncMock()
    mass.players._power_on_members = AsyncMock()
    mass.players._resolve_play_media_target = Mock(return_value=player)
    # the published queue is the live snapshot, so what an event said is taken as it goes out
    mass.published = []

    def _publish(event: EventType, **kwargs: Any) -> None:
        if event == EventType.QUEUE_UPDATED:
            queue = kwargs["data"]
            mass.published.append(_Update(state=queue.state, elapsed_time=queue.elapsed_time))

    mass.signal_event = Mock(side_effect=_publish)
    return mass


def _controller(
    queue: PlayerQueue,
    *,
    player: Any = None,
    log: Sequence[PlayLogEntry] | None = None,
) -> PlayerQueuesController:
    """Return a queue controller whose only queue is the given one, on a mocked server."""
    items = [_item(TRACK_A), _item(TRACK_B)]
    data = PlayerQueueData(queue=queue)
    data.items = items
    data.flow_mode_stream_log = list(log if log is not None else _flow_log())
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl._queue_data = {QUEUE_ID: data}
    ctrl.logger = MagicMock()
    ctrl.mass = _mass(player if player is not None else _player())
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


def _prev_state(queue: PlayerQueue, elapsed: int) -> CompareState:
    """Return the state the queue reported before an announcement took the player over."""
    return CompareState(
        queue_id=QUEUE_ID,
        state=PlaybackState.PLAYING,
        current_item_id=TRACK_B,
        next_item_id=None,
        current_item=queue.current_item,
        elapsed_time=elapsed,
        last_playing_elapsed_time=elapsed,
        stream_title=None,
        codec_type=None,
        output_player_ids=[QUEUE_ID],
    )


def _queue_updates(mass: Any) -> list[_Update]:
    """Return the QUEUE_UPDATED events the server published, as they went out."""
    return cast("list[_Update]", mass.published)


def _shown_position(queue: PlayerQueue) -> float:
    """Return the position a client displays for the queue, extrapolated as clients do."""
    return queue.corrected_elapsed_time


def test_the_clip_a_player_plays_does_not_move_the_queues_position() -> None:
    """The announcement's own stream position never lands on the queue."""
    log = _flow_log()
    # the live case: the flow session began where playback was resumed, 60s into an item
    log[0].seek_position = 60
    queue = _queue()
    player = _player(elapsed=0.4)
    ctrl = _controller(queue, player=player, log=log)
    # without the guard the clip's 0.4s of its own stream resolve to that session start,
    # which is the jump the live queue showed (60.001s, mid-item, then back)
    assert PlaybackTrackerMixin._get_flow_queue_stream_index(ctrl, queue, player)[1] == (
        pytest.approx(60.4)
    )

    PlayerQueuesController.on_player_elapsed_time_corrected(ctrl, player)

    assert queue.elapsed_time == PARKED_POSITION
    assert not cast("Mock", ctrl.mass.signal_event).called


async def test_the_queue_parks_at_the_position_the_stop_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop of an announcement parks the queue it stopped, at the position it resumes from."""
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    queue = _queue()
    queue.elapsed_time = LIVE_POSITION
    queue.elapsed_time_last_updated = now
    player = _player()

    async def _device_stop(_queue_id: str) -> None:
        # a slow device: the wall clock runs on while the stop travels to it
        nonlocal now
        now += 24.0

    ctrl = _controller(queue, player=player)
    ctrl.mass.players._handle_cmd_stop = AsyncMock(  # type: ignore[method-assign]
        side_effect=_device_stop
    )

    await ctrl._handle_stop(QUEUE_ID)

    assert queue.state == PlaybackState.PAUSED
    # the position the stop saved (65) and not the clock at the device stop's end (89)
    assert queue.elapsed_time == LIVE_POSITION
    assert _shown_position(queue) == LIVE_POSITION
    assert _queue_updates(ctrl.mass)[-1].state == PlaybackState.PAUSED


async def test_an_item_at_its_start_is_parked_at_its_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An item that stood at 0s parks at 0s, not at the clock the device stop let run on."""
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    queue = _queue()
    queue.elapsed_time = 0
    queue.elapsed_time_last_updated = now
    player = _player()

    async def _device_stop(_queue_id: str) -> None:
        # a slow device: the wall clock runs on while the stop travels to it
        nonlocal now
        now += 24.0

    ctrl = _controller(queue, player=player)
    ctrl.mass.players._handle_cmd_stop = AsyncMock(  # type: ignore[method-assign]
        side_effect=_device_stop
    )

    await ctrl._handle_stop(QUEUE_ID)

    assert queue.resume_pos == 0
    assert queue.state == PlaybackState.PAUSED
    assert queue.elapsed_time == 0
    # and 0 is where the restore picks the item up again
    assert PlayerQueuesController._last_played_position(ctrl, queue) == 0


async def test_a_stop_that_is_no_announcement_parks_nothing() -> None:
    """A stop for any other reason parks nothing: no client sees the queue paused."""
    queue = _queue()
    queue.elapsed_time = LIVE_POSITION
    player = _player(announcement=False, state=PlaybackState.IDLE)
    ctrl = _controller(queue, player=player)
    queue_data = ctrl._queue_data[QUEUE_ID]
    queue_data.prev_state = _prev_state(queue, LIVE_POSITION)

    await ctrl._handle_stop(QUEUE_ID)

    assert queue.state != PlaybackState.PAUSED
    assert queue.elapsed_time == LIVE_POSITION
    assert PlaybackState.PAUSED not in [update.state for update in _queue_updates(ctrl.mass)]


def test_a_queue_that_was_already_parked_keeps_the_position_it_was_paused_at() -> None:
    """A second announcement leaves a queue that was already parked as it is."""
    queue = _queue(PlaybackState.PAUSED)
    queue.elapsed_time = 7
    queue.resume_pos = 90
    ctrl = _controller(queue)

    PlayerQueuesController._park_for_announcement(ctrl, QUEUE_ID)

    assert queue.state == PlaybackState.PAUSED
    assert queue.elapsed_time == 7


def test_a_queue_that_plays_no_item_of_its_own_parks_nothing() -> None:
    """A queue that is no source of its player has no position of its own to park."""
    queue = _queue()
    queue.active = False
    queue.elapsed_time = 7
    queue.resume_pos = 90
    ctrl = _controller(queue)

    PlayerQueuesController._park_for_announcement(ctrl, QUEUE_ID)

    assert queue.state == PlaybackState.PLAYING
    assert queue.elapsed_time == 7


def test_the_parked_position_is_the_saved_one_not_the_running_clock() -> None:
    """The live pair: the item stood at 65.2s, the clip's own stream at 41.0s."""
    queue = _queue()
    queue.resume_pos = LIVE_POSITION
    queue.elapsed_time = LIVE_CLIP_POSITION
    queue.elapsed_time_last_updated = time.time()
    ctrl = _controller(queue)

    PlayerQueuesController._park_for_announcement(ctrl, QUEUE_ID)

    assert queue.elapsed_time == LIVE_POSITION
    assert _queue_updates(ctrl.mass)[-1].state == PlaybackState.PAUSED


def test_the_parked_position_is_the_saved_one_when_the_clock_ran_on() -> None:
    """The other direction: a stop that saved 41s parks at 41s, not at the item's 65s."""
    queue = _queue()
    queue.resume_pos = LIVE_CLIP_POSITION
    queue.elapsed_time = LIVE_POSITION
    queue.elapsed_time_last_updated = time.time()
    ctrl = _controller(queue)

    PlayerQueuesController._park_for_announcement(ctrl, QUEUE_ID)

    assert queue.elapsed_time == LIVE_CLIP_POSITION


def test_an_announcement_for_an_unknown_queue_is_ignored() -> None:
    """An unknown queue id is not an error for the announcement path."""
    queue = _queue()
    ctrl = _controller(queue)

    PlayerQueuesController._park_for_announcement(ctrl, "no-such-queue")

    assert queue.state == PlaybackState.PLAYING


async def test_the_announcement_parks_the_queue_it_stops() -> None:
    """The queue is parked by the stop the announcement itself asks for."""
    queue = _queue()
    queue.elapsed_time = LIVE_POSITION
    queue.resume_pos = 0
    player = _player()
    ctrl = _controller(queue, player=player)
    announcements = _announcements(ctrl)

    await announcements._prepare_for_announcement(
        player,
        volume_level=None,
        prev_state=PlaybackState.PLAYING,
        prev_synced_to=None,
        prev_group=None,
        prev_media_name=None,
        prev_volumes={},
        prev_muted=set(),
    )

    assert ctrl.mass.players._handle_cmd_stop.await_count == 1  # type: ignore[attr-defined]
    assert queue.state == PlaybackState.PAUSED
    assert queue.elapsed_time == LIVE_POSITION


async def test_the_queue_reports_playing_again_after_the_announcement() -> None:
    """The update after the clip publishes that the queue plays on, not that it is parked."""
    now = time.time()
    queue = _queue()
    queue.elapsed_time = LIVE_CLIP_POSITION
    queue.elapsed_time_last_updated = now
    player = _player()
    # a flow session whose first item was fully streamed before the second one started
    log = _flow_log(first_item_streamed=0.0)
    ctrl = _controller(queue, player=player, log=log)
    ctrl._queue_data[QUEUE_ID].prev_state = _prev_state(queue, LIVE_CLIP_POSITION)

    await ctrl._handle_stop(QUEUE_ID)
    parked = [update.state for update in _queue_updates(ctrl.mass)]
    assert parked[-1] == PlaybackState.PAUSED

    # the clip is over and the item plays on from the position it was parked at
    player.extra_data = {}
    player.playback_state = PlaybackState.PLAYING
    player.corrected_elapsed_time = LIVE_POSITION
    queue.elapsed_time_last_updated = now
    ctrl.on_player_update(player, {})

    updates = _queue_updates(ctrl.mass)
    assert len(updates) == len(parked) + 1
    assert updates[-1].state == PlaybackState.PLAYING
    assert updates[-1].elapsed_time == LIVE_POSITION


def test_the_reported_position_stays_monotone_through_the_announcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live case: no jump, and the item plays on from the position it was parked at."""
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    queue = _queue()
    player = _player(elapsed=0.0)
    ctrl = _controller(queue, player=player)
    before = _shown_position(queue)

    # the stop the announcement does saves where the item was and stalls the queue
    queue.resume_pos = int(queue.corrected_elapsed_time)
    PlayerQueuesController._park_for_announcement(ctrl, QUEUE_ID)
    during = _shown_position(queue)

    for reported in (0.0, 1.0, 3.0, 9.9):
        now += 1.0
        player.corrected_elapsed_time = reported
        PlayerQueuesController.on_player_elapsed_time_corrected(ctrl, player)
        assert _shown_position(queue) == during
    assert during <= before
    assert before - during < 1

    # the restore plays the item on from that very position
    queue.state = PlaybackState.PLAYING
    queue.elapsed_time_last_updated = now
    now += 1.0
    assert during <= _shown_position(queue) <= during + 1.5


async def test_play_pause_during_an_announcement_pauses_the_clip() -> None:
    """A press while the clip plays pauses it, as it did before the queue parked."""
    queue = _queue(PlaybackState.PAUSED)
    queue.elapsed_time = LIVE_POSITION
    queue.resume_pos = LIVE_POSITION
    ctrl = _controller(queue)
    ctrl.pause = AsyncMock()  # type: ignore[method-assign]
    ctrl.play = AsyncMock()  # type: ignore[method-assign]

    await ctrl.play_pause(QUEUE_ID)

    ctrl.pause.assert_awaited_once_with(QUEUE_ID)
    ctrl.play.assert_not_awaited()


async def test_play_pause_on_a_paused_queue_without_an_announcement_starts_it() -> None:
    """Without an announcement a paused queue is started, so the guard does not hold it back."""
    queue = _queue(PlaybackState.PAUSED)
    player = _player(announcement=False)
    ctrl = _controller(queue, player=player)
    ctrl.pause = AsyncMock()  # type: ignore[method-assign]
    ctrl.play = AsyncMock()  # type: ignore[method-assign]

    await ctrl.play_pause(QUEUE_ID)

    ctrl.play.assert_awaited_once_with(QUEUE_ID)
    ctrl.pause.assert_not_awaited()


@pytest.mark.parametrize(
    ("command", "current_index", "elapsed", "played_index"),
    [("next", 0, PARKED_POSITION, 1), ("previous", 1, 0, 0)],
)
async def test_a_step_during_an_announcement_powers_no_member_on(
    command: str, current_index: int, elapsed: int, played_index: int
) -> None:
    """A step on the group while the clip plays leaves its switched-off members off."""
    queue = _queue(PlaybackState.PAUSED)
    queue.current_index = current_index
    queue.current_item = _item(TRACK_A if current_index == 0 else TRACK_B)
    queue.elapsed_time = elapsed
    player = _player(player_type=PlayerType.GROUP)
    ctrl = _controller(queue, player=player)
    use_real_create_task(ctrl.mass)
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]

    await getattr(ctrl, command)(QUEUE_ID)

    moved = next(
        call
        for call in ctrl.mass.call_later.call_args_list  # type: ignore[attr-defined]
        if call.args[1] == ctrl._move_to_index
    )
    await moved.args[1](*moved.args[2:])

    ctrl.mass.players._power_on_members.assert_not_awaited()  # type: ignore[attr-defined]
    ctrl.play_index.assert_awaited_once_with(QUEUE_ID, played_index)
