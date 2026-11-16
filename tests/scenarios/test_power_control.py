"""Which protocol tells whether a speaker on a power control is there: the one that reports for it."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import PlayerSource

from music_assistant.controllers.players.power import (
    DEVICE_CHECK_DELAY,
    DEVICE_CHECK_INTERVAL,
    DEVICE_CHECK_TIMEOUT,
)

from .harness import TEST_TRACK_URI
from .test_group_power_on import GROUPS, _downstairs, _play, _plays_along, _starts
from .test_grouping import (
    _device,
    _group,
    _grouping_commands,
    _mixed_makes,
    _player_id,
    _state,
    _stop,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from .harness import FakePowerControl, FakeProtocol, Role, Scenario

DEN = {IdentifierType.MAC_ADDRESS: "00:11:22:33:44:55", IdentifierType.IP_ADDRESS: "192.168.1.30"}
# what a protocol that only controls its device supports: it plays nothing itself
CONTROL_FEATURES = frozenset({PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE})


async def _den(
    scenario: Scenario, *protocols: tuple[str, Role]
) -> tuple[list[FakeProtocol], FakePowerControl, str]:
    """
    Attach the Den speaker with the given protocols, on a smart plug that is switched on.

    :param protocols: The provider domain and the role of each protocol of the speaker.
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
    return speakers, await _plug(scenario, player_id), player_id


async def _plug(scenario: Scenario, player_id: str) -> FakePowerControl:
    """Put a player on a smart plug that is switched on, and return the plug."""
    plug = await scenario.add_power_control(f"switch.{player_id}_plug", "Plug", powered=True)
    await scenario.configure_player(player_id, power_control=plug.control_id)
    return plug


async def _receiver(scenario: Scenario, speaker: FakeProtocol, number: int) -> FakeProtocol:
    """
    Give a speaker an AirPlay receiver next to its own protocol.

    :param speaker: The own protocol of the speaker, which is registered.
    :param number: The number of its device (see _device).
    :return: The AirPlay receiver.
    """
    airplay = await scenario.add_provider("airplay")
    receiver = airplay.protocol(
        f"airplay_{speaker.protocol_id}", "Receiver", role="audio", identifiers=_device(number)
    )
    await scenario.register(receiver)
    await scenario.idle()
    assert scenario.player_of(receiver) == _player_id(scenario, speaker)
    return receiver


async def _cut(scenario: Scenario, plug: FakePowerControl, *noticed: FakeProtocol) -> None:
    """
    Switch a plug off, which cuts the power of its speaker.

    :param plug: The plug.
    :param noticed: The protocols of the speaker that noticed and read unavailable; any other
        one still reads available, as a protocol can for minutes after a power cut.
    """
    await scenario.switch(plug, False)
    for protocol in noticed:
        await scenario.report(protocol, available=False)
    await scenario.idle()


async def _reach(scenario: Scenario, protocol: FakeProtocol) -> None:
    """Let a protocol reach its speaker again, which a power on looks for every 0.5 s."""
    await scenario.report(protocol, available=True)
    await scenario.advance(1)


async def _boot(scenario: Scenario, protocol: FakeProtocol) -> None:
    """Let the device of a protocol be up again: it answers a check, and the protocol reaches it."""
    protocol.device_answers = True
    await _reach(scenario, protocol)


def _start(
    scenario: Scenario, player_id: str, command: str, source_id: str
) -> Coroutine[Any, Any, None]:
    """
    Return a request that starts playback on a player, which may wait.

    :param command: ``"queue play_media"`` plays a track, ``"queue resume"`` and
        ``"players cmd_play"`` play what the queue of the player holds, ``"queue transfer"``
        moves the queue of another player to it and plays it there.
    :param source_id: The player whose queue a transfer moves.
    """
    if command == "queue play_media":
        return scenario.mass.player_queues.play_media(player_id, TEST_TRACK_URI)
    if command == "queue resume":
        return scenario.mass.player_queues.resume(player_id)
    if command == "queue transfer":
        return scenario.mass.player_queues.transfer_queue(source_id, player_id, auto_play=True)
    return scenario.mass.players.cmd_play(player_id)


