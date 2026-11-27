"""What grouping does: which protocol carries each member, who leads, what is released."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pytest
from aiosonos.exceptions import CommandTimeout
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import (
    InsufficientPermissions,
    PlayerCommandFailed,
    UnsupportedFeaturedException,
)
from music_assistant_models.player import PlayerSource

from music_assistant.constants import DEFAULT_POWER_ON_STARTUP_TIME
from music_assistant.controllers.players.power import EXTERNAL_POWER_OFF_STOP_DELAY
from tests.common import as_user

from .harness import (
    DEFAULT_FEATURES,
    MUSIC_PROVIDER,
    TEST_TRACK_URI,
    FakePlayerProvider,
    FakePowerControl,
    FakeProtocol,
    Scenario,
)

if TYPE_CHECKING:
    from music_assistant_models.player import Player as PlayerState

GROUPING = frozenset({*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS})

# the start-up time (in seconds) of a speaker on a smart plug that cuts its power
STARTUP_TIME = DEFAULT_POWER_ON_STARTUP_TIME


def _device(number: int) -> dict[IdentifierType, str]:
    """Return the identifiers of one device of the scenario."""
    return {
        IdentifierType.MAC_ADDRESS: f"00:11:22:33:44:{number:02x}",
        IdentifierType.IP_ADDRESS: f"192.168.1.{number}",
    }


async def _speakers(
    scenario: Scenario, *names: str, grouping: bool = True
) -> tuple[FakePlayerProvider, list[FakeProtocol]]:
    """
    Attach native speakers of one make that can all group with each other.

    :param names: The names of the speakers, which also make their protocol ids.
    :param grouping: Whether they support grouping at all.
    """
    sonos = await scenario.add_provider("sonos")
    ids = [f"sonos_{name.lower()}" for name in names]
    speakers = [
        sonos.protocol(
            protocol_id,
            name,
            identifiers=_device(number),
            supported_features=set(GROUPING if grouping else DEFAULT_FEATURES),
            can_group_with={other for other in ids if other != protocol_id},
        )
        for number, (protocol_id, name) in enumerate(zip(ids, names, strict=True), start=10)
    ]
    for speaker in speakers:
        await scenario.register(speaker)
    await scenario.idle()
    return sonos, speakers


def _player_id(scenario: Scenario, protocol: FakeProtocol) -> str:
    """Return the id of the player a protocol is attached to."""
    player_id = scenario.player_of(protocol)
    assert player_id is not None
    return player_id


def _state(scenario: Scenario, player_id: str) -> PlayerState:
    """Return the state a player publishes."""
    state = scenario.player(player_id)
    assert state is not None
    return state


def _active_output(scenario: Scenario, player_id: str) -> str | None:
    """Return the output a player plays through."""
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    return player.active_output_protocol


def _grouping_commands(protocol: FakeProtocol) -> list[tuple[object, ...]]:
    """Return the grouping commands a protocol received, in order."""
    return [command for command in protocol.commands if command[0] in ("set_members", "ungroup")]


def _grouping_timeline(scenario: Scenario) -> list[tuple[str, tuple[object, ...]]]:
    """Return the grouping commands all devices received, in order, with the protocol id."""
    return [entry for entry in scenario.timeline if entry[1][0] in ("set_members", "ungroup")]


async def test_speakers_of_one_make_group_on_their_own_protocol(scenario: Scenario) -> None:
    """
    Speakers of one make group through their own protocol.

    The leader is asked to add the member by the id its provider knows it by, and the member
    shows as synced to the leader.
    """
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)

    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id])
    await scenario.idle()

    assert _grouping_commands(kitchen) == [("set_members", ["sonos_office"], None)]
    assert _state(scenario, office_id).synced_to == kitchen_id
    assert _state(scenario, kitchen_id).group_members == [kitchen_id, office_id]


async def test_a_member_leaves_its_group_when_it_is_ungrouped(scenario: Scenario) -> None:
    """Ungrouping a member asks its leader to let it go; the leader keeps its other members."""
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id, den_id])
    await scenario.idle()

    await scenario.mass.players.cmd_ungroup(office_id)
    await scenario.idle()

    assert _grouping_commands(kitchen)[-1] == ("set_members", None, ["sonos_office"])
    assert _state(scenario, office_id).synced_to is None
    assert _state(scenario, kitchen_id).group_members == [kitchen_id, den_id]


async def test_ungrouping_an_idle_leader_dissolves_its_group(scenario: Scenario) -> None:
    """A leader that plays nothing and is ungrouped lets all its members go."""
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id])
    await scenario.idle()

    await scenario.mass.players.cmd_ungroup(kitchen_id)
    await scenario.idle()

    assert _grouping_commands(kitchen)[-1] == ("set_members", None, ["sonos_office"])
    assert _state(scenario, office_id).synced_to is None
    assert _state(scenario, kitchen_id).group_members == []


async def test_a_speaker_that_can_not_group_with_the_leader_is_left_out(
    scenario: Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    """A speaker the leader can not group with stays out; the others join, and it is logged."""
    sonos, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    garden = sonos.protocol("sonos_garden", "Garden", identifiers=_device(30))
    await scenario.register(garden)
    await scenario.idle()
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    garden_id = _player_id(scenario, garden)
    scenario.expect_log("can not be grouped with")

    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        await scenario.mass.players.cmd_set_members(
            kitchen_id, player_ids_to_add=[garden_id, office_id]
        )
        await scenario.idle()

    assert _grouping_commands(kitchen) == [("set_members", ["sonos_office"], None)]
    assert _state(scenario, garden_id).synced_to is None
    assert "can not be grouped with" in caplog.text


async def test_an_unavailable_speaker_is_left_out(
    scenario: Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    """A speaker that can not play right now stays out of the group, and it is logged."""
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    await scenario.report(den, available=False)
    await scenario.idle()
    scenario.expect_log("is not available")

    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        await scenario.mass.players.cmd_set_members(
            kitchen_id, player_ids_to_add=[den_id, office_id]
        )
        await scenario.idle()

    assert _grouping_commands(kitchen) == [("set_members", ["sonos_office"], None)]
    assert "is not available" in caplog.text


async def test_a_speaker_moves_over_from_the_group_it_is_in(scenario: Scenario) -> None:
    """A speaker in another group leaves that group first, then joins the new leader."""
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    await scenario.mass.players.cmd_set_members(den_id, player_ids_to_add=[office_id])
    await scenario.idle()

    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id])
    await scenario.idle()

    assert _grouping_timeline(scenario)[-2:] == [
        ("sonos_den", ("set_members", None, ["sonos_office"])),
        ("sonos_kitchen", ("set_members", ["sonos_office"], None)),
    ]
    assert _grouping_commands(kitchen) == [("set_members", ["sonos_office"], None)]
    assert _state(scenario, office_id).synced_to == kitchen_id


async def test_a_speaker_without_grouping_can_not_lead(scenario: Scenario) -> None:
    """A group change on a speaker that can not group is refused before anything happens."""
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office", grouping=False)
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)

    with pytest.raises(UnsupportedFeaturedException):
        await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id])

    assert _grouping_commands(kitchen) == []
    assert _state(scenario, kitchen_id).playback_state == PlaybackState.IDLE


async def test_a_leader_that_leaves_its_playing_group_hands_it_to_a_member(
    scenario: Scenario,
) -> None:
    """
    Ungrouping a leader that plays its own queue keeps the group playing under a member.

    The queue moves to the first member, the other members regroup under it, and the old
    leader leaves.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id, den_id])
    await scenario.play(kitchen_id)

    await scenario.mass.players.cmd_ungroup(kitchen_id)
    await scenario.idle()

    assert _state(scenario, office_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, office_id).group_members == [office_id, den_id]
    assert _state(scenario, den_id).synced_to == office_id
    assert _state(scenario, kitchen_id).synced_to is None
    assert _state(scenario, kitchen_id).group_members == []
    office_queue, kitchen_queue = scenario.queue(office_id), scenario.queue(kitchen_id)
    assert office_queue is not None
    assert office_queue.current_item is not None
    assert office_queue.current_item.uri == TEST_TRACK_URI
    assert _state(scenario, office_id).active_source == office_id
    assert kitchen_queue is not None
    assert kitchen_queue.items == 0


