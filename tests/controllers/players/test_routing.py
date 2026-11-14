"""Tests for where the commands to a player go, who owns its volume and what it publishes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)

from music_assistant.constants import CONF_OUTPUT_CHANNELS, CONF_VOLUME_CONTROL
from tests.common import grouping_player_controller
from tests.scenarios.harness import DEFAULT_FEATURES, FakeProtocol, Scenario

if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from tests.common import MockProtocol

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
    await scenario.register(native)
    await scenario.register(audio)
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
    assert _names(audio).count("volume_set") == 1
    # the level the device had before it took over is never shown for the player
    assert 80 not in published
    state = scenario.player(player_id)
    assert state is not None
    assert state.active_output_protocol == "ap_kitchen"
    assert state.volume_level == 30


async def test_players_grouped_through_an_audio_protocol_hand_it_their_level_and_mute(
    scenario: Scenario,
) -> None:
    """The protocol that carries the audio of a group starts at the level and mute of each player."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    airplay_features = {*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS}
    hall = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B0"}
    protocols = [
        sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN, volume_level=30),
        airplay.protocol(
            "ap_kitchen",
            "Kitchen",
            role="audio",
            identifiers=KITCHEN,
            volume_level=80,
            supported_features=airplay_features,
            can_group_with={"ap_hall"},
        ),
        sonos.protocol("RINCON_HALL", "Hall", identifiers=hall, volume_level=20, volume_muted=True),
        airplay.protocol(
            "ap_hall",
            "Hall",
            role="audio",
            identifiers=hall,
            volume_level=90,
            supported_features=airplay_features,
            can_group_with={"ap_kitchen"},
        ),
    ]
    for protocol in protocols:
        await scenario.register(protocol)
    await scenario.idle()
    kitchen_native, kitchen_airplay, hall_native, hall_airplay = protocols
    kitchen_id = scenario.single_player(kitchen_native, kitchen_airplay).player_id
    hall_id = scenario.single_player(hall_native, hall_airplay).player_id

    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[hall_id])
    await scenario.idle()

    kitchen_state, hall_state = scenario.player(kitchen_id), scenario.player(hall_id)
    assert kitchen_state is not None
    assert hall_state is not None
    assert (kitchen_state.active_output_protocol, kitchen_state.volume_level) == ("ap_kitchen", 30)
    assert ("volume_set", 30) in kitchen_airplay.commands
    assert hall_state.active_output_protocol == "ap_hall"
    assert (hall_state.volume_level, hall_state.volume_muted) == (20, True)
    assert {("volume_set", 20), ("volume_mute", True)} <= set(hall_airplay.commands)


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


async def test_native_protocol_that_can_not_be_reached_leaves_the_controls_to_the_others(
    scenario: Scenario,
) -> None:
    """While its native protocol is attached but unreachable, the player is controlled through AirPlay."""
    player_id, native, audio = await _native_and_airplay(
        scenario,
        volume_level=30,
        powered=True,
        supported_features={*DEFAULT_FEATURES, PlayerFeature.POWER},
    )

    await scenario.report(native, available=False)

    state = scenario.player(player_id)
    assert state is not None
    assert state.available
    assert (state.volume_control, state.mute_control) == ("ap_kitchen", "ap_kitchen")
    assert state.volume_level == 30
    # powering on a device that can not be reached is how its own protocol wakes it
    assert state.power_control == "native"
    native.commands.clear()

    await scenario.mass.players.cmd_volume_set(player_id, 44)
    await scenario.mass.players.cmd_volume_mute(player_id, True)
    await scenario.idle()

    assert native.commands == []
    assert {("volume_set", 44), ("volume_mute", True)} <= set(audio.commands)

    # a control that is stored as the choice of the user stays where it is
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    scenario.mass.config.set_player_value(player_id, CONF_VOLUME_CONTROL, "native")
    player.update_state(force_update=True)

    state = scenario.player(player_id)
    assert state is not None
    assert state.volume_control == "native"

    # back in reach, the native protocol owns the controls again
    scenario.mass.config.set_player_value(player_id, CONF_VOLUME_CONTROL, None)
    await scenario.report(native, available=True)

    state = scenario.player(player_id)
    assert state is not None
    assert (state.volume_control, state.mute_control, state.power_control) == (
        "native",
        "native",
        "native",
    )


