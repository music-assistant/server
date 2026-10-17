"""Tests for a Sonos group whose coordinator the speakers moved outside Music Assistant."""

import asyncio
import logging
import time
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosonos.api.models import MusicService
from aiosonos.const import EventType as SonosEventType
from aiosonos.const import GroupCoordinatorChangedEvent, SessionErrorEvent
from aiosonos.exceptions import FailedCommand

from music_assistant.providers.sonos import player as player_module
from music_assistant.providers.sonos.player import SonosPlayer

if TYPE_CHECKING:
    from aiosonos.api.models import GroupCoordinatorChanged, SessionError

OFFICE = "RINCON_F0F6C1CC8D0401400"
BATHROOM = "RINCON_48A6B86DD3F101400"
OFFICE_GROUP = "RINCON_F0F6C1CC8D0401400:140007645"


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


def _player(mass: MagicMock, player_id: str) -> SonosPlayer:
    """Create a connected SonosPlayer with a mocked client."""
    player = SonosPlayer.__new__(SonosPlayer)
    player.mass = mass
    player.logger = logging.getLogger("test.sonos.player")
    player._player_id = player_id
    player._attr_name = player_id
    player._cache = {}
    player._config = MagicMock()
    player._config.name = None
    player.connected = True
    player.cloud_queue_id = None
    player.cloud_queue_item_generation = 1
    player._evicted_cloud_queue = None
    player.client = MagicMock()
    # the group it coordinates plays our queue under the session it inherited, not one of its own
    player.client.player.group.active_session_id = None
    player.client.player.group.active_service = MusicService.MUSIC_ASSISTANT
    player.update_state = MagicMock()  # type: ignore[misc, method-assign]
    player.stop = AsyncMock()  # type: ignore[method-assign]
    return player


def _report_coordinator(player: SonosPlayer, coordinates: bool) -> None:
    """Let the player's own connection report whether it coordinates its group."""
    cast("MagicMock", player.client).player.is_coordinator = coordinates


def _office_and_bathroom() -> tuple[SonosPlayer, SonosPlayer, list[Coroutine[Any, Any, None]]]:
    """Office plays our queue; Bathroom is the member the speakers will elect."""
    mass = MagicMock()
    scheduled: list[Coroutine[Any, Any, None]] = []
    mass.create_task.side_effect = lambda coro, **_kwargs: scheduled.append(coro)
    office = _player(mass, OFFICE)
    office.cloud_queue_id = OFFICE
    bathroom = _player(mass, BATHROOM)
    _report_coordinator(bathroom, True)
    mass.players.get_player.side_effect = {OFFICE: office, BATHROOM: bathroom}.get
    return office, bathroom, scheduled


@pytest.mark.asyncio
async def test_a_moved_group_stops_our_queue_on_the_new_coordinator() -> None:
    """The speaker the group moved to stops playing; this player no longer serves the queue."""
    office, bathroom, scheduled = _office_and_bathroom()

    office._on_group_coordinator_changed(_moved())
    await scheduled.pop()

    cast("AsyncMock", bathroom.stop).assert_awaited_once()
    cast("AsyncMock", office.stop).assert_not_awaited()
    assert office.cloud_queue_id is None


@pytest.mark.asyncio
async def test_the_stop_waits_for_the_new_coordinator_to_report_the_role() -> None:
    """The new coordinator's own connection learns of the move a moment later."""
    office, bathroom, scheduled = _office_and_bathroom()
    _report_coordinator(bathroom, False)

    office._on_group_coordinator_changed(_moved())
    stop = asyncio.ensure_future(scheduled.pop())
    await asyncio.sleep(0.15)
    cast("AsyncMock", bathroom.stop).assert_not_awaited()
    _report_coordinator(bathroom, True)
    await stop

    cast("AsyncMock", bathroom.stop).assert_awaited_once()


@pytest.mark.asyncio
async def test_nothing_is_stopped_when_the_new_coordinator_never_takes_the_role() -> None:
    """A speaker that does not report coordinating the group in time is left alone."""
    office, bathroom, scheduled = _office_and_bathroom()
    _report_coordinator(bathroom, False)

    with patch.object(player_module, "MOVED_GROUP_TIMEOUT", 0.2):
        office._on_group_coordinator_changed(_moved())
        await scheduled.pop()

    cast("AsyncMock", bathroom.stop).assert_not_awaited()
    assert office.cloud_queue_id == OFFICE


@pytest.mark.asyncio
async def test_a_new_play_in_the_meantime_is_left_alone() -> None:
    """A queue loaded on this player while waiting is not what moved, so nothing is stopped."""
    office, bathroom, scheduled = _office_and_bathroom()
    _report_coordinator(bathroom, False)

    office._on_group_coordinator_changed(_moved())
    stop = asyncio.ensure_future(scheduled.pop())
    await asyncio.sleep(0)
    office.cloud_queue_id = "another_queue"
    _report_coordinator(bathroom, True)
    await stop

    cast("AsyncMock", bathroom.stop).assert_not_awaited()
    assert office.cloud_queue_id == "another_queue"


