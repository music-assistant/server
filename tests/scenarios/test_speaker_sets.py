"""The players of the other speakers of a set of speakers: they are removed for good."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.dsp import DSPConfig
from music_assistant_models.enums import EventType, IdentifierType, PlayerType

from music_assistant.constants import CONF_PLAYER_DSP, CONF_PLAYER_QUEUES, CONF_PLAYERS

from .harness import TEST_TRACK_URI, Event, Scenario

if TYPE_CHECKING:
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
# how long a protocol of a speaker takes to detach, in seconds, as one that closes a connection
DETACH_TIME = 2


async def test_the_player_of_a_speaker_that_goes_up_in_a_set_is_removed_for_good(
    scenario: Scenario,
) -> None:
    """
    The player of a speaker that goes up in a set is removed with everything stored about it.

    Clients are told once that it is removed; its record, the records of its protocols, its DSP
    and queue settings and its saved queue are gone. Its plug, which cuts its power, gets no
    command and stays on.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    plug = await scenario.add_power_control("switch.patio_plug", "Patio plug", powered=True)
    await scenario.configure_player(patio_id, power_control=plug.control_id)
    await scenario.configure_player(patio_id, power_control_cuts_power=True)
    await scenario.mass.config.save_dsp_config(patio_id, DSPConfig(enabled=True))
    await _change_a_queue_setting(scenario, patio_id)
    await scenario.play(patio_id)
    await scenario.idle()
    assert all(
        scenario.mass.config.get(f"{key}/{patio_id}") is not None
        for key in (CONF_PLAYERS, CONF_PLAYER_DSP, CONF_PLAYER_QUEUES)
    )
    assert await scenario.saved_queue(patio_id) == [TEST_TRACK_URI]
    switched = len(plug.commands)
    scenario.clear_events()

    await _report_set(scenario, living, patio)

    assert scenario.removals() == [(patio_id, None)]
    for key in (CONF_PLAYERS, CONF_PLAYER_DSP, CONF_PLAYER_QUEUES):
        assert scenario.mass.config.get(f"{key}/{patio_id}") is None
    for protocol in (patio, patio_airplay):
        assert scenario.mass.config.get_protocol_record(protocol.protocol_id) is None
    assert await scenario.saved_queue(patio_id) is None
    assert (plug.commands[switched:], plug.powered) == ([], True)


async def test_the_providers_of_a_removed_speaker_do_not_look_for_it_again(
    scenario: Scenario,
) -> None:
    """
    The providers of a speaker that goes up in a set are not asked to look for it again.

    A provider that looks again finds the speaker, which is still on the network, and makes it
    a new player; after this removal no player comes back.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    scenario.clear_events()

    await _report_set(scenario, living, patio)

    assert scenario.events(EventType.PLAYER_ADDED) == []
    assert scenario.players_carrying(patio, patio_airplay) == []


async def test_a_speaker_of_a_set_that_another_provider_finds_again_is_a_new_player(
    scenario: Scenario,
) -> None:
    """
    The same report once more changes nothing; another provider that finds the speaker again makes it a new player.

    That new player is the user's to remove.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    await _report_set(scenario, living, patio)
    players = scenario.visible_players()
    scenario.clear_events()

    await _report_set(scenario, living, patio)

    assert scenario.events(EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED) == []
    assert scenario.visible_players() == players

    await scenario.register(scenario.protocol("airplay_patio"))
    await scenario.idle()

    new_player = scenario.single_player(scenario.protocol("airplay_patio"))
    assert new_player.player_id != patio_id
    assert scenario.events(EventType.PLAYER_ADDED) == [
        Event(EventType.PLAYER_ADDED, new_player.player_id)
    ]


