"""What becomes of the player of a speaker that goes up in a set of speakers, and when it leaves."""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from music_assistant_models.dsp import DSPConfig
from music_assistant_models.enums import EventType, IdentifierType, PlaybackState, PlayerFeature

from music_assistant.constants import (
    CONF_ABSORBED_BY,
    CONF_PLAYER_DSP,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
)
from music_assistant.controllers.players.lifecycle import REMOVE_UNAVAILABLE_GRACE

from .harness import DEFAULT_FEATURES, TEST_TRACK_URI, Event, Scenario

if TYPE_CHECKING:
    from music_assistant_models.player import Player as PlayerState

    from music_assistant.models.player import Player

    from .harness import FakeProtocol

LIVING = {
    IdentifierType.MAC_ADDRESS: "A8:40:41:00:00:01",
    IdentifierType.IP_ADDRESS: "192.168.1.21",
}
PATIO = {
    IdentifierType.MAC_ADDRESS: "A8:40:41:00:00:02",
    IdentifierType.IP_ADDRESS: "192.168.1.22",
}
KITCHEN = {
    IdentifierType.MAC_ADDRESS: "A8:40:41:00:00:03",
    IdentifierType.IP_ADDRESS: "192.168.1.23",
}
DEN = {
    IdentifierType.MAC_ADDRESS: "A8:40:41:00:00:04",
    IdentifierType.IP_ADDRESS: "192.168.1.24",
}
GROUPING = frozenset({*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS})
# the events that tell clients a player came, changed or went
PLAYER_EVENTS = (EventType.PLAYER_ADDED, EventType.PLAYER_UPDATED, EventType.PLAYER_REMOVED)
# how long a protocol of a speaker takes to detach, in seconds, as one that closes a connection
DETACH_TIME = 2


async def test_a_speaker_that_goes_up_in_a_set_is_no_player_anymore(scenario: Scenario) -> None:
    """
    A speaker that goes up in a set is announced as removed, and gone from players and settings.

    Its record, its DSP and queue settings and its saved queue stay stored.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    await scenario.mass.config.save_dsp_config(patio_id, DSPConfig(enabled=True))
    await _change_a_queue_setting(scenario, patio_id)
    await scenario.play(patio_id)
    await scenario.idle()
    stored = {
        key: scenario.mass.config.get(f"{key}/{patio_id}")
        for key in (CONF_PLAYER_DSP, CONF_PLAYER_QUEUES)
    }
    scenario.clear_events()

    await _report_set(scenario, living, patio)

    assert scenario.removals() == [(patio_id, None)]
    assert patio_id not in [player.player_id for player in scenario.visible_players()]
    for include_values, include_unavailable, include_disabled in itertools.product(
        (False, True), repeat=3
    ):
        listed = await scenario.mass.config.get_player_configs(
            include_values=include_values,
            include_unavailable=include_unavailable,
            include_disabled=include_disabled,
        )
        assert patio_id not in [config.player_id for config in listed]
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/{patio_id}") is not None
    assert {key: scenario.mass.config.get(f"{key}/{patio_id}") for key in stored} == stored
    assert await scenario.saved_queue(patio_id) == [TEST_TRACK_URI]


@pytest.mark.parametrize("plug", [False, True])
async def test_nothing_reaches_a_speaker_that_goes_up_in_a_set(
    scenario: Scenario, plug: bool
) -> None:
    """
    The server sends no command to a playing speaker that goes up in a set, nor to any other device.

    No stop and no power off, also not when it is on a plug that cuts its power, which stays
    switched on. Its protocols are detached as for a disabled player, which ends what they
    themselves stream to it.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    control = None
    if plug:
        control = await scenario.add_power_control("switch.patio_plug", "Patio plug", powered=True)
        await scenario.configure_player(patio_id, power_control=control.control_id)
        await scenario.configure_player(patio_id, power_control_cuts_power=True)
    await scenario.play(patio_id)
    await scenario.idle()
    received = len(scenario.timeline)
    switched = len(control.commands) if control else 0

    await _report_set(scenario, living, patio)

    assert scenario.timeline[received:] == []
    if control is not None:
        assert (control.commands[switched:], control.powered) == ([], True)


