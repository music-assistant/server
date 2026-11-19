"""A speaker on a smart plug: off and starting up when the plug cuts its power, else a plain switch."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError

from music_assistant.constants import (
    ATTR_PLAY_ACTION_IN_PROGRESS,
    CONF_POWER_CONTROL_CUTS_POWER,
    CONF_POWER_ON_STARTUP_TIME,
    DEFAULT_POWER_ON_STARTUP_TIME,
)
from music_assistant.controllers.players.power import EXTERNAL_POWER_OFF_STOP_DELAY

from .test_group_power_on import (
    GROUPS,
    _downstairs,
    _play,
    _plays_along,
    _refuse_to_switch_on,
    _starts,
)
from .test_grouping import _state, _stop

if TYPE_CHECKING:
    from .harness import FakePowerControl, FakeProtocol, Role, Scenario

DEN = {IdentifierType.MAC_ADDRESS: "00:11:22:33:44:55", IdentifierType.IP_ADDRESS: "192.168.1.30"}
# what a protocol that only controls its device supports: it plays nothing itself
CONTROL_FEATURES = frozenset({PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE})


async def _den(
    scenario: Scenario,
    *protocols: tuple[str, Role],
    cuts_power: bool = True,
    startup_time: int | None = None,
) -> tuple[list[FakeProtocol], FakePowerControl, str]:
    """
    Attach the Den speaker with the given protocols, on a smart plug that is switched on.

    :param protocols: The provider domain and the role of each protocol of the speaker.
    :param cuts_power: Whether the plug is set to cut the power of the speaker.
    :param startup_time: The start-up time the speaker is given, the default when omitted.
    :return: The protocols, the plug and the id of the player.
    """
    speakers = []
    for domain, role in protocols:
        provider = await scenario.add_provider(domain)
        state: dict[str, Any] = (
            {"supported_features": set(CONTROL_FEATURES)} if role == "control" else {}
        )
        speakers.append(
            provider.protocol(f"{domain}_den", "Den", role=role, identifiers=DEN, **state)
        )
        await scenario.register(speakers[-1])
    await scenario.idle()
    player_id = scenario.single_player(*speakers).player_id
    return speakers, await _plug(scenario, player_id, cuts_power, startup_time), player_id


async def _plug(
    scenario: Scenario, player_id: str, cuts_power: bool = True, startup_time: int | None = None
) -> FakePowerControl:
    """
    Put a player on a smart plug that is switched on, and return the plug.

    :param player_id: The player.
    :param cuts_power: Whether the plug is set to cut the power of the player.
    :param startup_time: The start-up time the player is given, the default when omitted.
    """
    plug = await scenario.add_power_control(f"switch.{player_id}_plug", "Plug", powered=True)
    await scenario.configure_player(player_id, power_control=plug.control_id)
    # these settings are offered once the plug is the power control of the player
    settings: dict[str, Any] = {CONF_POWER_CONTROL_CUTS_POWER: cuts_power}
    if startup_time is not None:
        settings[CONF_POWER_ON_STARTUP_TIME] = startup_time
    await scenario.configure_player(player_id, **settings)
    return plug


async def _cut(scenario: Scenario, plug: FakePowerControl, *noticed: FakeProtocol) -> None:
    """
    Switch a plug off, which cuts the power of its speaker, and leave it off for a while.

    :param plug: The plug.
    :param noticed: The protocols of the speaker that noticed and read unavailable; any other
        one still reads available, as a protocol can for minutes after a power cut.
    """
    await scenario.switch(plug, False)
    for protocol in noticed:
        await scenario.report(protocol, available=False)
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY)
    await scenario.idle()


async def _reach(scenario: Scenario, *protocols: FakeProtocol) -> None:
    """Let protocols reach their speaker again."""
    for protocol in protocols:
        await scenario.report(protocol, available=True)


@pytest.mark.parametrize("noticed", ["both protocols", "its own protocol", "no protocol"])
async def test_a_speaker_whose_plug_cuts_its_power_is_off_while_the_plug_is_off(
    scenario: Scenario, noticed: str
) -> None:
    """
    A speaker whose plug cuts its power is off while the plug is off, whatever its protocols read.

    Den has its Sonos protocol and an AirPlay receiver, and is on a smart plug that is set
    to cut its power. The plug is switched off. At once the player shows as available,
    powered off and idle, whether both protocols noticed the cut, only its own or neither.
    A volume command is ignored and a seek is refused, and nothing is sent to the device.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    away = {"both protocols": [den, den_airplay], "its own protocol": [den]}.get(noticed, [])
    players = scenario.mass.players

    await scenario.switch(plug, False)
    for protocol in away:
        await scenario.report(protocol, available=False)

    for _ in range(2):
        state = _state(scenario, player_id)
        assert (state.available, state.powered) == (True, False)
        assert state.playback_state == PlaybackState.IDLE
        await players.cmd_volume_set(player_id, 30)
        with pytest.raises(PlayerUnavailableError, match="is switched off") as error:
            await players.cmd_seek(player_id, 10)
        assert error.value.translation_key == "player_switched_off"
        assert error.value.translation_args == ["Den"]
        # the same once the power counts as cut
        await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY)
        await scenario.idle()
    assert den.commands == den_airplay.commands == []
    assert plug.commands == []