async def _mixed_makes(scenario: Scenario) -> tuple[FakeProtocol, str, str]:
    """
    Attach a Sonos and a WiiM speaker that both have AirPlay, and nothing else in common.

    Returns the AirPlay protocol of the Sonos and the ids of the two players.
    """
    sonos = await scenario.add_provider("sonos")
    wiim = await scenario.add_provider("wiim")
    airplay = await scenario.add_provider("airplay")
    lounge = sonos.protocol(
        "sonos_lounge", "Lounge", identifiers=_device(40), supported_features=set(GROUPING)
    )
    study = wiim.protocol("wiim_study", "Study", identifiers=_device(41))
    lounge_airplay = airplay.protocol(
        "airplay_lounge",
        "Lounge",
        role="audio",
        identifiers=_device(40),
        supported_features=set(GROUPING),
        can_group_with={"airplay_study"},
    )
    study_airplay = airplay.protocol(
        "airplay_study",
        "Study",
        role="audio",
        identifiers=_device(41),
        supported_features=set(GROUPING),
        can_group_with={"airplay_lounge"},
    )
    for protocol in (lounge, lounge_airplay, study, study_airplay):
        await scenario.register(protocol)
    await scenario.idle()
    return lounge_airplay, _player_id(scenario, lounge), _player_id(scenario, study)


async def test_speakers_of_different_makes_group_over_a_shared_audio_protocol(
    scenario: Scenario,
) -> None:
    """
    Speakers that can not group natively group over an audio protocol both have.

    The leader's AirPlay protocol takes the member's AirPlay protocol, and both players play
    through AirPlay from then on.
    """
    lounge_airplay, lounge_id, study_id = await _mixed_makes(scenario)

    await scenario.mass.players.cmd_set_members(lounge_id, player_ids_to_add=[study_id])
    await scenario.idle()

    assert _grouping_commands(lounge_airplay) == [("set_members", ["airplay_study"], None)]
    assert _state(scenario, study_id).synced_to == lounge_id
    assert _active_output(scenario, lounge_id) == "airplay_lounge"
    assert _active_output(scenario, study_id) == "airplay_study"


async def test_a_native_session_moves_to_the_protocol_a_new_member_needs(
    scenario: Scenario,
) -> None:
    """
    A leader that plays on its own protocol moves its playback to AirPlay for a new member.

    The member can only follow over AirPlay, so the leader's output becomes its AirPlay
    protocol and the playback resumes there.
    """
    lounge_airplay, lounge_id, study_id = await _mixed_makes(scenario)
    await scenario.play(lounge_id)

    await scenario.mass.players.cmd_set_members(lounge_id, player_ids_to_add=[study_id])
    await scenario.idle()

    assert _grouping_commands(lounge_airplay) == [("set_members", ["airplay_study"], None)]
    assert any(command[0] == "play_media" for command in lounge_airplay.commands)
    assert _active_output(scenario, lounge_id) == "airplay_lounge"
    assert _state(scenario, lounge_id).playback_state == PlaybackState.PLAYING


async def test_a_leader_paused_during_the_join_moves_its_output_without_resuming(
    scenario: Scenario,
) -> None:
    """
    A leader paused at the device during the join hands its output over without starting playback.

    The playback moves to the leader's AirPlay protocol for the new member as it does while it
    plays, but nothing resumes it there.
    """
    lounge_airplay, lounge_id, study_id = await _mixed_makes(scenario)
    lounge = scenario.protocol("sonos_lounge")
    await scenario.play(lounge_id)
    join = lounge_airplay.set_members

    async def _paused_on_the_device_while_it_joins(
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        await scenario.report(lounge, playback_state=PlaybackState.PAUSED)
        await join(player_ids_to_add, player_ids_to_remove)

    lounge_airplay.set_members = _paused_on_the_device_while_it_joins  # type: ignore[method-assign]

    await scenario.mass.players.cmd_set_members(lounge_id, player_ids_to_add=[study_id])
    await scenario.idle()

    assert _grouping_commands(lounge_airplay) == [("set_members", ["airplay_study"], None)]
    assert _active_output(scenario, lounge_id) == "airplay_lounge"
    assert not any(command[0] in ("play_media", "play") for command in lounge_airplay.commands)
    assert _state(scenario, study_id).synced_to == lounge_id
    assert _state(scenario, lounge_id).playback_state != PlaybackState.PLAYING


async def test_a_member_is_controlled_through_its_leader_and_leaves_on_its_own_access(
    scenario: Scenario,
) -> None:
    """
    A pause, resume or stop on a member reaches its leader and needs access to the leader too.

    Leaving the group needs access to the member only.
    """
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id])
    await scenario.idle()
    await scenario.play(kitchen_id)
    office.commands.clear()
    players = scenario.mass.players

    # resume follows pause before the queue's pause timeout stops a paused player
    for command, reaches_leader, state in (
        (players.cmd_pause, "pause", PlaybackState.PAUSED),
        (players.cmd_resume, "play_media", PlaybackState.PLAYING),
        (players.cmd_stop, "stop", PlaybackState.IDLE),
    ):
        sent = len(kitchen.commands)
        with as_user([office_id]), pytest.raises(InsufficientPermissions):
            await command(office_id)
        await scenario.settle()
        assert kitchen.commands[sent:] == []

        with as_user([office_id, kitchen_id]):
            await command(office_id)
        await scenario.settle()
        assert [received[0] for received in kitchen.commands[sent:]] == [reaches_leader]
        assert _state(scenario, kitchen_id).playback_state == state
        assert _state(scenario, office_id).synced_to == kitchen_id
    await scenario.idle()
    assert office.commands == []

    with as_user([office_id]):
        await players.cmd_ungroup(office_id)
    await scenario.idle()
    assert _grouping_commands(kitchen)[-1] == ("set_members", None, ["sonos_office"])
    assert _state(scenario, office_id).synced_to is None


async def test_powering_off_a_member_takes_it_out_of_its_group(scenario: Scenario) -> None:
    """A member that is powered off leaves its group; the leader keeps the others."""
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id, den_id])
    await scenario.idle()

    await scenario.mass.players.cmd_power(office_id, False)
    await scenario.idle()

    assert _grouping_commands(kitchen)[-1] == ("set_members", None, ["sonos_office"])
    assert _state(scenario, kitchen_id).group_members == [kitchen_id, den_id]


async def test_a_member_that_goes_offline_is_let_go_by_its_leader(scenario: Scenario) -> None:
    """A member whose device drops off the network is removed from its leader's group."""
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id, den_id])
    await scenario.idle()

    await scenario.report(office, available=False)
    await scenario.idle()

    assert _grouping_commands(kitchen)[-1] == ("set_members", None, ["sonos_office"])
    assert _state(scenario, kitchen_id).group_members == [kitchen_id, den_id]


async def _sync_group(scenario: Scenario, *member_ids: str) -> str:
    """Create a dynamic sync group of the given players and return its id."""
    group = await scenario.mass.players.create_group_player(
        "sync_group", "Downstairs", list(member_ids), dynamic=True
    )
    await scenario.idle()
    return group.player_id


async def _stop(scenario: Scenario, player_id: str, *, idle: bool = True) -> None:
    """
    Stop what a player plays, letting the clock run for the stop to settle.

    :param player_id: The player to stop.
    :param idle: Whether the server has nothing left to do afterwards; False while a player
        still starts up, which ends on a timer of the server.
    """
    stop = await scenario.command(scenario.mass.players.cmd_stop(player_id))
    await scenario.advance(10)
    await stop
    if idle:
        await scenario.idle()


async def _switch_off(scenario: Scenario, speaker: FakeProtocol) -> FakePowerControl:
    """
    Put a speaker on a smart plug that cuts its power, and switch the plug off.

    That takes its device off the network. Its player then shows as available and powered
    off, and its power counts as cut. The speaker needs STARTUP_TIME to start up, as any
    player does unless the user sets another time.

    :param speaker: The speaker, which is registered.
    :return: The plug.
    """
    plug = await scenario.add_power_control(
        f"switch.{speaker.protocol_id}_plug", f"{speaker.protocol_id} plug", powered=True
    )
    player_id = _player_id(scenario, speaker)
    await scenario.configure_player(player_id, power_control=plug.control_id)
    # this setting is offered once the plug is the power control of the player
    await scenario.configure_player(player_id, power_control_cuts_power=True)
    await scenario.switch(plug, False)
    await scenario.unregister(speaker)
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY)
    await scenario.idle()
    return plug


async def _come_back(scenario: Scenario, speaker: FakeProtocol) -> None:
    """
    Let the device of a speaker that was switched on come back on the network.

    The rest of the start-up time of its player passes with it.
    """
    await scenario.register(speaker)
    await _start_up(scenario, _player_id(scenario, speaker))