async def test_no_protocol_attaches_to_a_speaker_in_a_set(scenario: Scenario) -> None:
    """
    No protocol of a speaker in a set attaches, and no player is created for one.

    Not its AirPlay receiver that registers again, nor a Cast receiver that is new and shares
    its address, nor a bridge that rides on its AirPlay receiver: each stays unattached, and
    the server wants none of them.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    chromecast = await scenario.add_provider("chromecast")
    sendspin = await scenario.add_provider("sendspin")
    patio_cast = chromecast.protocol("cast_patio", "Patio", role="audio", identifiers=PATIO)
    patio_bridge = sendspin.protocol(
        "sendspin_bridge_patio", "Patio", role="audio", underlying_protocol_id="airplay_patio"
    )
    await _report_set(scenario, living, patio)
    players = scenario.visible_players()
    stored_players = set(scenario.mass.config.get(CONF_PLAYERS))
    scenario.clear_events()

    for protocol in (patio_airplay, patio_cast, patio_bridge):
        await scenario.register(protocol)
    await scenario.idle()

    for protocol in (patio, patio_airplay, patio_cast, patio_bridge):
        assert scenario.player_of(protocol) is None
        assert not scenario.mass.players.wants_protocol(protocol.protocol_id)
    for protocol in (patio_cast, patio_bridge):
        record = scenario.mass.config.get_protocol_record(protocol.protocol_id)
        assert (record or {}).get("player_id") == patio_id
    assert scenario.visible_players() == players
    assert scenario.events(*PLAYER_EVENTS) == []
    assert set(scenario.mass.config.get(CONF_PLAYERS)) == stored_players


async def test_a_speaker_the_set_lets_go_comes_back_with_its_id(scenario: Scenario) -> None:
    """
    A speaker the set no longer names is announced as added under its id, with what it had.

    Its name, DSP and queue settings and its queue are back, and its protocols attach once
    their providers offer them again.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    await scenario.configure_player(patio_id, name="Patio Speaker")
    await scenario.mass.config.save_dsp_config(patio_id, DSPConfig(enabled=True))
    await _change_a_queue_setting(scenario, patio_id)
    await scenario.play(patio_id)
    await _stop(scenario, patio_id)
    stored = {
        key: scenario.mass.config.get(f"{key}/{patio_id}")
        for key in (CONF_PLAYER_DSP, CONF_PLAYER_QUEUES)
    }
    await _report_set(scenario, living, patio)
    scenario.clear_events()

    await _report_set(scenario, living)

    assert scenario.events(EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED) == [
        Event(EventType.PLAYER_ADDED, patio_id)
    ]
    patio_player = scenario.single_player(patio, patio_airplay)
    assert (patio_player.player_id, patio_player.name, patio_player.available) == (
        patio_id,
        "Patio Speaker",
        True,
    )
    assert scenario.player_of(patio) == scenario.player_of(patio_airplay) == patio_id
    assert {key: scenario.mass.config.get(f"{key}/{patio_id}") for key in stored} == stored
    assert [item.uri for item in scenario.mass.player_queues.items(patio_id)] == [TEST_TRACK_URI]
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/{patio_id}/{CONF_ABSORBED_BY}") is None


async def test_a_speaker_in_a_set_stays_out_after_a_restart(scenario: Scenario) -> None:
    """A speaker in a set is not brought back at a restart, and none of its protocols attach."""
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    await _report_set(scenario, living, patio)
    scenario.clear_events()

    await scenario.restart()
    await scenario.idle()

    assert Event(EventType.PLAYER_ADDED, patio_id) not in scenario.events(EventType.PLAYER_ADDED)
    assert patio_id not in [player.player_id for player in scenario.visible_players()]
    for protocol_id in ("sonos_patio", "airplay_patio"):
        assert scenario.player_of(scenario.protocol(protocol_id)) is None


async def test_a_set_taken_apart_while_the_server_was_down_lets_its_speaker_go(
    scenario: Scenario,
) -> None:
    """The first report after a start that no longer names a speaker brings its player back."""
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    await _report_set(scenario, living, patio)
    await scenario.restart()
    await scenario.idle()
    scenario.clear_events()

    await _report_set(scenario, scenario.protocol("sonos_living"))

    assert scenario.events(EventType.PLAYER_ADDED) == [Event(EventType.PLAYER_ADDED, patio_id)]
    for protocol_id in ("sonos_patio", "airplay_patio"):
        assert scenario.player_of(scenario.protocol(protocol_id)) == patio_id