@pytest.mark.parametrize("startup_time", [None, 45], ids=["default", "45 s"])
async def test_a_play_switches_the_plug_on_and_waits_for_the_startup_time(
    scenario: Scenario, startup_time: int | None
) -> None:
    """
    A play on a switched-off speaker switches its plug on and waits for its start-up time.

    Den's plug, which cuts its power, is off. A play switches the plug on, and the player
    shows as available, powered on and idle. The device is back on the network within
    seconds, but nothing is sent to it until the start-up time of the player is over,
    counted from the moment the plug was switched on: 30 s unless the user set another time.
    Then Den plays, once, through its Sonos protocol.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio"), startup_time=startup_time
    )
    seconds = startup_time or DEFAULT_POWER_ON_STARTUP_TIME
    # the settings are those of the player, not of its own protocol
    stored = scenario.mass.config.get_player_value
    assert stored(player_id, CONF_POWER_CONTROL_CUTS_POWER) is True
    assert stored(player_id, CONF_POWER_ON_STARTUP_TIME) == startup_time
    assert CONF_POWER_CONTROL_CUTS_POWER not in (scenario.protocol_config("sonos_den") or {})
    await _cut(scenario, plug, den, den_airplay)

    play = await _play(scenario, player_id)

    assert plug.commands == [True]
    await scenario.advance(5)
    await _reach(scenario, den, den_airplay)
    await scenario.advance(seconds - 6)
    assert not play.done(), play.exception()
    assert den.commands == den_airplay.commands == []
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, True)
    assert state.playback_state == PlaybackState.IDLE

    await scenario.advance(2)
    await play
    await scenario.idle()

    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    assert plug.commands == [True]
    await _stop(scenario, player_id)


async def test_the_play_button_shows_the_play_while_the_speaker_starts_up(
    scenario: Scenario,
) -> None:
    """
    The play button shows that a play is under way while the speaker starts up for it.

    Den's plug, which cuts its power, is off, and Den holds the track it played before. The
    play button is pressed: for the whole start-up time Den's queue shows that a play is in
    progress, which makes the button spin in the app. Once Den plays, it shows that no more.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await scenario.play(player_id)
    await _stop(scenario, player_id)
    await _cut(scenario, plug, den, den_airplay)
    queue = scenario.mass.player_queues.get(player_id)
    assert queue is not None

    press = await scenario.command(scenario.mass.players.cmd_play_pause(player_id))
    await scenario.advance(5)
    await _reach(scenario, den, den_airplay)
    await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME - 6)

    assert not press.done(), press.exception()
    assert queue.extra_attributes.get(ATTR_PLAY_ACTION_IN_PROGRESS) is True, (
        "the queue does not show the play while Den starts up"
    )
    await scenario.advance(2)
    await press
    await scenario.idle()
    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
    assert queue.extra_attributes.get(ATTR_PLAY_ACTION_IN_PROGRESS) is False
    await _stop(scenario, player_id)


async def test_a_plug_switched_on_elsewhere_lets_the_startup_time_run(scenario: Scenario) -> None:
    """
    A plug that is switched on outside of Music Assistant lets the start-up time run as well.

    Den's plug, which cuts its power, is off, and someone switches it on in Home Assistant.
    From that moment Den starts up: it shows as available, powered on and idle, and a
    volume command is ignored. A second report of the plug does not start the time over.
    A play meanwhile waits for what is left of the start-up time and does not switch the
    plug, and then Den plays.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await _cut(scenario, plug, den)

    await scenario.switch(plug, True)

    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, True)
    assert state.playback_state == PlaybackState.IDLE
    await scenario.mass.players.cmd_volume_set(player_id, 30)
    assert den.commands == den_airplay.commands == []
    await scenario.advance(10)
    await _reach(scenario, den)
    await scenario.switch(plug, True)
    play = await _play(scenario, player_id)
    await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME - 11)
    assert not play.done(), play.exception()
    assert den.commands == den_airplay.commands == []

    await scenario.advance(2)
    await play
    await scenario.idle()

    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    assert plug.commands == []
    await _stop(scenario, player_id)


async def test_a_plug_switched_on_again_right_away_lets_no_startup_time_run(
    scenario: Scenario,
) -> None:
    """
    A plug that reads off for a moment cut nothing: no start-up time follows.

    Den's plug, which cuts its power, reads off and on again within seconds, as Home
    Assistant reports a plug while it reloads. Den shows as powered off for that moment.
    Back on, it does not start up: a play starts at once and does not switch the plug.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )

    await scenario.switch(plug, False)
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY - 5)
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, False)
    await scenario.switch(plug, True)
    await scenario.idle()

    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, True)
    await scenario.play(player_id)
    assert plug.commands == []
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    assert den.power_reports == den_airplay.power_reports == []
    await _stop(scenario, player_id)