async def _start_up(scenario: Scenario, player_id: str) -> None:
    """Let the rest of the start-up time of a player pass."""
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    if (ends_at := player.startup_ends_at) is not None:
        await scenario.advance(ends_at - scenario.mass.loop.time())
    # also lets what the end of the start-up time started run
    await scenario.advance(1)


async def _load_music(scenario: Scenario) -> None:
    """Load the music provider a play request plays from, as Scenario.play() does."""
    if scenario.mass.get_provider(MUSIC_PROVIDER) is None:
        await scenario.mass.config._create_provider_instance(MUSIC_PROVIDER, {})
        await scenario.settle()


async def _group(
    scenario: Scenario, group_type: str, *member_ids: str, dynamic: bool = True
) -> str:
    """
    Create the group Downstairs of the given players and return its id.

    :param group_type: ``"sync_group"`` or ``"universal_group"``.
    :param dynamic: Whether its members can change; else it has a fixed member list.
    """
    if scenario.mass.get_provider(group_type) is None:
        await scenario.mass.config._create_provider_instance(group_type, {})
    group = await scenario.mass.players.create_group_player(
        group_type, "Downstairs", list(member_ids), dynamic=dynamic
    )
    await scenario.idle()
    return group.player_id


async def test_a_sync_group_groups_its_members_on_a_leader_when_it_plays(
    scenario: Scenario,
) -> None:
    """
    A sync group that starts playing groups its members on one of them, its leader.

    The leader plays the group's queue and the other members follow it.
    """
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    group_id = await _sync_group(scenario, kitchen_id, office_id)

    await scenario.play(group_id)
    await scenario.idle()

    assert _grouping_commands(kitchen) == [("set_members", ["sonos_office"], None)]
    assert _state(scenario, office_id).synced_to == kitchen_id
    assert _state(scenario, kitchen_id).active_group == group_id
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    await _stop(scenario, group_id)


async def test_a_member_added_to_a_playing_sync_group_joins_its_leader(
    scenario: Scenario,
) -> None:
    """A player added to a sync group that plays joins the group's leader right away."""
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    group_id = await _sync_group(scenario, kitchen_id, office_id)
    await scenario.play(group_id)
    await scenario.idle()

    await scenario.mass.players.cmd_set_members(group_id, player_ids_to_add=[den_id])
    await scenario.idle()

    assert _grouping_commands(kitchen)[-1] == ("set_members", ["sonos_den"], None)
    assert den_id in _state(scenario, group_id).group_members
    await _stop(scenario, group_id)


async def test_a_member_change_on_a_sync_groups_leader_goes_to_the_group(
    scenario: Scenario,
) -> None:
    """
    Adding a player to the leader of a playing sync group adds it to the group itself.

    The group decides who joins, so the player becomes a member of the group, which joins it
    to the leader.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    group_id = await _sync_group(scenario, kitchen_id, office_id)
    await scenario.play(group_id)
    await scenario.idle()

    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[den_id])
    await scenario.idle()

    assert _grouping_commands(kitchen)[-1] == ("set_members", ["sonos_den"], None)
    assert den_id in _state(scenario, group_id).group_members
    await _stop(scenario, group_id)


async def test_ungrouping_a_member_of_a_playing_sync_group_takes_only_that_member_out(
    scenario: Scenario,
) -> None:
    """A member of a playing sync group that is ungrouped leaves it; the group plays on."""
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    group_id = await _sync_group(scenario, kitchen_id, office_id, den_id)
    await scenario.play(group_id)
    await scenario.idle()

    await scenario.mass.players.cmd_ungroup(den_id)
    await scenario.idle()

    assert _grouping_commands(kitchen)[-1] == ("set_members", None, ["sonos_den"])
    assert den_id not in _state(scenario, group_id).group_members
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    await _stop(scenario, group_id)


async def test_an_airplay_speaker_gives_up_its_own_group_before_it_joins_another(
    scenario: Scenario,
) -> None:
    """
    A speaker whose members ride its own stream lets them go before it joins another leader.

    Its members would be silent once it follows the other leader, so its group is dissolved
    first.
    """
    airplay = await scenario.add_provider("airplay")
    ids = ["airplay_hall", "airplay_attic", "airplay_porch"]
    hall, attic, porch = (
        airplay.protocol(
            protocol_id,
            protocol_id.removeprefix("airplay_").title(),
            identifiers=_device(number),
            supported_features=set(GROUPING),
            can_group_with={other for other in ids if other != protocol_id},
            native_grouping_requires_own_stream=True,
        )
        for number, protocol_id in enumerate(ids, start=50)
    )
    for speaker in (hall, attic, porch):
        await scenario.register(speaker)
    await scenario.idle()
    hall_id, attic_id = _player_id(scenario, hall), _player_id(scenario, attic)
    porch_id = _player_id(scenario, porch)
    await scenario.mass.players.cmd_set_members(attic_id, player_ids_to_add=[porch_id])
    await scenario.idle()

    await scenario.mass.players.cmd_set_members(hall_id, player_ids_to_add=[attic_id])
    await scenario.idle()

    assert _grouping_timeline(scenario)[-2:] == [
        ("airplay_attic", ("set_members", None, ["airplay_porch"])),
        ("airplay_hall", ("set_members", ["airplay_attic"], None)),
    ]
    assert _grouping_commands(hall) == [("set_members", ["airplay_attic"], None)]
    assert _state(scenario, porch_id).synced_to is None
    assert _state(scenario, attic_id).synced_to == hall_id


async def test_members_on_the_leaders_own_stream_follow_it_onto_airplay(
    scenario: Scenario,
) -> None:
    """
    Members that play from their leader's own stream follow it onto AirPlay, or leave its group.

    Kitchen plays with Office and Den on HEOS, which can not pass AirPlay on to its members.
    The user adds Office again and Patio, which only has AirPlay: Office follows over its AirPlay
    receiver in the same call, Den has none and leaves the group with a warning.
    """
    heos = await scenario.add_provider("heos")
    airplay = await scenario.add_provider("airplay")
    ids = ["heos_kitchen", "heos_office", "heos_den"]
    kitchen, office, den = (
        heos.protocol(
            protocol_id,
            protocol_id.removeprefix("heos_").title(),
            identifiers=_device(number),
            supported_features=set(GROUPING),
            can_group_with={other for other in ids if other != protocol_id},
            native_grouping_requires_own_stream=True,
        )
        for number, protocol_id in enumerate(ids, start=70)
    )
    kitchen_airplay = airplay.protocol(
        "airplay_kitchen",
        "Kitchen",
        role="audio",
        identifiers=_device(70),
        supported_features=set(GROUPING),
        can_group_with={"airplay_office", "airplay_patio"},
    )
    office_airplay = airplay.protocol(
        "airplay_office", "Office", role="audio", identifiers=_device(71)
    )
    patio = airplay.protocol("airplay_patio", "Patio", identifiers=_device(73))
    for protocol in (kitchen, office, den, kitchen_airplay, office_airplay, patio):
        await scenario.register(protocol)
    await scenario.idle()
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    den_id, patio_id = _player_id(scenario, den), _player_id(scenario, patio)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id, den_id])
    await scenario.idle()
    await scenario.play(kitchen_id)
    assert _state(scenario, office_id).synced_to == kitchen_id
    scenario.expect_log("Cannot group Den with Kitchen")

    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id, patio_id])
    await scenario.idle()

    assert _grouping_commands(kitchen_airplay) == [
        ("set_members", ["airplay_patio", "airplay_office"], None)
    ]
    assert _grouping_commands(kitchen)[-1] == ("set_members", None, ["heos_office", "heos_den"])
    assert _state(scenario, office_id).synced_to == kitchen_id
    assert _state(scenario, patio_id).synced_to == kitchen_id
    assert _state(scenario, den_id).synced_to is None
    assert _active_output(scenario, office_id) == "airplay_office"
    assert _active_output(scenario, kitchen_id) == "airplay_kitchen"
    assert _state(scenario, kitchen_id).playback_state == PlaybackState.PLAYING


async def test_a_protocol_switch_that_would_silence_a_member_is_refused(
    scenario: Scenario,
) -> None:
    """
    A new member that needs another protocol is refused while that would silence a member.

    The leader plays over AirPlay with a member that only has AirPlay; a speaker that only has
    Sendspin would move the group to Sendspin, so it is refused and nothing changes.
    """
    sonos = await scenario.add_provider("sonos")
    wiim = await scenario.add_provider("wiim")
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    lounge = sonos.protocol(
        "sonos_lounge", "Lounge", identifiers=_device(60), supported_features=set(GROUPING)
    )
    study = wiim.protocol("wiim_study", "Study", identifiers=_device(61))
    garage = wiim.protocol("wiim_garage", "Garage", identifiers=_device(62))
    lounge_airplay = airplay.protocol(
        "airplay_lounge",
        "Lounge",
        role="audio",
        identifiers=_device(60),
        supported_features=set(GROUPING),
        can_group_with={"airplay_study"},
    )
    study_airplay = airplay.protocol(
        "airplay_study", "Study", role="audio", identifiers=_device(61)
    )
    lounge_sendspin = sendspin.protocol(
        "sendspin_lounge",
        "Lounge",
        role="audio",
        identifiers=_device(60),
        supported_features=set(GROUPING),
        can_group_with={"sendspin_garage"},
    )
    garage_sendspin = sendspin.protocol(
        "sendspin_garage", "Garage", role="audio", identifiers=_device(62)
    )
    for protocol in (
        lounge,
        lounge_airplay,
        lounge_sendspin,
        study,
        study_airplay,
        garage,
        garage_sendspin,
    ):
        await scenario.register(protocol)
    await scenario.idle()
    lounge_id, study_id = _player_id(scenario, lounge), _player_id(scenario, study)
    garage_id = _player_id(scenario, garage)
    await scenario.mass.players.cmd_set_members(lounge_id, player_ids_to_add=[study_id])
    await scenario.play(lounge_id)

    with pytest.raises(PlayerCommandFailed, match="cannot play over"):
        await scenario.mass.players.cmd_set_members(lounge_id, player_ids_to_add=[garage_id])
    await scenario.idle()

    assert _grouping_commands(lounge_sendspin) == []
    assert _state(scenario, study_id).synced_to == lounge_id
    assert _state(scenario, garage_id).synced_to is None
    assert _active_output(scenario, lounge_id) == "airplay_lounge"
    assert _state(scenario, lounge_id).playback_state == PlaybackState.PLAYING


async def test_ungrouping_a_member_of_a_fixed_group_ends_the_groups_session(
    scenario: Scenario,
) -> None:
    """
    A member of a group with a fixed member list can not leave it on its own.

    Ungrouping it ends the session of the whole group instead, which releases every member.
    """
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    group = await scenario.mass.players.create_group_player(
        "sync_group", "Downstairs", [kitchen_id, office_id], dynamic=False
    )
    group_id = group.player_id
    await scenario.play(group_id)
    await scenario.idle()

    ungroup = await scenario.command(scenario.mass.players.cmd_ungroup(office_id))
    await scenario.advance(10)
    await ungroup
    await scenario.idle()

    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert _state(scenario, office_id).synced_to is None
    assert _state(scenario, office_id).active_group is None
    assert _state(scenario, kitchen_id).active_group is None


async def test_a_returning_member_of_a_fixed_group_rejoins_its_leader(
    scenario: Scenario,
) -> None:
    """
    A member of a playing group with a fixed member list that drops off and returns rejoins.

    The leader's device lets the member go while it is away; once it is back, its leader takes
    it again and the group plays on.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    group = await scenario.mass.players.create_group_player(
        "sync_group", "Downstairs", [kitchen_id, office_id, den_id], dynamic=False
    )
    group_id = group.player_id
    await scenario.play(group_id)
    await scenario.idle()

    await scenario.unregister(office)
    # the leader's device drops the member it lost
    await scenario.report(kitchen, group_members=["sonos_kitchen", "sonos_den"])
    await scenario.idle()
    assert _state(scenario, kitchen_id).group_members == [kitchen_id, den_id]

    await scenario.register(office)
    await scenario.idle()

    assert _grouping_commands(kitchen)[-1] == ("set_members", ["sonos_office"], None)
    assert _state(scenario, office_id).synced_to == kitchen_id
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    await _stop(scenario, group_id)


