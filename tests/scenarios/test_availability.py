"""What a player is worth while its device is unreachable: availability, power on, commands."""

from __future__ import annotations

from typing import Any

import pytest
from music_assistant_models.enums import IdentifierType
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import PlayerMedia

from .harness import FakePowerControl, FakeProtocol, Scenario, VisiblePlayer

DEN = {IdentifierType.MAC_ADDRESS: "00:11:22:33:44:55", IdentifierType.IP_ADDRESS: "192.168.1.30"}
BATHROOM = {
    IdentifierType.MAC_ADDRESS: "B8:27:EB:AA:BB:CC",
    IdentifierType.IP_ADDRESS: "192.168.1.31",
}

MEDIA = PlayerMedia(uri="http://ma.local/stream/track.flac")


async def test_player_stays_available_on_its_audio_protocols(scenario: Scenario) -> None:
    """
    A player whose native protocol goes unavailable stays available on its audio protocols.

    It plays through one of them.
    """
    snapcast = await scenario.add_provider("snapcast")
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    snapclient = snapcast.protocol("ma_den", "Den", identifiers=DEN)
    den_airplay = airplay.protocol("airplay_den", "Den", role="audio", identifiers=DEN)
    den_cast = chromecast.protocol("cast_den", "Den", role="audio", identifiers=DEN)
    for protocol in (snapclient, den_airplay, den_cast):
        await scenario.register(protocol)
    player_id = scenario.single_player(snapclient, den_airplay, den_cast).player_id

    await scenario.report(snapclient, available=False)
    await scenario.idle()

    assert scenario.players_carrying(snapclient) == [
        VisiblePlayer(
            player_id=player_id,
            name="Den",
            available=True,
            powered=None,
            provider="snapcast",
            outputs=(("airplay", True), ("chromecast", True), ("snapcast", False)),
        )
    ]
    await scenario.mass.players.play_media(player_id, MEDIA)
    await scenario.idle()
    assert ("play_media", MEDIA) in den_airplay.commands + den_cast.commands
    assert snapclient.commands == []


@pytest.mark.parametrize(
    "unusable",
    [{"available": False}, {"needs_setup": True}],
    ids=["unreachable", "needs_setup"],
)
async def test_bridge_alone_does_not_make_its_player_available(
    scenario: Scenario, unusable: dict[str, Any]
) -> None:
    """
    A bridge counts only while the protocol it rides on can play.

    The player of an AirPlay receiver that is unreachable or not yet paired is unavailable,
    and its bridge is no output then, however available the bridge itself reports. Once the
    receiver can play, the player is available and plays through its bridge when preferred.
    """
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    receiver = airplay.protocol("ap_den", "Den", identifiers=DEN, **unusable)
    bridge = sendspin.protocol(
        "sendspin_bridge_den", "Den", role="audio", underlying_protocol_id="ap_den"
    )
    for protocol in (receiver, bridge):
        await scenario.register(protocol)
    await scenario.idle()
    player_id = scenario.single_player(receiver, bridge).player_id

    assert scenario.players_carrying(receiver)[0].available is False
    assert scenario.players_carrying(receiver)[0].outputs == (
        ("airplay", False),
        ("sendspin", False),
    )
    with pytest.raises(PlayerUnavailableError):
        await scenario.mass.players.play_media(player_id, MEDIA)

    await scenario.report(receiver, available=True, needs_setup=False)
    await scenario.idle()

    assert scenario.players_carrying(receiver)[0].available is True
    assert scenario.players_carrying(receiver)[0].outputs == (
        ("airplay", True),
        ("sendspin", True),
    )
    await scenario.configure_player(player_id, preferred_output_protocol="sendspin_bridge_den")
    await scenario.mass.players.play_media(player_id, MEDIA)
    await scenario.idle()
    assert ("play_media", MEDIA) in bridge.commands
    assert ("play_media", MEDIA) not in receiver.commands


