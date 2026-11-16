"""Which protocol tells whether a speaker on a power control is there: the one that reports for it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerUnavailableError

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
    The known window: a speaker reads as on the network until its own protocol notices the cut.

    Den's plug is cut and none of its protocols has noticed yet. The player shows as
    available and powered off, and nothing tells that its device is gone: a play switches
    the plug on and starts at once, on a device that still boots.
    """
    (den, den_airplay), plug, player_id = await _den(
        scenario, ("sonos", "native"), ("airplay", "audio")
    )
    await _cut(scenario, plug)
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