async def _fixed_group_without_a_member(
    scenario: Scenario,
) -> tuple[FakeProtocol, FakeProtocol, str]:
    """
    Play a group with a fixed member list whose member Office dropped off and is back.

    :return: The protocols of Kitchen, which leads the group, and Office, and the id of the group.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    office.supported_features.add(PlayerFeature.SELECT_SOURCE)
    await scenario.report(office, source_list=[PlayerSource(id="line_in", name="Line-in")])
    for speaker in (kitchen, office, den):
        speaker.supported_features.add(PlayerFeature.POWER)
        await scenario.report(speaker, powered=True)
    group = await scenario.mass.players.create_group_player(
        "sync_group",
        "Downstairs",
        [_player_id(scenario, x) for x in (kitchen, office, den)],
        dynamic=False,
    )
    await scenario.play(group.player_id)
    await scenario.idle()
    # the leader's device drops the member, which its group takes back two seconds later
    await scenario.report(kitchen, group_members=["sonos_kitchen", "sonos_den"])
    await scenario.advance(1)
    assert _player_id(scenario, office) not in _state(scenario, group.player_id).group_members
    assert _state(scenario, _player_id(scenario, office)).active_group == group.player_id
    kitchen.commands.clear()
    return kitchen, office, group.player_id


@pytest.mark.parametrize(
    "action", ["play", "select_source", "ungroup", "power_off", "power_off_at_device"]
)
async def test_a_fixed_group_ends_when_a_member_that_dropped_off_is_taken_out(
    scenario: Scenario, action: str
) -> None:
    """
    A fixed group plays on all of its members or not at all, also while one of them is out of it.

    :param action: What the user does with the member.
    """
    kitchen, office, group_id = await _fixed_group_without_a_member(scenario)
    office_id = _player_id(scenario, office)
    players = scenario.mass.players

    if action == "power_off_at_device":
        await scenario.report(office, powered=False)
        await scenario.advance(1)
    else:
        command = await scenario.command(
            {
                "play": lambda: scenario.mass.player_queues.play_media(office_id, TEST_TRACK_URI),
                "select_source": lambda: players.select_source(office_id, "line_in"),
                "ungroup": lambda: players.cmd_ungroup(office_id),
                "power_off": lambda: players.cmd_power(office_id, False),
            }[action]()
        )
        await scenario.advance(1)
        await command

    assert kitchen.playback_state == PlaybackState.IDLE
    await scenario.advance(30)
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert ("set_members", ["sonos_office"], None) not in kitchen.commands
    assert _state(scenario, office_id).synced_to is None
    assert _state(scenario, office_id).active_group is None
    if action == "play":
        assert office.playback_state == PlaybackState.PLAYING
        await scenario.command(players.cmd_stop(office_id))
        await scenario.idle()


async def test_a_fixed_group_plays_on_when_a_member_that_dropped_off_can_not_be_reached(
    scenario: Scenario,
) -> None:
    """A member that is gone is free, even if it reads as off, and is taken back once it is back."""
    kitchen, office, group_id = await _fixed_group_without_a_member(scenario)
    office_id = _player_id(scenario, office)
    scenario.expect_log("is not available")

    await scenario.report(office, available=False, powered=False)
    await scenario.advance(30)

    assert _state(scenario, office_id).active_group is None
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    await scenario.report(office, available=True, powered=True)
    await scenario.idle()
    assert _state(scenario, office_id).synced_to == _player_id(scenario, kitchen)
    await _stop(scenario, group_id)


async def test_a_member_of_a_fixed_group_that_plays_something_of_its_own_is_left_alone(
    scenario: Scenario,
) -> None:
    """
    A member that is taken out at the device and plays is left alone until that ends.

    The others play on.
    """
    kitchen, office, group_id = await _fixed_group_without_a_member(scenario)
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    await scenario.report(office, playback_state=PlaybackState.PLAYING, active_source="spotify")

    await scenario.advance(30)

    assert kitchen.commands == []
    assert _state(scenario, office_id).synced_to is None
    assert _state(scenario, office_id).active_group is None
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    await scenario.report(office, playback_state=PlaybackState.IDLE, active_source=None)
    await scenario.report(kitchen, volume_level=20)
    await scenario.idle()
    assert _state(scenario, office_id).synced_to == kitchen_id
    await _stop(scenario, group_id)


async def test_an_offline_member_its_leader_still_lists_is_removed(scenario: Scenario) -> None:
    """
    A member that dropped off and that its leader's device still lists is removed on request.

    The leader's own protocol is asked to let it go, and nothing is sent to the member.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id, den_id])
    await scenario.idle()
    await scenario.report(office, available=False)
    await scenario.idle()
    # the leader's device lists the member it lost again
    await scenario.report(kitchen, group_members=["sonos_kitchen", "sonos_office", "sonos_den"])
    await scenario.idle()
    assert _state(scenario, kitchen_id).group_members == [kitchen_id, office_id, den_id]

    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_remove=[office_id])
    await scenario.idle()

    assert _grouping_commands(kitchen)[-1] == ("set_members", None, ["sonos_office"])
    assert _state(scenario, kitchen_id).group_members == [kitchen_id, den_id]
    assert office.commands == []


