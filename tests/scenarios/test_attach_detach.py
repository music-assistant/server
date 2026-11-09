"""Attach and detach of the protocols of one device."""

from __future__ import annotations

from unittest.mock import ANY

import pytest
from music_assistant_models.enums import EventType, IdentifierType

from .harness import Event, Scenario, VisiblePlayer

KITCHEN = {
    IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
    IdentifierType.IP_ADDRESS: "192.168.1.20",
}
DEN = {
    IdentifierType.MAC_ADDRESS: "00:11:22:33:44:55",
    IdentifierType.IP_ADDRESS: "192.168.1.30",
}


async def test_native_device_attaches_as_available_player(scenario: Scenario) -> None:
    """A native protocol that attaches is listed as an available player with a queue."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)

    await scenario.attach(speaker)

    player = scenario.assert_single_player(speaker)
    assert player == VisiblePlayer(
        player_id=ANY,
        name="Kitchen",
        available=True,
        powered=None,
        provider="sonos",
        outputs=(("sonos", True),),
    )
    assert scenario.queue(player.player_id) is not None
    assert scenario.events(EventType.PLAYER_ADDED, EventType.QUEUE_ADDED) == [
        Event(EventType.PLAYER_ADDED, player.player_id),
        Event(EventType.QUEUE_ADDED, player.player_id),
    ]


async def test_airplay_and_cast_of_one_device_become_one_player(scenario: Scenario) -> None:
    """
    The AirPlay and the Cast protocol of one device end up as one player.

    Today both stay hidden for the 15 s evaluation delay, after which a universal player
    stands for the device.
    """
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    den_airplay = airplay.protocol("airplay_den", "Den", role="audio", identifiers=DEN)
    den_cast = chromecast.protocol("cast_den", "Den", role="audio", identifiers=DEN)

    await scenario.attach(den_airplay)
    await scenario.attach(den_cast)
    await scenario.advance(14.9)

    assert scenario.visible_players() == []

    await scenario.advance(0.1)

    player = scenario.assert_single_player(den_airplay, den_cast)
    assert player == VisiblePlayer(
        player_id=ANY,
        name="Den",
        available=True,
        powered=None,
        provider="universal_player",
        outputs=(("airplay", True), ("chromecast", True)),
    )
    assert scenario.events(EventType.PLAYER_ADDED) == [
        Event(EventType.PLAYER_ADDED, player.player_id)
    ]


async def test_detached_native_player_vanishes_also_after_restart(scenario: Scenario) -> None:
    """
    A native player whose protocol detaches drops out of the player list, with its queue.

    Today's behaviour: a player only exists while its protocol is attached. Clients get an
    update but no removal, the config stays, and a restart does not bring the player back.
    """
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(speaker)
    player_id = scenario.assert_single_player(speaker).player_id
    scenario.clear_events()

    await scenario.detach(speaker)

    assert scenario.visible_players() == []
    assert scenario.queue(player_id) is None
    assert scenario.events(EventType.PLAYER_UPDATED, EventType.PLAYER_REMOVED) == [
        Event(EventType.PLAYER_UPDATED, player_id)
    ]
    assert await scenario.player_config(player_id) is not None

    await scenario.restart()

    assert scenario.provider(sonos.instance_id).available
    assert scenario.visible_players() == []


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 1 (#415): a player outlives its detached protocol",
)
async def test_detached_native_player_stays_listed_unavailable(scenario: Scenario) -> None:
    """A native player whose protocol detaches stays listed as unavailable and keeps its queue."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(speaker)
    player_id = scenario.assert_single_player(speaker).player_id
    scenario.clear_events()

    await scenario.detach(speaker)

    assert [(player.player_id, player.available) for player in scenario.visible_players()] == [
        (player_id, False)
    ]
    assert scenario.queue(player_id) is not None
    assert scenario.events(EventType.PLAYER_REMOVED) == []