async def test_a_plug_that_reads_off_for_a_moment_does_not_stop_the_music(
    scenario: Scenario,
) -> None:
    """
    A plug that reads off for a moment while its speaker plays leaves the music playing.

    Den plays its queue when its plug, which cuts its power, reads off for some seconds,
    as Home Assistant reports a plug while it reloads. For that moment Den shows as powered
    off and idle. Back on, Den shows as playing again: its queue was not stopped, the device
    got no command, and no start-up time runs, so a volume command reaches it at once.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await scenario.play(player_id)
    sent = list(den.commands)

    await scenario.switch(plug, False)
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, False)
    assert state.playback_state == PlaybackState.IDLE
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY - 5)
    await scenario.switch(plug, True)
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY)

    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, True)
    assert state.playback_state == PlaybackState.PLAYING
    queue = scenario.queue(player_id)
    assert queue is not None
    assert queue.state == PlaybackState.PLAYING
    assert den.commands == sent
    await scenario.mass.players.cmd_volume_set(player_id, 30)
    assert den.commands == [*sent, ("volume_set", 30)]
    assert den_airplay.commands == []
    assert den.power_reports == den_airplay.power_reports == []
    assert plug.commands == []
    await _stop(scenario, player_id)


@pytest.mark.parametrize("back", ["nothing", "its receiver"])
async def test_a_speaker_that_is_not_back_after_its_startup_time_fails_the_play_with_a_hint(
    scenario: Scenario, back: str
) -> None:
    """
    A play fails with a hint when nothing reaches the speaker once its start-up time is over.

    Den's plug, which cuts its power, is off, and both its protocols noticed. A play
    switches the plug on and waits for the start-up time. When neither protocol reaches Den
    by then, the play fails and tells the user to raise the start-up time, and Den shows as
    powered on and unavailable. When its AirPlay receiver is back and its Sonos protocol is
    not, Den plays through the receiver.

    :param back: What reaches Den when its start-up time is over.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await _cut(scenario, plug, den, den_airplay)

    play = await _play(scenario, player_id)
    if back == "its receiver":
        await _reach(scenario, den_airplay)
    await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME + 1)

    assert play.done()
    assert plug.commands == [True]
    if back == "its receiver":
        await play
        await scenario.idle()
        assert (_starts(den), _starts(den_airplay)) == (0, 1)
        await _stop(scenario, player_id)
        return
    with pytest.raises(PlayerUnavailableError, match="could not be reached 30s after") as error:
        await play
    assert error.value.translation_key == "player_not_started_up"
    assert error.value.translation_args == ["Den", DEFAULT_POWER_ON_STARTUP_TIME]
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (False, True)
    assert den.commands == den_airplay.commands == []