def _seek(scenario: Scenario, player_id: str, command: str) -> Coroutine[Any, Any, None]:
    """
    Return a request that moves 10 s into the current item of a player, which may wait.

    :param command: ``"queue seek"`` and ``"players cmd_seek"`` seek to 10 s, ``"queue skip"``
        skips 10 s ahead.
    """
    if command == "queue seek":
        return scenario.mass.player_queues.seek(player_id, 10)
    if command == "queue skip":
        return scenario.mass.player_queues.skip(player_id, 10)
    return scenario.mass.players.cmd_seek(player_id, 10)


@pytest.mark.parametrize("receiver", ["never noticed the cut", "is back first"])
async def test_a_speaker_is_off_and_back_when_its_own_protocol_says_so(
    scenario: Scenario, receiver: str
) -> None:
    """
    A play on a switched-off speaker waits for its own protocol, whatever its receiver reads.

    Den has its Sonos protocol and an AirPlay receiver, and is on a smart plug that is cut.
    Its Sonos protocol reads unavailable, while its AirPlay receiver reads available: it
    never noticed the cut, or it is back before the Sonos protocol is. The player shows as
    available and powered off. A play switches the plug on and waits, nothing is sent to
    the device, and Den plays through its Sonos protocol once that is back.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    noticed = (den,) if receiver == "never noticed the cut" else (den, den_airplay)
    await _cut(scenario, plug, *noticed)
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, False)

    play = await _play(scenario, player_id)

    assert not play.done(), play.exception()
    assert plug.commands == [True]
    await scenario.advance(10)
    if den_airplay in noticed:
        await _reach(scenario, den_airplay)
    await scenario.advance(10)
    assert not play.done(), play.exception()
    assert den.commands == den_airplay.commands == []
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, True)
    assert state.playback_state == PlaybackState.IDLE

    await _reach(scenario, den)
    await play
    await scenario.idle()

    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    assert plug.commands == [True]
    await _stop(scenario, player_id)


async def test_a_play_before_the_own_protocol_noticed_the_cut_starts_at_once(
    scenario: Scenario,
) -> None:
    """
    A speaker whose protocols can not check reads as on the network until one notices the cut.

    Den's plug is cut and none of its protocols has noticed yet. Its Sonos protocol is asked
    to check its device, but can not. The player shows as available and powered off, and
    nothing tells that its device is gone: a play switches the plug on and starts at once, on
    a device that still boots.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await _cut(scenario, plug)
    assert len(den.device_checks) == 1
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, False)

    await scenario.play(player_id)

    assert plug.commands == [True]
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    await _stop(scenario, player_id)


