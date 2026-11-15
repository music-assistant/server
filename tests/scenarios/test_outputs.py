"""Which output of a player plays: its own protocol, or the one the user prefers."""

from __future__ import annotations

from music_assistant_models.enums import IdentifierType

from .harness import FakeProtocol, Scenario

KITCHEN = {
    IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
    IdentifierType.IP_ADDRESS: "192.168.1.20",
}


def _played(protocol: FakeProtocol) -> bool:
    """Return whether a protocol got media to play."""
    return any(command[0] == "play_media" for command in protocol.commands)


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