async def test_a_speaker_is_a_player_again_once_the_player_of_its_set_is_removed(
    scenario: Scenario,
) -> None:
    """
    Once the player of the set is removed, the protocols of its speaker attach again.

    After a restart the player of the speaker is brought back, and nothing names the set anymore.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    living_id = scenario.single_player(living).player_id
    await _report_set(scenario, living, patio)
    scenario.set_present("sonos_living", False)
    scenario.clear_events()

    await scenario.remove(living_id)
    await scenario.register(patio_airplay)
    await scenario.idle()

    assert scenario.events(EventType.PLAYER_ADDED) == [Event(EventType.PLAYER_ADDED, patio_id)]
    assert scenario.player_of(patio) == scenario.player_of(patio_airplay) == patio_id

    await scenario.restart()
    await scenario.idle()

    assert patio_id in [player.player_id for player in scenario.visible_players()]
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/{patio_id}/{CONF_ABSORBED_BY}") is None


@pytest.mark.parametrize("dry_run", [True, False])
async def test_removing_all_unavailable_players_leaves_a_speaker_in_a_set_alone(
    scenario: Scenario, dry_run: bool
) -> None:
    """Removing all unavailable players, also as a dry run, neither names nor removes it."""
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    await _report_set(scenario, living, patio)
    await scenario.advance(REMOVE_UNAVAILABLE_GRACE)

    removed = await scenario.mass.players.remove_unavailable(dry_run=dry_run)
    await scenario.idle()

    assert patio_id not in removed
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/{patio_id}/{CONF_ABSORBED_BY}") == (
        "sonos_living"
    )


@pytest.mark.parametrize(
    "report", ["unknown_set", "unknown_speaker", "own_player", "group", "other_set", "same_list"]
)
async def test_a_report_that_changes_nothing_does_nothing(scenario: Scenario, report: str) -> None:
    """
    A report of a set that changes nothing announces nothing and stores nothing.

    That is one of an unknown set, one that names an unknown speaker, a speaker of the set's own
    player, a group or a speaker that another set holds, and the same list once more.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    living_airplay = scenario.provider("airplay").protocol(
        "airplay_living", "Living", role="audio", identifiers=LIVING
    )
    den = scenario.provider("sonos").protocol("sonos_den", "Den", identifiers=DEN)
    await scenario.register(living_airplay)
    await scenario.register(den)
    await scenario.idle()
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    set_id, speaker_ids = "sonos_living", ["sonos_patio"]
    if report == "unknown_set":
        set_id = "sonos_attic"
    elif report == "unknown_speaker":
        speaker_ids = ["sonos_attic"]
    elif report == "own_player":
        speaker_ids = ["airplay_living"]
    elif report == "group":
        group = await scenario.mass.players.create_group_player(
            "sync_group", "Downstairs", [patio_id], dynamic=True
        )
        speaker_ids = scenario.mass.config.get_protocol_ids_of_player(group.player_id)
    elif report == "other_set":
        await _report_set(scenario, den, patio)
    else:
        await _report_set(scenario, living, patio)
    await scenario.idle()
    players = scenario.visible_players()
    stored = scenario.mass.config.get(CONF_PLAYERS)
    stored_absorbed_by = {
        player_id: record.get(CONF_ABSORBED_BY) for player_id, record in stored.items()
    }
    scenario.clear_events()

    with patch.object(
        scenario.mass.config, "save", wraps=scenario.mass.config.save
    ) as config_writes:
        await scenario.mass.players.set_absorbed_protocols(set_id, speaker_ids)
    await scenario.idle()

    assert config_writes.call_count == 0
    assert scenario.events(*PLAYER_EVENTS) == []
    assert scenario.visible_players() == players
    assert {
        player_id: record.get(CONF_ABSORBED_BY)
        for player_id, record in scenario.mass.config.get(CONF_PLAYERS).items()
    } == stored_absorbed_by


async def test_a_speaker_the_user_disabled_stays_disabled_when_its_set_lets_it_go(
    scenario: Scenario,
) -> None:
    """A speaker the user disabled before it went up in a set is still disabled when it leaves."""
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    await scenario.disable(patio_id)
    await scenario.idle()
    await _report_set(scenario, living, patio)
    scenario.clear_events()

    await _report_set(scenario, living)

    assert scenario.events(EventType.PLAYER_ADDED) == []
    assert patio_id not in [player.player_id for player in scenario.visible_players()]
    assert (patio_id, False) in [
        (config.player_id, config.enabled) for config in await scenario.player_configs()
    ]


