"""Tests for a Sonos group whose coordinator the speakers moved outside Music Assistant."""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosonos.api.models import MusicService, PlayBackState
from aiosonos.const import EventType as SonosEventType
from aiosonos.const import GroupCoordinatorChangedEvent, SessionErrorEvent
from aiosonos.exceptions import FailedCommand
from music_assistant_models.enums import MediaType, PlaybackState

from music_assistant.providers.sonos import player as player_module
from music_assistant.providers.sonos.player import SonosPlayer

if TYPE_CHECKING:
    from aiosonos.api.models import GroupCoordinatorChanged, SessionError

OFFICE = "RINCON_F0F6C1CC8D0401400"
BATHROOM = "RINCON_48A6B86DD3F101400"
OFFICE_GROUP = "RINCON_F0F6C1CC8D0401400:140007645"
SYNC_GROUP = "syncgroup_upstairs"


def _moved(new_coordinator: str = BATHROOM) -> GroupCoordinatorChangedEvent:
    """Build the event an Era 300 sent when its group of three moved to another speaker."""
    body = cast(
        "GroupCoordinatorChanged",
        {
            "_objectType": "groupCoordinatorChanged",
            "groupStatus": "GROUP_STATUS_MOVED",
            "groupName": "Bathroom + 1",
            "websocketUrl": "wss://192.168.113.18:1443/websocket/api",
            "playerId": new_coordinator,
        },
    )
    return GroupCoordinatorChangedEvent(
        SonosEventType.GROUP_COORDINATOR_CHANGED, OFFICE_GROUP, body
    )


def _updated() -> GroupCoordinatorChangedEvent:
    """Build the event the speaker sends when a member joins or leaves the group."""
    body = cast(
        "GroupCoordinatorChanged",
        {
            "_objectType": "groupCoordinatorChanged",
            "groupStatus": "GROUP_STATUS_UPDATED",
            "groupName": "Office + 1",
        },
    )
    return GroupCoordinatorChangedEvent(
        SonosEventType.GROUP_COORDINATOR_CHANGED, OFFICE_GROUP, body
    )


def _evicted() -> SessionErrorEvent:
    """Build the event a speaker sends when another app or source took the group over."""
    body = cast(
        "SessionError",
        {
            "_objectType": "sessionError",
            "errorCode": "ERROR_SESSION_EVICTED",
            "reason": "Another user joined the session.",
            "sessionId": "session1",
        },
    )
    return SessionErrorEvent(SonosEventType.SESSION_ERROR, OFFICE_GROUP, body)