async def test_a_plug_that_does_not_cut_the_power_is_a_plain_switch(scenario: Scenario) -> None:
    """
    A speaker whose plug is not set to cut its power is there as long as its protocols reach it.

    Den is on a smart plug, and its settings offer to say that the plug cuts its power,
    which is off, with a start-up time of 30 s. The plug is switched off while the device
    keeps its power: Den shows as available and powered off, and a volume command reaches
    it. Once its protocols no longer reach the device, Den is unavailable and a play is
    refused: the plug is not switched on for it. Its protocols are never told that its power
    was cut.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio"), cuts_power=False
    )
    config = await scenario.player_config(player_id)
    assert config is not None
    assert config.get_value(CONF_POWER_CONTROL_CUTS_POWER) is False
    assert config.get_value(CONF_POWER_ON_STARTUP_TIME) == DEFAULT_POWER_ON_STARTUP_TIME

    await scenario.switch(plug, False)
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY)
    await scenario.idle()

    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, False)
    await scenario.mass.players.cmd_volume_set(player_id, 30)
    assert den.commands == [("volume_set", 30)]
    await scenario.report(den, available=False)
    await scenario.report(den_airplay, available=False)
    await scenario.idle()
    assert _state(scenario, player_id).available is False
    play = await _play(scenario, player_id)
    assert play.done()
    with pytest.raises(PlayerUnavailableError):
        await play
    assert plug.commands == []
    assert den.power_reports == den_airplay.power_reports == []


async def test_a_speaker_that_kept_its_power_plays_once_its_plug_reads_on(
    scenario: Scenario,
) -> None:
    """
    A speaker whose plug does not cut its power plays as soon as the plug reads on.

    Den's plug is not set to cut its power: it only feeds an amplifier. The plug is off and
    the device is still there. A play switches the plug on, once, and starts as soon as the
    plug reports on, with no start-up time.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio"), cuts_power=False
    )
    await scenario.switch(plug, False)
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY)
    await scenario.idle()

    await scenario.play(player_id)

    assert plug.commands == [True]
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    assert _state(scenario, player_id).powered is True
    await _stop(scenario, player_id)


@pytest.mark.parametrize("plug_is", ["off", "on"])
async def test_a_server_start_takes_a_plug_that_is_off_as_a_power_cut(
    scenario: Scenario, plug_is: str
) -> None:
    """
    After a server start a speaker is off or on as its plug reads, without a start-up time.

    Den's plug cuts its power. The server starts while the plug is on: Den is on at once,
    and a play starts right away. The server starts while the plug is off: Den shows as
    available and powered off, and when the plug is switched on in Home Assistant, Den
    starts up for its start-up time before a play starts.

    :param plug_is: What the plug reads when the server starts.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    if plug_is == "off":
        await _cut(scenario, plug)

    await scenario.restart()
    await scenario.idle()

    den, den_airplay = scenario.protocol("sonos_den"), scenario.protocol("airplay_den")
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, plug_is == "on")
    if plug_is == "off":
        await scenario.switch(plug, True)
    play = await _play(scenario, player_id)
    if plug_is == "off":
        await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME - 1)
        assert not play.done(), play.exception()
        assert den.commands == den_airplay.commands == []
        await scenario.advance(2)
    await play
    await scenario.idle()
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    assert plug.commands == []
    await _stop(scenario, player_id)


async def test_a_power_off_during_the_startup_switches_the_plug_off_again(
    scenario: Scenario,
) -> None:
    """
    A power off while a speaker starts up ends the play that waits and switches the plug off.

    Den's plug, which cuts its power, is off. A play switches it on and waits for the
    start-up time. The user powers Den off: the play fails as switched off, the plug is
    switched off again and Den shows as powered off. Nothing is left of the start-up: a new
    play seconds later switches the plug on again and waits for a whole start-up time.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await _cut(scenario, plug, den, den_airplay)
    play = await _play(scenario, player_id)
    await scenario.advance(10)
    assert not play.done(), play.exception()

    await scenario.mass.players.cmd_power(player_id, False)
    await scenario.settle()

    with pytest.raises(PlayerUnavailableError, match="is switched off"):
        await play
    assert plug.commands == [True, False]
    await scenario.advance(5)
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, False)
    play = await _play(scenario, player_id)
    assert plug.commands == [True, False, True]
    await _reach(scenario, den, den_airplay)
    await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME - 1)
    assert not play.done(), play.exception()
    assert den.commands == den_airplay.commands == []
    await scenario.advance(2)
    await play
    await scenario.idle()
    assert plug.commands == [True, False, True]
    await _stop(scenario, player_id)


