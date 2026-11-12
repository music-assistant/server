"""Attach and detach of the protocols of one device."""

from __future__ import annotations

import asyncio
from unittest.mock import ANY

import pytest
from music_assistant_models.enums import EventType, IdentifierType, PlaybackState

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


async def test_a_returning_device_resumes_where_its_queue_was(scenario: Scenario) -> None:
    """
    A player whose device drops off while it plays keeps the item and position of its queue.

    The queue stops when the device goes. The device comes back idle, and nothing plays by
    itself; a resume plays the item from where it was.
    """
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(speaker)
    player_id = scenario.single_player(speaker).player_id
    await scenario.play(player_id)
    await scenario.report(speaker, elapsed_time=25, elapsed_time_last_updated=scenario.wall_time())
    await scenario.advance(5)

    await scenario.detach(speaker)
    await scenario.idle()

    queue = scenario.queue(player_id)
    assert queue is not None
    assert queue.state == PlaybackState.IDLE
    assert queue.current_item is not None
    item_id = queue.current_item.queue_item_id
    position = queue.elapsed_time
    # what it played in the 5 s since the device reported 25 s, as the queue last saw it
    assert 25 < position <= 30

    # the provider finds the device again, as it reports itself after a reboot
    returned = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(returned)
    await scenario.idle()

    assert returned.commands == []
    assert (queue.state, queue.elapsed_time) == (PlaybackState.IDLE, position)
    assert queue.current_item is not None
    assert queue.current_item.queue_item_id == item_id

    resume = await scenario.command(scenario.mass.player_queues.resume(player_id))
    await scenario.idle()
    await resume

    assert [command[0] for command in returned.commands] == ["play_media"]
    assert returned.commands[0][1].queue_item_id == item_id
    resumed = scenario.queue(player_id)
    assert resumed is not None
    assert (resumed.state, resumed.elapsed_time) == (PlaybackState.PLAYING, int(position))


async def test_a_new_device_whose_setup_is_called_off_leaves_nothing_behind(
    scenario: Scenario,
) -> None:
    """
    A device whose first setup is called off halfway is no player and leaves nothing stored.

    Clients are told about no player, and the device is a player once it is found again.
    """
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    never = asyncio.Event()

    async def _never_returns() -> None:
        await never.wait()

    speaker.on_config_updated = _never_returns  # type: ignore[method-assign]
    scenario.clear_events()
    attach = await scenario.command(scenario.mass.players.attach(speaker))
    attach.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attach
    await scenario.idle()

    assert scenario.visible_players() == []
    assert await scenario.player_configs() == []
    assert await scenario.stored_config_mentioning("RINCON_KITCHEN") == []
    assert scenario.events(EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED) == []

    found_again = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(found_again)
    await scenario.idle()

    assert [player.name for player in scenario.players_carrying(found_again)] == ["Kitchen"]