class _Household:
    """Mocked Music Assistant around two Sonos players, delivering the events they wait on."""

    def __init__(self) -> None:
        self.mass = MagicMock()
        self.scheduled: list[Coroutine[Any, Any, None]] = []
        self.task_ids: list[str] = []
        self.listeners: list[Callable[..., None]] = []
        self.queue = MagicMock()
        self.queue.state = PlaybackState.IDLE
        # the new coordinator's queue once the queue was transferred to it
        self.target_queue = MagicMock()
        self.target_queue.current_item.queue_item_id = "track0"
        self.target_queue.current_item.media_type = MediaType.TRACK
        self.target_queue.resume_pos = 25.4

        def _create_task(coro: Coroutine[Any, Any, None], **kwargs: Any) -> MagicMock:
            self.scheduled.append(coro)
            self.task_ids.append(kwargs["task_id"])
            return MagicMock()

        def _subscribe(callback: Callable[..., None], *_args: object) -> MagicMock:
            self.listeners.append(callback)
            return MagicMock()

        self.mass.create_task.side_effect = _create_task
        self.mass.subscribe.side_effect = _subscribe
        self.mass.player_queues.get.side_effect = {
            OFFICE: self.queue,
            SYNC_GROUP: self.queue,
            BATHROOM: self.target_queue,
        }.get
        self.mass.player_queues.transfer_queue = AsyncMock()
        self.mass.player_queues.play_index = AsyncMock()
        self.office = self._player(OFFICE, _subscribe)
        self.office.cloud_queue_id = OFFICE
        self.bathroom = self._player(BATHROOM, _subscribe)
        self.report_coordinator(True)
        # what moved: the copy of Office's cloud queue, as the speaker reports its container
        self.serve(f"mass:{OFFICE}")
        players = {OFFICE: self.office, BATHROOM: self.bathroom}
        self.mass.players.get_player.side_effect = players.get

    def _player(self, player_id: str, subscribe: Callable[..., MagicMock]) -> SonosPlayer:
        """Create a connected SonosPlayer with a mocked client."""
        player = SonosPlayer.__new__(SonosPlayer)
        player.mass = self.mass
        player.logger = logging.getLogger("test.sonos.player")
        player._player_id = player_id
        player._attr_name = player_id
        player._cache = {}
        player._config = MagicMock()
        player._config.name = None
        player.connected = True
        player.cloud_queue_id = None
        player._flow_queue_id = None
        player.cloud_queue_item_generation = 1
        player._evicted_cloud_queue = None
        player._queue_session_id = "session1"
        player._hand_over_tasks = set()
        player.client = MagicMock()
        player.client.subscribe.side_effect = subscribe
        group = player.client.player.group
        # the group it coordinates plays the moved copy of our queue, not a session of its own
        group.active_service = MusicService.MUSIC_ASSISTANT
        group.playback_state = PlayBackState.PLAYBACK_STATE_PLAYING
        group.is_subscribed = True
        player.update_state = MagicMock()  # type: ignore[misc, method-assign]
        player.stop = AsyncMock()  # type: ignore[method-assign]
        return player

    @property
    def transfer(self) -> AsyncMock:
        """The core queue transfer the hand-over calls."""
        return cast("AsyncMock", self.mass.player_queues.transfer_queue)

    @property
    def play_index(self) -> AsyncMock:
        """The core call that starts the transferred queue."""
        return cast("AsyncMock", self.mass.player_queues.play_index)

    def serve(self, object_id: str) -> None:
        """Let Bathroom's group report the container it plays."""
        group = cast("MagicMock", self.bathroom.client).player.group
        group.playback_metadata = {"container": {"id": {"objectId": object_id}}}

    def report_coordinator(self, coordinates: bool) -> None:
        """Let Bathroom's own connection report whether it coordinates its group."""
        cast("MagicMock", self.bathroom.client).player.is_coordinator = coordinates

    def deliver(self) -> None:
        """Deliver a speaker or queue event to everything waiting on one."""
        for listener in list(self.listeners):
            listener(None)

    async def run_hand_over(self) -> asyncio.Future[None]:
        """Start the scheduled hand-over and let it reach its wait."""
        hand_over = asyncio.ensure_future(self.scheduled.pop())
        await asyncio.sleep(0)
        return hand_over


@pytest.mark.asyncio
async def test_a_moved_group_hands_our_queue_to_the_new_coordinator() -> None:
    """As when the leader of a group is removed in MA, the queue follows the music."""
    home = _Household()

    home.office._on_group_coordinator_changed(_moved())
    await home.scheduled.pop()

    home.transfer.assert_awaited_once_with(OFFICE, BATHROOM, auto_play=False)
    # started at the transferred position: core's resume would read the clock of the new
    # coordinator's queue, which reads as playing while its speaker plays the moved copy
    home.play_index.assert_awaited_once_with(BATHROOM, "track0", 25)
    cast("AsyncMock", home.bathroom.stop).assert_not_awaited()
    assert home.office.cloud_queue_id is None


@pytest.mark.asyncio
async def test_a_paused_group_hands_over_its_queue_without_playing() -> None:
    """A group that was paused is handed over paused, as core does."""
    home = _Household()
    group = cast("MagicMock", home.bathroom.client).player.group
    group.playback_state = PlayBackState.PLAYBACK_STATE_PAUSED

    home.office._on_group_coordinator_changed(_moved())
    await home.scheduled.pop()

    home.transfer.assert_awaited_once_with(OFFICE, BATHROOM, auto_play=False)
    home.play_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_moved_radio_stream_starts_from_the_live_edge() -> None:
    """A radio stream cannot be started part way in, as core's resume knows too."""
    home = _Household()
    home.target_queue.current_item.media_type = MediaType.RADIO

    home.office._on_group_coordinator_changed(_moved())
    await home.scheduled.pop()

    home.play_index.assert_awaited_once_with(BATHROOM, "track0", 0)