async def test_a_plug_switched_off_during_the_startup_ends_the_play_that_waits(
    scenario: Scenario,
) -> None:
    """
    A plug that is switched off while its speaker starts up ends the play that waits.

    Den's plug, which cuts its power, is off. A play switches it on and waits for the
    start-up time. Someone switches the plug off in Home Assistant: the play fails as
    switched off and Den shows as powered off. Switched on again seconds later, Den starts
    up for a whole start-up time, as its power was cut while it started up.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await _cut(scenario, plug, den, den_airplay)
    play = await _play(scenario, player_id)
    await scenario.advance(10)

    await scenario.switch(plug, False)

    assert play.done()
    with pytest.raises(PlayerUnavailableError, match="is switched off") as error:
        await play
    assert error.value.translation_key == "player_switched_off"
    assert error.value.translation_args == ["Den"]
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, False)
    await scenario.advance(5)
    await scenario.switch(plug, True)
    await _reach(scenario, den, den_airplay)
    play = await _play(scenario, player_id)
    await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME - 1)
    assert not play.done(), play.exception()
    assert den.commands == den_airplay.commands == []
    await scenario.advance(2)
    await play
    await scenario.idle()
    assert plug.commands == [True]
    await _stop(scenario, player_id)


CHANGES = [
    "the setting is switched off",
    "the power control is set to none",
    "another plug that is on is picked",
    "the plug goes with its provider",
]


@pytest.mark.parametrize("change", CHANGES)
async def test_a_plug_that_no_longer_cuts_the_power_of_a_speaker_confirms_no_power_cut(
    scenario: Scenario, change: str
) -> None:
    """
    A plug that reads off confirms no power cut once it is not the plug that cuts the power.

    Den's plug, which cuts its power, is switched off. Five seconds later, before its power
    counts as cut, the user switches the setting off, sets the power control of Den to none
    or picks another plug, which is on, or the plug goes with its provider. Den's protocols
    are not told that its power was cut. When the other plug is switched off after that,
    they are told 15 s after that plug went off.

    :param change: What happens before the power counts as cut.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await scenario.switch(plug, False)
    await scenario.advance(5)

    other_plug_off_at = None
    if change == "the setting is switched off":
        await scenario.configure_player(player_id, power_control_cuts_power=False)
    elif change == "the power control is set to none":
        await scenario.configure_player(player_id, power_control=PLAYER_CONTROL_NONE)
    elif change == "another plug that is on is picked":
        other_plug = await scenario.add_power_control("switch.other", "Other plug", powered=True)
        await scenario.configure_player(player_id, power_control=other_plug.control_id)
        await scenario.advance(3)
        other_plug_off_at = scenario.mass.loop.time()
        await scenario.switch(other_plug, False)
    else:
        scenario.mass.players.remove_player_control(plug.control_id)
        await scenario.settle()
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY - 4)

    assert den.power_reports == den_airplay.power_reports == []
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY)
    if other_plug_off_at is None:
        assert den.power_reports == den_airplay.power_reports == []
    else:
        told_at = other_plug_off_at + EXTERNAL_POWER_OFF_STOP_DELAY
        assert den.power_reports == den_airplay.power_reports == [("cut", told_at)]
    await scenario.idle()


@pytest.mark.parametrize("change", ["the setting is switched off", "another plug is picked"])
async def test_a_change_of_the_power_setting_during_the_startup_ends_the_play_that_waits(
    scenario: Scenario, change: str
) -> None:
    """
    A change of the power setting while a speaker starts up ends the play that waits.

    Den's plug, which cuts its power, is off. A play switches it on and waits for the
    start-up time. The user switches the setting off, or picks another plug, which is off:
    the play fails, as called off or as switched off, and nothing is left of the start-up. With the setting
    off, Den is a speaker on a plain switch that is on: a new play starts at once. With the
    other plug, a new play switches that plug on and waits for a whole start-up time.

    :param change: What the user changes while Den starts up.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await _cut(scenario, plug, den, den_airplay)
    play = await _play(scenario, player_id)
    await scenario.advance(5)
    await _reach(scenario, den, den_airplay)

    other_plug = None
    if change == "the setting is switched off":
        await scenario.configure_player(player_id, power_control_cuts_power=False)
    else:
        other_plug = await scenario.add_power_control("switch.other", "Other plug")
        await scenario.configure_player(player_id, power_control=other_plug.control_id)

    assert play.done()
    # the plug of the setting reads on, the other plug off
    failure = "power on of player Den was called off" if other_plug is None else "is switched off"
    with pytest.raises(PlayerUnavailableError, match=failure):
        await play
    assert den.commands == den_airplay.commands == []
    if other_plug is None:
        await scenario.play(player_id)
    else:
        play = await _play(scenario, player_id)
        assert other_plug.commands == [True]
        await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME - 1)
        assert not play.done(), play.exception()
        assert den.commands == den_airplay.commands == []
        await scenario.advance(2)
        await play
        await scenario.idle()
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    assert plug.commands == [True]
    await _stop(scenario, player_id)


@pytest.mark.parametrize("change", [*CHANGES, "the setting is switched off during the start-up"])
async def test_protocols_told_of_a_power_cut_are_told_it_is_back_once_nothing_follows_it(
    scenario: Scenario, change: str
) -> None:
    """
    Protocols that were told of a power cut are told that the power is back once nothing says so.

    Den's plug, which cuts its power, is off, and Den's protocols were told that its power
    was cut. The user switches the setting off, sets the power control of Den to none or
    picks another plug, or the plug goes with its provider: nothing tells the protocols any
    more when the power is back, so they are told at once and reach Den by themselves
    again. The same when the setting is switched off while Den starts up for a play, which
    fails as called off.

    :param change: What happens after the power was cut.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await _cut(scenario, plug, den, den_airplay)
    assert [kind for kind, _ in den.power_reports] == ["cut"]
    play = None
    if change == "the setting is switched off during the start-up":
        play = await _play(scenario, player_id)
        await scenario.advance(5)
    changed_at = scenario.mass.loop.time()

    if change.startswith("the setting is switched off"):
        await scenario.configure_player(player_id, power_control_cuts_power=False)
    elif change == "the power control is set to none":
        await scenario.configure_player(player_id, power_control=PLAYER_CONTROL_NONE)
    elif change == "another plug that is on is picked":
        other_plug = await scenario.add_power_control("switch.other", "Other plug", powered=True)
        await scenario.configure_player(player_id, power_control=other_plug.control_id)
    else:
        scenario.mass.players.remove_player_control(plug.control_id)
        await scenario.settle()

    assert den.power_reports[1:] == den_airplay.power_reports[1:] == [("restored", changed_at)]
    if play is not None:
        with pytest.raises(PlayerUnavailableError, match="power on of player Den was called off"):
            await play
    # told once: nothing follows the power of Den any more
    await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME + EXTERNAL_POWER_OFF_STOP_DELAY)
    assert len(den.power_reports) == len(den_airplay.power_reports) == 2
    await scenario.idle()


