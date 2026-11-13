"""Tests for which player a protocol belongs to when it attaches, and when it has to wait."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest
from music_assistant_models.enums import EventType, IdentifierType, PlaybackState, ProtocolRole
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import (
    CONF_LEGACY_IDS,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYERS,
)
from music_assistant.controllers.players.controller import PlayerController
from tests.scenarios.harness import FAKE_SETTING, Event, FakeProtocol, Scenario

KITCHEN = {
    IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
    IdentifierType.IP_ADDRESS: "192.168.1.20",
}
# what a protocol reports of the same device while its hardware address is not known yet
KITCHEN_ADDRESS = {IdentifierType.IP_ADDRESS: "192.168.1.20"}
PLAYER_EVENTS = (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED)


def _edge(scenario: Scenario, protocol_id: str) -> str | None:
    """Return the id of the player a protocol is stored as belonging to."""
    record = scenario.mass.config.get_protocol_record(protocol_id)
    return record.get("player_id") if record else None


async def test_audio_protocol_without_a_hardware_address_waits_for_its_device(
    scenario: Scenario,
) -> None:
    """A protocol that could still turn out to belong to a device gets a player after 15 s."""
    airplay = await scenario.add_provider("airplay")
    speaker = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    scenario.clear_events()

    await scenario.register(speaker)
    await scenario.advance(14)

    assert scenario.player_of(speaker) is None
    assert scenario.mass.players.get_protocol("ap_kitchen") is speaker
    assert scenario.visible_players() == []
    assert scenario.mass.config.get(CONF_PLAYER_PROTOCOLS, {}) == {}

    await scenario.advance(1)

    # the player is there at 15 s, before any follow-up work runs
    players = scenario.players_carrying(speaker)
    assert len(players) == 1
    player_id = players[0].player_id
    await scenario.idle()
    assert player_id.startswith("pl")
    assert scenario.events(*PLAYER_EVENTS) == [Event(EventType.PLAYER_ADDED, player_id)]


async def test_waiting_protocol_gets_its_player_once_its_hardware_address_is_known(
    scenario: Scenario,
) -> None:
    """The wait ends as soon as the address resolves to the hardware address of the device."""
    airplay = await scenario.add_provider("airplay")
    speaker = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    await scenario.register(speaker)

    await scenario.report(speaker, identifiers=KITCHEN)
    await scenario.idle()

    assert scenario.player_of(speaker) is not None


async def test_waiting_protocol_joins_the_player_its_native_protocol_creates(
    scenario: Scenario,
) -> None:
    """A protocol that waits is attached as soon as another protocol created its player."""
    airplay = await scenario.add_provider("airplay")
    sonos = await scenario.add_provider("sonos")
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(audio)
    await scenario.advance(2)

    await scenario.register(native)
    await scenario.idle()

    assert scenario.player_of(audio) == scenario.player_of(native) is not None
    assert len(scenario.visible_players()) == 1


async def test_two_waiting_protocols_of_one_device_become_one_player(scenario: Scenario) -> None:
    """Protocols that wait together end up on the player the first of them gets."""
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    first = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    second = chromecast.protocol("cc_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    await scenario.register(first)
    await scenario.register(second)

    await scenario.advance(15)
    await scenario.idle()

    assert scenario.player_of(first) == scenario.player_of(second) is not None
    assert len(scenario.visible_players()) == 1


async def test_protocol_that_is_lost_while_it_waits_becomes_no_player(scenario: Scenario) -> None:
    """A detach during the wait ends it without leaving a player or a config behind."""
    airplay = await scenario.add_provider("airplay")
    speaker = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    await scenario.register(speaker)

    await scenario.unregister(speaker)
    await scenario.advance(20)
    await scenario.idle()

    assert scenario.visible_players() == []
    assert scenario.mass.players.get_protocol("ap_kitchen") is None
    assert scenario.mass.config.get(CONF_PLAYER_PROTOCOLS, {}) == {}


async def test_native_protocol_never_waits(scenario: Scenario) -> None:
    """The protocol that stands for the device itself gets its player at once."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN_ADDRESS)

    await scenario.register(speaker)

    assert scenario.player_of(speaker) is not None


async def test_protocol_that_rides_on_another_waits_for_it(scenario: Scenario) -> None:
    """A derived protocol is attached to the player of its base, however late that comes."""
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    base = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    bridge = sendspin.protocol(
        "sendspin_bridge_kitchen", "Kitchen", role="audio", underlying_protocol_id="ap_kitchen"
    )
    await scenario.register(bridge)
    await scenario.advance(60)

    assert scenario.player_of(bridge) is None

    await scenario.register(base)
    await scenario.idle()

    assert scenario.player_of(bridge) == scenario.player_of(base) is not None


@pytest.mark.parametrize("returns", ["while_running", "after_a_restart"])
async def test_protocol_that_returns_finds_its_place_also_when_a_bridge_waits_for_it(
    scenario: Scenario, returns: str
) -> None:
    """A protocol that is away keeps the place of its domain against a bridge, in any order."""
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    carrier = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    client = sendspin.protocol("sendspin_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    bridge = sendspin.protocol(
        "sendspin_bridge_kitchen", "Kitchen", role="audio", underlying_protocol_id="ap_kitchen"
    )
    for protocol in (carrier, client, bridge):
        await scenario.register(protocol)
    player_id = scenario.single_player(carrier, client).player_id
    if returns == "while_running":
        await scenario.unregister(client)
        await scenario.idle()
        assert scenario.player_of(bridge) is None
    else:
        for protocol_id in ("ap_kitchen", "sendspin_kitchen", "sendspin_bridge_kitchen"):
            scenario.set_present(protocol_id, False)
        await scenario.restart()
        carrier = scenario.protocol("ap_kitchen")
        bridge = scenario.protocol("sendspin_bridge_kitchen")
        client = scenario.protocol("sendspin_kitchen")
        # the bridge runs in the server and is there before the client reconnects
        await scenario.register(carrier)
        await scenario.register(bridge)
        await scenario.advance(3)
        assert scenario.player_of(bridge) is None

    await scenario.register(client)
    await scenario.idle()

    assert scenario.player_of(client) == player_id
    assert scenario.player_of(bridge) is None
    assert [player.player_id for player in scenario.visible_players()] == [player_id]


