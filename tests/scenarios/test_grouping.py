"""What grouping does: which protocol carries each member, who leads, what is released."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature
from music_assistant_models.errors import (
    InsufficientPermissions,
    PlayerCommandFailed,
    UnsupportedFeaturedException,
)

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
    from music_assistant_models.player import PlayerMedia

GROUPING = frozenset({*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS})


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


async def _stop(scenario: Scenario, player_id: str) -> None:
    """Stop what a player plays, letting the clock run for the stop to settle."""
    stop = await scenario.command(scenario.mass.players.cmd_stop(player_id))
    await scenario.advance(10)
    await stop
    await scenario.idle()


async def _switch_off(scenario: Scenario, speaker: FakeProtocol) -> FakePowerControl:
    """
    Put a speaker on a smart plug and switch the plug off, which takes its device off the network.

    Its player then shows as available and powered off.

    :param speaker: The speaker, which is registered.
    :return: The plug.
    """
    plug = await scenario.add_power_control(
        f"switch.{speaker.protocol_id}_plug", f"{speaker.protocol_id} plug", powered=True
    )
    await scenario.configure_player(_player_id(scenario, speaker), power_control=plug.control_id)
    await scenario.switch(plug, False)
    await scenario.unregister(speaker)
    await scenario.idle()
    return plug


async def _come_back(scenario: Scenario, speaker: FakeProtocol) -> None:
    """Let the device of a speaker that was switched on come back on the network."""
    await scenario.register(speaker)
    # a power on looks for the device every 0.5 s
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


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="a sync group switches a member on (commit 4)"
)
@pytest.mark.parametrize("dynamic", [True, False], ids=["dynamic", "fixed"])
async def test_a_sync_group_switches_a_member_on_and_starts_without_it(
    scenario: Scenario, dynamic: bool
) -> None:
    """
    A sync group with a member that is switched off at its plug starts without it.

    The group plays on the members that are on and switches the plug on. Once the device is
    back, the member joins the leader and the group plays on.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    group_id = await _group(scenario, "sync_group", kitchen_id, office_id, den_id, dynamic=dynamic)
    plug = await _switch_off(scenario, den)
    scenario.expect_log("is not available")
    scenario.expect_log("Failed to stop queue")

    await scenario.play(group_id)

    assert plug.commands == [True]
    assert _grouping_commands(kitchen) == [("set_members", ["sonos_office"], None)]
    assert _state(scenario, den_id).synced_to is None

    await _come_back(scenario, den)
    await scenario.idle()

    assert _grouping_commands(kitchen)[-1] == ("set_members", ["sonos_den"], None)
    assert _state(scenario, den_id).synced_to == kitchen_id
    assert den_id in _state(scenario, group_id).group_members
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    await _stop(scenario, group_id)


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="a universal group switches a member on (commit 5)"
)
async def test_a_universal_group_switches_a_member_on_and_starts_without_it(
    scenario: Scenario,
) -> None:
    """
    A universal group with a member that is switched off at its plug starts without it.

    The group plays its stream on the members that are on and switches the plug on. Once the
    device is back, the member plays the stream of the group as well.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    group_id = await _group(scenario, "universal_group", kitchen_id, office_id, den_id)
    plug = await _switch_off(scenario, den)

    await scenario.play(group_id)

    assert plug.commands == [True]
    assert [
        _state(scenario, player_id).playback_state for player_id in (kitchen_id, office_id)
    ] == [
        PlaybackState.PLAYING,
        PlaybackState.PLAYING,
    ]
    assert den.commands == []

    await _come_back(scenario, den)
    await scenario.idle()

    assert [command[0] for command in den.commands] == ["play_media"]
    assert _state(scenario, den_id).active_group == group_id
    assert _state(scenario, den_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    await _stop(scenario, group_id)


@pytest.mark.parametrize(
    "group_type",
    [
        pytest.param(
            "sync_group",
            marks=pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="a sync group switches its members on (commit 4)",
            ),
        ),
        pytest.param(
            "universal_group",
            marks=pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="a universal group switches its members on (commit 5)",
            ),
        ),
    ],
)
async def test_a_group_of_switched_off_speakers_starts_on_the_first_that_comes_online(
    scenario: Scenario, group_type: str
) -> None:
    """
    A group none of whose members can play switches them on and waits for the first one.

    The play starts once the first device is back, on that member; the other one joins when
    its device is back.
    """
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    group_id = await _group(scenario, group_type, kitchen_id, office_id)
    kitchen_plug = await _switch_off(scenario, kitchen)
    office_plug = await _switch_off(scenario, office)
    await _load_music(scenario)

    play = await scenario.command(scenario.mass.player_queues.play_media(group_id, TEST_TRACK_URI))

    assert not play.done(), play.exception()
    assert (kitchen_plug.commands, office_plug.commands) == ([True], [True])

    await _come_back(scenario, office)
    await play
    await scenario.idle()

    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, office_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, office_id).active_group == group_id

    await _come_back(scenario, kitchen)
    await scenario.idle()

    assert _state(scenario, kitchen_id).playback_state == PlaybackState.PLAYING
    assert kitchen_id in _state(scenario, group_id).group_members
    await _stop(scenario, group_id)


@pytest.mark.parametrize(
    "group_type",
    [
        pytest.param(
            "sync_group",
            marks=pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="a sync group switches its members on (commit 4)",
            ),
        ),
        pytest.param(
            "universal_group",
            marks=pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="a universal group switches its members on (commit 5)",
            ),
        ),
    ],
)
@pytest.mark.parametrize("command", ["stop", "pause", "power off"])
async def test_a_stop_of_the_group_calls_off_the_power_on_of_its_members(
    scenario: Scenario, group_type: str, command: str
) -> None:
    """
    A stop, a pause or a power off of the group ends the power on of a member that still waits.

    The device that comes back afterwards stays out of the group: it gets no command and is
    idle. Its plug stays on.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    group_id = await _group(scenario, group_type, kitchen_id, office_id, den_id)
    if command == "power off":
        await scenario.configure_player(group_id, power_control=PLAYER_CONTROL_FAKE)
    plug = await _switch_off(scenario, den)
    scenario.expect_log("is not available")
    scenario.expect_log("Failed to stop queue")
    await scenario.play(group_id)
    assert plug.commands == [True]
    assert _state(scenario, den_id).powered is True

    players = scenario.mass.players
    call_off = await scenario.command(
        {
            "stop": players.cmd_stop(group_id),
            "pause": scenario.mass.player_queues.pause(group_id),
            "power off": players.cmd_power(group_id, False),
        }[command]
    )
    await scenario.advance(10)
    await call_off
    await _come_back(scenario, den)
    await scenario.idle()

    assert den.commands == []
    assert ("set_members", ["sonos_den"], None) not in _grouping_commands(kitchen)
    assert _state(scenario, den_id).synced_to is None
    assert _state(scenario, den_id).active_group is None
    assert _state(scenario, den_id).playback_state == PlaybackState.IDLE
    assert plug.commands == [True]
    if command == "pause":
        await _stop(scenario, group_id)