def _published_levels(scenario: Scenario, player_id: str) -> list[int | None]:
    """Collect every volume level the player publishes from now on."""
    published: list[int | None] = []
    scenario.mass.players.subscribe_player_state_update(
        lambda player, changed: (
            published.append(changed["volume_level"][1])
            if player.player_id == player_id and "volume_level" in changed
            else None
        )
    )
    return published


@pytest.mark.parametrize("playing", [False, True])
async def test_audio_protocol_that_takes_over_from_an_unreachable_native_gets_the_level_to_play(
    scenario: Scenario, playing: bool
) -> None:
    """
    The level and mute of the player stay when its native drops; AirPlay gets them when it plays.

    Nothing is sent to it before that: the level it has may be newer than what the native knew.
    """
    player_id, native, audio = await _native_and_airplay(
        scenario, volume_level=30, volume_muted=True
    )
    if playing:
        await scenario.play(player_id)
    audio.commands.clear()
    published = _published_levels(scenario, player_id)

    await scenario.report(native, available=False)
    await scenario.idle()
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    # an update that changes nothing else keeps them too
    player.update_state(force_update=True)

    assert audio.commands == []
    state = scenario.player(player_id)
    assert state is not None
    assert (state.volume_control, state.mute_control) == ("ap_kitchen", "ap_kitchen")
    assert (state.volume_level, state.volume_muted) == (30, True)
    # the level the output had before it took over is never shown for the player
    assert 80 not in published

    await scenario.play(player_id)

    assert audio.commands[:2] == [("volume_set", 30), ("volume_mute", True)]
    assert _names(audio)[2] == "play_media"
    state = scenario.player(player_id)
    assert state is not None
    assert (state.volume_level, state.volume_muted) == (30, True)

    # given the level, what the output reports counts again
    await scenario.report(audio, volume_level=50)
    state = scenario.player(player_id)
    assert state is not None
    assert state.volume_level == 50


async def test_output_that_takes_over_is_never_made_louder_than_the_device_was_left(
    scenario: Scenario,
) -> None:
    """
    The native connection died quietly while the device was turned down at its buttons.

    Only the DLNA side of the device heard of it: its lower level is the one that counts, for
    what the player shows and for what it plays at.
    """
    sonos = await scenario.add_provider("sonos")
    dlna = await scenario.add_provider("dlna")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN, volume_level=60)
    audio = dlna.protocol(
        "uuid:kitchen", "Kitchen", role="audio", identifiers=KITCHEN, volume_level=60
    )
    await scenario.register(native)
    await scenario.register(audio)
    await scenario.idle()
    player_id = scenario.single_player(native, audio).player_id
    await scenario.report(audio, volume_level=8)
    audio.commands.clear()
    published = _published_levels(scenario, player_id)

    await scenario.report(native, available=False)
    await scenario.idle()

    assert audio.commands == []
    state = scenario.player(player_id)
    assert state is not None
    assert (state.volume_control, state.volume_level) == ("uuid:kitchen", 8)
    assert 60 not in published

    # turned down further at the device
    await scenario.report(audio, volume_level=5)
    state = scenario.player(player_id)
    assert state is not None
    assert state.volume_level == 5

    await scenario.configure_player(player_id, preferred_output_protocol="uuid:kitchen")
    await scenario.play(player_id)

    assert "play_media" in _names(audio)
    assert all(command[1] <= 5 for command in audio.commands if command[0] == "volume_set")
    state = scenario.player(player_id)
    assert state is not None
    assert state.volume_level == 5