@pytest.mark.asyncio
async def test_a_transferred_queue_without_a_current_item_is_not_started() -> None:
    """A queue that moved without a current item has nothing to start."""
    home = _Household()
    home.target_queue.current_item = None

    home.office._on_group_coordinator_changed(_moved())
    await home.scheduled.pop()

    home.transfer.assert_awaited_once()
    home.play_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_hand_over_waits_for_the_new_coordinator_to_report_the_role() -> None:
    """The new coordinator's own connection learns of the move a moment later."""
    home = _Household()
    home.report_coordinator(False)

    home.office._on_group_coordinator_changed(_moved())
    hand_over = await home.run_hand_over()
    home.deliver()
    await asyncio.sleep(0)
    home.transfer.assert_not_awaited()
    home.report_coordinator(True)
    home.deliver()
    await hand_over

    home.transfer.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_hand_over_waits_for_the_new_coordinator_to_read_what_it_plays() -> None:
    """The role is reported before the group's status is in; the check needs the status."""
    home = _Household()
    group = cast("MagicMock", home.bathroom.client).player.group
    group.is_subscribed = False

    home.office._on_group_coordinator_changed(_moved())
    hand_over = await home.run_hand_over()
    home.transfer.assert_not_awaited()
    group.is_subscribed = True
    home.deliver()
    await hand_over

    home.transfer.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_hand_over_waits_until_our_queue_stops_playing_here() -> None:
    """An Arc that switched to TV still shows our queue playing for a moment."""
    home = _Household()
    home.queue.state = PlaybackState.PLAYING

    home.office._on_group_coordinator_changed(_moved())
    hand_over = await home.run_hand_over()
    home.deliver()
    await asyncio.sleep(0)
    # handing over now would stop this player, and the TV audio with it
    home.transfer.assert_not_awaited()
    home.queue.state = PlaybackState.IDLE
    home.deliver()
    await hand_over

    home.transfer.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_queue_that_keeps_playing_here_is_not_handed_over() -> None:
    """Without the queue going idle in time, nothing is handed over."""
    home = _Household()
    home.queue.state = PlaybackState.PLAYING

    with patch.object(player_module, "MOVED_GROUP_TIMEOUT", 0.05):
        home.office._on_group_coordinator_changed(_moved())
        await home.scheduled.pop()

    home.transfer.assert_not_awaited()
    assert home.office.cloud_queue_id == OFFICE


@pytest.mark.asyncio
async def test_nothing_is_handed_over_when_the_new_coordinator_never_takes_the_role() -> None:
    """A speaker that does not report coordinating the group in time is left alone."""
    home = _Household()
    home.report_coordinator(False)

    with patch.object(player_module, "MOVED_GROUP_TIMEOUT", 0.05):
        home.office._on_group_coordinator_changed(_moved())
        await home.scheduled.pop()

    home.transfer.assert_not_awaited()
    cast("AsyncMock", home.bathroom.stop).assert_not_awaited()
    assert home.office.cloud_queue_id == OFFICE


@pytest.mark.parametrize("lost", ["role", "status", "connection"])
@pytest.mark.asyncio
async def test_a_role_given_up_again_before_the_hand_over_runs_is_left_alone(lost: str) -> None:
    """The role can be lost again between the report that ends the wait and the hand-over."""
    home = _Household()
    home.report_coordinator(False)

    home.office._on_group_coordinator_changed(_moved())
    hand_over = await home.run_hand_over()
    home.report_coordinator(True)
    home.deliver()
    # before the hand-over resumes: the speaker metadata still shows our queue
    if lost == "role":
        home.report_coordinator(False)
    elif lost == "status":
        cast("MagicMock", home.bathroom.client).player.group.is_subscribed = False
    else:
        home.bathroom.connected = False
    await hand_over

    home.transfer.assert_not_awaited()
    cast("AsyncMock", home.bathroom.stop).assert_not_awaited()
    assert home.office.cloud_queue_id == OFFICE