@pytest.mark.parametrize("group_type", ["sync_group", "universal_group"])
@pytest.mark.parametrize("playing", [False, True], ids=["idle", "playing"])
async def test_an_offline_member_is_removed_from_a_group_player(
    scenario: Scenario, group_type: str, playing: bool
) -> None:
    """A member of a group player that dropped off is removed, and nothing is sent to it."""
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    group_id = await _group(scenario, group_type, kitchen_id, office_id, den_id)
    if playing:
        await scenario.play(group_id)
    sent = len(office.commands)

    await scenario.report(office, available=False)
    await scenario.idle()
    await scenario.mass.players.cmd_set_members(group_id, player_ids_to_remove=[office_id])
    await scenario.idle()

    assert _state(scenario, group_id).group_members == [kitchen_id, den_id]
    assert len(office.commands) == sent
    if playing:
        await _stop(scenario, group_id)


@pytest.mark.parametrize("group_type", ["sync_group", "universal_group"])
async def test_an_offline_member_of_a_fixed_group_is_refused_as_an_online_one(
    scenario: Scenario, group_type: str
) -> None:
    """A member of a group with a fixed member list can not be removed, offline or online."""
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    group_id = await _group(scenario, group_type, kitchen_id, office_id, den_id, dynamic=False)
    await scenario.report(office, available=False)
    await scenario.idle()

    refusals: list[tuple[str, str | None]] = []
    for member_id in (office_id, den_id):
        with pytest.raises(UnsupportedFeaturedException) as refusal:
            await scenario.mass.players.cmd_set_members(group_id, player_ids_to_remove=[member_id])
        refusals.append((str(refusal.value), refusal.value.translation_key))
    await scenario.idle()

    assert refusals[0] == refusals[1]
    assert _state(scenario, group_id).group_members == [kitchen_id, office_id, den_id]


async def _garden(scenario: Scenario) -> tuple[FakeProtocol, str]:
    """
    Attach Garden, a speaker of another make that shares no output with Kitchen, Office or Den.

    Returns its protocol and the id of its player.
    """
    wiim = await scenario.add_provider("wiim")
    garden = wiim.protocol(
        "wiim_garden", "Garden", identifiers=_device(30), supported_features=set(GROUPING)
    )
    await scenario.register(garden)
    await scenario.idle()
    return garden, _player_id(scenario, garden)