async def test_a_switch_on_that_fails_leaves_the_power_cut_as_it_was(scenario: Scenario) -> None:
    """
    A plug that fails to switch on leaves the power of its speaker cut, and that told.

    Den's plug, which cuts its power, is off, and Den's protocols were told that its power
    was cut. A power on of Den fails: the plug does not answer. The user then switches the
    setting off: Den's protocols are told that the power is back, as nothing follows it any
    more.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await _cut(scenario, plug, den, den_airplay)
    _refuse_to_switch_on(scenario, plug)

    power_on = await scenario.command(scenario.mass.players.cmd_power(player_id, True))
    with pytest.raises(PlayerCommandFailed, match="The plug does not answer"):
        await power_on
    changed_at = scenario.mass.loop.time()
    await scenario.configure_player(player_id, power_control_cuts_power=False)

    # what Den's protocols were told about its power
    assert [kind for kind, _ in den.power_reports] == ["cut", "restored"]
    assert den.power_reports[1:] == den_airplay.power_reports[1:] == [("restored", changed_at)]
    await scenario.idle()


def _answer_late(scenario: Scenario, plug: FakePowerControl) -> asyncio.Event:
    """
    Let the switch-on of a plug wait for its answer, which is that the plug does not answer.

    :param plug: The plug.
    :return: The event that lets the answer come.
    """
    control = scenario.mass.players.get_player_control(plug.control_id)
    assert control is not None
    answer = asyncio.Event()

    async def _fail_late() -> None:
        await answer.wait()
        raise PlayerCommandFailed("The plug does not answer")

    control.power_on = _fail_late
    scenario.expect_log("The plug does not answer")
    return answer


@pytest.mark.parametrize("end", ["fails", "is cancelled", "fails once the plug is gone"])
async def test_a_switch_on_that_ends_badly_once_the_plug_switched_lets_the_startup_time_run(
    scenario: Scenario, end: str
) -> None:
    """
    A switch-on that fails after the plug switched leaves the start-up time running.

    Den's plug, which cuts its power, is off, and Den's protocols were told that its power
    was cut. A power on of Den switches the plug, which reports on, and then the switch-on
    fails, or its command is cancelled, before Home Assistant answered. The power is on, so
    the start-up time runs on: when it is over Den's protocols are told that the power is
    back, and Den is on and available. The start-up time also runs on when the plug went
    with its provider before the switch-on failed: nothing says any more whether it is on.

    :param end: How the switch-on ends.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    den.connects_when_power_is_back = True
    await _cut(scenario, plug, den, den_airplay)
    answer = _answer_late(scenario, plug)

    power_on = await scenario.command(scenario.mass.players.cmd_power(player_id, True))
    switched_on_at = scenario.mass.loop.time()
    if end == "fails once the plug is gone":
        scenario.mass.players.remove_player_control(plug.control_id)
        await scenario.settle()
    else:
        await scenario.switch(plug, True)
    if end == "is cancelled":
        power_on.cancel()
        with pytest.raises(asyncio.CancelledError):
            await power_on
    else:
        answer.set()
        await scenario.settle()
        with pytest.raises(PlayerCommandFailed, match="The plug does not answer"):
            await power_on
    await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME + EXTERNAL_POWER_OFF_STOP_DELAY)

    # what Den's protocols were told about its power
    assert [kind for kind, _ in den.power_reports] == ["cut", "restored"]
    back_at = switched_on_at + DEFAULT_POWER_ON_STARTUP_TIME
    assert den.power_reports[1:] == den_airplay.power_reports[1:] == [("restored", back_at)]
    if end != "fails once the plug is gone":
        state = _state(scenario, player_id)
        assert (state.available, state.powered) == (True, True)
    await scenario.idle()


