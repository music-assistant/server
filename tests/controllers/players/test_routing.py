"""Tests for where the commands to a player go, who owns its volume and what it publishes."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
)

from music_assistant.constants import CONF_OUTPUT_CHANNELS
from tests.scenarios.harness import FakeProtocol, Scenario

KITCHEN = {
    IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
    IdentifierType.IP_ADDRESS: "192.168.1.20",
}


def _names(protocol: FakeProtocol) -> list[str]:
    """Return the names of the commands a protocol received, in order."""
    return [command[0] for command in protocol.commands]


async def _native_and_airplay(
    scenario: Scenario, **native_state: Any
) -> tuple[str, FakeProtocol, FakeProtocol]:
    """Return a player of a device with a native protocol and an AirPlay protocol."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN, **native_state)
    audio = airplay.protocol(
        "ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN, volume_level=80
    )
    await scenario.attach(native)
    await scenario.attach(audio)
    await scenario.idle()
    return scenario.single_player(native, audio).player_id, native, audio


async def test_audio_protocol_that_takes_over_gets_the_level_of_the_player_first(
    scenario: Scenario,
) -> None:
    """The output that starts to render starts at the level of the player, before the audio."""
    player_id, native, audio = await _native_and_airplay(scenario, volume_level=30)
    await scenario.configure_player(player_id, preferred_output_protocol="ap_kitchen")
    published: list[Any] = []
    scenario.mass.players.subscribe_player_state_update(
        lambda player, changed: (
            published.append(changed["volume_level"][1])
            if player.player_id == player_id and "volume_level" in changed
            else None
        )
    )

    await scenario.play(player_id)
    await scenario.idle()

    assert "play_media" in _names(audio)
    assert "play_media" not in _names(native)
    assert _names(audio).index("volume_set") < _names(audio).index("play_media")
    assert ("volume_set", 30) in audio.commands
    # the level the device had before it took over is never shown for the player
    assert 80 not in published
    state = scenario.player(player_id)
    assert state is not None
    assert state.active_output_protocol == "ap_kitchen"
    assert state.volume_level == 30


async def test_volume_goes_to_the_output_that_renders_and_only_it_reports_back(
    scenario: Scenario,
) -> None:
    """While an audio protocol plays it owns the volume: commands go there, feedback from there."""
    player_id, native, audio = await _native_and_airplay(scenario, volume_level=30)
    await scenario.configure_player(player_id, preferred_output_protocol="ap_kitchen")
    await scenario.play(player_id)

    await scenario.mass.players.cmd_volume_set(player_id, 40)
    await scenario.idle()

    assert audio.commands[-1] == ("volume_set", 40)
    assert "volume_set" not in _names(native)

    # a level the idle native protocol reports does not move the volume of the player
    await scenario.report(native, volume_level=5)
    await scenario.idle()
    state = scenario.player(player_id)
    assert state is not None
    assert state.volume_level == 40

    await scenario.report(audio, volume_level=45)
    await scenario.idle()
    state = scenario.player(player_id)
    assert state is not None
    assert state.volume_level == 45


async def test_native_protocol_owns_the_volume_while_nothing_else_renders(
    scenario: Scenario,
) -> None:
    """Without an active audio protocol the volume of a player is that of its native protocol."""
    player_id, native, audio = await _native_and_airplay(scenario, volume_level=30)

    await scenario.mass.players.cmd_volume_set(player_id, 25)
    await scenario.idle()

    assert native.commands[-1] == ("volume_set", 25)
    assert "volume_set" not in _names(audio)


async def test_detach_of_the_idle_native_leaves_the_playing_output_alone(
    scenario: Scenario,
) -> None:
    """The queue keeps playing on an audio protocol when the native protocol goes away."""
    player_id, native, audio = await _native_and_airplay(scenario)
    await scenario.configure_player(player_id, preferred_output_protocol="ap_kitchen")
    await scenario.play(player_id)

    await scenario.detach(native)
    await scenario.idle()

    queue = scenario.queue(player_id)
    assert queue is not None
    assert queue.state == PlaybackState.PLAYING
    await scenario.mass.players.cmd_pause(player_id)
    await scenario.idle()
    assert "pause" in _names(audio)


