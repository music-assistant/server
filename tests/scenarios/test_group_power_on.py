"""What ends the power on of a speaker that a group or a leader takes along, and what does not."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerUnavailableError

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