@pytest.mark.parametrize(
    "report",
    ["unknown_set", "unknown_speaker", "own_player", "group", "other_provider", "closing"],
)
async def test_a_report_that_names_nothing_to_remove_removes_nothing(
    scenario: Scenario, report: str
) -> None:
    """
    A report removes no player when it names nothing a set can hold.

    That is a report for a player id instead of the protocol of a set, one that names a player
    id instead of a protocol of a speaker, the protocol of the set itself, a group, or a protocol
    of another provider; and a report that comes while the server shuts down.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    group = scenario.provider("sonos").protocol(
        "sonos_group", "Downstairs", role="virtual", type=PlayerType.GROUP
    )
    await scenario.register(group)
    await scenario.idle()
    living_id = scenario.single_player(living).player_id
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    players = scenario.visible_players()
    scenario.clear_events()

    if report == "closing":
        sonos = scenario.provider("sonos")
        unload = sonos.unload

        async def _report_set_and_unload(is_removed: bool = False) -> None:
            """Report the set while the connections of the provider go down, then unload."""
            await _set_of(scenario, "sonos_living", "sonos_patio")
            await unload(is_removed)

        sonos.unload = _report_set_and_unload  # type: ignore[method-assign]
        await scenario.restart()
        await scenario.idle()
    else:
        set_id, speaker_id = {
            "unknown_set": (living_id, "sonos_patio"),
            "unknown_speaker": ("sonos_living", patio_id),
            "own_player": ("sonos_living", "sonos_living"),
            "group": ("sonos_living", "sonos_group"),
            "other_provider": ("sonos_living", "airplay_patio"),
        }[report]
        await _set_of(scenario, set_id, speaker_id)
        await scenario.idle()

    assert scenario.removals() == []
    assert [player.player_id for player in scenario.visible_players()] == [
        player.player_id for player in players
    ]


async def test_the_same_set_reported_twice_at_once_removes_its_speaker_once(
    scenario: Scenario,
) -> None:
    """
    Two reports of the same set at the same moment remove the speaker once.

    Also while a protocol of the speaker takes its time to detach: clients are told once that it
    is removed.
    """
    living, patio, patio_airplay = await _living_and_patio(scenario)
    patio_id = scenario.single_player(patio, patio_airplay).player_id
    patio_airplay.detach_time = DETACH_TIME
    scenario.clear_events()

    reports = [
        await scenario.command(_set_of(scenario, living.protocol_id, patio.protocol_id)),
        await scenario.command(_set_of(scenario, living.protocol_id, patio.protocol_id)),
    ]
    await scenario.advance(DETACH_TIME)
    for report in reports:
        await report
    await scenario.idle()

    assert scenario.removals() == [(patio_id, None)]


async def test_a_set_reported_while_its_protocol_attaches_removes_its_speaker(
    scenario: Scenario,
) -> None:
    """
    A provider may report the set while the protocol that delivers it attaches.

    The protocol attaches, and the player of the speaker the set names is removed.
    """
    sonos = await scenario.add_provider("sonos")
    patio = sonos.protocol("sonos_patio", "Patio", identifiers=PATIO)
    await scenario.register(patio)
    await scenario.idle()
    patio_id = scenario.single_player(patio).player_id
    living = sonos.protocol("sonos_living", "Living", identifiers=LIVING)

    async def _report_set_on_attach(player: Player) -> None:  # noqa: ARG001
        """Report the set the protocol delivers, as a provider does once it is attached."""
        await _set_of(scenario, living.protocol_id, patio.protocol_id)

    living.on_attach = _report_set_on_attach  # type: ignore[method-assign]
    attach = await scenario.command(scenario.mass.players.register(living))
    await scenario.idle()

    assert attach.done(), "the attach of the protocol that delivers the set did not finish"
    await attach
    assert scenario.player_of(living) is not None
    assert scenario.removals() == [(patio_id, None)]


async def _living_and_patio(scenario: Scenario) -> tuple[FakeProtocol, FakeProtocol, FakeProtocol]:
    """
    Set up the Living speaker, whose protocol delivers a set, and the Patio speaker.

    The Patio speaker has a Sonos protocol and an AirPlay receiver: one player of two providers.

    :return: The protocol of the Living speaker, and the two of the Patio speaker.
    """
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    living = sonos.protocol("sonos_living", "Living", identifiers=LIVING)
    patio = sonos.protocol("sonos_patio", "Patio", identifiers=PATIO)
    patio_airplay = airplay.protocol("airplay_patio", "Patio", role="audio", identifiers=PATIO)
    for protocol in (living, patio, patio_airplay):
        await scenario.register(protocol)
    await scenario.idle()
    return living, patio, patio_airplay


async def _report_set(scenario: Scenario, protocol: FakeProtocol, *speakers: FakeProtocol) -> None:
    """
    Let the provider of a protocol report the other speakers of the set it delivers.

    :param protocol: The protocol that delivers the set.
    :param speakers: The protocols of the other speakers of the set.
    """
    await _set_of(scenario, protocol.protocol_id, *(speaker.protocol_id for speaker in speakers))
    await scenario.idle()


async def _set_of(scenario: Scenario, protocol_id: str, *speaker_ids: str) -> None:
    """
    Report the other speakers of the set a protocol delivers, without waiting for the server.

    :param protocol_id: Id of the protocol that delivers the set.
    :param speaker_ids: Ids of the protocols of the other speakers of the set.
    """
    await scenario.mass.players.remove_absorbed_players(protocol_id, list(speaker_ids))


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