async def test_a_stop_calls_off_the_power_on_of_a_speaker_whose_receiver_reads_available(
    scenario: Scenario,
) -> None:
    """
    A stop ends the play that waits for a speaker, also while its receiver reads available.

    Den's plug is cut and its Sonos protocol reads unavailable, while its AirPlay receiver
    never noticed. A play switches the plug on and waits. The user stops Den: the play
    fails as switched off, and nothing is sent to the device, also not once it is back.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await _cut(scenario, plug, den)
    play = await _play(scenario, player_id)
    assert not play.done(), play.exception()

    await _stop(scenario, player_id)

    with pytest.raises(PlayerUnavailableError, match="is switched off"):
        await play
    await _reach(scenario, den)
    await scenario.idle()
    assert den.commands == den_airplay.commands == []
    assert _state(scenario, player_id).playback_state == PlaybackState.IDLE
    assert plug.commands == [True]


@pytest.mark.parametrize("receiver", ["own protocol only", "receiver reads available"])
@pytest.mark.parametrize(
    "command", ["queue play_media", "queue resume", "players cmd_play", "queue transfer"]
)
async def test_a_play_after_a_stop_waits_again_for_a_speaker_that_still_boots(
    scenario: Scenario, receiver: str, command: str
) -> None:
    """
    A speaker that is stopped while its play waits, and played again, is waited for again.

    Den played a track before its plug was cut; its Sonos protocol noticed the cut and reads
    unavailable, while its AirPlay receiver, when it has one, never noticed. A play (or a
    transfer of the queue of Kitchen, which plays) switches the plug on and waits; a stop
    calls it off and leaves the plug on, so the device still boots. The new play waits for
    Den's own protocol, holds no lock meanwhile and does not switch the plug again: Den plays
    through its Sonos protocol once that is back.
    """
    protocols: list[tuple[str, Role]] = [("sonos", "native")]
    if receiver == "receiver reads available":
        protocols.append(("airplay", "audio"))
    speakers, plug, player_id = await _den(scenario, *protocols)
    den = speakers[0]
    kitchen = scenario.provider("sonos").protocol(
        "sonos_kitchen", "Kitchen", identifiers=_device(1)
    )
    await scenario.register(kitchen)
    kitchen_id = _player_id(scenario, kitchen)
    await scenario.play(player_id)
    await _stop(scenario, player_id)
    if command == "queue transfer":
        await scenario.play(kitchen_id)
    await _cut(scenario, plug, den)
    play = await scenario.command(_start(scenario, player_id, command, kitchen_id))
    assert not play.done(), play.exception()
    await _stop(scenario, player_id)
    with pytest.raises(PlayerUnavailableError, match="is switched off"):
        await play

    play = await scenario.command(_start(scenario, player_id, command, kitchen_id))

    assert not play.done(), play.exception()
    await scenario.advance(10)
    assert not play.done(), play.exception()
    # it waits before any lock is taken, as the first play did
    assert not any(lock.locked() for lock in scenario.mass.players._player_command_locks.values())
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, True)
    assert [_starts(speaker) for speaker in speakers] == [1] + [0] * (len(speakers) - 1)
    await _reach(scenario, den)
    await play
    await scenario.idle()

    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
    assert [_starts(speaker) for speaker in speakers] == [2] + [0] * (len(speakers) - 1)
    assert plug.commands == [True]
    await _stop(scenario, player_id)


@pytest.mark.parametrize("command", ["queue seek", "queue skip", "players cmd_seek"])
async def test_a_seek_after_a_stop_waits_for_a_speaker_that_still_boots_without_a_lock(
    scenario: Scenario, command: str
) -> None:
    """
    A seek or a skip on a speaker that still boots after a stop waits for it, holding no lock.

    As in AWAY-24, Den played a track, its plug was cut, a play switched the plug on and a stop
    called that off, so the device still boots while its AirPlay receiver reads available. A
    seek (or a skip) in the item waits for Den's Sonos protocol and holds no lock meanwhile: a
    stop of the queue sent then returns at once and calls it off. A seek after that waits again,
    and Den plays through its Sonos protocol once that is back. The plug is switched once.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await scenario.play(player_id)
    await _stop(scenario, player_id)
    await _cut(scenario, plug, den)
    play = await _play(scenario, player_id)
    await _stop(scenario, player_id)
    with pytest.raises(PlayerUnavailableError, match="is switched off"):
        await play

    seek = await scenario.command(_seek(scenario, player_id, command))

    assert not seek.done(), seek.exception()
    assert not any(
        lock.locked() for lock in scenario.mass.players._player_command_locks.values()
    ), "the seek waits for the device while it holds a player lock"
    stop = await scenario.command(scenario.mass.player_queues.stop(player_id))
    await scenario.advance(1)
    assert stop.done(), "the stop waits for the lock the seek holds"
    await stop
    with pytest.raises(PlayerUnavailableError, match="is switched off"):
        await seek

    seek = await scenario.command(_seek(scenario, player_id, command))

    assert not seek.done(), seek.exception()
    await scenario.advance(10)
    assert not seek.done(), seek.exception()
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    await _reach(scenario, den)
    await seek
    await scenario.idle()

    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
    assert (_starts(den), _starts(den_airplay)) == (2, 0)
    assert plug.commands == [True]
    await _stop(scenario, player_id)


