"""What ends the power on of a speaker that a group or a leader takes along, and what does not."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError

from music_assistant.controllers.players.constants import PlayerLockPurpose

from .harness import TEST_TRACK_URI
from .test_grouping import (
    _come_back,
    _group,
    _grouping_commands,
    _load_music,
    _player_id,
    _speakers,
    _state,
    _stop,
    _switch_off,
)

if TYPE_CHECKING:
    from .harness import Scenario


async def _owner(scenario: Scenario, owner_type: str, leader_id: str, member_id: str) -> str:
    """
    Make one player the owner of the playback of another and return the id of the owner.

    :param owner_type: ``"leader"`` for a player that leads the other one, else the type of
        group player to create of the two: ``"sync_group"`` or ``"universal_group"``.
    :param leader_id: The player that leads, or the first member of the group.
    :param member_id: The player that follows, or the second member of the group.
    """
    if owner_type == "leader":
        await scenario.mass.players.cmd_set_members(leader_id, player_ids_to_add=[member_id])
        await scenario.idle()
        return leader_id
    return await _group(scenario, owner_type, leader_id, member_id)


@pytest.mark.parametrize("owner_type", ["leader", "sync_group", "universal_group"])
@pytest.mark.parametrize("command", ["stop", "pause"])
async def test_a_speaker_whose_owner_is_stopped_while_it_waits_to_join_stays_out(
    scenario: Scenario, owner_type: str, command: str
) -> None:
    """
    A stop or a pause of a leader or group keeps out a speaker that was about to join it.

    The speaker was added while it was switched off at its plug. Its device is back while
    another command holds the owner, so its join waits. The owner is stopped or paused
    meanwhile: the speaker gets no command, is idle and in no group.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    owner_id = await _owner(scenario, owner_type, kitchen_id, office_id)
    plug = await _switch_off(scenario, den)
    players = scenario.mass.players
    await scenario.play(owner_id)
    await players.cmd_set_members(owner_id, player_ids_to_add=[den_id])
    await scenario.settle()
    assert plug.commands == [True]

    async with players.get_player_lock(owner_id, PlayerLockPurpose.PLAYBACK):
        await _come_back(scenario, den)
        call_off = await scenario.command(
            players.cmd_stop(owner_id)
            if command == "stop"
            else scenario.mass.player_queues.pause(owner_id)
        )
        await scenario.advance(2)
    await scenario.advance(10)
    await call_off
    await scenario.idle()

    assert den.commands == []
    assert ("set_members", ["sonos_den"], None) not in _grouping_commands(kitchen)
    assert _state(scenario, den_id).synced_to is None
    assert _state(scenario, den_id).active_group is None
    assert _state(scenario, den_id).playback_state == PlaybackState.IDLE
    assert plug.commands == [True]
    if command == "pause":
        await _stop(scenario, owner_id)