async def test_a_sync_group_is_not_led_by_a_member_that_can_group_with_none_of_the_others(
    scenario: Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A first member that can group with none of the others does not lead the sync group.

    Kitchen leads instead, Office follows it, and Garden is left out, which is logged.
    """
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    garden, garden_id = await _garden(scenario)
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    group_id = await _sync_group(scenario, garden_id, kitchen_id, office_id)
    scenario.expect_log("can not be grouped with")

    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        await scenario.play(group_id)
        await scenario.idle()

    assert _state(scenario, office_id).synced_to == kitchen_id
    assert _grouping_commands(kitchen) == [("set_members", ["sonos_office"], None)]
    assert _state(scenario, kitchen_id).active_group == group_id
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert _grouping_commands(garden) == []
    assert _state(scenario, garden_id).synced_to is None
    assert garden_id not in _state(scenario, group_id).group_members
    assert f"Removing {garden_id} from group" in caplog.text
    await _stop(scenario, group_id)


async def test_a_first_member_that_can_group_with_some_of_the_others_leads_the_sync_group(
    scenario: Scenario,
) -> None:
    """
    A first member that can group with some of the others leads, whoever could lead more.

    After Downstairs was created, Garden, its first member, kept only Office to group with,
    while Office can group with all of them. Garden leads and Office follows it; Kitchen and
    Den, which can not group with Garden, are left out, which is logged.
    """
    _, (garden, kitchen, office, den) = await _speakers(
        scenario, "Garden", "Kitchen", "Office", "Den"
    )
    garden_id, kitchen_id, office_id, den_id = (
        _player_id(scenario, x) for x in (garden, kitchen, office, den)
    )
    group_id = await _sync_group(scenario, garden_id, kitchen_id, office_id, den_id)
    await scenario.report(garden, can_group_with={"sonos_office"})
    await scenario.report(kitchen, can_group_with={"sonos_office", "sonos_den"})
    await scenario.report(den, can_group_with={"sonos_kitchen", "sonos_office"})
    await scenario.idle()
    scenario.expect_log("can not be grouped with")

    await scenario.play(group_id)
    await scenario.idle()

    assert _state(scenario, office_id).synced_to == garden_id
    assert _state(scenario, garden_id).active_group == group_id
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    for member_id in (kitchen_id, den_id):
        assert _state(scenario, member_id).synced_to is None
        assert member_id not in _state(scenario, group_id).group_members
    await _stop(scenario, group_id)


async def test_a_sync_group_whose_members_can_not_group_plays_on_its_first_member(
    scenario: Scenario,
) -> None:
    """A sync group whose members share no output plays on its first member alone."""
    sonos = await scenario.add_provider("sonos")
    kitchen, office = (
        sonos.protocol(
            f"sonos_{name.lower()}",
            name,
            identifiers=_device(number),
            supported_features=set(GROUPING),
        )
        for number, name in enumerate(("Kitchen", "Office"), start=10)
    )
    for speaker in (kitchen, office):
        await scenario.register(speaker)
    await scenario.idle()
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    group_id = await _sync_group(scenario, kitchen_id, office_id)
    scenario.expect_log("can not be grouped with")

    await scenario.play(group_id)
    await scenario.idle()

    assert _state(scenario, kitchen_id).active_group == group_id
    assert _state(scenario, kitchen_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, office_id).synced_to is None
    assert _grouping_commands(office) == []
    await _stop(scenario, group_id)


def _refuse_members(speaker: FakeProtocol) -> None:
    """Let a speaker refuse every member it is asked to add, as one asked to lead a stereo pair."""
    set_members = speaker.set_members

    async def _set_members(
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        if player_ids_to_add:
            msg = f"Cannot add a member to {speaker.name}"
            raise PlayerCommandFailed(msg)
        await set_members(player_ids_to_remove=player_ids_to_remove)

    speaker.set_members = _set_members  # type: ignore[method-assign]


async def _play_on_queue(scenario: Scenario, player_id: str) -> None:
    """Play a track on a player through its queue, letting the clock run for it to start."""
    await _load_music(scenario)
    play = await scenario.command(scenario.mass.player_queues.play_media(player_id, TEST_TRACK_URI))
    await scenario.advance(10)
    assert play.done()
    await play


@pytest.mark.parametrize("dynamic", [False, True], ids=["fixed", "dynamic"])
async def test_a_sync_group_whose_leader_refuses_its_members_forgets_that_leader(
    scenario: Scenario, dynamic: bool
) -> None:
    """
    A sync group whose leader refuses the other members is not formed, and keeps no leader.

    The play fails with the refusal. Once Office is put first in the group's settings, the next
    play lets Office lead, which takes Kitchen.
    """
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    group_id = await _group(scenario, "sync_group", kitchen_id, office_id, dynamic=dynamic)
    _refuse_members(kitchen)

    with pytest.raises(PlayerCommandFailed, match="Cannot add a member to Kitchen"):
        await _play_on_queue(scenario, group_id)
    await scenario.idle()

    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert _state(scenario, group_id).group_members == [kitchen_id, office_id]
    for member_id in (kitchen_id, office_id):
        assert _state(scenario, member_id).active_group is None
        assert _state(scenario, member_id).synced_to is None

    await scenario.configure_player(group_id, group_members=[office_id, kitchen_id])
    await _play_on_queue(scenario, group_id)
    await scenario.idle()

    assert _grouping_commands(office) == [("set_members", ["sonos_kitchen"], None)]
    assert _state(scenario, kitchen_id).synced_to == office_id
    assert _state(scenario, office_id).active_group == group_id
    assert _state(scenario, office_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    await _stop(scenario, group_id)


async def test_a_formed_sync_group_keeps_its_leader_when_it_can_not_take_a_member_back(
    scenario: Scenario,
) -> None:
    """
    A playing sync group whose leader refuses to take a member back keeps its leader.

    The play that asks for the member fails with the refusal; Kitchen still leads the group.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id, office_id, den_id = (_player_id(scenario, x) for x in (kitchen, office, den))
    group_id = await _sync_group(scenario, kitchen_id, office_id, den_id)
    await scenario.play(group_id)
    await scenario.idle()
    # the leader's device drops a member
    await scenario.report(kitchen, group_members=["sonos_kitchen", "sonos_den"])
    await scenario.idle()
    _refuse_members(kitchen)

    play = await scenario.command(scenario.mass.player_queues.play_media(group_id, TEST_TRACK_URI))
    await scenario.advance(1)
    assert play.done()
    with pytest.raises(PlayerCommandFailed, match="Cannot add a member to Kitchen"):
        await play

    assert _state(scenario, group_id).group_members == [kitchen_id, den_id]
    assert _state(scenario, den_id).synced_to == kitchen_id
    for member_id in (kitchen_id, den_id):
        assert _state(scenario, member_id).active_group == group_id
    await _stop(scenario, group_id)


async def test_a_sync_group_whose_leader_refuses_a_member_lets_the_ones_it_took_go(
    scenario: Scenario,
) -> None:
    """
    A sync group whose leader took one member and refuses another lets the first one go again.

    Kitchen takes Office over AirPlay and then refuses Den on its own protocol: the play fails,
    Office is released and no speaker is left playing through an output of the group.
    """
    sonos = await scenario.add_provider("sonos")
    wiim = await scenario.add_provider("wiim")
    airplay = await scenario.add_provider("airplay")
    kitchen = sonos.protocol(
        "sonos_kitchen",
        "Kitchen",
        identifiers=_device(50),
        supported_features=set(GROUPING),
        can_group_with={"sonos_den"},
    )
    den = sonos.protocol(
        "sonos_den",
        "Den",
        identifiers=_device(52),
        supported_features=set(GROUPING),
        can_group_with={"sonos_kitchen"},
    )
    office = wiim.protocol("wiim_office", "Office", identifiers=_device(51))
    kitchen_airplay = airplay.protocol(
        "airplay_kitchen",
        "Kitchen",
        role="audio",
        identifiers=_device(50),
        supported_features=set(GROUPING),
        can_group_with={"airplay_office"},
    )
    office_airplay = airplay.protocol(
        "airplay_office",
        "Office",
        role="audio",
        identifiers=_device(51),
        supported_features=set(GROUPING),
        can_group_with={"airplay_kitchen"},
    )
    for protocol in (kitchen, kitchen_airplay, office, office_airplay, den):
        await scenario.register(protocol)
    await scenario.idle()
    kitchen_id, office_id, den_id = (_player_id(scenario, x) for x in (kitchen, office, den))
    group_id = await _group(scenario, "sync_group", kitchen_id, office_id, den_id, dynamic=False)
    _refuse_members(kitchen)

    with pytest.raises(PlayerCommandFailed, match="Cannot add a member to Kitchen"):
        await _play_on_queue(scenario, group_id)
    await scenario.idle()

    assert _grouping_commands(kitchen_airplay) == [
        ("set_members", ["airplay_office"], None),
        ("set_members", None, ["airplay_office"]),
    ]
    assert _state(scenario, group_id).group_members == [kitchen_id, office_id, den_id]
    for member_id in (kitchen_id, office_id, den_id):
        assert _state(scenario, member_id).active_group is None
        assert _state(scenario, member_id).synced_to is None
        assert _active_output(scenario, member_id) is None


async def test_a_sync_group_whose_leader_leaves_is_led_by_a_member_that_can_group_with_the_others(
    scenario: Scenario,
) -> None:
    """
    When the leader leaves a playing sync group, a member that can group with the others leads.

    Garden was off the network when Downstairs started, so it stayed a member, and came back
    without an output it shares with the others. When Kitchen, the leader, leaves, Office leads
    the group, Den follows it, and Garden, next in the member list, is left out.
    """
    _, (kitchen, garden, office, den) = await _speakers(
        scenario, "Kitchen", "Garden", "Office", "Den"
    )
    kitchen_id, garden_id, office_id, den_id = (
        _player_id(scenario, x) for x in (kitchen, garden, office, den)
    )
    group_id = await _sync_group(scenario, kitchen_id, garden_id, office_id, den_id)
    await scenario.unregister(garden)
    await scenario.idle()
    scenario.expect_log("is not available")
    await scenario.play(group_id)
    await scenario.idle()
    assert _state(scenario, den_id).synced_to == kitchen_id
    await scenario.register(garden)
    await scenario.report(garden, can_group_with=set())
    await scenario.report(office, can_group_with={"sonos_kitchen", "sonos_den"})
    await scenario.report(den, can_group_with={"sonos_kitchen", "sonos_office"})
    await scenario.idle()
    scenario.expect_log("can not be grouped with")

    remove = await scenario.command(
        scenario.mass.players.cmd_set_members(group_id, player_ids_to_remove=[kitchen_id])
    )
    await scenario.advance(10)
    await remove
    await scenario.idle()

    assert _state(scenario, den_id).synced_to == office_id
    assert _state(scenario, office_id).active_group == group_id
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert _grouping_commands(garden) == []
    assert _state(scenario, garden_id).synced_to is None
    assert _state(scenario, kitchen_id).synced_to is None
    await _stop(scenario, group_id)


async def _group_behind_an_airplay_leader(
    scenario: Scenario,
) -> tuple[FakeProtocol, FakeProtocol, FakeProtocol, str]:
    """
    Play a sync group over the AirPlay output of Kitchen, with Office as its Sonos follower.

    Both speakers play natively until a receiver that only has AirPlay joins, which then leaves.

    :return: The Sonos protocols of Kitchen and Office, the AirPlay protocol of Kitchen, and the
        id of the group.
    """
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    airplay = await scenario.add_provider("airplay")
    kitchen_airplay, receiver = (
        airplay.protocol(
            protocol_id,
            name,
            role="audio",
            identifiers=_device(number),
            supported_features=set(GROUPING),
            can_group_with={other},
        )
        for protocol_id, name, number, other in (
            ("airplay_kitchen", "Kitchen", 10, "airplay_receiver"),
            ("airplay_receiver", "Receiver", 30, "airplay_kitchen"),
        )
    )
    for protocol in (kitchen_airplay, receiver):
        await scenario.register(protocol)
    await scenario.idle()
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    receiver_id = _player_id(scenario, receiver)
    group_id = await _sync_group(scenario, kitchen_id, office_id)
    await scenario.play(group_id)
    await scenario.idle()
    for change in ("player_ids_to_add", "player_ids_to_remove"):
        command = await scenario.command(
            scenario.mass.players.cmd_set_members(group_id, **{change: [receiver_id]})
        )
        await scenario.advance(10)
        await command
        await scenario.idle()
    assert _active_output(scenario, kitchen_id) == "airplay_kitchen"
    assert _state(scenario, office_id).synced_to == kitchen_id
    assert _state(scenario, group_id).group_members == [kitchen_id, office_id]
    return kitchen, office, kitchen_airplay, group_id


def _fall_silent(scenario: Scenario, speaker: FakeProtocol, seconds: float) -> None:
    """
    Let a Sonos leader answer no group command for a while; it lets its members go after that.

    :param speaker: The Sonos protocol of the leader.
    :param seconds: How long it stays silent.
    """
    loop = scenario.mass.loop
    silent_until = loop.time() + seconds
    loop.call_later(seconds, speaker._report_members, [], [])

    async def _set_members(
        player_ids_to_add: list[str] | None = None, player_ids_to_remove: list[str] | None = None
    ) -> None:
        if loop.time() < silent_until:
            await asyncio.sleep(10)
            raise CommandTimeout("groups:modifyGroupMembers", 10)
        await FakeProtocol.set_members(speaker, player_ids_to_add, player_ids_to_remove)

    speaker.set_members = _set_members  # type: ignore[method-assign]


def _hang_on_a_group_command_after_airplay(
    scenario: Scenario, speaker: FakeProtocol, airplay: FakeProtocol
) -> None:
    """
    Let a Sonos leader fall silent for 40 s on a group command right after its AirPlay output stopped.

    :param speaker: The Sonos protocol of the leader.
    :param airplay: Its AirPlay protocol.
    """
    loop = scenario.mass.loop
    stopped_at = float("-inf")

    async def _stop() -> None:
        nonlocal stopped_at
        stopped_at = loop.time()
        await FakeProtocol.stop(airplay)

    async def _set_members(
        player_ids_to_add: list[str] | None = None, player_ids_to_remove: list[str] | None = None
    ) -> None:
        if loop.time() - stopped_at < 0.5:
            _fall_silent(scenario, speaker, 40)
            await speaker.set_members(player_ids_to_add, player_ids_to_remove)
            return
        await FakeProtocol.set_members(speaker, player_ids_to_add, player_ids_to_remove)

    airplay.stop = _stop  # type: ignore[method-assign]
    speaker.set_members = _set_members  # type: ignore[method-assign]


async def test_a_sync_group_plays_on_at_once_when_its_airplay_leader_leaves(
    scenario: Scenario,
) -> None:
    """
    A leader that plays over AirPlay lets its Sonos follower go before its AirPlay output stops.

    It falls silent on a group command right after that stop, which would hold up the group.
    """
    kitchen, office, kitchen_airplay, group_id = await _group_behind_an_airplay_leader(scenario)
    _hang_on_a_group_command_after_airplay(scenario, kitchen, kitchen_airplay)
    kitchen_id = _player_id(scenario, kitchen)
    scenario.timeline.clear()

    leave = await scenario.command(scenario.mass.players.cmd_ungroup(kitchen_id))
    await scenario.advance(5)
    assert leave.done()
    await leave
    await scenario.idle()

    assert [(protocol_id, command[0]) for protocol_id, command in scenario.timeline] == [
        ("sonos_kitchen", "set_members"),
        ("airplay_kitchen", "stop"),
        ("sonos_office", "play_media"),
    ]
    assert _active_output(scenario, kitchen_id) is None
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, group_id).group_members == [_player_id(scenario, office)]
    await _stop(scenario, group_id)