async def test_commands_of_a_player_with_only_an_audio_protocol_go_to_it(
    scenario: Scenario,
) -> None:
    """A device that is only known through an audio protocol is played and controlled through it."""
    airplay = await scenario.add_provider("airplay")
    audio = airplay.protocol(
        "ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN, volume_level=20
    )
    await scenario.attach(audio)
    await scenario.idle()
    player_id = scenario.single_player(audio).player_id

    await scenario.play(player_id)
    await scenario.mass.players.cmd_volume_set(player_id, 55)
    await scenario.mass.players.cmd_pause(player_id)
    await scenario.idle()

    assert {"play_media", "pause"} <= set(_names(audio))
    assert ("volume_set", 55) in audio.commands

    # the playback path goes: the queue stops where it was
    await scenario.mass.players.cmd_play(player_id)
    await scenario.idle()
    await scenario.detach(audio)
    await scenario.idle()
    queue = scenario.queue(player_id)
    assert queue is not None
    assert queue.state != PlaybackState.PLAYING
    assert queue.current_index is not None


async def test_device_that_follows_a_native_leader_gives_up_its_own_output(
    scenario: Scenario,
) -> None:
    """A control protocol that reports a native group leader ends the output it played on."""
    wiim = await scenario.add_provider("wiim")
    dlna = await scenario.add_provider("dlna")
    control = wiim.protocol("wiim_den", "Den", identifiers=KITCHEN, powered=True)
    # it controls the device, the audio goes through DLNA
    control._set_reported(
        role=ProtocolRole.CONTROL,
        supported_features={PlayerFeature.VOLUME_SET, PlayerFeature.PAUSE},
    )
    audio = dlna.protocol("dlna_den", "Den", role="audio", identifiers=KITCHEN)
    leader = wiim.protocol(
        "wiim_hall",
        "Hall",
        identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B0"},
    )
    for protocol in (control, audio, leader):
        await scenario.attach(protocol)
    await scenario.idle()
    player_id = scenario.single_player(control, audio).player_id
    await scenario.play(player_id)
    state = scenario.player(player_id)
    assert state is not None
    assert state.active_output_protocol == "dlna_den"
    assert "play_media" in _names(audio)

    await scenario.report(leader, group_members=["wiim_hall", "wiim_den"])
    await scenario.idle()

    state = scenario.player(player_id)
    assert state is not None
    assert state.synced_to == scenario.player_of(leader)
    assert state.active_output_protocol is None
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    assert player.active_output_protocol is None


async def test_published_type_is_never_protocol(scenario: Scenario) -> None:
    """A protocol that still reports the protocol type gives its player the player type."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    speaker._set_reported(type=PlayerType.PROTOCOL)

    await scenario.attach(speaker)

    state = scenario.player(scenario.single_player(speaker).player_id)
    assert state is not None
    assert state.type == PlayerType.PLAYER


async def test_device_protocol_is_the_own_protocol_else_the_preferred_audio_protocol(
    scenario: Scenario,
) -> None:
    """What a player reports for its device comes from its own protocol, else its best output."""
    player_id, native, audio = await _native_and_airplay(scenario)
    chromecast = await scenario.add_provider("chromecast")
    cast = chromecast.protocol("cc_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.attach(cast)
    await scenario.idle()
    player = scenario.mass.players.get_player(player_id)
    assert player is not None

    assert player.device_protocol is scenario.mass.players.get_protocol("RINCON_KITCHEN")
    assert player.provider_id == native.provider.instance_id
    assert player.state.device_info.model == "Fake sonos"

    await scenario.detach(native)
    await scenario.idle()

    # airplay is preferred over cast
    assert player.device_protocol is scenario.mass.players.get_protocol("ap_kitchen")
    assert player.provider_id == audio.provider.instance_id
    assert player.state.device_info.model == "Fake airplay"


async def test_output_channels_of_the_player_apply_to_the_output_that_renders(
    scenario: Scenario,
) -> None:
    """The channel setting of a player reaches an audio protocol, which can set its own."""
    player_id, _, _ = await _native_and_airplay(scenario)
    streams = scenario.mass.streams.audio
    audio = scenario.mass.players.get_protocol("ap_kitchen")
    assert audio is not None
    assert not player_id.startswith(("RINCON", "ap_"))

    await scenario.configure_player(player_id, output_channels="left")

    assert scenario.mass.config.get_protocol_value("RINCON_KITCHEN", CONF_OUTPUT_CHANNELS) == "left"
    assert streams._get_output_channels(audio, player_id) == "left"

    scenario.mass.config.set_protocol_value("ap_kitchen", CONF_OUTPUT_CHANNELS, "right")

    assert streams._get_output_channels(audio, player_id) == "right"