@pytest.mark.parametrize("after", ["switching_off", "a_restart"])
async def test_bridge_takes_the_place_of_a_protocol_that_is_away_and_switched_off(
    scenario: Scenario, after: str
) -> None:
    """An output that is gone and that the user switched off leaves the place of its domain free."""
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    carrier = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    client = sendspin.protocol("sendspin_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    bridge = sendspin.protocol(
        "sendspin_bridge_kitchen", "Kitchen", role="audio", underlying_protocol_id="ap_kitchen"
    )
    for protocol in (carrier, client, bridge):
        await scenario.register(protocol)
    player_id = scenario.single_player(carrier, client).player_id
    # the client of the device is gone for good, its record stays
    await scenario.unregister(client)
    await scenario.idle()
    assert scenario.player_of(bridge) is None

    await scenario.mass.config.save_protocol_config("sendspin_kitchen", {"enabled": False})
    await scenario.idle()
    if after == "a_restart":
        for protocol_id in ("ap_kitchen", "sendspin_bridge_kitchen"):
            scenario.set_present(protocol_id, False)
        await scenario.restart()
        await scenario.register(scenario.protocol("ap_kitchen"))
        bridge = scenario.protocol("sendspin_bridge_kitchen")
        await scenario.register(bridge)
        await scenario.idle()

    assert scenario.player_of(bridge) == player_id
    assert [player.player_id for player in scenario.visible_players()] == [player_id]


async def test_protocol_switched_on_again_gets_its_place_back_from_the_bridge(
    scenario: Scenario,
) -> None:
    """An output switched on again holds its place: the bridge waits, the output returns to it."""
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    carrier = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    client = sendspin.protocol("sendspin_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    bridge = sendspin.protocol(
        "sendspin_bridge_kitchen", "Kitchen", role="audio", underlying_protocol_id="ap_kitchen"
    )
    for protocol in (carrier, client, bridge):
        await scenario.register(protocol)
    player_id = scenario.single_player(carrier, client).player_id
    await scenario.unregister(client)
    await scenario.mass.config.save_protocol_config("sendspin_kitchen", {"enabled": False})
    await scenario.idle()
    assert scenario.player_of(bridge) == player_id
    # the server holds a bridge that waits: its provider does not offer it again
    scenario.set_present("sendspin_bridge_kitchen", False)

    await scenario.mass.config.save_protocol_config("sendspin_kitchen", {"enabled": True})
    await scenario.idle()

    assert scenario.player_of(bridge) is None
    state = scenario.player(player_id)
    assert state is not None
    assert {info.protocol_id: info.available for info in state.protocols} == {
        "ap_kitchen": True,
        "sendspin_bridge_kitchen": False,
        "sendspin_kitchen": False,
    }

    await scenario.register(client)
    await scenario.idle()

    assert scenario.player_of(client) == player_id
    assert scenario.player_of(bridge) is None
    assert [player.player_id for player in scenario.visible_players()] == [player_id]

    # switched off once more, the output leaves its place to the bridge that waits
    await scenario.mass.config.save_protocol_config("sendspin_kitchen", {"enabled": False})
    await scenario.idle()

    assert scenario.player_of(client) is None
    assert scenario.player_of(bridge) == player_id


@pytest.mark.parametrize("seen_again", ["while_running", "after_a_restart"])
async def test_protocol_switched_off_keeps_its_player_when_it_is_seen_again(
    scenario: Scenario, seen_again: str
) -> None:
    """
    The own client of a device the user switched off is no player of its own when it is seen again.

    A bridge holds the place of its domain meanwhile; the client keeps its link and its switch,
    and takes its place back when the user switches it on again.
    """
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    carrier = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    client = sendspin.protocol("sendspin_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    bridge = sendspin.protocol(
        "sendspin_bridge_kitchen", "Kitchen", role="audio", underlying_protocol_id="ap_kitchen"
    )
    for protocol in (carrier, client, bridge):
        await scenario.register(protocol)
    player_id = scenario.single_player(carrier, client).player_id
    await scenario.mass.config.save_protocol_config("sendspin_kitchen", {"enabled": False})
    await scenario.idle()
    assert scenario.player_of(bridge) == player_id
    if seen_again == "after_a_restart":
        await scenario.restart()
        for protocol_id in ("ap_kitchen", "sendspin_bridge_kitchen"):
            await scenario.register(scenario.protocol(protocol_id))
    scenario.clear_events()

    # its provider looks for its devices again and offers the client that is still there
    await scenario.provider("sendspin").discover_players()
    await scenario.idle()

    assert scenario.player_of(scenario.protocol("sendspin_kitchen")) is None
    assert scenario.events(*PLAYER_EVENTS) == []
    assert sorted(scenario.mass.config.get(CONF_PLAYERS)) == [player_id]
    assert _edge(scenario, "sendspin_kitchen") == player_id
    assert (scenario.mass.config.get_protocol_record("sendspin_kitchen") or {})["enabled"] is False
    assert [player.player_id for player in scenario.visible_players()] == [player_id]

    await scenario.mass.config.save_protocol_config("sendspin_kitchen", {"enabled": True})
    await scenario.idle()

    assert scenario.player_of(scenario.protocol("sendspin_kitchen")) == player_id
    assert scenario.player_of(scenario.protocol("sendspin_bridge_kitchen")) is None
    assert [player.player_id for player in scenario.visible_players()] == [player_id]


