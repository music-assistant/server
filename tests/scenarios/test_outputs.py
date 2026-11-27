"""Which output of a player plays: its own protocol, or the one the user prefers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlayerFeature

from music_assistant.constants import PLAYER_CONTROL_PROTOCOL

from .harness import DEFAULT_FEATURES, FakeProtocol, Scenario

if TYPE_CHECKING:
    from music_assistant_models.player import Player as PlayerState

KITCHEN = {
    IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
    IdentifierType.IP_ADDRESS: "192.168.1.20",
}
LOUNGE = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A1"}
STUDY = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A2"}
GROUPING = frozenset({*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS})


def _played(protocol: FakeProtocol) -> bool:
    """Return whether a protocol got media to play."""
    return any(command[0] == "play_media" for command in protocol.commands)


def _volume_commands(protocol: FakeProtocol) -> list[tuple[object, ...]]:
    """Return the volume and mute commands a protocol received, in order."""
    return [command for command in protocol.commands if command[0] in ("volume_set", "volume_mute")]


def _state(scenario: Scenario, player_id: str) -> PlayerState:
    """Return the state a player publishes."""
    state = scenario.player(player_id)
    assert state is not None
    return state


async def test_playback_goes_to_the_output_the_user_prefers(scenario: Scenario) -> None:
    """
    A player plays through its own protocol until the user prefers another output.

    From then on it plays through the output the user picked.
    """
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(native)
    await scenario.register(audio)
    await scenario.idle()
    player_id = scenario.single_player(native, audio).player_id

    await scenario.play(player_id)

    assert (_played(native), _played(audio)) == (True, False)
    await scenario.mass.players.cmd_stop(player_id)
    await scenario.idle()
    native.commands.clear()

    await scenario.configure_player(player_id, preferred_output_protocol="ap_kitchen")
    await scenario.play(player_id)

    assert (_played(native), _played(audio)) == (False, True)
    state = scenario.player(player_id)
    assert state is not None
    assert state.active_output_protocol == "ap_kitchen"


async def _stopped_airplay_group(
    scenario: Scenario, *, auto: bool = False
) -> tuple[FakeProtocol, FakeProtocol, str, str]:
    """
    Group Study under Lounge over AirPlay, play the group and stop it.

    Lounge is a Sonos speaker and Study a WiiM speaker; each has an AirPlay receiver, the only
    way they group. Returns Lounge's own protocol, its AirPlay receiver and the ids of Lounge
    and Study.

    :param auto: Whether the user selected "auto" as the volume and mute control of both.
    """
    sonos = await scenario.add_provider("sonos")
    wiim = await scenario.add_provider("wiim")
    airplay = await scenario.add_provider("airplay")
    lounge = sonos.protocol("sonos_lounge", "Lounge", identifiers=LOUNGE)
    lounge_airplay = airplay.protocol(
        "airplay_lounge",
        "Lounge",
        role="audio",
        identifiers=LOUNGE,
        supported_features=set(GROUPING),
        can_group_with={"airplay_study"},
    )
    study = wiim.protocol("wiim_study", "Study", identifiers=STUDY)
    study_airplay = airplay.protocol(
        "airplay_study",
        "Study",
        role="audio",
        identifiers=STUDY,
        supported_features=set(GROUPING),
        can_group_with={"airplay_lounge"},
    )
    for protocol in (lounge, lounge_airplay, study, study_airplay):
        await scenario.register(protocol)
    await scenario.idle()
    lounge_id = scenario.single_player(lounge, lounge_airplay).player_id
    study_id = scenario.single_player(study, study_airplay).player_id
    if auto:
        for player_id in (lounge_id, study_id):
            await scenario.configure_player(
                player_id,
                volume_control=PLAYER_CONTROL_PROTOCOL,
                mute_control=PLAYER_CONTROL_PROTOCOL,
            )
    await scenario.mass.players.cmd_set_members(lounge_id, player_ids_to_add=[study_id])
    await scenario.play(lounge_id)
    await scenario.mass.players.cmd_stop(lounge_id)
    await scenario.idle()
    lounge.commands.clear()
    lounge_airplay.commands.clear()
    return lounge, lounge_airplay, lounge_id, study_id


async def test_a_stopped_leader_takes_volume_on_its_own_protocol_while_it_keeps_its_group(
    scenario: Scenario,
) -> None:
    """
    A stopped leader takes volume and mute on its own protocol, though its group keeps its output.

    Its AirPlay receiver stays its output for the group, but plays nothing, and an AirPlay
    receiver only takes a volume command while it streams. The user selected "auto" as the
    volume and mute control.
    """
    lounge, lounge_airplay, lounge_id, _ = await _stopped_airplay_group(scenario, auto=True)

    await scenario.mass.players.cmd_volume_set(lounge_id, 30)
    await scenario.mass.players.cmd_volume_mute(lounge_id, True)
    await scenario.idle()

    assert _volume_commands(lounge_airplay) == []
    assert _volume_commands(lounge) == [("volume_set", 30), ("volume_mute", True)]
    assert _state(scenario, lounge_id).active_output_protocol == "airplay_lounge"


async def test_a_member_plays_again_at_the_level_it_got_while_its_group_was_stopped(
    scenario: Scenario,
) -> None:
    """
    A member whose level and mute changed while its group was stopped plays again at them.

    Its AirPlay receiver stayed its output for the group, and the level and mute went to its
    own protocol meanwhile. The receiver gets them before the group plays again: an AirPlay
    receiver sends the level it holds when its stream starts. The user selected "auto" as the
    volume and mute control.
    """
    _, _, lounge_id, study_id = await _stopped_airplay_group(scenario, auto=True)
    study, study_airplay = scenario.protocol("wiim_study"), scenario.protocol("airplay_study")
    study.commands.clear()
    await scenario.mass.players.cmd_volume_set(study_id, 20)
    await scenario.mass.players.cmd_volume_mute(study_id, True)
    await scenario.idle()
    assert _volume_commands(study) == [("volume_set", 20), ("volume_mute", True)]
    study_airplay.commands.clear()
    scenario.timeline.clear()

    await scenario.play(lounge_id)

    assert _volume_commands(study_airplay) == [("volume_set", 20), ("volume_mute", True)]
    commands = [(protocol_id, command[0]) for protocol_id, command in scenario.timeline]
    assert commands.index(("airplay_study", "volume_mute")) < commands.index(
        ("airplay_lounge", "play_media")
    )


async def test_a_stopped_leader_lets_go_of_its_output_once_its_group_ended(
    scenario: Scenario,
) -> None:
    """
    A stopped leader whose last member left no longer names the output it kept for its group.

    Volume and mute go to its own protocol.
    """
    lounge, lounge_airplay, lounge_id, study_id = await _stopped_airplay_group(scenario)

    await scenario.mass.players.cmd_set_members(lounge_id, player_ids_to_remove=[study_id])
    await scenario.mass.players.cmd_volume_set(lounge_id, 30)
    await scenario.mass.players.cmd_volume_mute(lounge_id, True)
    await scenario.idle()

    assert _state(scenario, lounge_id).active_output_protocol is None
    assert _state(scenario, study_id).active_output_protocol is None
    assert _volume_commands(lounge_airplay) == []
    assert _volume_commands(lounge) == [("volume_set", 30), ("volume_mute", True)]


async def test_a_member_lets_go_of_the_output_it_played_its_group_on_once_it_left(
    scenario: Scenario,
) -> None:
    """
    A member that played a group on its Sendspin client no longer names it once it left.

    The leader is a Sendspin speaker, which takes the member's Sendspin client into its own
    group. Volume and mute of the member go to its own protocol again.
    """
    sendspin = await scenario.add_provider("sendspin")
    wiim = await scenario.add_provider("wiim")
    den = sendspin.protocol(
        "sendspin_den",
        "Den",
        identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A3"},
        supported_features=set(GROUPING),
        can_group_with={"sendspin_study"},
    )
    study = wiim.protocol("wiim_study", "Study", identifiers=STUDY)
    study_sendspin = sendspin.protocol("sendspin_study", "Study", role="audio", identifiers=STUDY)
    for protocol in (den, study, study_sendspin):
        await scenario.register(protocol)
    await scenario.idle()
    den_id = scenario.single_player(den).player_id
    study_id = scenario.single_player(study, study_sendspin).player_id
    await scenario.mass.players.cmd_set_members(den_id, player_ids_to_add=[study_id])
    await scenario.play(den_id)
    await scenario.mass.players.cmd_stop(den_id)
    await scenario.idle()
    assert _state(scenario, study_id).active_output_protocol == "sendspin_study"
    study.commands.clear()
    study_sendspin.commands.clear()

    await scenario.mass.players.cmd_set_members(den_id, player_ids_to_remove=[study_id])
    await scenario.mass.players.cmd_volume_set(study_id, 30)
    await scenario.mass.players.cmd_volume_mute(study_id, True)
    await scenario.idle()

    assert _state(scenario, study_id).active_output_protocol is None
    assert _volume_commands(study_sendspin) == []
    assert _volume_commands(study) == [("volume_set", 30), ("volume_mute", True)]