async def test_a_sync_group_whose_airplay_leader_and_its_member_leave_at_once_lets_them_go(
    scenario: Scenario,
) -> None:
    """Members that leave together with their leader on AirPlay are free at once, and silent."""
    kitchen, office, kitchen_airplay, group_id = await _group_behind_an_airplay_leader(scenario)
    _hang_on_a_group_command_after_airplay(scenario, kitchen, kitchen_airplay)
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)

    remove = await scenario.command(
        scenario.mass.players.cmd_set_members(
            group_id, player_ids_to_remove=[kitchen_id, office_id]
        )
    )
    await scenario.advance(5)
    assert remove.done()
    await remove
    await scenario.idle()

    assert _state(scenario, office_id).synced_to is None
    assert kitchen_airplay.playback_state == PlaybackState.IDLE
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert _state(scenario, group_id).group_members == []


async def test_a_sync_group_leader_on_its_own_protocol_stops_before_it_lets_its_members_go(
    scenario: Scenario,
) -> None:
    """A leader that plays on the protocol its members follow it on is never left playing alone."""
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    kitchen_id = _player_id(scenario, kitchen)
    group_id = await _sync_group(scenario, kitchen_id, _player_id(scenario, office))
    await scenario.play(group_id)
    await scenario.idle()
    scenario.timeline.clear()

    leave = await scenario.command(scenario.mass.players.cmd_ungroup(kitchen_id))
    await scenario.advance(5)
    assert leave.done()
    await leave
    await scenario.idle()

    assert [(protocol_id, command[0]) for protocol_id, command in scenario.timeline] == [
        ("sonos_kitchen", "stop"),
        ("sonos_kitchen", "set_members"),
        ("sonos_office", "play_media"),
    ]
    await _stop(scenario, group_id)


async def test_a_sync_group_plays_on_once_its_silent_old_leader_lets_the_member_go(
    scenario: Scenario,
) -> None:
    """
    A group whose leader leaves plays on, however long that leader holds on to the member.

    Kitchen leads over AirPlay with Office as its Sonos follower. It answers no group command for
    36 s, and only then lets Office go.
    """
    kitchen, office, _, group_id = await _group_behind_an_airplay_leader(scenario)
    _fall_silent(scenario, kitchen, 36)
    scenario.expect_log("Could not ungroup the members of Kitchen")
    scenario.expect_log("Player Office still reports synced_to")

    leave = await scenario.command(scenario.mass.players.cmd_ungroup(_player_id(scenario, kitchen)))
    await scenario.advance(40)
    await leave
    await scenario.idle()

    assert [command[0] for command in office.commands].count("play_media") == 1
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, group_id).group_members == [_player_id(scenario, office)]
    await _stop(scenario, group_id)


async def test_a_member_taken_out_while_its_silent_old_leader_holds_it_does_not_start(
    scenario: Scenario,
) -> None:
    """A member that leaves a group that waits for it is out at once, and starts nothing later."""
    kitchen, office, _, group_id = await _group_behind_an_airplay_leader(scenario)
    _fall_silent(scenario, kitchen, 36)
    scenario.expect_log("Could not ungroup the members of Kitchen")
    scenario.expect_log("Player Office still reports synced_to")
    leave = await scenario.command(scenario.mass.players.cmd_ungroup(_player_id(scenario, kitchen)))
    await scenario.advance(30)
    await leave

    remove = await scenario.command(
        scenario.mass.players.cmd_set_members(
            group_id, player_ids_to_remove=[_player_id(scenario, office)]
        )
    )
    assert remove.done()
    await remove
    await scenario.advance(30)
    await scenario.idle()

    assert _state(scenario, group_id).group_members == []
    assert "play_media" not in [command[0] for command in office.commands]
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE


async def test_a_play_does_not_wait_until_a_silent_old_leader_lets_the_new_one_go(
    scenario: Scenario,
) -> None:
    """A play that the new leader of a group can not start yet fails, and a later one plays."""
    kitchen, office, _, group_id = await _group_behind_an_airplay_leader(scenario)
    _fall_silent(scenario, kitchen, 36)
    scenario.expect_log("Could not ungroup the members of Kitchen")
    scenario.expect_log("Player Office still reports synced_to")
    scenario.expect_log("Player Office is stuck")
    scenario.expect_log("Aborting syncgroup form")
    leave = await scenario.command(scenario.mass.players.cmd_ungroup(_player_id(scenario, kitchen)))
    await scenario.advance(14)

    play = await scenario.command(scenario.mass.players.cmd_play(group_id))
    await scenario.advance(19)
    assert play.done()
    with pytest.raises(PlayerCommandFailed):
        await play
    await leave
    await scenario.advance(10)
    later_play = await scenario.command(scenario.mass.players.cmd_play(group_id))
    await scenario.advance(5)
    await later_play
    await scenario.idle()

    assert [command[0] for command in office.commands].count("play_media") == 1
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    await _stop(scenario, group_id)


@pytest.mark.parametrize("through_queue", [False, True])
@pytest.mark.parametrize("seconds", [4, 14, 20, 30])
async def test_a_sync_group_stopped_while_its_silent_old_leader_holds_a_member_stays_silent(
    scenario: Scenario, seconds: float, through_queue: bool
) -> None:
    """
    A group that is stopped while it waits for a member starts nothing once that member is free.

    :param seconds: How long after its leader left the group is stopped.
    :param through_queue: Whether the queue of the group is stopped instead of the group.
    """
    kitchen, office, _, group_id = await _group_behind_an_airplay_leader(scenario)
    _fall_silent(scenario, kitchen, 36)
    scenario.expect_log("Could not ungroup the members of Kitchen")
    scenario.expect_log("Player Office still reports synced_to")
    leave = await scenario.command(scenario.mass.players.cmd_ungroup(_player_id(scenario, kitchen)))
    await scenario.advance(seconds)

    stop = await scenario.command(
        scenario.mass.player_queues.stop(group_id)
        if through_queue
        else scenario.mass.players.cmd_stop(group_id)
    )
    await scenario.advance(90)
    await leave
    await stop
    await scenario.idle()

    assert "play_media" not in [command[0] for command in office.commands]
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE


async def test_a_player_added_to_an_idle_sync_group_is_checked_against_the_member_that_leads(
    scenario: Scenario,
) -> None:
    """
    A player added to a sync group that does not play is checked against its future leader.

    Garden, listed first, can not lead Kitchen, so Office, which Kitchen can lead, is taken in,
    and the group plays on Kitchen and Office.
    """
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    _, garden_id = await _garden(scenario)
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    group_id = await _sync_group(scenario, garden_id, kitchen_id)

    await scenario.mass.players.cmd_set_members(group_id, player_ids_to_add=[office_id])
    await scenario.idle()

    assert office_id in _state(scenario, group_id).group_members
    scenario.expect_log("can not be grouped with")
    await scenario.play(group_id)
    await scenario.idle()
    assert _state(scenario, office_id).synced_to == kitchen_id
    await _stop(scenario, group_id)