@pytest.mark.asyncio
async def test_a_new_play_in_the_meantime_is_left_alone() -> None:
    """A queue loaded on this player while waiting is not what moved."""
    home = _Household()
    home.report_coordinator(False)

    home.office._on_group_coordinator_changed(_moved())
    hand_over = await home.run_hand_over()
    home.office.cloud_queue_id = "another_queue"
    home.report_coordinator(True)
    home.deliver()
    await hand_over

    home.transfer.assert_not_awaited()
    assert home.office.cloud_queue_id == "another_queue"


@pytest.mark.asyncio
async def test_the_same_queue_played_again_in_the_meantime_is_left_alone() -> None:
    """A reload of the queue on this player while waiting (a seek, a track) is a fresh play."""
    home = _Household()
    home.report_coordinator(False)

    home.office._on_group_coordinator_changed(_moved())
    hand_over = await home.run_hand_over()
    home.office.cloud_queue_item_generation += 1  # what play_media does for the same queue
    home.report_coordinator(True)
    home.deliver()
    await hand_over

    home.transfer.assert_not_awaited()
    assert home.office.cloud_queue_id == OFFICE


@pytest.mark.asyncio
async def test_a_new_coordinator_taken_over_in_the_meantime_is_left_alone() -> None:
    """Another app took the new coordinator before it reported the role."""
    home = _Household()
    home.report_coordinator(False)

    home.office._on_group_coordinator_changed(_moved())
    hand_over = await home.run_hand_over()
    home.serve("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
    home.report_coordinator(True)
    home.deliver()
    await hand_over

    home.transfer.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_new_coordinator_given_a_play_in_the_meantime_is_left_alone() -> None:
    """MA started playing on the new coordinator while waiting; its load is still out."""
    home = _Household()
    home.report_coordinator(False)

    home.office._on_group_coordinator_changed(_moved())
    hand_over = await home.run_hand_over()
    # what play_media does first; its session id stays empty until the load is answered
    home.bathroom.cloud_queue_item_generation += 1
    home.report_coordinator(True)
    home.deliver()
    await hand_over

    home.transfer.assert_not_awaited()
    cast("AsyncMock", home.bathroom.stop).assert_not_awaited()


@pytest.mark.asyncio
async def test_a_moved_group_of_a_group_players_queue_stops_playback() -> None:
    """Only a player's own queue is handed over; a group player's stops, as core dissolves it."""
    home = _Household()
    home.office.cloud_queue_id = SYNC_GROUP
    home.serve(f"mass:{SYNC_GROUP}")
    # the group player's queue does not follow this player's report
    home.queue.state = PlaybackState.PLAYING

    home.office._on_group_coordinator_changed(_moved())
    await home.scheduled.pop()

    cast("AsyncMock", home.bathroom.stop).assert_awaited_once()
    home.transfer.assert_not_awaited()
    assert home.office.cloud_queue_id is None


@pytest.mark.asyncio
async def test_a_refused_stop_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A stop the new coordinator refuses is a warning, not an error in a background task."""
    home = _Household()
    home.office.cloud_queue_id = SYNC_GROUP
    home.serve(f"mass:{SYNC_GROUP}")
    home.bathroom.stop = AsyncMock(  # type: ignore[method-assign]
        side_effect=FailedCommand("ERROR_COMMAND_FAILED")
    )

    with caplog.at_level(logging.WARNING):
        home.office._on_group_coordinator_changed(_moved())
        await home.scheduled.pop()

    assert "Could not stop playback" in caplog.text


@pytest.mark.asyncio
async def test_a_flow_mode_queue_is_handed_over_too() -> None:
    """A queue played as one stream moves with the group like a cloud queue."""
    home = _Household()
    home.office.cloud_queue_id = None
    home.office._flow_queue_id = OFFICE
    # a stream reports the container it was loaded with: mass:<queue>:<item>
    home.serve(f"mass:{OFFICE}:track0")

    home.office._on_group_coordinator_changed(_moved())
    await home.scheduled.pop()

    home.transfer.assert_awaited_once_with(OFFICE, BATHROOM, auto_play=False)
    assert home.office._flow_queue_id is None


@pytest.mark.asyncio
async def test_a_queue_this_player_no_longer_plays_is_not_handed_over() -> None:
    """A claim left from a queue that ran to its end does not replace what the group plays."""
    home = _Household()
    # Office played its own queue to the end long ago; the group carries another queue now
    home.serve("mass:kitchen_queue")

    home.office._on_group_coordinator_changed(_moved())
    await home.scheduled.pop()

    home.transfer.assert_not_awaited()
    home.play_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_queue_whose_id_only_starts_like_ours_is_not_ours() -> None:
    """The queue id is matched whole, up to the item separator of a stream."""
    home = _Household()
    home.serve(f"mass:{OFFICE}_party")

    home.office._on_group_coordinator_changed(_moved())
    await home.scheduled.pop()

    home.transfer.assert_not_awaited()


def test_a_group_not_playing_our_queue_is_left_alone() -> None:
    """With no queue of ours loaded, a move is none of Music Assistant's business."""
    home = _Household()
    home.office.cloud_queue_id = None

    home.office._on_group_coordinator_changed(_moved())

    assert home.scheduled == []


def test_a_membership_change_is_not_a_move() -> None:
    """GROUP_STATUS_UPDATED, sent when a member joins or leaves, changes nothing."""
    home = _Household()

    home.office._on_group_coordinator_changed(_updated())

    assert home.scheduled == []
    assert home.office.cloud_queue_id == OFFICE


def test_a_move_naming_this_player_is_not_handed_over() -> None:
    """A move that elects this player itself leaves the queue where it is."""
    home = _Household()

    home.office._on_group_coordinator_changed(_moved(OFFICE))

    assert home.scheduled == []
    assert home.office.cloud_queue_id == OFFICE


@pytest.mark.parametrize("known", [False, True], ids=["unknown", "disconnected"])
def test_a_move_to_a_speaker_we_do_not_control_is_only_logged(known: bool) -> None:
    """A speaker Music Assistant does not know, or cannot reach, is given nothing."""
    home = _Household()
    target = BATHROOM if known else "RINCON_UNKNOWN"
    home.bathroom.connected = False

    home.office._on_group_coordinator_changed(_moved(target))

    assert home.scheduled == []
    home.transfer.assert_not_awaited()
    cast("AsyncMock", home.bathroom.stop).assert_not_awaited()
    assert home.office.cloud_queue_id == OFFICE


def test_each_move_gets_its_own_hand_over() -> None:
    """A move after a new play here is a new one; the same move reported twice is not."""
    home = _Household()

    home.office._on_group_coordinator_changed(_moved())
    home.office._on_group_coordinator_changed(_moved())
    home.office.cloud_queue_item_generation += 1
    home.office._on_group_coordinator_changed(_moved())
    for coro in home.scheduled:
        coro.close()

    assert home.task_ids[0] == home.task_ids[1]
    assert home.task_ids[2] != home.task_ids[0]


@pytest.mark.asyncio
async def test_a_move_right_after_an_eviction_still_hands_over_our_queue() -> None:
    """The speaker may evict our session just before it reports the move."""
    home = _Household()
    home.office._on_session_error(_evicted())

    home.office._on_group_coordinator_changed(_moved())
    await home.scheduled.pop()

    home.transfer.assert_awaited_once_with(OFFICE, BATHROOM, auto_play=False)
    assert home.office._evicted_cloud_queue is None


def test_a_move_long_after_an_eviction_is_left_alone() -> None:
    """Once the grace period is over, an evicted queue is no longer ours to hand over."""
    home = _Household()
    home.office._on_session_error(_evicted())
    home.office._evicted_cloud_queue = (
        OFFICE,
        time.monotonic() - player_module.EVICTED_QUEUE_GRACE - 1,
    )

    home.office._on_group_coordinator_changed(_moved())

    assert home.scheduled == []
