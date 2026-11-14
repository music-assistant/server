"""What ends the power on of a speaker that a group or a leader takes along, and what does not."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerUnavailableError

from .harness import TEST_TRACK_URI
from .test_grouping import (
    _come_back,
    _group,
    _load_music,
    _player_id,
    _speakers,
    _state,
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