async def test_a_source_selected_after_a_stop_waits_again_for_a_speaker_that_still_boots(
    scenario: Scenario,
) -> None:
    """
    A source selected on a speaker whose device still boots after a stop waits for it.

    Den has an HDMI input on its Sonos protocol and an AirPlay receiver. Its plug is cut and
    its Sonos protocol reads unavailable, while its receiver never noticed. Selecting HDMI
    switches the plug on and waits; a stop calls it off and leaves the plug on. HDMI selected
    again waits for Den's own protocol, and reaches the device once that is back.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await scenario.report(
        den,
        supported_features={*den.supported_features, PlayerFeature.SELECT_SOURCE},
        source_list=[PlayerSource(id="hdmi", name="HDMI")],
    )
    await _cut(scenario, plug, den)
    select = await scenario.command(scenario.mass.players.select_source(player_id, "hdmi"))
    assert not select.done(), select.exception()
    await _stop(scenario, player_id)
    with pytest.raises(PlayerUnavailableError, match="is switched off"):
        await select

    select = await scenario.command(scenario.mass.players.select_source(player_id, "hdmi"))

    assert not select.done(), select.exception()
    await scenario.advance(10)
    assert not select.done(), select.exception()
    assert den.commands == den_airplay.commands == []
    await _reach(scenario, den)
    await select
    await scenario.idle()

    assert [command[0] for command in den.commands] == ["select_source"]
    assert den_airplay.commands == []
    assert plug.commands == [True]


@pytest.mark.parametrize("noticed", ["the AirPlay receiver", "the Cast receiver"])
async def test_the_most_preferred_receiver_tells_for_a_speaker_without_a_protocol_of_its_own(
    scenario: Scenario, noticed: str
) -> None:
    """
    For a speaker of receivers alone, its most preferred receiver tells whether it is there.

    Den is an AirPlay and a Cast receiver, on a smart plug that is cut. When its AirPlay
    receiver reads unavailable, the player is switched off, whatever its Cast receiver
    reads: a play switches the plug on, waits, and plays once the AirPlay receiver is back.
    When only its Cast receiver noticed the cut, nothing waits: the play starts at once.
    """
    (den_airplay, den_cast), plug, player_id = await _den(
        scenario, ("airplay", "audio"), ("chromecast", "audio")
    )
    await _cut(scenario, plug, den_airplay if noticed == "the AirPlay receiver" else den_cast)
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, False)

    play = await _play(scenario, player_id)

    assert plug.commands == [True]
    if noticed == "the AirPlay receiver":
        assert not play.done(), play.exception()
        await scenario.advance(10)
        assert den_airplay.commands == den_cast.commands == []
        await _reach(scenario, den_airplay)
    await play
    await scenario.idle()
    assert (_starts(den_airplay), _starts(den_cast)) == (1, 0)
    await _stop(scenario, player_id)


@pytest.mark.parametrize("noticed", ["its own protocol", "its renderer", "both"])
async def test_a_speaker_whose_own_protocol_only_controls_is_there_while_its_renderer_is_too(
    scenario: Scenario, noticed: str
) -> None:
    """
    A speaker whose own protocol only controls is there while that and a renderer reach it.

    Den has a LinkPlay protocol that controls it and a DLNA renderer that plays, and is on a
    smart plug that is cut. Whichever of the two reads unavailable, the player shows as
    available and powered off. A play switches the plug on and waits, and the player shows
    as available and powered on until both reach the device again, also when its own
    protocol is back first. Then Den plays through its renderer.
    """
    (linkplay, renderer), plug, player_id = await _den(
        scenario, ("wiim", "control"), ("dlna", "audio")
    )
    away = {"its own protocol": [linkplay], "its renderer": [renderer]}.get(
        noticed, [linkplay, renderer]
    )
    await _cut(scenario, plug, *away)
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, False)

    play = await _play(scenario, player_id)

    assert plug.commands == [True]
    for protocol in away:
        await scenario.advance(10)
        assert not play.done(), play.exception()
        state = _state(scenario, player_id)
        assert (state.available, state.powered) == (True, True)
        assert linkplay.commands == renderer.commands == []
        await _reach(scenario, protocol)
    await play
    await scenario.idle()
    assert (_starts(linkplay), _starts(renderer)) == (0, 1)
    await _stop(scenario, player_id)


@pytest.mark.parametrize("native", ["squeezelite", "sonos"])
async def test_a_speaker_whose_own_protocol_is_detached_waits_for_it_whatever_its_receiver_reads(
    scenario: Scenario, native: str
) -> None:
    """
    A speaker whose provider detaches its own protocol is switched off until that one is back.

    Den has its own protocol and an AirPlay receiver, and is on a smart plug that is cut. The
    provider of its own protocol detaches it when the device goes, as Squeezelite does, instead
    of letting it read unavailable, while its AirPlay receiver never noticed the cut. The player
    shows as available and powered off. A play switches the plug on and waits, and so does a
    new play after a stop called the first one off. Nothing is sent to the device, and Den
    plays through its own protocol once that is attached again.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, (native, "native"), ("airplay", "audio")
    )
    await _cut(scenario, plug)
    await scenario.unregister(den)
    await scenario.idle()
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, False)

    play = await _play(scenario, player_id)

    assert not play.done(), play.exception()
    assert plug.commands == [True]
    await _stop(scenario, player_id)
    with pytest.raises(PlayerUnavailableError, match="is switched off"):
        await play
    play = await _play(scenario, player_id)
    assert not play.done(), play.exception()
    await scenario.advance(10)
    assert not play.done(), play.exception()
    assert den.commands == den_airplay.commands == []
    await scenario.register(den)
    await scenario.advance(1)
    await play
    await scenario.idle()

    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    assert plug.commands == [True]
    await _stop(scenario, player_id)