async def test_volume_commands_after_the_native_dropped_start_from_the_level_of_the_player(
    scenario: Scenario,
) -> None:
    """A step up and an unmute while the native can not be reached act on what the player had."""
    player_id, native, audio = await _native_and_airplay(
        scenario, volume_level=30, volume_muted=True
    )
    await scenario.report(native, available=False)
    audio.commands.clear()

    await scenario.mass.players.cmd_volume_up(player_id)
    await scenario.mass.players.cmd_volume_mute(player_id, False)
    await scenario.idle()

    state = scenario.player(player_id)
    assert state is not None
    assert state.volume_level is not None
    assert 30 < state.volume_level < 80
    assert state.volume_muted is False
    assert audio.commands == [("volume_set", state.volume_level), ("volume_mute", False)]


@pytest.mark.parametrize(
    "change",
    [
        "second_output_drops",
        "output_flaps",
        "output_blinks",
        "native_detaches",
        "pinned_and_back",
        "pinned_output",
    ],
)
async def test_level_the_player_had_is_kept_while_its_native_can_not_be_reached(
    scenario: Scenario, change: str
) -> None:
    """
    Whatever takes or keeps the volume control meanwhile, the player stays at its level.

    The native is at 30 when it drops. The next play starts at that level, also through an
    output whose own level is higher.
    """
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN, volume_level=30)
    audio = airplay.protocol(
        "ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN, volume_level=80
    )
    protocols = [native, audio]
    if change == "second_output_drops":
        chromecast = await scenario.add_provider("chromecast")
        protocols.append(
            chromecast.protocol(
                "cast_kitchen", "Kitchen", role="audio", identifiers=KITCHEN, volume_level=40
            )
        )
    for protocol in protocols:
        await scenario.register(protocol)
    await scenario.idle()
    player_id = scenario.single_player(*protocols).player_id
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    published = _published_levels(scenario, player_id)

    await scenario.report(native, available=False)
    if change == "second_output_drops":
        # the output that took the control drops as well, the other one takes it
        state = scenario.player(player_id)
        assert state is not None
        assert state.volume_control == "cast_kitchen"
        await scenario.report(protocols[2], available=False)
    elif change == "output_flaps":
        await scenario.unregister(audio)
        await scenario.register(audio)
    elif change == "output_blinks":
        await scenario.report(audio, available=False)
        await scenario.report(audio, available=True)
    elif change == "native_detaches":
        # a reload of its provider
        await scenario.unregister(native)
    elif change == "pinned_and_back":
        # the user picks the fake control and goes back to the automatic one
        scenario.mass.config.set_player_value(player_id, CONF_VOLUME_CONTROL, "fake")
        player.update_state(force_update=True)
        scenario.mass.config.set_player_value(player_id, CONF_VOLUME_CONTROL, None)
        player.update_state(force_update=True)
    else:
        # the user picks the output as the volume control
        scenario.mass.config.set_player_value(player_id, CONF_VOLUME_CONTROL, "ap_kitchen")
        player.update_state(force_update=True)
    await scenario.idle()

    state = scenario.player(player_id)
    assert state is not None
    assert (state.volume_control, state.volume_level) == ("ap_kitchen", 30)
    assert 80 not in published

    audio.commands.clear()
    await scenario.play(player_id)

    assert audio.commands[0] == ("volume_set", 30)
    assert "play_media" in _names(audio)
    state = scenario.player(player_id)
    assert state is not None
    assert state.volume_level == 30


async def test_player_shows_muted_when_the_output_in_control_reports_it_while_held(
    scenario: Scenario,
) -> None:
    """An output that reports muted while the native is away shows the player muted."""
    player_id, native, audio = await _native_and_airplay(
        scenario, volume_level=30, volume_muted=False
    )
    await scenario.report(native, available=False)

    await scenario.report(audio, volume_muted=True)

    state = scenario.player(player_id)
    assert state is not None
    assert (state.mute_control, state.volume_muted) == ("ap_kitchen", True)