@pytest.mark.parametrize(
    "group_type",
    [
        pytest.param(
            "sync_group",
            marks=pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="a sync group switches its members on (commit 4)",
            ),
        ),
        pytest.param(
            "universal_group",
            marks=pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="a universal group switches its members on (commit 5)",
            ),
        ),
    ],
)
async def test_a_member_that_does_not_come_online_is_left_out(
    scenario: Scenario, group_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A member whose device does not come back within 90 s is left out, which is logged once.

    The group plays on without it, and the member, whose plug is on by then, shows as
    unavailable.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    group_id = await _group(scenario, group_type, kitchen_id, office_id, den_id)
    plug = await _switch_off(scenario, den)
    scenario.expect_log("is not available")
    scenario.expect_log("Failed to stop queue")
    scenario.expect_log("did not come online")

    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        await scenario.play(group_id)
        assert plug.commands == [True]
        await scenario.advance(95)
        await scenario.idle()

    assert [
        message
        for record in caplog.records
        if "did not come online" in (message := record.getMessage())
    ] == ["Player Den did not come online, it is left out of Downstairs"]
    assert _state(scenario, den_id).available is False
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert plug.commands == [True]
    await _stop(scenario, group_id)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="a switched-off speaker added to a leader joins when it is online (commit 3)",
)
async def test_a_switched_off_speaker_added_to_a_leader_joins_once_it_is_online(
    scenario: Scenario,
) -> None:
    """
    A speaker that is switched off at its plug and added to a leader is switched on.

    The other speaker added with it joins at once. The switched-off one joins once its device
    is back, and nothing is logged about leaving it out.
    """
    _, (kitchen, office, den) = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id = _player_id(scenario, kitchen)
    office_id, den_id = _player_id(scenario, office), _player_id(scenario, den)
    plug = await _switch_off(scenario, den)
    scenario.expect_log("is not available")
    scenario.expect_log("Failed to stop queue")

    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[den_id, office_id])
    await scenario.settle()

    assert plug.commands == [True]
    assert _grouping_commands(kitchen) == [("set_members", ["sonos_office"], None)]

    await _come_back(scenario, den)
    await scenario.idle()

    assert _grouping_commands(kitchen)[-1] == ("set_members", ["sonos_den"], None)
    assert _state(scenario, den_id).synced_to == kitchen_id
    assert _state(scenario, kitchen_id).group_members == [kitchen_id, office_id, den_id]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="a speaker that was switched on to join is not powered off as idle (commit 3)",
)
async def test_a_woken_member_is_not_powered_off_as_idle_before_it_joins(
    scenario: Scenario,
) -> None:
    """
    A speaker that was switched on to join a leader is not powered off as idle before it joined.

    The speaker has an idle power off of one minute. Its device is back while the leader is
    busy for longer than that, so it waits, switched on and idle. It still joins once the
    leader is free, and its plug was never switched off.
    """
    _, (kitchen, den) = await _speakers(scenario, "Kitchen", "Den")
    kitchen_id, den_id = _player_id(scenario, kitchen), _player_id(scenario, den)
    await scenario.configure_player(den_id, idle_power_off=1)
    plug = await _switch_off(scenario, den)
    scenario.expect_log("is not available")
    scenario.expect_log("Failed to stop queue")
    await _load_music(scenario)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[den_id])
    await scenario.settle()
    assert plug.commands == [True]
    busy = asyncio.Event()

    async def _busy_play_media(media: PlayerMedia) -> None:
        await busy.wait()
        await FakeProtocol.play_media(kitchen, media)

    kitchen.play_media = _busy_play_media  # type: ignore[method-assign]
    play = await scenario.command(
        scenario.mass.player_queues.play_media(kitchen_id, TEST_TRACK_URI)
    )

    await _come_back(scenario, den)
    await scenario.advance(100)

    assert plug.commands == [True]
    assert _state(scenario, den_id).synced_to is None
    busy.set()
    await scenario.settle()
    await play
    await scenario.idle()

    assert _state(scenario, den_id).synced_to == kitchen_id
    assert plug.commands == [True]
    await _stop(scenario, kitchen_id)