async def test_a_switch_on_that_fails_keeps_a_power_cut_that_was_told_meanwhile(
    scenario: Scenario,
) -> None:
    """
    A switch-on that fails does not undo a power cut that was told while it was under way.

    Den's plug, which cuts its power, is switched off. Before that counts as a cut, a power
    on of Den switches the plug: it reports on, and off again, and Den's protocols are told
    that its power is cut. Then the switch-on fails. The cut that was told is kept: when the
    user switches the setting off, Den's protocols are told that the power is back.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await scenario.switch(plug, False)
    answer = _answer_late(scenario, plug)

    power_on = await scenario.command(scenario.mass.players.cmd_power(player_id, True))
    await scenario.switch(plug, True)
    await scenario.switch(plug, False)
    assert [kind for kind, _ in den.power_reports] == ["cut"]
    answer.set()
    await scenario.settle()
    with pytest.raises(PlayerCommandFailed, match="The plug does not answer"):
        await power_on
    changed_at = scenario.mass.loop.time()
    await scenario.configure_player(player_id, power_control_cuts_power=False)

    # what Den's protocols were told about its power
    assert [kind for kind, _ in den.power_reports] == ["cut", "restored"]
    assert den.power_reports[1:] == den_airplay.power_reports[1:] == [("restored", changed_at)]
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY)
    assert len(den.power_reports) == 2
    await scenario.idle()


@pytest.mark.parametrize("on_report", ["before the stop", "after the stop", "never"])
async def test_a_stop_while_the_plug_of_a_member_is_switched_on_loses_no_power_cut(
    scenario: Scenario, on_report: str
) -> None:
    """
    A stop of a group while the plug of a member is being switched on loses no power cut.

    Den, a member of Downstairs, is on a smart plug that cuts its power, and its protocol
    was told that the power was cut. A play of Downstairs switches the plug on, and Home
    Assistant has not answered that when the user stops Downstairs: the play is called off.
    When the plug reported on before the stop, Den starts up all the same: once its start-up
    time is over its protocol is told that the power is back, and Den is on and available.
    When the plug reports on after the stop, the start-up time runs from then. When it
    never does, the power of Den is still cut, and its protocol still told so.

    :param on_report: When the plug reports that it is on.
    """
    group_id, speakers, player_ids = await _downstairs(
        scenario, "universal group", "Kitchen", "Office", "Den"
    )
    den, den_id = speakers[2], player_ids[2]
    den.connects_when_power_is_back = True
    plug = await _plug(scenario, den_id)
    await _cut(scenario, plug, den)
    assert [kind for kind, _ in den.power_reports] == ["cut"]
    # the answer to the switch-on never comes
    _answer_late(scenario, plug)

    play = await _play(scenario, group_id)
    switched_on_at = scenario.mass.loop.time()
    if on_report == "before the stop":
        await scenario.switch(plug, True)
    stop = await scenario.command(scenario.mass.players.cmd_stop(group_id))
    await scenario.advance(2)
    await stop
    with pytest.raises(PlayerUnavailableError, match="was called off") as error:
        await play
    assert error.value.translation_key == "player_stopped_during_startup"
    assert error.value.translation_args == ["Downstairs"]
    if on_report == "after the stop":
        switched_on_at = scenario.mass.loop.time()
        await scenario.switch(plug, True)
    await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME + EXTERNAL_POWER_OFF_STOP_DELAY)

    if on_report == "never":
        assert [kind for kind, _ in den.power_reports] == ["cut"]
        await scenario.configure_player(den_id, power_control_cuts_power=False)
        # what Den's protocol was told about its power
        assert [kind for kind, _ in den.power_reports] == ["cut", "restored"]
    else:
        # what Den's protocol was told about its power
        assert [kind for kind, _ in den.power_reports] == ["cut", "restored"]
        assert den.power_reports[1][1] == switched_on_at + DEFAULT_POWER_ON_STARTUP_TIME
        state = _state(scenario, den_id)
        assert (state.available, state.powered) == (True, True)
    await scenario.idle()


@pytest.mark.parametrize("cut_by", ["the plug", "a power off of the player"])
async def test_the_protocols_are_told_of_a_power_cut_and_of_the_power_being_back(
    scenario: Scenario, cut_by: str
) -> None:
    """
    The protocols of a speaker are told when its power is cut, and when it is back.

    Den's plug, which cuts its power, is switched off in Home Assistant: once it has stayed
    off for 15 s, Den's Sonos protocol and its AirPlay receiver are told that the power was
    cut. After a power off of Den through Music Assistant they are told at once. A play
    switches the plug on, and when the start-up time is over both are told that the power
    is back, before anything is played. Den's Sonos protocol, which only reaches the device
    again when it is told, plays the music.

    :param cut_by: What switched the plug off.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    den.connects_when_power_is_back = True
    cut_at = scenario.mass.loop.time()

    if cut_by == "the plug":
        await scenario.switch(plug, False)
        await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY - 1)
        assert den.power_reports == den_airplay.power_reports == []
        await scenario.advance(1)
        told_at = cut_at + EXTERNAL_POWER_OFF_STOP_DELAY
    else:
        await scenario.mass.players.cmd_power(player_id, False)
        await scenario.settle()
        told_at = cut_at
    assert den.power_reports == den_airplay.power_reports == [("cut", told_at)]
    await scenario.report(den, available=False)
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY)
    await scenario.idle()
    assert len(den.power_reports) == 1

    play = await _play(scenario, player_id)
    switched_on_at = scenario.mass.loop.time()
    await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME - 1)
    assert len(den.power_reports) == 1
    assert not play.done(), play.exception()
    await scenario.advance(2)
    await play
    await scenario.idle()

    back_at = switched_on_at + DEFAULT_POWER_ON_STARTUP_TIME
    assert den.power_reports[1:] == den_airplay.power_reports[1:] == [("restored", back_at)]
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    await _stop(scenario, player_id)