async def test_native_that_drops_again_leaves_the_level_it_had_the_second_time(
    scenario: Scenario,
) -> None:
    """What the player keeps when its native drops is what the native had last, every time."""
    player_id, native, _ = await _native_and_airplay(scenario, volume_level=30)
    await scenario.report(native, available=False)
    await scenario.report(native, available=True)
    await scenario.report(native, volume_level=60)

    await scenario.report(native, available=False)

    state = scenario.player(player_id)
    assert state is not None
    assert (state.volume_control, state.volume_level) == ("ap_kitchen", 60)


async def test_native_that_can_be_reached_again_takes_the_controls_back_at_its_own_level(
    scenario: Scenario,
) -> None:
    """A native back in reach reports the level of the device again, and gets no level itself."""
    player_id, native, audio = await _native_and_airplay(scenario, volume_level=30)
    await scenario.report(native, available=False)
    await scenario.mass.players.cmd_volume_set(player_id, 44)
    await scenario.idle()
    state = scenario.player(player_id)
    assert state is not None
    assert state.volume_level == 44
    native.commands.clear()
    audio.commands.clear()

    await scenario.report(native, available=True)
    await scenario.idle()

    state = scenario.player(player_id)
    assert state is not None
    assert (state.volume_control, state.volume_level) == ("native", 30)
    assert native.commands == []
    assert audio.commands == []