async def test_a_speaker_enabled_while_in_a_set_comes_back_once_the_set_lets_it_go(
    scenario: Scenario,
) -> None:
    """
    A disabled speaker that is enabled while it is in a set stays out until the set lets it go.

    Then it is announced as added under its id, and its protocols attach.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    await scenario.disable(patio_id)
    await scenario.idle()
    await _report_set(scenario, living, patio)
    scenario.clear_events()

    await scenario.enable(patio_id)
    await scenario.idle()

    assert scenario.events(EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED) == []
    assert scenario.player_of(patio) is None

    await _report_set(scenario, living)

    assert scenario.events(EventType.PLAYER_ADDED) == [Event(EventType.PLAYER_ADDED, patio_id)]
    assert scenario.player_of(patio) == scenario.player_of(patio_airplay) == patio_id


@pytest.mark.parametrize("group_type", ["sync_session", "sync_group", "universal_group"])
async def test_a_group_plays_on_without_a_speaker_that_goes_up_in_a_set(
    scenario: Scenario, group_type: str
) -> None:
    """
    A group or sync session that has the speaker as a member plays on and no longer lists it.

    The server sends no command about it to the leader or to any other member.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario, grouping=True)
    kitchen = scenario.provider("sonos").protocol(
        "sonos_kitchen",
        "Kitchen",
        identifiers=KITCHEN,
        supported_features=set(GROUPING),
        can_group_with={"sonos_patio"},
    )
    await scenario.register(kitchen)
    await scenario.idle()
    kitchen_id = scenario.single_player(kitchen).player_id
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    if group_type == "sync_session":
        await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[patio_id])
        playing_id = kitchen_id
    else:
        if scenario.mass.get_provider(group_type) is None:
            await scenario.mass.config._create_provider_instance(group_type, {})
        group = await scenario.mass.players.create_group_player(
            group_type, "Downstairs", [kitchen_id, patio_id], dynamic=True
        )
        playing_id = group.player_id
    await scenario.play(playing_id)
    await scenario.idle()
    assert patio_id in _state(scenario, playing_id).group_members
    received = len(scenario.timeline)

    await _report_set(scenario, living, patio)

    assert _state(scenario, playing_id).playback_state == PlaybackState.PLAYING
    assert patio_id not in _state(scenario, playing_id).group_members
    assert patio_id not in _state(scenario, kitchen_id).group_members
    assert scenario.timeline[received:] == []
    await _stop(scenario, playing_id)


async def test_the_same_set_reported_twice_at_once_takes_its_speaker_out_once(
    scenario: Scenario,
) -> None:
    """
    Two reports of the same set at the same moment take the speaker out of use once.

    Also while a protocol of the speaker takes its time to detach: clients are told once that
    it is removed, and its queue is let go of once.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    patio_airplay.detach_time = DETACH_TIME
    scenario.clear_events()
    queues = scenario.mass.player_queues

    with patch.object(queues, "on_player_remove", wraps=queues.on_player_remove) as queue_removals:
        reports = [
            await scenario.command(_set_of(scenario, living, patio)),
            await scenario.command(_set_of(scenario, living, patio)),
        ]
        await scenario.advance(DETACH_TIME)
        for report in reports:
            await report
    await scenario.idle()

    assert scenario.removals() == [(patio_id, None)]
    assert [call.args[0] for call in queue_removals.call_args_list] == [patio_id]


async def test_a_set_reported_and_taken_apart_at_once_lets_its_speaker_go(
    scenario: Scenario,
) -> None:
    """
    A report that names a speaker and one that no longer does, at the same moment, end as the second.

    The speaker is taken out of use and brought back: clients are told it is removed and then
    added, it no longer names the set, and its protocols attach again.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    patio_airplay.detach_time = DETACH_TIME
    scenario.clear_events()

    reports = [
        await scenario.command(_set_of(scenario, living, patio)),
        await scenario.command(_set_of(scenario, living)),
    ]
    await scenario.advance(DETACH_TIME)
    for report in reports:
        await report
    await scenario.idle()

    assert scenario.events(EventType.PLAYER_REMOVED, EventType.PLAYER_ADDED) == [
        Event(EventType.PLAYER_REMOVED, patio_id),
        Event(EventType.PLAYER_ADDED, patio_id),
    ]
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/{patio_id}/{CONF_ABSORBED_BY}") is None
    assert scenario.player_of(patio) == scenario.player_of(patio_airplay) == patio_id


