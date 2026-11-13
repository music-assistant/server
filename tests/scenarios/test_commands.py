"""Commands that meet on one player: each waits for its turn, or fails when it does not come."""

from __future__ import annotations

import asyncio
import logging

import pytest
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature
from music_assistant_models.errors import ResourceBusyError
from music_assistant_models.player import PlayerMedia, PlayerSource

from tests.common import record_lock_waits

from .harness import DEFAULT_FEATURES, TEST_TRACK_URI, FakeProtocol, Scenario

DEN = {IdentifierType.MAC_ADDRESS: "00:11:22:33:44:55", IdentifierType.IP_ADDRESS: "192.168.1.30"}
KITCHEN = {
    IdentifierType.MAC_ADDRESS: "00:11:22:33:44:66",
    IdentifierType.IP_ADDRESS: "192.168.1.31",
}
BATHROOM = {IdentifierType.MAC_ADDRESS: "B8:27:EB:AA:BB:CC"}
# another track of the server's test music provider than the one play() plays
OTHER_TRACK_URI = "test://track/1_1_1"

MEDIA = PlayerMedia(uri="http://ma.local/stream/track.flac")


async def _speaker_with_an_input(scenario: Scenario) -> tuple[FakeProtocol, str]:
    """Return a native speaker with an HDMI input, and the id of its player."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_DEN", "Den", identifiers=DEN)
    await scenario.attach(speaker)
    await scenario.report(
        speaker,
        supported_features={*DEFAULT_FEATURES, PlayerFeature.SELECT_SOURCE},
        source_list=[PlayerSource(id="hdmi", name="HDMI")],
    )
    await scenario.idle()
    return speaker, scenario.single_player(speaker).player_id


async def _two_speakers(scenario: Scenario) -> tuple[FakeProtocol, str, FakeProtocol, str]:
    """Return the Den and the Kitchen speaker, each with the id of its player."""
    sonos = await scenario.add_provider("sonos")
    den = sonos.protocol("RINCON_DEN", "Den", identifiers=DEN)
    kitchen = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(den)
    await scenario.attach(kitchen)
    await scenario.idle()
    return (
        den,
        scenario.single_player(den).player_id,
        kitchen,
        scenario.single_player(kitchen).player_id,
    )


def _slow(speaker: FakeProtocol, command: str, seconds: float) -> None:
    """Let a command of the device take the given time before it does what it does."""
    handle = getattr(FakeProtocol, command)

    async def _slow_command(*args: object) -> None:
        await asyncio.sleep(seconds)
        await handle(speaker, *args)

    setattr(speaker, command, _slow_command)


def _plays(scenario: Scenario, player_id: str) -> tuple[PlaybackState, list[str | None]]:
    """Return the playback state of a player and the uris of what its queue holds."""
    state = scenario.player(player_id)
    queue = scenario.queue(player_id)
    assert state is not None
    assert queue is not None
    items = scenario.mass.player_queues.items(player_id)
    return state.playback_state, [
        item.media_item.uri if item.media_item else None for item in items
    ]


def _names(speaker: FakeProtocol) -> list[str]:
    """Return the names of the commands the device received, in order."""
    return [command[0] for command in speaker.commands]


async def test_a_command_waits_for_the_command_that_holds_the_player(
    scenario: Scenario,
) -> None:
    """
    A command sent while another one still works on the player reaches the device after it.

    The play takes 40 s at the device; the input switch sent right behind it must not reach
    the device while the play is still under way, where the play would switch it back.
    """
    speaker, player_id = await _speaker_with_an_input(scenario)

    async def _slow_play_media(media: PlayerMedia) -> None:
        await asyncio.sleep(40)
        await FakeProtocol.play_media(speaker, media)

    speaker.play_media = _slow_play_media  # type: ignore[method-assign]
    play = await scenario.command(scenario.mass.players.play_media(player_id, MEDIA))
    switch = await scenario.command(scenario.mass.players.select_source(player_id, "hdmi"))

    await scenario.advance(39)
    assert "select_source" not in _names(speaker)
    assert not switch.done()

    await scenario.advance(1)
    await play
    await switch

    commands = _names(speaker)
    assert commands.index("play_media") < commands.index("select_source")


async def test_a_command_behind_a_command_that_never_returns_fails_as_busy(
    scenario: Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A command that waits 120 s for a player that stays busy fails, and is not sent.

    The error tells the user the player is still busy, the log names what the command that
    holds the player waits for, and the player takes commands again once that one returns.
    """
    speaker, player_id = await _speaker_with_an_input(scenario)
    answered = asyncio.Event()

    async def _play_media_that_never_returns(media: PlayerMedia) -> None:
        await answered.wait()
        await FakeProtocol.play_media(speaker, media)

    speaker.play_media = _play_media_that_never_returns  # type: ignore[method-assign]
    scenario.expect_log("Gave up waiting 120s for the playback lock of player Den")
    play = await scenario.command(scenario.mass.players.play_media(player_id, MEDIA))
    switch = await scenario.command(scenario.mass.players.select_source(player_id, "hdmi"))

    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        await scenario.advance(119)
        assert not switch.done()
        await scenario.advance(1)
        with pytest.raises(ResourceBusyError) as busy:
            await switch

    assert busy.value.translation_key == "player_busy"
    assert busy.value.translation_owner == "core.players"
    assert busy.value.translation_args == ["Den"]
    assert "select_source" not in _names(speaker)
    assert "_play_media_that_never_returns" in caplog.text

    answered.set()
    await scenario.settle()
    await play
    await scenario.mass.players.select_source(player_id, "hdmi")
    await scenario.settle()

    assert _names(speaker)[-1] == "select_source"