async def test_member_that_joins_through_airplay_while_its_native_is_unreachable_keeps_its_level(
    scenario: Scenario,
) -> None:
    """A member whose native can not be reached joins a group through AirPlay at its own level."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    airplay_features = {*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS}
    hall = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B0"}
    protocols = [
        sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN, volume_level=30),
        airplay.protocol(
            "ap_kitchen",
            "Kitchen",
            role="audio",
            identifiers=KITCHEN,
            volume_level=80,
            supported_features=airplay_features,
            can_group_with={"ap_hall"},
        ),
        sonos.protocol("RINCON_HALL", "Hall", identifiers=hall, volume_level=20),
        airplay.protocol(
            "ap_hall",
            "Hall",
            role="audio",
            identifiers=hall,
            volume_level=90,
            supported_features=airplay_features,
            can_group_with={"ap_kitchen"},
        ),
    ]
    for protocol in protocols:
        await scenario.register(protocol)
    await scenario.idle()
    kitchen_native, kitchen_airplay, hall_native, hall_airplay = protocols
    kitchen_id = scenario.single_player(kitchen_native, kitchen_airplay).player_id
    hall_id = scenario.single_player(hall_native, hall_airplay).player_id
    published = _published_levels(scenario, hall_id)

    await scenario.report(hall_native, available=False)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[hall_id])
    await scenario.idle()

    hall_state = scenario.player(hall_id)
    assert hall_state is not None
    assert (hall_state.active_output_protocol, hall_state.volume_level) == ("ap_hall", 20)
    assert ("volume_set", 20) in hall_airplay.commands
    assert 90 not in published
    kitchen_state = scenario.player(kitchen_id)
    assert kitchen_state is not None
    assert hall_id in kitchen_state.group_members


async def test_volume_control_the_user_chose_stays_when_the_native_can_not_be_reached(
    scenario: Scenario,
) -> None:
    """A volume control the user stored keeps the volume: nothing is handed to another protocol."""
    player_id, native, audio = await _native_and_airplay(scenario, volume_level=30)
    # stored as such: the config save leaves out a value that equals the default
    scenario.mass.config.set_player_value(player_id, CONF_VOLUME_CONTROL, "native")
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    player.update_state(force_update=True)
    audio.commands.clear()

    await scenario.report(native, available=False)
    await scenario.idle()

    state = scenario.player(player_id)
    assert state is not None
    assert (state.volume_control, state.volume_level) == ("native", 30)
    assert "volume_set" not in _names(audio)


@pytest.mark.parametrize("refused", ["volume_set", "volume_mute"])
@pytest.mark.parametrize("action", ["play", "join"])
async def test_output_that_refuses_the_level_of_the_player_is_activated_all_the_same(
    scenario: Scenario, action: str, refused: str
) -> None:
    """A protocol that fails to take the level or mute over still takes the audio of the player."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    airplay_features = {*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS}
    hall = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B0"}
    protocols = [
        sonos.protocol(
            "RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN, volume_level=30, volume_muted=False
        ),
        airplay.protocol(
            "ap_kitchen",
            "Kitchen",
            role="audio",
            identifiers=KITCHEN,
            volume_level=80,
            supported_features=airplay_features,
            can_group_with={"ap_hall"},
        ),
        sonos.protocol(
            "RINCON_HALL", "Hall", identifiers=hall, volume_level=20, volume_muted=False
        ),
        airplay.protocol(
            "ap_hall",
            "Hall",
            role="audio",
            identifiers=hall,
            volume_level=90,
            supported_features=airplay_features,
            can_group_with={"ap_kitchen"},
        ),
    ]
    for protocol in protocols:
        await scenario.register(protocol)
    await scenario.idle()
    kitchen_native, kitchen_airplay, hall_native, hall_airplay = protocols
    kitchen_id = scenario.single_player(kitchen_native, kitchen_airplay).player_id
    hall_id = scenario.single_player(hall_native, hall_airplay).player_id

    async def _refuse(*_args: Any) -> None:
        raise ConnectionError("no answer")

    refusing = kitchen_airplay if action == "play" else hall_airplay
    setattr(refusing, refused, _refuse)
    scenario.expect_log("Could not give ap_")

    if action == "play":
        await scenario.configure_player(kitchen_id, preferred_output_protocol="ap_kitchen")
        await scenario.play(kitchen_id)
        assert "play_media" in _names(kitchen_airplay)
    else:
        await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[hall_id])
        await scenario.idle()
        kitchen_state = scenario.player(kitchen_id)
        assert kitchen_state is not None
        assert hall_id in kitchen_state.group_members
        assert ("set_members", ["ap_hall"], None) in kitchen_airplay.commands
    refusing_state = scenario.player(kitchen_id if action == "play" else hall_id)
    assert refusing_state is not None
    assert refusing_state.active_output_protocol == refusing.protocol_id