@pytest.mark.parametrize("group", GROUPS)
async def test_a_start_tells_the_protocols_of_a_member_that_its_power_is_back(
    scenario: Scenario, group: str
) -> None:
    """
    A start tells the protocols of a switched-off member that its power is back, and starts once.

    Den, a member of Downstairs, is on a smart plug that cuts its power, and its Sonos
    protocol noticed the cut. Once Den is up again, the protocol would only reach it on a
    reconnect minutes later. A play of Downstairs switches the plug on, and when Den's
    start-up time is over its Sonos protocol is told that the power is back and reaches the
    device. Downstairs then starts once, on all members.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den = speakers[2]
    plug = await _plug(scenario, player_ids[2])
    den.connects_when_power_is_back = True
    await _cut(scenario, plug, den)

    play = await _play(scenario, group_id)
    switched_on_at = scenario.mass.loop.time()

    assert plug.commands == [True]
    await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME - 1)
    assert not play.done(), play.exception()
    assert [speaker.commands for speaker in speakers] == [[], [], []]
    await scenario.advance(2)
    await play
    await scenario.idle()

    assert [kind for kind, _ in den.power_reports] == ["cut", "restored"]
    assert den.power_reports[1][1] == switched_on_at + DEFAULT_POWER_ON_STARTUP_TIME
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    assert plug.commands == [True]
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
async def test_a_group_player_on_a_power_control_has_no_startup_time(
    scenario: Scenario, group: str
) -> None:
    """
    A group player on a power control is not offered a start-up time, and has none.

    Downstairs is on a smart plug that is switched off. Its settings do not offer to say
    that the plug cuts its power, and a setting stored for it all the same changes nothing:
    a power on of Downstairs switches the plug on and returns at once.
    """
    group_id, speakers, _ = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    plug = await scenario.add_power_control("switch.downstairs_plug", "Plug", powered=True)
    await scenario.configure_player(group_id, power_control=plug.control_id)
    config = await scenario.player_config(group_id)
    assert config is not None
    assert CONF_POWER_CONTROL_CUTS_POWER not in config.values
    assert CONF_POWER_ON_STARTUP_TIME not in config.values
    scenario.mass.config.set_player_value(group_id, CONF_POWER_CONTROL_CUTS_POWER, True)
    await scenario.switch(plug, False)
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY)
    await scenario.idle()
    state = _state(scenario, group_id)
    assert (state.available, state.powered) == (True, False)

    power_on = await scenario.command(scenario.mass.players.cmd_power(group_id, True))

    assert power_on.done()
    await power_on
    assert plug.commands == [True]
    assert _state(scenario, group_id).powered is True
    assert [speaker.commands for speaker in speakers] == [[], [], []]