async def test_a_stop_sent_to_the_target_of_a_transfer_stops_what_the_transfer_started(
    scenario: Scenario,
) -> None:
    """
    A stop on the player a queue is moving to waits for the move, and then stops the player.

    The source takes 40 s to stop. A stop sent to the target meanwhile must not return before
    the move is done, after which the move would start the target anyway.
    """
    den, den_id, kitchen, kitchen_id = await _two_speakers(scenario)
    await scenario.play(den_id)
    _slow(den, "stop", 40)
    transfer = await scenario.command(
        scenario.mass.player_queues.transfer_queue(den_id, kitchen_id)
    )

    await scenario.advance(10)
    stop = await scenario.command(scenario.mass.player_queues.stop(kitchen_id))
    assert not stop.done()

    await scenario.advance(30)
    await transfer
    await stop
    await scenario.idle()

    assert _names(kitchen)[-1] == "stop"
    assert _plays(scenario, kitchen_id)[0] == PlaybackState.IDLE
    assert _plays(scenario, den_id) == (PlaybackState.IDLE, [])


async def test_a_resume_that_is_still_starting_is_done_before_a_transfer_moves_its_queue(
    scenario: Scenario,
) -> None:
    """
    A transfer of a queue that is still starting moves it once it plays: one player plays.

    The resume of Den takes 40 s at the device. A transfer to Kitchen sent meanwhile waits for
    it, then stops Den and starts Kitchen; it must not leave Den playing an empty queue.
    """
    den, den_id, _kitchen, kitchen_id = await _two_speakers(scenario)
    await scenario.play(den_id)
    await scenario.mass.player_queues.stop(den_id)
    await scenario.idle()
    _slow(den, "play_media", 40)
    resume = await scenario.command(scenario.mass.player_queues.resume(den_id))
    transfer = await scenario.command(
        scenario.mass.player_queues.transfer_queue(den_id, kitchen_id, auto_play=True)
    )

    await scenario.advance(40)
    await resume
    await transfer
    await scenario.idle()

    assert _plays(scenario, kitchen_id) == (PlaybackState.PLAYING, [TEST_TRACK_URI])
    assert _plays(scenario, den_id) == (PlaybackState.IDLE, [])
    assert _names(den)[-1] == "stop"


async def test_a_play_on_the_target_that_is_still_starting_is_done_before_a_transfer_replaces_it(
    scenario: Scenario,
) -> None:
    """
    A transfer to a player that is still starting a play waits for it before it moves the queue.

    The play takes 40 s at Kitchen. The queue of Den, moved there meanwhile, must not replace the
    queue the play is still starting.
    """
    _den, den_id, kitchen, kitchen_id = await _two_speakers(scenario)
    await scenario.play(den_id)
    _slow(kitchen, "play_media", 40)
    play = await scenario.command(
        scenario.mass.player_queues.play_media(kitchen_id, OTHER_TRACK_URI)
    )
    transfer = await scenario.command(
        scenario.mass.player_queues.transfer_queue(den_id, kitchen_id)
    )

    await scenario.advance(10)
    assert _plays(scenario, kitchen_id)[1] == [OTHER_TRACK_URI]
    assert _plays(scenario, den_id) == (PlaybackState.PLAYING, [TEST_TRACK_URI])

    await scenario.advance(30)
    await play
    await scenario.advance(40)
    await transfer
    await scenario.idle()

    assert _plays(scenario, kitchen_id) == (PlaybackState.PLAYING, [TEST_TRACK_URI])
    assert _plays(scenario, den_id) == (PlaybackState.IDLE, [])