async def test_output_without_a_volume_of_its_own_gets_none_while_the_native_is_unreachable(
    scenario: Scenario,
) -> None:
    """The level of an output that has no volume goes nowhere while the native can not be reached."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN, volume_level=30)
    audio = airplay.protocol(
        "ap_kitchen",
        "Kitchen",
        role="audio",
        identifiers=KITCHEN,
        supported_features={PlayerFeature.PLAY_MEDIA, PlayerFeature.PAUSE},
    )
    await scenario.register(native)
    await scenario.register(audio)
    await scenario.idle()
    player = scenario.mass.players.get_player(scenario.single_player(native, audio).player_id)
    assert player is not None
    assert player.volume_control_for_output("ap_kitchen") == "native"

    await scenario.report(native, available=False)

    assert player.volume_control_for_output("ap_kitchen") == "none"
    assert player.mute_control_for_output("ap_kitchen") == "none"
    # without a volume control the player shows no level, also not the one it keeps
    state = scenario.player(player.player_id)
    assert state is not None
    assert (state.volume_control, state.volume_level) == ("none", None)


async def test_detach_of_the_idle_native_leaves_the_playing_output_alone(
    scenario: Scenario,
) -> None:
    """The queue keeps playing on an audio protocol when the native protocol goes away."""
    player_id, native, audio = await _native_and_airplay(scenario)
    await scenario.configure_player(player_id, preferred_output_protocol="ap_kitchen")
    await scenario.play(player_id)

    await scenario.unregister(native)
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
    await scenario.register(audio)
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
    await scenario.unregister(audio)
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
    # it controls the device, the audio goes through DLNA
    control = wiim.protocol(
        "wiim_den",
        "Den",
        role="control",
        identifiers=KITCHEN,
        powered=True,
        supported_features={PlayerFeature.VOLUME_SET, PlayerFeature.PAUSE},
    )
    audio = dlna.protocol("dlna_den", "Den", role="audio", identifiers=KITCHEN)
    leader = wiim.protocol(
        "wiim_hall",
        "Hall",
        identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B0"},
    )
    for protocol in (control, audio, leader):
        await scenario.register(protocol)
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


async def test_control_protocol_alone_makes_a_player_neither_available_nor_playable(
    scenario: Scenario,
) -> None:
    """A device whose audio protocols are away has nothing to play on, whatever controls it."""
    wiim = await scenario.add_provider("wiim")
    dlna = await scenario.add_provider("dlna")
    control = wiim.protocol(
        "wiim_den",
        "Den",
        role="control",
        identifiers=KITCHEN,
        supported_features={PlayerFeature.VOLUME_SET, PlayerFeature.SET_MEMBERS},
    )
    audio = dlna.protocol("dlna_den", "Den", role="audio", identifiers=KITCHEN)
    await scenario.register(control)
    await scenario.register(audio)
    await scenario.idle()
    player_id = scenario.single_player(control, audio).player_id
    player = scenario.mass.players.get_player(player_id)
    assert player is not None

    def _availability() -> tuple[bool, bool, bool]:
        state = scenario.player(player_id)
        assert state is not None
        return state.available, player.available, player.available_for_playback

    assert _availability() == (True, True, True)

    await scenario.unregister(audio)

    # the control protocol still answers, which is no output for a sync group to lead on
    assert control.available
    assert _availability() == (False, False, False)

    await scenario.register(audio)

    assert _availability() == (True, True, True)

    # nor does it make the player usable while its output still has to be set up
    await scenario.report(audio, needs_setup=True)

    state = scenario.player(player_id)
    assert state is not None
    assert (state.available, state.needs_setup) == (False, True)


# the speakers of a Cast group, by their Cast id: name and MAC address
CAST_MEMBERS = {
    "uuid-a": ("Player A", "54:78:C9:E6:0E:A1"),
    "uuid-b": ("Player B", "54:78:C9:E6:0E:B1"),
}


@pytest.mark.parametrize("pre_split", [False, True], ids=["minted", "pre_split"])
async def test_ungrouping_a_member_of_a_cast_group_releases_the_group(
    scenario: Scenario, pre_split: bool
) -> None:
    """A group that reports its fixed members by their protocol ids has them as static members."""
    chromecast = await scenario.add_provider("chromecast")
    for protocol_id, (name, mac) in CAST_MEMBERS.items():
        chromecast.protocol(protocol_id, name, identifiers={IdentifierType.MAC_ADDRESS: mac})
    chromecast.protocol(
        "uuid-group",
        "Group Z",
        type=PlayerType.GROUP,
        supported_features={*DEFAULT_FEATURES, PlayerFeature.POWER},
        powered=True,
        group_members=list(CAST_MEMBERS),
        static_group_members=list(CAST_MEMBERS),
    )
    protocol_ids = [*CAST_MEMBERS, "uuid-group"]
    if pre_split:
        for protocol_id in protocol_ids:
            scenario.set_present(protocol_id, True)
        await scenario.seed_pre_split_config(
            {
                protocol_id: {
                    "player_id": protocol_id,
                    "provider": chromecast.instance_id,
                    "player_type": "group" if protocol_id == "uuid-group" else "player",
                    "enabled": True,
                    "name": None,
                    "default_name": None,
                    "values": {},
                }
                for protocol_id in protocol_ids
            }
        )
    else:
        for protocol_id in protocol_ids:
            await scenario.register(scenario.protocol(protocol_id))
    await scenario.idle()
    member_ids = [
        scenario.player_of(scenario.protocol(protocol_id)) for protocol_id in CAST_MEMBERS
    ]
    group_id = scenario.player_of(scenario.protocol("uuid-group"))
    assert (member_ids == list(CAST_MEMBERS)) is pre_split
    group_state, member_state = scenario.player(str(group_id)), scenario.player(str(member_ids[0]))
    assert group_state is not None
    assert member_state is not None
    assert group_state.static_group_members == member_ids
    assert member_state.active_group == group_id

    await scenario.mass.players.cmd_ungroup(str(member_ids[0]))
    await scenario.settle()

    assert ("power", False) in scenario.protocol("uuid-group").commands

    # they stay its static members while it holds none of them
    await scenario.report(scenario.protocol("uuid-group"), group_members=[])

    group_state = scenario.player(str(group_id))
    assert group_state is not None
    assert group_state.static_group_members == member_ids


async def test_static_member_of_a_group_of_players_that_is_not_there_stays_listed(
    scenario: Scenario,
) -> None:
    """A group of players names its fixed members by their player ids, also one that is away."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    speaker_id = scenario.single_player(speaker).player_id
    snapcast = await scenario.add_provider("snapcast")
    group = snapcast.protocol(
        "group_all",
        "All rooms",
        role="virtual",
        type=PlayerType.GROUP,
        static_group_members=[speaker_id, "pl0123456789ab"],
    )

    await scenario.register(group)

    group_state = scenario.player(scenario.single_player(group).player_id)
    assert group_state is not None
    assert group_state.static_group_members == [speaker_id, "pl0123456789ab"]