@pytest.mark.parametrize(
    "away", ["its provider is disabled", "its provider is not loaded", "it is switched off"]
)
async def test_a_speaker_whose_own_protocol_can_not_come_back_is_told_for_by_its_receiver(
    scenario: Scenario, away: str
) -> None:
    """
    A speaker whose own protocol can not come back is there while its receiver reaches it.

    Den has its Squeezelite protocol and an AirPlay receiver, and is on a smart plug. The user
    disabled the Squeezelite provider, or it is not loaded (as after a failed load), or the user
    switched the Squeezelite protocol off as an output of Den. Den then plays through its
    receiver, which tells whether Den is there, as for a speaker of receivers alone: with the
    plug on, a play starts at once; with the plug cut and the receiver no longer reaching Den, a
    play switches the plug on, waits, and plays once the receiver is back.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("squeezelite", "native"), ("airplay", "audio")
    )
    squeezelite = scenario.provider("squeezelite")
    if away == "its provider is disabled":
        await scenario.mass.config.save_provider_config(
            "squeezelite", {"enabled": False}, instance_id=squeezelite.instance_id
        )
    elif away == "its provider is not loaded":
        await scenario.mass.unload_provider(squeezelite.instance_id)
    else:
        await scenario.mass.config.save_protocol_config(den.protocol_id, {"enabled": False})
    await scenario.idle()
    assert scenario.player_of(den) is None

    await scenario.play(player_id)

    assert _starts(den_airplay) == 1
    await _stop(scenario, player_id)
    await _cut(scenario, plug, den_airplay)
    play = await _play(scenario, player_id)
    assert not play.done(), play.exception()
    await _reach(scenario, den_airplay)
    await play
    await scenario.idle()

    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
    assert (_starts(den), _starts(den_airplay)) == (0, 2)
    assert plug.commands == [True]
    await _stop(scenario, player_id)


async def test_a_speaker_whose_plug_is_cut_is_checked_and_waited_for(scenario: Scenario) -> None:
    """
    A speaker whose plug is cut is checked, and a play waits for its device.

    Den has its Sonos protocol, which can check its device, and an AirPlay receiver, which can
    not, and is on a smart plug that is cut while neither has noticed. Shortly after, the Sonos
    protocol is asked to check: the device does not answer, and the protocol reads unavailable.
    A play 10 s after the cut switches the plug on and waits, nothing is sent to the device,
    and Den plays once, through its Sonos protocol, once that reaches it again.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    den.device_answers = False
    cut_at = scenario.mass.loop.time()
    await scenario.switch(plug, False)
    await scenario.advance(10)

    assert den.device_checks == [cut_at + DEVICE_CHECK_DELAY]
    assert den_airplay.device_checks == []
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, False)

    play = await _play(scenario, player_id)

    assert not play.done(), play.exception()
    assert plug.commands == [True]
    await scenario.advance(10)
    assert not play.done(), play.exception()
    assert den.commands == den_airplay.commands == []

    await _boot(scenario, den)
    await play
    await scenario.idle()

    assert _state(scenario, player_id).playback_state == PlaybackState.PLAYING
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    assert plug.commands == [True]
    await _stop(scenario, player_id)