async def test_a_play_on_the_source_waits_until_the_target_of_a_transfer_plays(
    scenario: Scenario,
) -> None:
    """
    A play on the player a queue moves away from waits until the player it moves to plays.

    Kitchen takes 40 s to start the moved queue. A play on Den sent meanwhile, after Den has
    stopped and its queue is empty, still waits for the move to end, and then plays on Den.
    """
    _den, den_id, _kitchen, kitchen_id = await _two_speakers(scenario)
    await scenario.play(den_id)
    _slow(_kitchen, "play_media", 40)
    transfer = await scenario.command(
        scenario.mass.player_queues.transfer_queue(den_id, kitchen_id)
    )

    await scenario.advance(10)
    assert _plays(scenario, den_id) == (PlaybackState.IDLE, [])
    play = await scenario.command(scenario.mass.player_queues.play_media(den_id, OTHER_TRACK_URI))
    assert not play.done()

    await scenario.advance(30)
    await transfer
    await play
    await scenario.idle()

    assert _plays(scenario, kitchen_id) == (PlaybackState.PLAYING, [TEST_TRACK_URI])
    assert _plays(scenario, den_id) == (PlaybackState.PLAYING, [OTHER_TRACK_URI])


async def test_a_transfer_to_a_player_that_stays_busy_fails_and_leaves_both_as_they_were(
    scenario: Scenario,
) -> None:
    """
    A transfer to a player busy with a play that never returns fails as busy after 120 s.

    Nothing has moved by then: the source still plays its queue, the target still holds the
    queue of its play.
    """
    den, den_id, kitchen, kitchen_id = await _two_speakers(scenario)
    await scenario.play(den_id)
    answered = asyncio.Event()

    async def _play_media_that_never_returns(media: PlayerMedia) -> None:
        await answered.wait()
        await FakeProtocol.play_media(kitchen, media)

    kitchen.play_media = _play_media_that_never_returns  # type: ignore[method-assign]
    scenario.expect_log("Gave up waiting 120s for the playback lock of player Kitchen")
    play = await scenario.command(
        scenario.mass.player_queues.play_media(kitchen_id, OTHER_TRACK_URI)
    )
    den_commands = len(den.commands)
    transfer = await scenario.command(
        scenario.mass.player_queues.transfer_queue(den_id, kitchen_id)
    )

    await scenario.advance(120)
    with pytest.raises(ResourceBusyError) as busy:
        await transfer

    assert busy.value.translation_args == ["Kitchen"]
    assert _plays(scenario, den_id) == (PlaybackState.PLAYING, [TEST_TRACK_URI])
    assert len(den.commands) == den_commands
    assert _plays(scenario, kitchen_id)[1] == [OTHER_TRACK_URI]

    answered.set()
    await scenario.settle()
    await play


async def test_a_transfer_to_a_switched_off_player_holds_neither_player_while_it_powers_on(
    scenario: Scenario,
) -> None:
    """
    A queue moved to a player switched off at its plug switches the plug on first, holding nothing.

    The device takes its time to come back. A seek on the source meanwhile runs at once, and
    once the device is back the queue moves there and plays.
    """
    _den, den_id, _kitchen, _kitchen_id = await _two_speakers(scenario)
    squeezelite = await scenario.add_provider("squeezelite")
    pi = squeezelite.protocol("b8:27:eb:aa:bb:cc", "Bathroom", identifiers=BATHROOM)
    await scenario.attach(pi)
    bathroom_id = scenario.single_player(pi).player_id
    plug = await scenario.add_power_control("switch.bathroom_plug", "Bathroom plug", powered=True)
    await scenario.configure_player(bathroom_id, power_control=plug.control_id)
    await scenario.switch(plug, False)
    await scenario.detach(pi)
    await scenario.idle()
    await scenario.play(den_id)
    transfer = await scenario.command(
        scenario.mass.player_queues.transfer_queue(den_id, bathroom_id)
    )
    assert plug.commands == [True]
    assert not transfer.done()

    waits = record_lock_waits(scenario.mass.players)
    seek = await scenario.command(scenario.mass.player_queues.seek(den_id, 10))

    assert seek.done()
    await seek
    assert waits == []
    await scenario.attach(pi)
    # the power on looks for the device every 0.5 s
    await scenario.advance(1)
    await transfer
    await scenario.idle()

    assert _plays(scenario, bathroom_id) == (PlaybackState.PLAYING, [TEST_TRACK_URI])
    assert _plays(scenario, den_id) == (PlaybackState.IDLE, [])
