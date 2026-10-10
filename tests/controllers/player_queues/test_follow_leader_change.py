"""Tests for a queue following a leader change the device made itself (PlaybackTrackerMixin)."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import PlaybackState, PlayerType
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import ATTR_PLAY_ACTION_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from tests.common import scheduled_call

BASE_URL = "http://ma.local:8097"
OLD_LEADER = "old"
ELECTED = "elected"


def _player(
    player_id: str,
    *,
    media: PlayerMedia | None,
    playback_state: PlaybackState = PlaybackState.PLAYING,
    synced_to: str | None = None,
    active_group: str | None = None,
    player_type: PlayerType = PlayerType.PLAYER,
) -> MagicMock:
    """Build a native player reporting the given media."""
    player = MagicMock()
    player.player_id = player_id
    player.display_name = player_id
    player.type = player_type
    player.active_output_protocol = "native"
    player.current_media = media
    player.extra_data = {}
    player.state.type = player_type
    player.state.playback_state = playback_state
    player.state.synced_to = synced_to
    player.state.active_group = active_group
    player.state.active_source = player_id
    return player


def _old_leader_stream(item_id: str = "old-1") -> PlayerMedia:
    """Build the media a Sonos reports while it plays an item of the old leader's queue."""
    return PlayerMedia(uri=f"{BASE_URL}/single/sess/{OLD_LEADER}/{item_id}/{OLD_LEADER}.flac")


def _controller(
    *, old_leader_state: PlaybackState = PlaybackState.IDLE, old_leader_items: int = 2
) -> PlayerQueuesController:
    """
    Build a bare controller with the old leader's queue and the elected player's empty queue.

    :param old_leader_state: Playback state of the old leader's queue.
    :param old_leader_items: Number of items left on the old leader's queue.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._update_queue_from_player = Mock()  # type: ignore[method-assign]
    ctrl.transfer_queue = AsyncMock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.streams.base_url = BASE_URL
    # a scheduled coroutine is read back (and closed) by _scheduled
    ctrl.mass.create_task = Mock()
    ctrl._queue_data = {}
    for queue_id, state, count in (
        (OLD_LEADER, old_leader_state, old_leader_items),
        (ELECTED, PlaybackState.IDLE, 0),
    ):
        queue = PlayerQueue(
            queue_id=queue_id,
            active=True,
            display_name=queue_id,
            available=True,
            items=count,
            state=state,
        )
        ctrl._queue_data[queue_id] = PlayerQueueData(
            queue=queue,
            items=[
                QueueItem(queue_id=queue_id, queue_item_id=f"{queue_id}-{n}", name="x", duration=60)
                for n in range(1, count + 1)
            ],
        )
    players = {OLD_LEADER: _player(OLD_LEADER, media=None, playback_state=PlaybackState.IDLE)}
    ctrl.mass.players.get_player = Mock(side_effect=players.get)
    ctrl._test_players = players  # type: ignore[attr-defined]
    return ctrl


def _register(ctrl: PlayerQueuesController, player: MagicMock) -> None:
    """Make the controller's player lookup know the given player."""
    ctrl._test_players[player.player_id] = player  # type: ignore[attr-defined]


def _create_task(ctrl: PlayerQueuesController) -> Mock:
    """Return the controller's mocked task scheduler."""
    return cast("Mock", ctrl.mass.create_task)


def _reconcile(ctrl: PlayerQueuesController) -> Mock:
    """Return the controller's mocked queue-from-player reconciliation."""
    return cast("Mock", ctrl._update_queue_from_player)


def _transfer(ctrl: PlayerQueuesController) -> AsyncMock:
    """Return the controller's mocked transfer_queue."""
    return cast("AsyncMock", ctrl.transfer_queue)


def _scheduled(ctrl: PlayerQueuesController) -> tuple[str, dict[str, object]] | None:
    """Return the name and arguments of the task the controller scheduled, if any."""
    create_task = _create_task(ctrl)
    if not create_task.call_args_list:
        return None
    coro = create_task.call_args.args[0]
    return scheduled_call(coro)


