"""What a player is worth while its device is unreachable: availability, power on, commands."""

from __future__ import annotations

from typing import Any

import pytest
from music_assistant_models.enums import IdentifierType
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import PlayerMedia

from .harness import Scenario, VisiblePlayer

DEN = {IdentifierType.MAC_ADDRESS: "00:11:22:33:44:55", IdentifierType.IP_ADDRESS: "192.168.1.30"}
BATHROOM = {
    IdentifierType.MAC_ADDRESS: "B8:27:EB:AA:BB:CC",
    IdentifierType.IP_ADDRESS: "192.168.1.31",
}

MEDIA = PlayerMedia(uri="http://ma.local/stream/track.flac")


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 1 (#415): a player is available while any attached protocol is",
)
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
        await scenario.attach(protocol)
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
    await scenario.settle()
    assert ("play_media", MEDIA) in den_airplay.commands + den_cast.commands
    assert snapclient.commands == []


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 1 (#415): a player outlives its detached protocol",
)
async def test_offline_device_is_powered_on_through_its_power_control(scenario: Scenario) -> None:
    """
    A player whose device is gone is powered on through its external power control.

    While the control is off the player shows available and powered off; power on switches
    the control, and the player plays once the device is back.
    """
    squeezelite = await scenario.add_provider("squeezelite")
    pi = squeezelite.protocol("b8:27:eb:aa:bb:cc", "Bathroom", identifiers=BATHROOM)
    await scenario.attach(pi)
    player_id = scenario.single_player(pi).player_id
    plug = await scenario.add_power_control("switch.bathroom_plug", "Bathroom plug", powered=True)
    await scenario.configure_player(player_id, power_control=plug.control_id)
    await scenario.switch(plug, False)
    await scenario.detach(pi)
    await scenario.idle()

    assert [(p.player_id, p.available, p.powered) for p in scenario.visible_players()] == [
        (player_id, True, False)
    ]

    power_on = await scenario.command(scenario.mass.players.cmd_power(player_id, True))

    assert plug.commands == [True]
    assert not power_on.done()
    await scenario.attach(pi)
    # the power on looks for the device every 0.5 s
    await scenario.advance(1)
    await power_on
    await scenario.mass.players.play_media(player_id, MEDIA)
    await scenario.settle()

    player = scenario.player(player_id)
    assert player is not None
    assert player.powered is True
    assert ("play_media", MEDIA) in pi.commands


async def _detached_player(scenario: Scenario) -> str:
    """Return the id of a native player whose device has gone away."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_DEN", "Den", identifiers=DEN)
    await scenario.attach(speaker)
    player_id = scenario.single_player(speaker).player_id
    await scenario.detach(speaker)
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
    """A command to a player whose device is gone fails as unavailable instead of doing nothing."""
    player_id = await _detached_player(scenario)

    with pytest.raises(PlayerUnavailableError):
        await getattr(scenario.mass.players, command)(player_id, *args)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 1 (#415): a player outlives its detached protocol",
)
async def test_power_on_of_an_unavailable_player_is_accepted(scenario: Scenario) -> None:
    """Power on is the one command an unavailable player takes, so its power control can bring it back."""
    player_id = await _detached_player(scenario)

    assert [(p.player_id, p.available) for p in scenario.visible_players()] == [(player_id, False)]
    power_on = await scenario.command(scenario.mass.players.cmd_power(player_id, True))
    assert power_on.done()
    assert power_on.exception() is None