async def test_a_speaker_that_kept_its_power_passes_the_check_and_plays_at_once(
    scenario: Scenario,
) -> None:
    """
    A speaker that kept its power while its plug reads off passes the check and plays at once.

    Den's plug reads off while the device is still there: the plug only feeds an amplifier, or
    Home Assistant can not reach the plug and reports it as off. The Sonos protocol is asked to
    check and the device answers, so Den stays reachable: a play switches the plug on and
    starts at once.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    den.device_answers = True
    await scenario.switch(plug, False)
    await scenario.advance(10)

    assert len(den.device_checks) == 1
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, False)

    await scenario.play(player_id)

    assert plug.commands == [True]
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    await _stop(scenario, player_id)


async def test_a_plug_switched_on_again_before_the_check_is_not_checked(
    scenario: Scenario,
) -> None:
    """
    A plug that is switched on again before the check leaves its speaker unchecked.

    Den's plug is switched off and on again within a few seconds, as Home Assistant can report
    a plug while it reloads. Den is not checked, nothing waits, and a play starts at once.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    den.device_answers = False
    await scenario.switch(plug, False)
    await scenario.advance(DEVICE_CHECK_DELAY - 3)
    await scenario.switch(plug, True)
    await scenario.idle()

    assert den.device_checks == []
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, True)

    await scenario.play(player_id)

    assert plug.commands == []
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    await _stop(scenario, player_id)