async def test_members_that_follow_a_leader_outside_a_sync_group_are_not_passed_over(
    scenario: Scenario,
) -> None:
    """
    Members that follow a leader outside the sync group when it plays can still lead it.

    Kitchen, Office and Den follow Hall. Downstairs, of those three and the pair Patio and Porch
    of another make, plays: Kitchen, its first member, leads after it is released from Hall,
    Office and Den join it, and the pair, which can not group with Kitchen, is left out.
    """
    _, (kitchen, office, den, hall) = await _speakers(scenario, "Kitchen", "Office", "Den", "Hall")
    kitchen_id, office_id, den_id, hall_id = (
        _player_id(scenario, x) for x in (kitchen, office, den, hall)
    )
    wiim = await scenario.add_provider("wiim")
    patio, porch = (
        wiim.protocol(
            f"wiim_{name.lower()}",
            name,
            identifiers=_device(number),
            supported_features=set(GROUPING),
            can_group_with={f"wiim_{other.lower()}"},
        )
        for number, name, other in ((31, "Patio", "Porch"), (32, "Porch", "Patio"))
    )
    for speaker in (patio, porch):
        await scenario.register(speaker)
    await scenario.idle()
    patio_id, porch_id = _player_id(scenario, patio), _player_id(scenario, porch)
    await scenario.mass.players.cmd_set_members(
        hall_id, player_ids_to_add=[kitchen_id, office_id, den_id]
    )
    await scenario.advance(10)
    await scenario.idle()
    group_id = await _sync_group(scenario, kitchen_id, office_id, den_id, patio_id, porch_id)
    scenario.expect_log("can not be grouped with")
    scenario.expect_log("still reports synced_to")

    await _load_music(scenario)
    play = await scenario.command(scenario.mass.player_queues.play_media(group_id, TEST_TRACK_URI))
    await scenario.advance(10)
    await play
    await scenario.idle()

    assert _state(scenario, office_id).synced_to == kitchen_id
    assert _state(scenario, den_id).synced_to == kitchen_id
    assert _state(scenario, kitchen_id).synced_to is None
    assert _state(scenario, kitchen_id).active_group == group_id
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, patio_id).synced_to is None
    assert _state(scenario, porch_id).synced_to is None
    await _stop(scenario, group_id)


async def test_a_member_that_leads_another_playing_group_does_not_take_a_sync_group(
    scenario: Scenario,
) -> None:
    """
    A member that plays as the leader of another group does not lead the sync group.

    Den plays with Hall following it when Downstairs, of Kitchen, Office and Den, plays. Kitchen
    leads and Office follows it; Den is left out and keeps playing with Hall.
    """
    _, (kitchen, office, den, hall) = await _speakers(scenario, "Kitchen", "Office", "Den", "Hall")
    kitchen_id, office_id, den_id, hall_id = (
        _player_id(scenario, x) for x in (kitchen, office, den, hall)
    )
    group_id = await _sync_group(scenario, kitchen_id, office_id, den_id)
    await scenario.mass.players.cmd_set_members(den_id, player_ids_to_add=[hall_id])
    await scenario.play(den_id)
    await scenario.idle()
    scenario.expect_log("can not be grouped with")

    await scenario.play(group_id)
    await scenario.idle()

    assert _state(scenario, office_id).synced_to == kitchen_id
    assert _state(scenario, kitchen_id).active_group == group_id
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, hall_id).synced_to == den_id
    assert _state(scenario, den_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, den_id).active_group is None
    den_queue = scenario.queue(den_id)
    assert den_queue is not None
    assert den_queue.current_item is not None
    await _stop(scenario, group_id)
    await _stop(scenario, den_id)


async def test_a_member_that_leads_players_of_its_own_does_not_replace_the_first_member(
    scenario: Scenario,
) -> None:
    """
    A member that leads players of its own is not put in the place of the first member.

    Garden, the first member of Downstairs, can group with none of the others, and Kitchen plays
    with Hall following it. Downstairs plays on Garden alone, and Kitchen keeps playing with Hall.
    """
    _, (kitchen, office, hall) = await _speakers(scenario, "Kitchen", "Office", "Hall")
    _, garden_id = await _garden(scenario)
    kitchen_id, office_id, hall_id = (_player_id(scenario, x) for x in (kitchen, office, hall))
    group_id = await _sync_group(scenario, garden_id, kitchen_id, office_id)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[hall_id])
    await scenario.play(kitchen_id)
    await scenario.idle()
    scenario.expect_log("can not be grouped with")

    await scenario.play(group_id)
    await scenario.idle()

    assert _state(scenario, garden_id).active_group == group_id
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, hall_id).synced_to == kitchen_id
    assert _state(scenario, kitchen_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, kitchen_id).active_group is None
    assert _state(scenario, office_id).synced_to is None
    await _stop(scenario, group_id)
    await _stop(scenario, kitchen_id)


async def test_a_first_member_that_plays_an_input_of_its_own_keeps_leading_the_sync_group(
    scenario: Scenario,
) -> None:
    """
    A first member whose own input narrows what it can group with keeps leading the sync group.

    Garden, Kitchen, Office and Den all have AirPlay, and Kitchen and Office also group on their
    own make. Garden and Kitchen each play an input of their own, which leaves them only that
    make to group with. Downstairs plays: Garden, its first member, leads, and Office and Den
    follow it over AirPlay.
    """
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    garden, garden_id = await _garden(scenario)
    airplay = await scenario.add_provider("airplay")
    airplay_ids = {"airplay_garden", "airplay_kitchen", "airplay_office", "airplay_den"}
    for name, identifiers in (
        ("garden", _device(30)),
        ("kitchen", _device(10)),
        ("office", _device(11)),
    ):
        await scenario.register(
            airplay.protocol(
                f"airplay_{name}",
                f"{name.title()} AirPlay",
                role="audio",
                identifiers=identifiers,
                supported_features=set(GROUPING),
                can_group_with=airplay_ids - {f"airplay_{name}"},
            )
        )
    den = airplay.protocol(
        "airplay_den",
        "Den",
        identifiers=_device(40),
        supported_features=set(GROUPING),
        can_group_with=airplay_ids - {"airplay_den"},
    )
    await scenario.register(den)
    await scenario.idle()
    kitchen_id, office_id, den_id = (_player_id(scenario, x) for x in (kitchen, office, den))
    group_id = await _sync_group(scenario, garden_id, kitchen_id, office_id, den_id)
    for speaker, source in ((garden, "tv"), (kitchen, "line_in")):
        await scenario.report(speaker, playback_state=PlaybackState.PLAYING, active_source=source)
    await scenario.idle()

    await _load_music(scenario)
    play = await scenario.command(scenario.mass.player_queues.play_media(group_id, TEST_TRACK_URI))
    await scenario.advance(12)
    await play
    await scenario.idle()

    assert _state(scenario, garden_id).active_group == group_id
    assert _state(scenario, office_id).synced_to == garden_id
    assert _state(scenario, den_id).synced_to == garden_id
    assert _state(scenario, kitchen_id).active_group is None
    await _stop(scenario, group_id)


@pytest.mark.parametrize("leader_type", [PlayerType.PLAYER, PlayerType.MULTI_CHANNEL])
async def test_a_leader_counts_its_own_level_in_the_group_volume(
    scenario: Scenario, leader_type: PlayerType
) -> None:
    """
    A speaker or a multi-channel player that leads a group counts its own level.

    The group volume is the level of the loudest member, the leader included, and a step up
    of the group volume turns every member up, keeping their balance.
    """
    _, (living, office) = await _speakers(scenario, "Living", "Office")
    await scenario.report(living, type=leader_type, volume_level=60)
    await scenario.report(office, volume_level=30)
    living_id, office_id = _player_id(scenario, living), _player_id(scenario, office)
    await scenario.mass.players.cmd_set_members(living_id, player_ids_to_add=[office_id])
    await scenario.idle()

    assert _state(scenario, living_id).group_volume == 60

    await scenario.mass.players.cmd_group_volume_up(living_id)
    await scenario.idle()

    assert _state(scenario, living_id).volume_level == 63
    assert _state(scenario, office_id).volume_level == 35
