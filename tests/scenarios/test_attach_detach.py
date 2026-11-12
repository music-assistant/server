"""Attach and detach of the protocols of one device."""

from __future__ import annotations

from unittest.mock import ANY

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
    await scenario.idle()

    assert scenario.players_carrying(speaker) == [
        VisiblePlayer(
            player_id=ANY,
            name="Kitchen",
            available=True,
            powered=None,
            provider="sonos",
            outputs=(("sonos", True),),
        )
    ]
    player_id = scenario.single_player(speaker).player_id
    assert scenario.queue(player_id) is not None
    assert scenario.events(EventType.PLAYER_ADDED, EventType.QUEUE_ADDED) == [
        Event(EventType.PLAYER_ADDED, player_id),
        Event(EventType.QUEUE_ADDED, player_id),
    ]


async def test_airplay_and_cast_of_one_device_become_one_player(scenario: Scenario) -> None:
    """
    The AirPlay and the Cast protocol of one device end up as one player.

    Both report the hardware address of the device, so the player is there with the first
    protocol and the second one joins it.
    """
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    den_airplay = airplay.protocol("airplay_den", "Den", role="audio", identifiers=DEN)
    den_cast = chromecast.protocol("cast_den", "Den", role="audio", identifiers=DEN)

    await scenario.attach(den_airplay)
    await scenario.idle()

    player_id = scenario.single_player(den_airplay).player_id

    await scenario.attach(den_cast)
    await scenario.idle()

    assert scenario.players_carrying(den_airplay, den_cast) == [
        VisiblePlayer(
            player_id=player_id,
            name="Den",
            available=True,
            powered=None,
            provider="airplay",
            outputs=(("airplay", True), ("chromecast", True)),
        )
    ]
    assert scenario.events(EventType.PLAYER_ADDED) == [Event(EventType.PLAYER_ADDED, player_id)]


async def test_detached_native_player_is_listed_also_after_restart(scenario: Scenario) -> None:
    """
    A native player whose protocol detaches stays listed, also after a restart.

    Clients get an update but no removal, and the player is unavailable until its provider
    attaches the protocol again. After a restart clients are told about the player before
    any provider has loaded.
    """
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(speaker)
    player_id = scenario.single_player(speaker).player_id
    scenario.clear_events()

    await scenario.detach(speaker)
    await scenario.idle()

    assert [(p.player_id, p.available) for p in scenario.visible_players()] == [(player_id, False)]
    assert scenario.events(EventType.PLAYER_UPDATED, EventType.PLAYER_REMOVED) == [
        Event(EventType.PLAYER_UPDATED, player_id)
    ]

    scenario.clear_events()
    await scenario.restart()
    await scenario.idle()

    assert scenario.events(EventType.PLAYER_ADDED, EventType.PROVIDERS_UPDATED)[0] == Event(
        EventType.PLAYER_ADDED, player_id
    )
    assert scenario.provider(sonos.instance_id).available
    assert [(p.player_id, p.available) for p in scenario.visible_players()] == [(player_id, False)]
    assert scenario.queue(player_id) is not None

    await scenario.attach(scenario.protocol("RINCON_KITCHEN"))
    await scenario.idle()

    assert [(p.player_id, p.available) for p in scenario.visible_players()] == [(player_id, True)]


async def test_detached_native_player_stays_listed_unavailable(scenario: Scenario) -> None:
    """A native player whose protocol detaches stays listed as unavailable and keeps its queue."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(speaker)
    player_id = scenario.single_player(speaker).player_id
    scenario.clear_events()

    await scenario.detach(speaker)
    await scenario.idle()

    assert [(player.player_id, player.available) for player in scenario.visible_players()] == [
        (player_id, False)
    ]
    assert scenario.queue(player_id) is not None
    assert scenario.events(EventType.PLAYER_REMOVED) == []