async def test_a_set_reported_while_its_protocol_attaches_takes_its_speaker_out(
    scenario: Scenario,
) -> None:
    """
    A provider may report the set while the protocol that delivers it attaches.

    The protocol attaches, and the speaker the set names goes out of use.
    """
    sonos = await scenario.add_provider("sonos")
    patio = sonos.protocol("sonos_patio", "Patio", identifiers=PATIO)
    await scenario.register(patio)
    await scenario.idle()
    patio_id = scenario.single_player(patio).player_id
    living = sonos.protocol("sonos_living", "Living", identifiers=LIVING)

    async def _report_set_on_attach(player: Player) -> None:  # noqa: ARG001
        """Report the set the protocol delivers, as a provider does once it is attached."""
        await _set_of(scenario, living, patio)

    living.on_attach = _report_set_on_attach  # type: ignore[method-assign]
    attach = await scenario.command(scenario.mass.players.register(living))
    await scenario.idle()

    assert attach.done(), "the attach of the protocol that delivers the set did not finish"
    await attach
    assert scenario.player_of(living) is not None
    assert scenario.removals() == [(patio_id, None)]


async def _living_and_patio(
    scenario: Scenario, *, grouping: bool = False
) -> tuple[FakeProtocol, FakeProtocol, FakeProtocol]:
    """
    Set up the Living speaker, whose protocol delivers a set, and the Patio speaker.

    The Patio speaker has a Sonos protocol and an AirPlay receiver: one player of two providers.

    :param grouping: Whether the Patio speaker can group with the Kitchen speaker.
    :return: The protocol of the Living speaker, and the two of the Patio speaker.
    """
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    living = sonos.protocol("sonos_living", "Living", identifiers=LIVING)
    grouping_state: dict[str, Any] = (
        {"supported_features": set(GROUPING), "can_group_with": {"sonos_kitchen"}}
        if grouping
        else {}
    )
    patio = sonos.protocol("sonos_patio", "Patio", identifiers=PATIO, **grouping_state)
    patio_airplay = airplay.protocol("airplay_patio", "Patio", role="audio", identifiers=PATIO)
    for protocol in (living, patio, patio_airplay):
        await scenario.register(protocol)
    await scenario.idle()
    return living, patio, patio_airplay


async def _report_set(scenario: Scenario, protocol: FakeProtocol, *speakers: FakeProtocol) -> None:
    """
    Let the provider of a protocol report the other speakers of the set it delivers.

    :param protocol: The protocol that delivers the set.
    :param speakers: The protocols of the other speakers of the set; none for no set.
    """
    await scenario.mass.players.set_absorbed_protocols(
        protocol.protocol_id, [speaker.protocol_id for speaker in speakers]
    )
    await scenario.idle()


async def _set_of(scenario: Scenario, protocol: FakeProtocol, *speakers: FakeProtocol) -> None:
    """
    Report the other speakers of the set a protocol delivers, without waiting for the server.

    :param protocol: The protocol that delivers the set.
    :param speakers: The protocols of the other speakers of the set; none for no set.
    """
    await scenario.mass.players.set_absorbed_protocols(
        protocol.protocol_id, [speaker.protocol_id for speaker in speakers]
    )


async def _change_a_queue_setting(scenario: Scenario, queue_id: str) -> None:
    """Change one setting of a queue away from its default, as a user does."""
    entry = next(
        entry
        for entry in scenario.mass.player_queues.get_queue_config_entries()
        if entry.options and not entry.requires_reload
    )
    value = next(option.value for option in entry.options if option.value != entry.default_value)
    await scenario.mass.config.save_player_queue_config(queue_id, {entry.key: value})
    await scenario.settle()


def _state(scenario: Scenario, player_id: str) -> PlayerState:
    """Return the state a player publishes."""
    state = scenario.player(player_id)
    assert state is not None
    return state


async def _stop(scenario: Scenario, player_id: str) -> None:
    """Stop what a player plays, letting the clock run for the stop to settle."""
    stop = await scenario.command(scenario.mass.players.cmd_stop(player_id))
    await scenario.advance(10)
    await stop
    await scenario.idle()