@pytest.mark.parametrize(
    ("away_domain", "domain", "role", "own_identifiers"),
    [
        ("chromecast", "airplay", "audio", {IdentifierType.AIRPLAY_ID: "001132AABBCC"}),
        ("chromecast", "sonos", "native", {IdentifierType.UUID: "RINCON_B"}),
        ("squeezelite", "squeezelite", "audio", {}),
    ],
    ids=["audio", "native", "same_provider"],
)
async def test_new_device_at_the_address_of_a_device_that_is_away_is_a_player_of_its_own(
    scenario: Scenario,
    away_domain: str,
    domain: str,
    role: Any,
    own_identifiers: dict[IdentifierType, str],
) -> None:
    """An address that is handed out again ties a new device to nothing that is away."""
    away_provider = await scenario.add_provider(away_domain)
    provider = away_provider if domain == away_domain else await scenario.add_provider(domain)
    away = away_provider.protocol("away_a", "Player A", role="audio", identifiers=KITCHEN)
    await scenario.register(away)
    player_id = scenario.single_player(away).player_id
    await scenario.configure_player(player_id, name="Player A custom")
    await scenario.unregister(away)

    newcomer = provider.protocol(
        "new_b",
        "Player B",
        role=role,
        identifiers={
            IdentifierType.MAC_ADDRESS: "00:11:32:AA:BB:CC",
            IdentifierType.IP_ADDRESS: KITCHEN[IdentifierType.IP_ADDRESS],
            **own_identifiers,
        },
    )
    await scenario.register(newcomer)
    await scenario.idle()

    assert scenario.player_of(newcomer) not in (None, player_id)
    assert _edge(scenario, "away_a") == player_id
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/{player_id}/name") == "Player A custom"
    assert len(scenario.visible_players()) == 2


async def test_protocol_returns_to_the_player_it_is_stored_for(scenario: Scenario) -> None:
    """A protocol that comes back is attached to its player without matching it again."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(native)
    await scenario.register(audio)
    player_id = scenario.single_player(native, audio).player_id
    await scenario.unregister(audio)

    # it reports nothing its player could be recognised by anymore
    moved = airplay.protocol(
        "ap_kitchen",
        "Kitchen",
        role="audio",
        identifiers={IdentifierType.IP_ADDRESS: "192.168.1.77"},
    )
    await scenario.register(moved)

    assert scenario.player_of(moved) == player_id


async def test_stored_player_that_turns_out_to_be_another_device_is_left(
    scenario: Scenario,
) -> None:
    """A protocol whose stored player holds an identifier that rules it out is matched anew."""
    chromecast = await scenario.add_provider("chromecast")
    airplay = await scenario.add_provider("airplay")
    cast = chromecast.protocol(
        "cc_kitchen",
        "Kitchen",
        role="audio",
        identifiers=KITCHEN | {IdentifierType.AIRPLAY_ID: "AA:AA:AA:AA:AA:01"},
    )
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(cast)
    await scenario.register(audio)
    player_id = scenario.single_player(cast, audio).player_id
    await scenario.unregister(audio)

    other = airplay.protocol(
        "ap_kitchen",
        "Bedroom",
        role="audio",
        identifiers={
            IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B1",
            IdentifierType.AIRPLAY_ID: "AA:AA:AA:AA:AA:02",
        },
    )
    await scenario.register(other)
    await scenario.idle()

    new_player_id = scenario.player_of(other)
    assert new_player_id not in (None, player_id)
    assert _edge(scenario, "ap_kitchen") == new_player_id
    state = scenario.player(player_id)
    assert state is not None
    assert [protocol.protocol_id for protocol in state.protocols] == ["cc_kitchen"]


async def test_device_that_matches_a_disabled_player_stays_without_a_player(
    scenario: Scenario,
) -> None:
    """A protocol of a disabled player is stored as belonging to it, and attached once enabled."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(native)
    player_id = scenario.single_player(native).player_id
    await scenario.disable(player_id)
    scenario.clear_events()

    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(audio)
    await scenario.advance(15)
    await scenario.idle()

    assert scenario.visible_players() == []
    assert scenario.player_of(audio) is None
    assert _edge(scenario, "ap_kitchen") == player_id
    assert scenario.events(*PLAYER_EVENTS) == []

    await scenario.enable(player_id)
    await scenario.idle()

    assert scenario.player_of(scenario.protocol("ap_kitchen")) == player_id
    assert scenario.player_of(scenario.protocol("RINCON_KITCHEN")) == player_id


@pytest.mark.parametrize(
    ("domains", "roles"),
    [(("squeezelite", "snapcast"), ("native", "native")), (("airplay", "airplay"), ("audio",) * 2)],
    ids=["two_natives", "one_domain_twice"],
)
async def test_protocols_a_player_has_no_second_place_for_stay_separate(
    scenario: Scenario, domains: tuple[str, str], roles: tuple[Any, Any]
) -> None:
    """A player holds one protocol that stands for the device and one per domain."""
    first_provider = await scenario.add_provider(domains[0])
    second_provider = (
        first_provider if domains[1] == domains[0] else await scenario.add_provider(domains[1])
    )
    first = first_provider.protocol("first", "Pi", role=roles[0], identifiers=KITCHEN)
    second = second_provider.protocol("second", "Pi", role=roles[1], identifiers=KITCHEN)

    await scenario.register(first)
    await scenario.register(second)
    await scenario.idle()

    assert scenario.player_of(first) != scenario.player_of(second)
    assert None not in (scenario.player_of(first), scenario.player_of(second))