async def test_device_that_plays_the_queue_of_its_player_has_the_player_as_source(
    scenario: Scenario,
) -> None:
    """A protocol that reports its own id as active source plays the queue of its player."""
    sonos = await scenario.add_provider("sonos")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(native)
    player_id = scenario.single_player(native).player_id
    assert player_id != "RINCON_KITCHEN"

    await scenario.report(
        native, active_source="RINCON_KITCHEN", playback_state=PlaybackState.PLAYING
    )

    state = scenario.player(player_id)
    assert state is not None
    assert state.active_source == player_id


async def test_published_type_is_never_protocol(scenario: Scenario) -> None:
    """A protocol that still reports the protocol type gives its player the player type."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    speaker._set_reported(type=PlayerType.PROTOCOL)

    await scenario.register(speaker)

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
    await scenario.register(cast)
    await scenario.idle()
    player = scenario.mass.players.get_player(player_id)
    assert player is not None

    assert player.device_protocol is scenario.mass.players.get_protocol("RINCON_KITCHEN")
    assert player.provider_id == native.provider.instance_id
    assert player.state.device_info.model == "Fake sonos"

    await scenario.unregister(native)
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


async def test_sync_the_protocols_report_in_their_own_ids_is_one_of_players() -> None:
    """A sync is published and changed in player ids, while the protocols speak their own."""
    controller = grouping_player_controller("a", "b", "c")
    leader = controller.get_player("a")
    member = controller.get_player("b")
    assert leader is not None
    assert member is not None
    assert leader.state.can_group_with == {"b", "c"}

    cast("MockProtocol", leader.own_protocol)._attr_group_members = ["proto_a", "proto_b"]
    for player in controller:
        player.refresh_state(signal_event=False)

    assert leader.state.group_members == ["a", "b"]
    assert member.state.synced_to == "a"

    await controller.cmd_set_members("a", player_ids_to_remove=["b"])

    cast("AsyncMock", controller._handle_set_members).assert_awaited_once_with(leader, None, ["b"])