@pytest.mark.parametrize("failure", ["raises", "does not come back"])
async def test_a_device_check_that_fails_changes_nothing(
    scenario: Scenario, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    """
    A device check that raises, or does not come back, changes nothing.

    Den's plug is cut, and the check of its Sonos protocol raises, or does not come back and is
    given up on after 10 s. That is logged at debug, and Den reads as before: a play switches
    the plug on and starts at once, as for a protocol that can not check.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    checks: list[str] = []

    async def _check() -> bool:
        checks.append("asked")
        if failure == "raises":
            raise ConnectionError("no route to the speaker")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            checks.append("given up on")
            raise
        return True

    with (
        patch.object(den, "check_device", _check),
        caplog.at_level(logging.DEBUG, logger="music_assistant.players"),
    ):
        await scenario.switch(plug, False)
        await scenario.advance(DEVICE_CHECK_DELAY + DEVICE_CHECK_TIMEOUT + 1)

    if failure == "raises":
        assert checks == ["asked"]
        assert "The device check of player Den failed: no route to the speaker" in caplog.text
    else:
        assert checks == ["asked", "given up on"]
        assert "The device check of player Den did not come back" in caplog.text
    state = _state(scenario, player_id)
    assert (state.available, state.powered) == (True, False)

    await scenario.play(player_id)

    assert plug.commands == [True]
    assert (_starts(den), _starts(den_airplay)) == (1, 0)
    await _stop(scenario, player_id)


@pytest.mark.parametrize("goes", ["its protocol is lost", "the player is removed", "disabled"])
async def test_a_running_device_check_is_called_off_when_its_protocol_goes(
    scenario: Scenario, goes: str
) -> None:
    """
    A device check that still runs when its protocol is taken off its player is called off.

    Den's plug is cut and its Sonos protocol is asked to check its device, which takes long.
    Meanwhile the protocol is taken off the player: its provider lost the device, or the user
    removed or disabled the player. The check is called off, so it changes nothing afterwards.
    """
    (den, _), plug, player_id = await _den(scenario, ("sonos", "native"), ("airplay", "audio"))
    started = asyncio.Event()
    outcome: list[str] = []

    async def _check() -> bool:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            outcome.append("called off")
            raise
        return True

    with patch.object(den, "check_device", _check):
        await scenario.switch(plug, False)
        await scenario.advance(DEVICE_CHECK_DELAY + 1)
        assert started.is_set()

        if goes == "its protocol is lost":
            await scenario.unregister(den)
        elif goes == "the player is removed":
            await scenario.remove(player_id)
        else:
            await scenario.disable(player_id)

    assert outcome == ["called off"]


@pytest.mark.parametrize("goes", ["removed", "disabled"])
async def test_a_player_that_goes_before_its_check_is_not_checked(
    scenario: Scenario, goes: str
) -> None:
    """
    A player removed or disabled before the check after its power off is not checked.

    Den's plug is cut, and the user removes or disables Den before its check is due. What was
    planned for the check goes with the player, and Den's device is never checked.
    """
    (den, _), plug, player_id = await _den(scenario, ("sonos", "native"), ("airplay", "audio"))
    den.device_answers = False
    await scenario.switch(plug, False)
    await scenario.advance(DEVICE_CHECK_DELAY - 3)

    if goes == "removed":
        await scenario.remove(player_id)
    else:
        await scenario.disable(player_id)

    planned = [
        timer_id
        for timer_id in scenario.mass._tracked_timers
        if timer_id.startswith("device_check") and timer_id.endswith(player_id)
    ]
    assert planned == []
    await scenario.advance(10)
    assert den.device_checks == []


@pytest.mark.parametrize("group", GROUPS)
async def test_a_group_waits_for_the_own_protocol_of_a_switched_off_member(
    scenario: Scenario, group: str
) -> None:
    """
    A group waits for a switched-off member until its own protocol is back, and starts once.

    Den, a member of Downstairs, has an AirPlay receiver next to its Sonos protocol, and is
    on a smart plug that is cut. Its Sonos protocol reads unavailable, its AirPlay receiver
    never noticed. A play of Downstairs switches the plug on and waits: no speaker gets a
    command. Once Den's Sonos protocol is back, Downstairs starts once, on all members.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den = speakers[2]
    den_airplay = await _receiver(scenario, den, 12)
    plug = await _plug(scenario, player_ids[2])
    await _cut(scenario, plug, den)

    play = await _play(scenario, group_id)

    assert not play.done(), play.exception()
    assert plug.commands == [True]
    await scenario.advance(10)
    assert not play.done(), play.exception()
    assert [speaker.commands for speaker in (*speakers, den_airplay)] == [[], [], [], []]
    await _reach(scenario, den)
    await play
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    if GROUPS[group][0] == "sync_group":
        # the leader gets the stream, and the others are joined to it once
        assert sorted(_starts(speaker) for speaker in speakers) == [0, 0, 1]
        assert sum(len(_grouping_commands(speaker)) for speaker in speakers) == 1
    else:
        assert [_starts(speaker) for speaker in speakers] == [1, 1, 1]
    assert den_airplay.commands == []
    assert plug.commands == [True]
    await _stop(scenario, group_id)


async def test_a_group_over_airplay_waits_for_a_speaker_whose_receiver_reads_available(
    scenario: Scenario,
) -> None:
    """
    A group over AirPlay waits for a switched-off speaker whose AirPlay receiver reads available.

    Lounge and Study are speakers of two makes that group over their AirPlay receivers, in
    the sync group Downstairs. Lounge is on a smart plug that is cut: its Sonos protocol
    reads unavailable, its AirPlay receiver never noticed. A play of Downstairs switches the
    plug on and waits, and no receiver gets a command. Once Lounge's Sonos protocol is back,
    Downstairs starts once, on both speakers.
    """
    lounge_airplay, lounge_id, study_id = await _mixed_makes(scenario)
    lounge, study_airplay = scenario.protocol("sonos_lounge"), scenario.protocol("airplay_study")
    group_id = await _group(scenario, "sync_group", lounge_id, study_id, dynamic=False)
    plug = await _plug(scenario, lounge_id)
    await _cut(scenario, plug, lounge)

    play = await _play(scenario, group_id)

    assert not play.done(), play.exception()
    assert plug.commands == [True]
    await scenario.advance(10)
    assert not play.done(), play.exception()
    assert lounge_airplay.commands == study_airplay.commands == []
    await _reach(scenario, lounge)
    await play
    await scenario.idle()

    assert _plays_along(scenario, group_id, lounge_id)
    assert _state(scenario, study_id).synced_to == lounge_id
    assert _state(scenario, study_id).active_group == group_id
    # the leader gets the stream through its receiver, and Study is joined to it once
    assert (_starts(lounge_airplay), _starts(study_airplay)) == (1, 0)
    assert _grouping_commands(lounge_airplay) == [("set_members", ["airplay_study"], None)]
    assert lounge.commands == study_airplay.commands == []
    assert plug.commands == [True]
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", GROUPS)
async def test_a_new_start_waits_again_for_a_member_whose_own_protocol_is_not_back(
    scenario: Scenario, group: str
) -> None:
    """
    A group that is stopped while it waits, and played again, waits for the member's own protocol.

    Den's plug is cut and its Sonos protocol reads unavailable, while its AirPlay receiver
    never noticed. The stop of Downstairs leaves the plug on, and Den shows as available and
    powered on through its receiver while its device still boots. The new play does not
    start without Den's own protocol and does not switch the plug again: Downstairs starts
    on all members once that protocol is back.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den, den_id = speakers[2], player_ids[2]
    den_airplay = await _receiver(scenario, den, 12)
    plug = await _plug(scenario, den_id)
    await _cut(scenario, plug, den)
    play = await _play(scenario, group_id)
    await _stop(scenario, group_id)
    with pytest.raises(PlayerUnavailableError, match="was called off"):
        await play
    state = _state(scenario, den_id)
    assert (state.available, state.powered) == (True, True)

    play = await _play(scenario, group_id)

    assert not play.done(), play.exception()
    await scenario.advance(10)
    assert not play.done(), play.exception()
    assert [_starts(speaker) for speaker in (*speakers, den_airplay)] == [0, 0, 0, 0]
    await _reach(scenario, den)
    await play
    await scenario.idle()

    assert plug.commands == [True]
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    assert _starts(den_airplay) == 0
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", GROUPS)
async def test_a_group_whose_member_was_cut_waits_for_it_and_starts_once(
    scenario: Scenario, group: str
) -> None:
    """
    A group whose member's plug was cut a moment ago waits for that member and starts once.

    Den, a member of Downstairs, is on a smart plug that is cut while its Sonos protocol has
    not noticed. The check finds the device gone. A play of Downstairs 10 s after the cut
    switches the plug on and waits, and no speaker gets a command. Once Den's device is back,
    Downstairs starts once, on all members.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den = speakers[2]
    plug = await _plug(scenario, player_ids[2])
    den.device_answers = False
    await scenario.switch(plug, False)
    await scenario.advance(10)
    assert len(den.device_checks) == 1

    play = await _play(scenario, group_id)

    assert not play.done(), play.exception()
    assert plug.commands == [True]
    await scenario.advance(10)
    assert not play.done(), play.exception()
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    await _boot(scenario, den)
    await play
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    if GROUPS[group][0] == "sync_group":
        # the leader gets the stream, and the others are joined to it once
        assert sorted(_starts(speaker) for speaker in speakers) == [0, 0, 1]
        assert sum(len(_grouping_commands(speaker)) for speaker in speakers) == 1
    else:
        assert [_starts(speaker) for speaker in speakers] == [1, 1, 1]
    assert plug.commands == [True]
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", GROUPS)
async def test_a_start_asks_the_protocol_of_a_member_to_reach_its_device(
    scenario: Scenario, group: str
) -> None:
    """
    While a start waits for a member, its protocol is asked to reach the device again.

    Den, a member of Downstairs, is on a smart plug that is cut, and its Sonos protocol
    noticed. Once its device is up again, the protocol would only reach it on a reconnect
    minutes later. A play of Downstairs switches the plug on and waits, and meanwhile asks
    Den's Sonos protocol every few seconds to reach the device. The device is up 20 s after
    the play: the next ask reaches it, and Downstairs starts once, on all members, long before
    the play would give up on Den.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den = speakers[2]
    plug = await _plug(scenario, player_ids[2])
    den.device_answers = False
    await _cut(scenario, plug, den)
    assert den.device_checks == []

    play = await _play(scenario, group_id)

    await scenario.advance(20)
    assert not play.done(), play.exception()
    assert len(den.device_checks) >= 3
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    # the device is up, and its protocol does not reach it by itself
    den.device_answers = True
    await scenario.advance(DEVICE_CHECK_INTERVAL + 1)

    assert play.done()
    await play
    await scenario.idle()
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    assert plug.commands == [True]
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
async def test_a_group_player_on_a_power_control_is_not_checked(
    scenario: Scenario, group: str
) -> None:
    """
    A group player on a power control has no device of its own to check.

    Downstairs is on a smart plug that is switched off. Its own protocol is asked to check,
    and it can not: nothing changes, the group stays available and powered off, and none of
    its members is checked.
    """
    group_id, speakers, _ = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    plug = await _plug(scenario, group_id)
    group_player = scenario.mass.players.get_player(group_id)
    assert group_player is not None
    own = group_player.own_protocol
    assert own is not None
    check = AsyncMock(wraps=own.check_device)

    with patch.object(own, "check_device", check):
        await scenario.switch(plug, False)
        await scenario.advance(10)

    check.assert_awaited_once_with()
    assert await own.check_device() is None
    state = _state(scenario, group_id)
    assert (state.available, state.powered) == (True, False)
    assert [speaker.device_checks for speaker in speakers] == [[], [], []]
    assert plug.commands == []
