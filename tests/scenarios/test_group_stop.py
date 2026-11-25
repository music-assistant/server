"""What a stop does with a group that plays nothing but still holds its members."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import PlaybackState

from .harness import TEST_TRACK_URI, FakeProtocol, Scenario
from .test_grouping import _group, _player_id, _speakers, _state, _stop

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

# how long (in seconds) the group played before its leader left
PLAYED_SECONDS = 30


async def _run(scenario: Scenario, command: Coroutine[Any, Any, None]) -> None:
    """Run a command, which is done within a second."""
    task = await scenario.command(command)
    await scenario.advance(1)
    assert task.done()
    await task


async def _group_without_its_leader(
    scenario: Scenario, *, fake_power: bool = False
) -> tuple[str, list[str]]:
    """
    Let Kitchen leave the playing sync group it leads, which then plays on two seconds later.

    Until then the group is silent, and holds Office and Den.

    :param fake_power: Whether the group has fake power control, and is on.
    :return: The id of the group, and those of Office and Den.
    """
    _, speakers = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id, *member_ids = (_player_id(scenario, speaker) for speaker in speakers)
    group_id = await _group(scenario, "sync_group", kitchen_id, *member_ids)
    if fake_power:
        await scenario.configure_player(group_id, power_control=PLAYER_CONTROL_FAKE)
    await scenario.play(group_id)
    await scenario.advance(PLAYED_SECONDS)
    await _run(scenario, scenario.mass.players.cmd_ungroup(kitchen_id))
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    for member_id in member_ids:
        assert _state(scenario, member_id).active_group == group_id
    scenario.timeline.clear()
    return group_id, member_ids


def _command(
    scenario: Scenario, command: str, group_id: str, member_id: str
) -> Coroutine[Any, Any, None]:
    """
    Return a command that ends what a group plays.

    :param command: Which command, and what it is sent to.
    :param group_id: The group.
    :param member_id: A member of the group.
    """
    players, queues = scenario.mass.players, scenario.mass.player_queues
    match command:
        case "stop of the group":
            return players.cmd_stop(group_id)
        case "stop of a member":
            return players.cmd_stop(member_id)
        case "stop of the queue":
            return queues.stop(group_id)
        case _:
            return players.cmd_ungroup(group_id)


@pytest.mark.parametrize(
    "command", ["stop of the group", "stop of a member", "stop of the queue", "ungroup"]
)
async def test_a_sync_group_that_is_stopped_between_two_leaders_stays_silent(
    scenario: Scenario, command: str
) -> None:
    """
    A sync group that is stopped or released before it plays on with its new leader stays silent.

    Its members are free again.

    :param command: The command that ends what the group plays.
    """
    group_id, member_ids = await _group_without_its_leader(scenario)

    await _run(scenario, _command(scenario, command, group_id, member_ids[0]))
    await scenario.advance(60)

    assert scenario.timeline == []
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    for member_id in member_ids:
        assert _state(scenario, member_id).active_group is None


@pytest.mark.parametrize("command", ["stop of the group", "stop of a member", "stop of the queue"])
async def test_a_powered_sync_group_that_is_stopped_between_two_leaders_stays_on_and_silent(
    scenario: Scenario, command: str
) -> None:
    """
    A sync group with fake power control that is stopped between two leaders stays silent.

    It is still on and keeps its members, as after any stop.

    :param command: The command that ends what the group plays.
    """
    group_id, member_ids = await _group_without_its_leader(scenario, fake_power=True)

    await _run(scenario, _command(scenario, command, group_id, member_ids[0]))
    await scenario.advance(60)

    assert scenario.timeline == []
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert _state(scenario, group_id).powered
    for member_id in member_ids:
        assert _state(scenario, member_id).active_group == group_id


async def test_a_sync_group_stopped_between_two_leaders_plays_on_from_there_when_it_is_played(
    scenario: Scenario,
) -> None:
    """A play on a sync group stopped between two leaders plays on where the music stopped."""
    group_id, (office_id, den_id) = await _group_without_its_leader(scenario)
    await _run(scenario, scenario.mass.players.cmd_stop(group_id))
    await scenario.advance(60)
    queue = scenario.queue(group_id)
    assert queue is not None
    assert queue.state == PlaybackState.IDLE

    await _run(scenario, scenario.mass.players.cmd_play(group_id))

    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, den_id).synced_to == office_id
    assert queue.elapsed_time == pytest.approx(PLAYED_SECONDS, abs=1)
    await _stop(scenario, group_id)


async def _ended_group(
    scenario: Scenario, group_type: str, *, dynamic: bool
) -> tuple[str, list[str], list[FakeProtocol]]:
    """
    Play a group of Kitchen and Office until its music has just ended.

    The group then holds its members for 10 s more.

    :param group_type: ``"sync_group"`` or ``"universal_group"``.
    :param dynamic: Whether its members can change; else it has a fixed member list.
    :return: The id of the group, and the ids and the protocols of Kitchen and Office.
    """
    _, speakers = await _speakers(scenario, "Kitchen", "Office")
    member_ids = [_player_id(scenario, speaker) for speaker in speakers]
    group_id = await _group(scenario, group_type, *member_ids, dynamic=dynamic)
    await scenario.play(group_id)
    await scenario.advance(PLAYED_SECONDS)
    for speaker in speakers:
        await scenario.report(speaker, playback_state=PlaybackState.IDLE, current_media=None)
    for _ in range(40):
        # a universal group looks at its members every 30 s
        if _state(scenario, group_id).playback_state == PlaybackState.IDLE:
            break
        await scenario.advance(1)
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    for member_id in member_ids:
        assert _state(scenario, member_id).active_group == group_id
    scenario.timeline.clear()
    return group_id, member_ids, speakers


@pytest.mark.parametrize("command", ["stop of the group", "stop of a member", "ungroup"])
@pytest.mark.parametrize("group_type", ["sync_group", "universal_group"])
async def test_a_group_that_is_stopped_after_its_music_ended_frees_its_members(
    scenario: Scenario, group_type: str, command: str
) -> None:
    """
    A group whose music just ended lets its members go at once when it is stopped or released.

    :param group_type: The type of the group.
    :param command: The command.
    """
    group_id, member_ids, _ = await _ended_group(scenario, group_type, dynamic=True)

    await _run(scenario, _command(scenario, command, group_id, member_ids[1]))

    for member_id in member_ids:
        assert _state(scenario, member_id).active_group is None
        assert _state(scenario, member_id).synced_to is None


async def test_a_member_of_a_fixed_group_whose_music_ended_plays_its_own_music_at_once(
    scenario: Scenario,
) -> None:
    """
    Music played on a member of a fixed group whose own music just ended starts at once.

    The group lets its members go first: a speaker that still follows another one refuses it.
    """
    _, (kitchen_id, office_id), (_, office) = await _ended_group(
        scenario, "sync_group", dynamic=False
    )

    await _run(scenario, scenario.mass.player_queues.play_media(office_id, TEST_TRACK_URI))

    assert [(protocol_id, command[0]) for protocol_id, command in scenario.timeline] == [
        ("sonos_kitchen", "set_members"),
        ("sonos_office", "play_media"),
    ]
    assert office.playback_state == PlaybackState.PLAYING
    for player_id in (kitchen_id, office_id):
        assert _state(scenario, player_id).active_group is None
    await _stop(scenario, office_id)