async def test_protocol_that_becomes_a_second_own_protocol_gets_a_player_of_its_own(
    scenario: Scenario,
) -> None:
    """An audio protocol that reports to be the device itself leaves a player that has one."""
    sonos = await scenario.add_provider("sonos")
    squeezelite = await scenario.add_provider("squeezelite")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    other = squeezelite.protocol("sq_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(native)
    await scenario.register(other)
    player_id = scenario.single_player(native, other).player_id
    scenario.clear_events()

    await scenario.register(other, role="native")
    await scenario.idle()

    new_player_id = scenario.player_of(other)
    assert new_player_id not in (None, player_id)
    assert scenario.player_of(native) == player_id
    state = scenario.player(player_id)
    assert state is not None
    assert [protocol.protocol_id for protocol in state.protocols] == ["RINCON_KITCHEN"]
    assert scenario.events(*PLAYER_EVENTS) == [Event(EventType.PLAYER_ADDED, new_player_id)]
    # the new player has a queue of its own
    assert scenario.events(EventType.QUEUE_ADDED) == [Event(EventType.QUEUE_ADDED, new_player_id)]
    assert scenario.queue(new_player_id) is not None


async def test_native_protocol_that_becomes_an_audio_protocol_stops_what_it_played(
    scenario: Scenario,
) -> None:
    """A protocol that changes its role is detached and attached again on the same player."""
    squeezelite = await scenario.add_provider("squeezelite")
    speaker = squeezelite.protocol("sq_kitchen", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    await scenario.play(player_id)
    scenario.clear_events()

    await scenario.register(speaker, role="audio")
    await scenario.idle()

    assert scenario.player_of(speaker) == player_id
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    assert player.get_role("sq_kitchen") == ProtocolRole.AUDIO
    assert player.own_protocol is None
    assert player.active_output_protocol is None
    queue = scenario.queue(player_id)
    assert queue is not None
    assert queue.state == PlaybackState.IDLE
    assert scenario.events(*PLAYER_EVENTS) == []


async def test_session_only_player_keeps_its_player_when_its_protocol_changes_role(
    scenario: Scenario,
) -> None:
    """A role change keeps a player that nothing is stored of, with its id."""
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web_1", "Web Browser", role="audio", session_only=True)
    await scenario.register(browser)
    assert scenario.player_of(browser) == "sendspin_web_1"
    scenario.clear_events()

    await scenario.register(browser, role="native")

    assert scenario.player_of(browser) == "sendspin_web_1"
    assert scenario.events(*PLAYER_EVENTS) == []


@pytest.mark.parametrize("first", ["session_only", "device"])
async def test_session_only_player_shares_no_device_with_another_protocol(
    scenario: Scenario, first: str
) -> None:
    """A protocol that only exists while connected and a device it shares an address with stay apart."""
    sendspin = await scenario.add_provider("sendspin")
    chromecast = await scenario.add_provider("chromecast")
    app = sendspin.protocol(
        "sendspin_app_1", "TV app", session_only=True, identifiers=KITCHEN_ADDRESS
    )
    tv = chromecast.protocol("cast_tv", "TV", role="audio", identifiers=KITCHEN)
    for protocol in (app, tv) if first == "session_only" else (tv, app):
        await scenario.register(protocol)
    await scenario.idle()

    assert scenario.player_of(app) == "sendspin_app_1"
    assert scenario.player_of(tv) not in (None, "sendspin_app_1")


async def test_session_only_protocol_takes_the_place_of_no_other_session_only_player(
    scenario: Scenario,
) -> None:
    """A protocol that only exists while connected never takes over another one's player."""
    sendspin = await scenario.add_provider("sendspin")
    display = sendspin.protocol(
        "sendspin_display_1", "Display", session_only=True, identifiers=KITCHEN
    )
    await scenario.register(display)
    await scenario.configure_player("sendspin_display_1", name="Kitchen display")
    await scenario.unregister(display)
    # a session of the customised player stores what it reports
    await scenario.register(display)
    await scenario.unregister(display)

    visualizer = sendspin.protocol(
        "sendspin_visualizer_1", "Visualizer", session_only=True, identifiers=KITCHEN
    )
    await scenario.register(visualizer)

    assert scenario.player_of(visualizer) == "sendspin_visualizer_1"
    assert _edge(scenario, "sendspin_display_1") == "sendspin_display_1"
    assert scenario.mass.config.get_protocol_record("sendspin_visualizer_1") is None


async def test_session_only_protocol_never_follows_a_link_to_the_player_of_a_device(
    scenario: Scenario,
) -> None:
    """
    A protocol that connects as one that only exists while connected gets a player of its own.

    Its record may still link it to the player of a device, as a client that rode on that
    device before; that player keeps its own protocol.
    """
    chromecast = await scenario.add_provider("chromecast")
    sendspin = await scenario.add_provider("sendspin")
    tv = chromecast.protocol("cast_tv", "TV", identifiers=KITCHEN)
    await scenario.register(tv)
    player_id = scenario.single_player(tv).player_id
    scenario.mass.config.create_protocol_record(
        "sendspin_receiver",
        provider=sendspin.instance_id,
        player_id=player_id,
        role=ProtocolRole.AUDIO,
        default_name="TV",
    )
    await scenario.unregister(tv)

    receiver = sendspin.protocol("sendspin_receiver", "Web Browser", session_only=True)
    await scenario.register(receiver)
    await scenario.register(tv)
    await scenario.idle()

    assert scenario.player_of(receiver) == "sendspin_receiver"
    assert scenario.player_of(tv) == player_id
    state = scenario.player(player_id)
    assert state is not None
    assert [info.protocol_id for info in state.protocols] == ["cast_tv"]
    # the link stays, for when the client connects as an output of the device again
    assert _edge(scenario, "sendspin_receiver") == player_id


async def test_session_only_session_of_a_client_of_a_device_leaves_its_link_alone(
    scenario: Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A client of a device that connects as one that only exists while connected stores nothing.

    When it connects as a client of its device again, it returns to the player of the device.
    """
    sonos = await scenario.add_provider("sonos")
    sendspin = await scenario.add_provider("sendspin")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(native)
    player_id = scenario.single_player(native).player_id
    client = sendspin.protocol("sendspin_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(client)
    assert scenario.player_of(client) == player_id
    await scenario.unregister(client)
    stored = deepcopy(scenario.mass.config.get(CONF_PLAYER_PROTOCOLS))
    stored_players = sorted(scenario.mass.config.get(CONF_PLAYERS))

    session = sendspin.protocol("sendspin_kitchen", "Kitchen", role="native", session_only=True)
    with caplog.at_level(logging.INFO, logger="music_assistant"):
        for _ in range(2):
            await scenario.register(session)
            assert scenario.player_of(session) == "sendspin_kitchen"
            state = scenario.player(player_id)
            assert state is not None
            assert {info.protocol_id: info.available for info in state.protocols} == {
                "RINCON_KITCHEN": True,
                "sendspin_kitchen": False,
            }
            await scenario.unregister(session)

    assert "no longer belongs" not in caplog.text
    assert scenario.mass.config.get(CONF_PLAYER_PROTOCOLS) == stored
    assert sorted(scenario.mass.config.get(CONF_PLAYERS)) == stored_players
    assert _edge(scenario, "sendspin_kitchen") == player_id

    client = sendspin.protocol("sendspin_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(client)
    await scenario.idle()

    assert scenario.player_of(client) == player_id
    assert [player.player_id for player in scenario.visible_players()] == [player_id]


def _playable(scenario: Scenario, player_id: str) -> bool:
    """Return whether every protocol a player lists can play."""
    state = scenario.player(player_id)
    assert state is not None
    return all(info.available for info in state.protocols)


async def _web_session_of_a_device_client(
    scenario: Scenario, carrier_domain: str = "sonos"
) -> tuple[str, FakeProtocol]:
    """
    Connect a client of a device as a web player, while its record still names the device.

    Returns the id of the player of the device and the web client.
    """
    carrier_provider = await scenario.add_provider(carrier_domain)
    sendspin = await scenario.add_provider("sendspin")
    carrier = carrier_provider.protocol(
        "carrier_kitchen",
        "Kitchen",
        role="audio" if carrier_domain == "airplay" else "native",
        identifiers=KITCHEN,
    )
    await scenario.register(carrier)
    player_id = scenario.single_player(carrier).player_id
    client = sendspin.protocol("sendspin_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(client)
    await scenario.unregister(client)
    web = sendspin.protocol("sendspin_kitchen", "Kitchen", role="native", session_only=True)
    await scenario.register(web)
    assert scenario.player_of(web) == "sendspin_kitchen"
    assert _edge(scenario, "sendspin_kitchen") == player_id
    return player_id, web


async def test_web_player_reconnects_while_the_player_its_record_names_is_disabled(
    scenario: Scenario,
) -> None:
    """The player a stale record of a web client names is no player of that web client."""
    player_id, web = await _web_session_of_a_device_client(scenario)
    await scenario.disable(player_id)
    await scenario.unregister(web)

    await scenario.register(web)

    assert scenario.player_of(web) == "sendspin_kitchen"


async def test_provider_learns_whether_it_may_set_up_a_client_it_has_no_object_of_yet(
    scenario: Scenario,
) -> None:
    """Before it registers a new object, a provider asks with that object: what it is counts."""
    player_id, web = await _web_session_of_a_device_client(scenario)
    await scenario.unregister(web)
    await scenario.disable(player_id)
    sendspin = scenario.provider("sendspin")
    players = scenario.mass.players

    # a new connection of the client, of which the server knows no object yet
    session = FakeProtocol(sendspin, "sendspin_kitchen", web._device)
    device_client = FakeProtocol(sendspin, "sendspin_kitchen", web._device)
    device_client._attr_session_only = False

    assert players.wants_protocol("sendspin_kitchen", session) is True
    assert players.wants_protocol("sendspin_kitchen", device_client) is False


async def test_web_player_the_user_disabled_stays_away_until_it_is_enabled(
    scenario: Scenario,
) -> None:
    """The config a customised web player has of its own governs it, unlike a stale one."""
    sendspin = await scenario.add_provider("sendspin")
    web = sendspin.protocol("sendspin_web_1", "Web Browser", session_only=True)
    await scenario.register(web)
    await scenario.configure_player("sendspin_web_1", name="Study browser")
    await scenario.disable("sendspin_web_1")
    await scenario.unregister(web)

    await scenario.register(web)

    assert scenario.player_of(web) is None

    await scenario.enable("sendspin_web_1")
    await scenario.idle()

    assert scenario.player_of(web) == "sendspin_web_1"


@pytest.mark.parametrize("stored", ["switched_off_live", "switched_off_before"])
async def test_switch_of_the_output_a_stale_record_lists_leaves_the_web_player_alone(
    scenario: Scenario, stored: str
) -> None:
    """
    Switching off the output the player of a device lists for a web client is its own business.

    The web player stays, and connects again; one that connects while that output is switched
    off is a player of its own as well.
    """
    player_id, web = await _web_session_of_a_device_client(scenario)
    if stored == "switched_off_before":
        await scenario.unregister(web)
    scenario.clear_events()

    await scenario.configure_player(player_id, **{"sendspin_kitchen||protocol||enabled": False})
    await scenario.idle()

    assert (scenario.mass.config.get_protocol_record("sendspin_kitchen") or {})["enabled"] is False
    if stored == "switched_off_live":
        assert scenario.player_of(web) == "sendspin_kitchen"
        assert scenario.events(*PLAYER_EVENTS) == []
        assert _playable(scenario, "sendspin_kitchen")
        await scenario.unregister(web)
    await scenario.register(web)

    assert scenario.player_of(web) == "sendspin_kitchen"
    assert _playable(scenario, "sendspin_kitchen")


@pytest.mark.parametrize("web_client", ["connected", "gone"])
async def test_web_client_whose_record_names_a_device_holds_no_place_of_a_bridge(
    scenario: Scenario, web_client: str
) -> None:
    """A bridge of the device takes the place of its domain that the web client does not use."""
    player_id, web = await _web_session_of_a_device_client(scenario, carrier_domain="airplay")
    if web_client == "gone":
        await scenario.unregister(web)
    bridge = scenario.provider("sendspin").protocol(
        "sendspin_bridge_kitchen", "Kitchen", role="audio", underlying_protocol_id="carrier_kitchen"
    )

    await scenario.register(bridge)
    await scenario.idle()

    assert scenario.player_of(bridge) == player_id


async def test_removing_the_player_of_a_device_leaves_a_session_of_its_client_alone(
    scenario: Scenario,
) -> None:
    """A client of the device that is connected as a player of its own stays that player."""
    sonos = await scenario.add_provider("sonos")
    sendspin = await scenario.add_provider("sendspin")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(native)
    player_id = scenario.single_player(native).player_id
    client = sendspin.protocol("sendspin_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(client)
    await scenario.unregister(client)
    session = sendspin.protocol("sendspin_kitchen", "Kitchen", role="native", session_only=True)
    await scenario.register(session)
    removed: list[str] = []

    async def _on_remove() -> None:
        removed.append(session.protocol_id)

    session.on_remove = _on_remove  # type: ignore[method-assign]
    scenario.set_present("RINCON_KITCHEN", False)

    await scenario.remove(player_id)
    await scenario.idle()

    assert scenario.player_of(session) == "sendspin_kitchen"
    assert removed == []
    assert [(player.player_id, player.available) for player in scenario.visible_players()] == [
        ("sendspin_kitchen", True)
    ]
    # the config the removed player had of the client went with it
    assert scenario.mass.config.get_protocol_record("sendspin_kitchen") is None


async def test_protocol_that_takes_the_place_of_one_that_is_away_keeps_its_own_config(
    scenario: Scenario,
) -> None:
    """What a protocol stored itself wins over the record it takes over, which fills the gaps."""
    chromecast = await scenario.add_provider("chromecast")
    airplay = await scenario.add_provider("airplay")
    config = scenario.mass.config
    tv = chromecast.protocol("cast_tv", "TV", role="audio", identifiers=KITCHEN)
    old = airplay.protocol("ap_old", "TV", role="audio", identifiers=KITCHEN)
    await scenario.register(tv)
    await scenario.register(old)
    player_id = scenario.single_player(tv, old).player_id
    config.set_protocol_value("ap_old", FAKE_SETTING, "old")
    config.set_protocol_setup_value("ap_old", "credentials", "PAIRING-OF-OLD")
    config.set_protocol_setup_value("ap_old", "pairing_id", "ID-OF-OLD")
    config.set_protocol_value("ap_old", "enabled", False)
    await scenario.unregister(old)
    # a record of its own that is bound to no player, as the migration can leave one
    config.create_protocol_record(
        "ap_new",
        provider=airplay.instance_id,
        player_id=None,
        role=ProtocolRole.NATIVE,
        default_name="TV",
    )
    config.set_protocol_value("ap_new", FAKE_SETTING, "mine")
    config.set_protocol_setup_value("ap_new", "credentials", "PAIRING-OF-NEW")

    new = airplay.protocol("ap_new", "TV", role="audio", identifiers=KITCHEN)
    await scenario.register(new)

    assert scenario.player_of(new) == player_id
    record = config.get_protocol_record("ap_new")
    assert record is not None
    assert record["values"][FAKE_SETTING] == "mine"
    assert record["setup_data"] == {"credentials": "PAIRING-OF-NEW", "pairing_id": "ID-OF-OLD"}
    assert (record["enabled"], record["role"]) == (True, ProtocolRole.AUDIO)
    assert config.get_protocol_record("ap_old") is None


async def test_protocol_that_takes_the_place_of_one_that_is_away_takes_its_choices(
    scenario: Scenario,
) -> None:
    """The output and control choices that named the replaced protocol name its successor."""
    chromecast = await scenario.add_provider("chromecast")
    airplay = await scenario.add_provider("airplay")
    tv = chromecast.protocol("cast_tv", "TV", role="audio", identifiers=KITCHEN)
    old = airplay.protocol("ap_old", "TV", role="audio", identifiers=KITCHEN)
    await scenario.register(tv)
    await scenario.register(old)
    player_id = scenario.single_player(tv, old).player_id
    choices = ("preferred_output_protocol", "volume_control", "mute_control")
    await scenario.configure_player(player_id, **dict.fromkeys(choices, "ap_old"))
    await scenario.unregister(old)

    new = airplay.protocol("ap_new", "TV", role="audio", identifiers=KITCHEN)
    await scenario.register(new)

    assert scenario.player_of(new) == player_id
    config = scenario.mass.config
    assert [config.get_player_value(player_id, key) for key in choices] == ["ap_new"] * 3


async def test_waiting_protocol_joins_when_a_protocol_of_its_device_reports_more(
    scenario: Scenario,
) -> None:
    """An attached protocol that is refreshed with new identifiers lets a waiting one join."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol(
        "RINCON_KITCHEN",
        "Kitchen",
        identifiers={IdentifierType.MAC_ADDRESS: KITCHEN[IdentifierType.MAC_ADDRESS]},
    )
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    await scenario.register(native)
    player_id = scenario.single_player(native).player_id
    await scenario.register(audio)
    assert scenario.player_of(audio) is None

    native._set_reported(identifiers=KITCHEN)
    await scenario.register(native)
    await scenario.settle()

    assert scenario.player_of(audio) == player_id


async def test_config_stored_for_a_protocol_before_it_attaches_is_kept(scenario: Scenario) -> None:
    """A provider that stores the settings of a new device first finds them after the attach."""
    sonos = await scenario.add_provider("sonos")
    config = scenario.mass.config
    config.create_protocol_record(
        "RINCON_KITCHEN",
        provider=sonos.instance_id,
        player_id=None,
        role=ProtocolRole.NATIVE,
        default_name="Kitchen",
    )
    config.set_protocol_value("RINCON_KITCHEN", FAKE_SETTING, "seeded")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    seen: list[Any] = []

    async def _on_config_updated() -> None:
        seen.append(speaker.config.get_value(FAKE_SETTING))

    speaker.on_config_updated = _on_config_updated  # type: ignore[method-assign]
    await scenario.register(speaker)

    player_id = scenario.single_player(speaker).player_id
    assert seen == ["seeded"]
    assert _edge(scenario, "RINCON_KITCHEN") == player_id
    stored = scenario.protocol_config("RINCON_KITCHEN")
    assert stored is not None
    assert stored[FAKE_SETTING] == "seeded"
    assert config.get_protocol_ids_of_provider(sonos.instance_id) == ["RINCON_KITCHEN"]
    assert config.get_protocol_ids_of_provider("airplay") == []


async def test_protocol_is_found_by_its_id_also_when_that_is_a_legacy_id(
    scenario: Scenario,
) -> None:
    """The id of a protocol names that protocol, also where it names another player as well."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    other = sonos.protocol(
        "RINCON_DEN", "Den", identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:C0"}
    )
    await scenario.register(speaker)
    await scenario.register(other)
    kitchen_id = scenario.single_player(speaker).player_id
    den_id = scenario.single_player(other).player_id
    # the den player was once known under the id of the kitchen protocol
    scenario.mass.config.set_player_value(den_id, CONF_LEGACY_IDS, ["RINCON_KITCHEN"])
    players = scenario.mass.players
    players._build_legacy_id_index()

    assert players.get_player("RINCON_KITCHEN") is players.get_player(den_id)
    assert players.get_protocol("RINCON_KITCHEN") is speaker
    # the ids protocols report name protocols first
    assert players.get_player_by_protocol("RINCON_KITCHEN") is players.get_player(kitchen_id)
    assert scenario.mass.player_queues.get("RINCON_KITCHEN") is scenario.queue(den_id)


# a player from before the split that lost its protocols, which the user had given a name
KEPT_WRAPPER: dict[str, Any] = {
    "player_id": "up0a1b2c3d",
    "provider": "universal_player",
    "player_type": "player",
    "enabled": True,
    "name": "Kitchen radio",
    "default_name": "Kitchen",
    "values": {
        "linked_protocol_ids": [],
        "device_identifiers": {"mac_address": "54:78:C9:E6:0D:A0"},
    },
}


async def test_device_returns_to_the_player_that_was_kept_for_it(scenario: Scenario) -> None:
    """A stored player without protocols is offline until a device with its identifiers attaches."""
    sonos = await scenario.add_provider("sonos")
    sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)

    await scenario.seed_pre_split_config({"up0a1b2c3d": KEPT_WRAPPER})
    await scenario.idle()

    state = scenario.player("up0a1b2c3d")
    assert state is not None
    assert (state.name, state.available, state.provider) == ("Kitchen radio", False, "")
    assert state.setup_reason is None
    assert state.device_info.manufacturer

    await scenario.register(scenario.protocol("RINCON_KITCHEN"))
    await scenario.idle()

    assert scenario.player_of(scenario.protocol("RINCON_KITCHEN")) == "up0a1b2c3d"
    state = scenario.player("up0a1b2c3d")
    assert state is not None
    assert (state.name, state.available, state.provider) == ("Kitchen radio", True, "sonos")
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/up0a1b2c3d/provider") == sonos.instance_id


async def test_new_device_at_the_address_a_kept_player_stored_is_a_player_of_its_own(
    scenario: Scenario,
) -> None:
    """The address a player from before the split stored ties a new device to nothing."""
    sonos = await scenario.add_provider("sonos")
    kept = deepcopy(KEPT_WRAPPER)
    kept["values"]["device_identifiers"]["ip_address"] = KITCHEN[IdentifierType.IP_ADDRESS]
    await scenario.seed_pre_split_config({"up0a1b2c3d": kept})
    await scenario.idle()

    newcomer = scenario.provider(sonos.instance_id).protocol(
        "RINCON_B",
        "Player B",
        identifiers={
            IdentifierType.MAC_ADDRESS: "00:11:32:AA:BB:CC",
            IdentifierType.IP_ADDRESS: KITCHEN[IdentifierType.IP_ADDRESS],
        },
    )
    await scenario.register(newcomer)
    await scenario.idle()

    assert scenario.player_of(newcomer) not in (None, "up0a1b2c3d")


async def test_player_that_was_kept_without_protocols_can_be_listed_renamed_and_removed(
    scenario: Scenario,
) -> None:
    """A stored player that names no provider works like any other player that is away."""
    await scenario.seed_pre_split_config({"up0a1b2c3d": KEPT_WRAPPER})
    await scenario.idle()

    [state] = scenario.mass.players.all_player_states()
    assert (state.player_id, state.provider, state.available) == ("up0a1b2c3d", "", False)
    assert state.device_info.manufacturer
    assert [config.player_id for config in await scenario.player_configs()] == ["up0a1b2c3d"]
    assert len(await scenario.mass.config.get_player_configs(include_values=True)) == 1

    await scenario.configure_player("up0a1b2c3d", name="Study radio")

    renamed = scenario.player("up0a1b2c3d")
    assert renamed is not None
    assert renamed.name == "Study radio"
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/up0a1b2c3d/name") == "Study radio"

    await scenario.remove("up0a1b2c3d")
    await scenario.idle()

    assert scenario.visible_players() == []
    assert await scenario.player_configs() == []
    assert scenario.events(EventType.PLAYER_REMOVED)[-1] == Event(
        EventType.PLAYER_REMOVED, "up0a1b2c3d"
    )


async def test_players_are_restored_past_a_player_config_that_cannot_be_read(
    scenario: Scenario,
) -> None:
    """A stored player whose settings are no mapping is skipped at the start, the others come."""
    sonos = await scenario.add_provider("sonos")
    sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    scenario.expect_log("Could not restore player odd")

    await scenario.seed_pre_split_config(
        {
            "odd": {"player_id": "odd", "provider": None, "enabled": True, "values": [1, 2]},
            "RINCON_KITCHEN": {
                "player_id": "RINCON_KITCHEN",
                "provider": sonos.instance_id,
                "enabled": True,
                "values": {},
            },
        }
    )
    await scenario.idle()

    assert scenario.mass.players.get_player("odd") is None
    assert scenario.mass.players.get_player("RINCON_KITCHEN") is not None
    await scenario.register(scenario.protocol("RINCON_KITCHEN"))
    assert scenario.player_of(scenario.protocol("RINCON_KITCHEN")) == "RINCON_KITCHEN"
    # a new device is matched against the stored players, that one included
    den = scenario.provider(sonos.instance_id).protocol(
        "RINCON_DEN", "Den", identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B1"}
    )
    await scenario.register(den)
    assert scenario.player_of(den) not in (None, "odd", "RINCON_KITCHEN")


async def test_players_are_restored_without_the_user_database(scenario: Scenario) -> None:
    """The stored players come back at a start before the database of the users is opened."""
    restored: list[bool] = []
    post_setup = PlayerController.post_setup

    async def _post_setup_without_users(self: PlayerController) -> None:
        webserver = self.mass.webserver
        # any use of the webserver, which holds the users, fails from here on
        self.mass.webserver = None  # type: ignore[assignment]
        try:
            await post_setup(self)
        finally:
            self.mass.webserver = webserver
        restored.append(self.get_player("up0a1b2c3d") is not None)

    with patch.object(PlayerController, "post_setup", _post_setup_without_users):
        await scenario.seed_pre_split_config(
            {
                "up0a1b2c3d": KEPT_WRAPPER,
                "sendspin_web_1": {
                    "player_id": "sendspin_web_1",
                    "provider": "sendspin",
                    "player_type": "player",
                    "values": {},
                },
            }
        )

    assert restored == [True]
    assert [player.player_id for player in scenario.visible_players()] == ["up0a1b2c3d"]


async def test_session_only_player_keeps_what_is_stored_under_its_id(scenario: Scenario) -> None:
    """The split deletes the config of a web player, and nothing else that names its id."""
    sendspin = await scenario.add_provider("sendspin")
    sonos = await scenario.add_provider("sonos")
    browser = sendspin.protocol("sendspin_web_1", "Web Browser", session_only=True)
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    await scenario.register(browser)
    await scenario.play("sendspin_web_1")
    saved_queue = await scenario.saved_queue("sendspin_web_1")
    assert saved_queue
    auth = scenario.mass.webserver.auth
    user = await auth.create_user(
        username="guest",
        player_filter=["sendspin_web_1", scenario.single_player(speaker).player_id],
    )
    await scenario.unregister(browser)
    await scenario.idle()
    # the player that played was kept: an older version stored it without that mark
    await scenario.seed_config({CONF_PLAYERS: {"sendspin_web_1": None}})

    await scenario.seed_pre_split_config(
        {
            "sendspin_web_1": {
                "player_id": "sendspin_web_1",
                "provider": "sendspin",
                "player_type": "player",
                "values": {"sendspin_static_delay": 120},
            }
        }
    )
    await scenario.idle()

    assert await scenario.player_config("sendspin_web_1") is None
    stored_user = await scenario.mass.webserver.auth.get_user(user.user_id)
    assert stored_user is not None
    assert "sendspin_web_1" in stored_user.player_filter

    await scenario.register(scenario.protocol("sendspin_web_1"))
    await scenario.idle()

    assert scenario.player_of(scenario.protocol("sendspin_web_1")) == "sendspin_web_1"
    assert await scenario.saved_queue("sendspin_web_1") == saved_queue


async def test_rejected_first_save_of_a_session_only_player_stores_nothing(
    scenario: Scenario,
) -> None:
    """A web player whose first config change is refused stays without a stored config."""
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web_1", "Web Browser", session_only=True)
    await scenario.register(browser)

    with pytest.raises(InvalidDataError):
        await scenario.mass.config.save_player_config(
            "sendspin_web_1", {CONF_MIN_VOLUME: 80, CONF_MAX_VOLUME: 20}
        )
    await scenario.settle()

    assert await scenario.stored_config_mentioning("sendspin_web_1") == []

    await scenario.configure_player("sendspin_web_1", name="Study browser")

    assert scenario.mass.config.get(f"{CONF_PLAYERS}/sendspin_web_1/name") == "Study browser"
    assert _edge(scenario, "sendspin_web_1") == "sendspin_web_1"