async def _switched_off_player(scenario: Scenario) -> tuple[FakeProtocol, FakePowerControl, str]:
    """
    Return a native player's protocol, its power control and its id, with the device gone.

    The user switched the control off by hand, after which the device dropped off the
    network. The player is expected to show available and powered off.
    """
    squeezelite = await scenario.add_provider("squeezelite")
    pi = squeezelite.protocol("b8:27:eb:aa:bb:cc", "Bathroom", identifiers=BATHROOM)
    await scenario.register(pi)
    player_id = scenario.single_player(pi).player_id
    plug = await scenario.add_power_control("switch.bathroom_plug", "Bathroom plug", powered=True)
    await scenario.configure_player(player_id, power_control=plug.control_id)
    await scenario.switch(plug, False)
    await scenario.unregister(pi)
    await scenario.idle()
    return pi, plug, player_id


async def test_offline_device_is_powered_on_through_its_power_control(scenario: Scenario) -> None:
    """
    A player whose device is gone is powered on through its external power control.

    While the control is off the player shows available and powered off; power on switches
    the control, and the player plays once the device is back.
    """
    pi, plug, player_id = await _switched_off_player(scenario)

    assert [(p.player_id, p.available, p.powered) for p in scenario.visible_players()] == [
        (player_id, True, False)
    ]

    power_on = await scenario.command(scenario.mass.players.cmd_power(player_id, True))

    assert plug.commands == [True]
    assert not power_on.done()
    await scenario.register(pi)
    # the power on looks for the device every 0.5 s
    await scenario.advance(1)
    await power_on
    await scenario.mass.players.play_media(player_id, MEDIA)
    await scenario.settle()

    player = scenario.player(player_id)
    assert player is not None
    assert player.powered is True
    assert ("play_media", MEDIA) in pi.commands


async def test_play_on_a_switched_off_player_powers_it_on(scenario: Scenario) -> None:
    """
    Play on a player whose device is gone but has a power control is accepted: it powers on.

    The play command switches the control, waits for the device and plays, instead of being
    refused as unavailable.
    """
    pi, plug, player_id = await _switched_off_player(scenario)

    assert [(p.player_id, p.available, p.powered) for p in scenario.visible_players()] == [
        (player_id, True, False)
    ]

    play = await scenario.command(scenario.mass.players.play_media(player_id, MEDIA))

    assert plug.commands == [True]
    assert not play.done()
    await scenario.register(pi)
    # the power on looks for the device every 0.5 s
    await scenario.advance(1)
    await play

    assert ("play_media", MEDIA) in pi.commands


async def _detached_player(scenario: Scenario) -> str:
    """Return the id of a native player whose device has gone away, without a power control."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_DEN", "Den", identifiers=DEN)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    await scenario.unregister(speaker)
    await scenario.idle()
    return player_id


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("cmd_play", ()),
        ("cmd_pause", ()),
        ("cmd_stop", ()),
        ("cmd_volume_set", (40,)),
        ("cmd_volume_mute", (True,)),
        ("play_media", (MEDIA,)),
    ],
    ids=["play", "pause", "stop", "volume_set", "volume_mute", "play_media"],
)
async def test_command_to_an_unavailable_player_raises(
    scenario: Scenario, command: str, args: tuple[Any, ...]
) -> None:
    """A command to a listed player whose device is gone fails as unavailable, not silently."""
    player_id = await _detached_player(scenario)

    assert [(p.player_id, p.available) for p in scenario.visible_players()] == [(player_id, False)]
    with pytest.raises(PlayerUnavailableError):
        await getattr(scenario.mass.players, command)(player_id, *args)


async def test_power_on_of_an_unavailable_player_is_accepted(scenario: Scenario) -> None:
    """
    Power on is the one command an unavailable player takes.

    Acceptance (no error) is the rule: without a power control nothing else is observable,
    with one the control brings the device back.
    """
    player_id = await _detached_player(scenario)

    assert [(p.player_id, p.available) for p in scenario.visible_players()] == [(player_id, False)]
    power_on = await scenario.command(scenario.mass.players.cmd_power(player_id, True))
    assert power_on.done()
    assert power_on.exception() is None