@pytest.mark.asyncio
async def test_the_same_queue_played_again_in_the_meantime_is_left_alone() -> None:
    """A reload of the queue on this player while waiting (a seek, a track) is a fresh play."""
    office, bathroom, scheduled = _office_and_bathroom()
    _report_coordinator(bathroom, False)

    office._on_group_coordinator_changed(_moved())
    stop = asyncio.ensure_future(scheduled.pop())
    await asyncio.sleep(0)
    office.cloud_queue_item_generation += 1  # what play_media does for the same queue
    _report_coordinator(bathroom, True)
    await stop

    cast("AsyncMock", bathroom.stop).assert_not_awaited()
    assert office.cloud_queue_id == OFFICE


@pytest.mark.asyncio
async def test_the_stop_waits_for_the_new_coordinator_to_read_what_it_plays() -> None:
    """The role is reported before the group's status is in; the check needs the status."""
    office, bathroom, scheduled = _office_and_bathroom()
    cast("MagicMock", bathroom.client).player.group.is_subscribed = False

    office._on_group_coordinator_changed(_moved())
    stop = asyncio.ensure_future(scheduled.pop())
    await asyncio.sleep(0.15)
    cast("AsyncMock", bathroom.stop).assert_not_awaited()
    cast("MagicMock", bathroom.client).player.group.is_subscribed = True
    await stop

    cast("AsyncMock", bathroom.stop).assert_awaited_once()


@pytest.mark.asyncio
async def test_a_new_coordinator_taken_over_in_the_meantime_is_left_alone() -> None:
    """Another app took the new coordinator before it reported the role: not ours to stop."""
    office, bathroom, scheduled = _office_and_bathroom()
    _report_coordinator(bathroom, False)

    office._on_group_coordinator_changed(_moved())
    stop = asyncio.ensure_future(scheduled.pop())
    await asyncio.sleep(0)
    cast("MagicMock", bathroom.client).player.group.active_service = MusicService.SPOTIFY
    _report_coordinator(bathroom, True)
    await stop

    cast("AsyncMock", bathroom.stop).assert_not_awaited()


@pytest.mark.asyncio
async def test_a_new_coordinator_given_its_own_session_in_the_meantime_is_left_alone() -> None:
    """Music Assistant started playing on the new coordinator itself while waiting."""
    office, bathroom, scheduled = _office_and_bathroom()
    _report_coordinator(bathroom, False)

    office._on_group_coordinator_changed(_moved())
    stop = asyncio.ensure_future(scheduled.pop())
    await asyncio.sleep(0)
    cast("MagicMock", bathroom.client).player.group.active_session_id = "session2"
    _report_coordinator(bathroom, True)
    await stop

    cast("AsyncMock", bathroom.stop).assert_not_awaited()


def test_a_group_not_playing_our_queue_is_left_alone() -> None:
    """With no queue of ours loaded, a move is none of Music Assistant's business."""
    office, _, scheduled = _office_and_bathroom()
    office.cloud_queue_id = None

    office._on_group_coordinator_changed(_moved())

    assert scheduled == []


def test_a_membership_change_is_not_a_move() -> None:
    """GROUP_STATUS_UPDATED, sent when a member joins or leaves, changes nothing."""
    office, _, scheduled = _office_and_bathroom()

    office._on_group_coordinator_changed(_updated())

    assert scheduled == []
    assert office.cloud_queue_id == OFFICE


@pytest.mark.asyncio
async def test_a_move_to_a_speaker_we_do_not_control_is_only_logged() -> None:
    """A speaker Music Assistant does not know (or has disabled) cannot be stopped."""
    office, _, scheduled = _office_and_bathroom()

    office._on_group_coordinator_changed(_moved("RINCON_UNKNOWN"))
    await scheduled.pop()

    assert office.cloud_queue_id == OFFICE


@pytest.mark.asyncio
async def test_a_refused_stop_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A stop the new coordinator refuses is a warning, not an error in a background task."""
    office, bathroom, scheduled = _office_and_bathroom()
    bathroom.stop = AsyncMock(side_effect=FailedCommand("ERROR_COMMAND_FAILED"))  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        office._on_group_coordinator_changed(_moved())
        await scheduled.pop()

    assert "Could not stop playback" in caplog.text


@pytest.mark.asyncio
async def test_a_move_right_after_an_eviction_still_stops_our_queue() -> None:
    """The speaker may evict our session just before it reports the move."""
    office, bathroom, scheduled = _office_and_bathroom()
    office._on_session_error(_evicted())

    office._on_group_coordinator_changed(_moved())
    await scheduled.pop()

    cast("AsyncMock", bathroom.stop).assert_awaited_once()
    assert office._evicted_cloud_queue is None


def test_a_move_long_after_an_eviction_is_left_alone() -> None:
    """Once the grace period is over, an evicted queue is no longer ours to stop."""
    office, _, scheduled = _office_and_bathroom()
    office._on_session_error(_evicted())
    office._evicted_cloud_queue = (OFFICE, time.monotonic() - player_module.EVICTED_QUEUE_GRACE - 1)

    office._on_group_coordinator_changed(_moved())

    assert scheduled == []