class TestAbandonedQueuePlayedBy:
    """Which queue, if any, a player took over from a leader that left on its own."""

    def test_elected_player_playing_the_old_leaders_stream(self) -> None:
        """A solo playing player reporting an idle other queue's stream has taken it over."""
        ctrl = _controller()
        elected = _player(ELECTED, media=_old_leader_stream())

        assert ctrl._abandoned_queue_played_by(elected) == OLD_LEADER

    def test_sonos_container_uri_names_the_queue_too(self) -> None:
        """The Sonos cloud-queue container id `mass:<queue>` is the same evidence."""
        ctrl = _controller()
        elected = _player(ELECTED, media=PlayerMedia(uri=f"mass:{OLD_LEADER}"))

        assert ctrl._abandoned_queue_played_by(elected) == OLD_LEADER

    def test_queue_its_owner_still_plays_is_not_abandoned(self) -> None:
        """A queue that is playing is rendered by its owner, whatever another player reports."""
        ctrl = _controller(old_leader_state=PlaybackState.PLAYING)
        elected = _player(ELECTED, media=_old_leader_stream())

        assert ctrl._abandoned_queue_played_by(elected) is None

    def test_empty_queue_is_not_taken_over(self) -> None:
        """There is nothing to move when the reported queue holds no items."""
        ctrl = _controller(old_leader_items=0)
        elected = _player(ELECTED, media=_old_leader_stream())

        assert ctrl._abandoned_queue_played_by(elected) is None

    def test_queue_mid_action_is_left_to_that_action(self) -> None:
        """A transfer the server runs itself stops the source under the play lock first."""
        ctrl = _controller()
        elected = _player(ELECTED, media=_old_leader_stream())
        ctrl._queue_data[OLD_LEADER].queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = True

        assert ctrl._abandoned_queue_played_by(elected) is None

        ctrl._queue_data[OLD_LEADER].queue.extra_attributes.clear()
        ctrl._queue_data[OLD_LEADER].transitioning = True

        assert ctrl._abandoned_queue_played_by(elected) is None

    def test_group_players_queue_is_not_taken_over(self) -> None:
        """A group player's queue re-forms around another member by itself."""
        ctrl = _controller()
        ctrl._test_players[OLD_LEADER].state.type = PlayerType.GROUP  # type: ignore[attr-defined]
        elected = _player(ELECTED, media=_old_leader_stream())

        assert ctrl._abandoned_queue_played_by(elected) is None

    def test_member_of_a_group_renders_its_leader_by_design(self) -> None:
        """A synced player or a group member reporting the leader's stream is normal."""
        ctrl = _controller()
        synced = _player(ELECTED, media=_old_leader_stream(), synced_to=OLD_LEADER)
        grouped = _player(ELECTED, media=_old_leader_stream(), active_group="group")

        assert ctrl._abandoned_queue_played_by(synced) is None
        assert ctrl._abandoned_queue_played_by(grouped) is None

    def test_own_queue_or_unknown_media_is_no_takeover(self) -> None:
        """Playing its own queue, or media naming no known queue, moves nothing."""
        ctrl = _controller()
        own = _player(
            ELECTED, media=PlayerMedia(uri=f"{BASE_URL}/single/sess/{ELECTED}/x/{ELECTED}.flac")
        )
        unknown = _player(ELECTED, media=PlayerMedia(uri="http://elsewhere/radio"))

        assert ctrl._abandoned_queue_played_by(own) is None
        assert ctrl._abandoned_queue_played_by(unknown) is None
        assert ctrl._abandoned_queue_played_by(_player(ELECTED, media=None)) is None

    def test_only_a_playing_audio_player_takes_a_queue_over(self) -> None:
        """An idle player, or one that cannot render audio, takes nothing over."""
        ctrl = _controller()
        idle = _player(ELECTED, media=_old_leader_stream(), playback_state=PlaybackState.IDLE)
        display = _player(ELECTED, media=_old_leader_stream(), player_type=PlayerType.VISUALIZER)

        assert ctrl._abandoned_queue_played_by(idle) is None
        assert ctrl._abandoned_queue_played_by(display) is None


class TestOnPlayerUpdate:
    """The reconciliation step schedules the move and leaves the player's own queue alone."""

    def test_schedules_the_move_and_skips_reconciling_the_own_queue(self) -> None:
        """The elected player's own queue must not be shown playing the old leader's audio."""
        ctrl = _controller()
        elected = _player(ELECTED, media=_old_leader_stream())
        _register(ctrl, elected)

        ctrl.on_player_update(elected, {})

        scheduled = _scheduled(ctrl)
        assert scheduled is not None
        name, arguments = scheduled
        assert name.endswith("_follow_leader_change")
        assert arguments["queue_id"] == ELECTED
        assert arguments["abandoned_queue_id"] == OLD_LEADER
        assert _create_task(ctrl).call_args.kwargs == {"task_id": f"follow_leader_change_{ELECTED}"}
        _reconcile(ctrl).assert_not_called()

    def test_reconciles_normally_when_nothing_was_abandoned(self) -> None:
        """A player playing its own queue is reconciled as before."""
        ctrl = _controller()
        own = _player(
            ELECTED, media=PlayerMedia(uri=f"{BASE_URL}/single/sess/{ELECTED}/x/{ELECTED}.flac")
        )
        _register(ctrl, own)

        ctrl.on_player_update(own, {})

        assert _scheduled(ctrl) is None
        _reconcile(ctrl).assert_called_once_with(own)


class TestFollowLeaderChange:
    """The move itself: the old leader's queue is transferred and resumed on the new one."""

    async def test_transfers_and_resumes_the_abandoned_queue(self) -> None:
        """The queue is handed over with playback resumed where it was."""
        ctrl = _controller()
        _register(ctrl, _player(ELECTED, media=_old_leader_stream()))

        await ctrl._follow_leader_change(ELECTED, OLD_LEADER)

        _transfer(ctrl).assert_awaited_once_with(OLD_LEADER, ELECTED, auto_play=True)
        cast("MagicMock", ctrl.logger).info.assert_called_once()

    async def test_does_nothing_once_the_situation_has_settled(self) -> None:
        """A queue emptied or restarted before the task ran is not transferred."""
        ctrl = _controller(old_leader_items=0)
        _register(ctrl, _player(ELECTED, media=_old_leader_stream()))

        await ctrl._follow_leader_change(ELECTED, OLD_LEADER)

        _transfer(ctrl).assert_not_awaited()

    async def test_does_nothing_for_a_player_that_is_gone(self) -> None:
        """A player removed before the task ran is skipped."""
        ctrl = _controller()

        await ctrl._follow_leader_change(ELECTED, OLD_LEADER)

        _transfer(ctrl).assert_not_awaited()