@pytest.mark.parametrize("owner_type", ["leader", "sync_group", "universal_group"])
async def test_a_speaker_that_is_removed_before_it_is_online_stays_out(
    scenario: Scenario, owner_type: str
) -> None:
    """
    A speaker that is added while it is switched off, and removed again, does not join.

    Its plug was switched on for the leader or group it was added to. It is removed from that
    owner before its device is back. Once the device is back the speaker gets no command, is
    idle and in no group, and its owner does not list it.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    owner_id = await _owner(scenario, owner_type, kitchen_id, office_id)
    plug = await _switch_off(scenario, den)
    players = scenario.mass.players
    await scenario.play(owner_id)
    await players.cmd_set_members(owner_id, player_ids_to_add=[den_id])
    await scenario.settle()
    assert plug.commands == [True]

    await players.cmd_set_members(owner_id, player_ids_to_remove=[den_id])
    await scenario.settle()
    await _come_back(scenario, den)
    await scenario.idle()

    assert den.commands == []
    assert den_id not in _state(scenario, owner_id).group_members
    assert _state(scenario, den_id).synced_to is None
    assert _state(scenario, den_id).active_group is None
    assert plug.commands == [True]
    await _stop(scenario, owner_id)


@pytest.mark.parametrize("group_type", ["sync_group", "universal_group"])
async def test_a_power_off_of_a_group_that_waits_for_its_members_ends_the_wait(
    scenario: Scenario, group_type: str
) -> None:
    """
    A power off of a group that still waits for its first member ends the play that waits.

    All members are switched off at their plug, so the play of the group switches them on and
    waits. The group, which is off itself until it plays, is powered off: the play fails, and
    the group stays idle when a device is back after that.
    """
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    group_id = await _group(scenario, group_type, kitchen_id, office_id)
    await scenario.configure_player(group_id, power_control=PLAYER_CONTROL_FAKE)
    kitchen_plug = await _switch_off(scenario, kitchen)
    office_plug = await _switch_off(scenario, office)
    await _load_music(scenario)
    play = await scenario.command(scenario.mass.player_queues.play_media(group_id, TEST_TRACK_URI))
    assert not play.done(), play.exception()
    assert _state(scenario, group_id).powered is False

    power_off = await scenario.command(scenario.mass.players.cmd_power(group_id, False))
    await scenario.advance(1)
    await power_off
    await _come_back(scenario, office)
    await scenario.advance(10)

    assert play.done()
    with pytest.raises(PlayerUnavailableError):
        await play
    await scenario.idle()
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert kitchen.commands == office.commands == []
    assert (kitchen_plug.commands, office_plug.commands) == ([True], [True])


@pytest.mark.parametrize("group_type", ["sync_group", "universal_group"])
async def test_a_group_that_is_powered_on_waits_for_the_first_member_that_is_back(
    scenario: Scenario, group_type: str
) -> None:
    """
    A power on of a group whose members are all switched off waits for the first one.

    The plugs of the members are switched on, and the group is on once the first device is
    back.
    """
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    group_id = await _group(scenario, group_type, kitchen_id, office_id)
    await scenario.configure_player(group_id, power_control=PLAYER_CONTROL_FAKE)
    kitchen_plug = await _switch_off(scenario, kitchen)
    office_plug = await _switch_off(scenario, office)

    power_on = await scenario.command(scenario.mass.players.cmd_power(group_id, True))

    assert not power_on.done(), power_on.exception()
    assert (kitchen_plug.commands, office_plug.commands) == ([True], [True])
    assert _state(scenario, group_id).powered is False

    await _come_back(scenario, office)
    await power_on
    await scenario.idle()

    assert _state(scenario, group_id).powered is True
    power_off = await scenario.command(scenario.mass.players.cmd_power(group_id, False))
    await scenario.advance(10)
    await power_off


@pytest.mark.parametrize("group_type", ["sync_group", "universal_group"])
async def test_a_power_control_that_refuses_leaves_the_member_out(
    scenario: Scenario, group_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A member whose plug refuses to switch on is left out, which is logged once.

    The group, which has a fixed member list, plays on the other members. The warning names
    the member and the group. A later play of the group, when the plug works again, takes the
    member along.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    group_id = await _group(scenario, group_type, kitchen_id, office_id, den_id, dynamic=False)
    plug = await _switch_off(scenario, den)
    control = scenario.mass.players.get_player_control(plug.control_id)
    assert control is not None
    power_on = control.power_on

    async def _refuse() -> None:
        raise PlayerCommandFailed("The plug does not answer")

    control.power_on = _refuse
    scenario.expect_log("The plug does not answer")

    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        await scenario.play(group_id)
        await scenario.advance(2)

    assert [
        message
        for record in caplog.records
        if "The plug does not answer" in (message := record.getMessage())
    ] == [
        "Player Den could not be powered on, it is left out of Downstairs: The plug does not answer"
    ]
    assert plug.commands == []
    assert _state(scenario, den_id).powered is False
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING

    control.power_on = power_on
    await scenario.play(group_id)
    assert plug.commands == [True]
    await _come_back(scenario, den)
    await scenario.idle()

    assert _state(scenario, den_id).playback_state == PlaybackState.PLAYING
    assert den_id in _state(scenario, group_id).group_members
    await _stop(scenario, group_id)


async def test_a_universal_group_plays_once_on_a_member_that_is_back_before_the_play(
    scenario: Scenario,
) -> None:
    """
    A member whose device is back before the group starts gets the stream of the group once.

    The play of the group switched the member on and waits for another command that holds the
    group. The device is back meanwhile, so the play starts on it with the other members.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    group_id = await _group(scenario, "universal_group", kitchen_id, office_id, den_id)
    plug = await _switch_off(scenario, den)
    await _load_music(scenario)
    busy = asyncio.Event()

    async def _hold_the_group() -> None:
        async with scenario.mass.players.get_player_lock(group_id, PlayerLockPurpose.PLAYBACK):
            await busy.wait()

    holder = await scenario.command(_hold_the_group())
    play = await scenario.command(scenario.mass.player_queues.play_media(group_id, TEST_TRACK_URI))
    assert plug.commands == [True]
    await _come_back(scenario, den)
    busy.set()
    await scenario.advance(5)
    await holder
    await play
    await scenario.idle()

    assert [command[0] for command in den.commands] == ["play_media"]
    assert _state(scenario, den_id).active_group == group_id
    assert _state(scenario, den_id).playback_state == PlaybackState.PLAYING
    await _stop(scenario, group_id)


async def test_a_play_on_a_member_of_a_group_switches_on_the_members_that_are_off(
    scenario: Scenario,
) -> None:
    """
    A play on a member of a group player goes to the group, which takes its members along.

    The sync group, which has a fixed member list, plays on Kitchen and Office: the plug of
    Den refused to switch on. The group is paused and the plug works again. The user presses
    play on Office: the group plays on, Den's plug is switched on, and Den joins once its
    device is back.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    group_id = await _group(scenario, "sync_group", kitchen_id, office_id, den_id, dynamic=False)
    plug = await _switch_off(scenario, den)
    control = scenario.mass.players.get_player_control(plug.control_id)
    assert control is not None
    power_on = control.power_on

    async def _refuse() -> None:
        raise PlayerCommandFailed("The plug does not answer")

    control.power_on = _refuse
    scenario.expect_log("The plug does not answer")
    await scenario.play(group_id)
    control.power_on = power_on
    pause = await scenario.command(scenario.mass.players.cmd_pause(group_id))
    await scenario.advance(2)
    await pause
    assert _state(scenario, group_id).playback_state == PlaybackState.PAUSED
    assert plug.commands == []

    play = await scenario.command(scenario.mass.players.cmd_play(office_id))
    await scenario.advance(5)
    await play

    assert plug.commands == [True]
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    await _come_back(scenario, den)
    await scenario.idle()

    assert _state(scenario, den_id).synced_to == kitchen_id
    await _stop(scenario, group_id)
