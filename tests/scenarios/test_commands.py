"""Commands that meet on one player: each waits for its turn, or fails when it does not come."""

from __future__ import annotations

import asyncio
import logging

import pytest
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature
from music_assistant_models.errors import ResourceBusyError
from music_assistant_models.player import PlayerMedia, PlayerSource

from tests.common import record_lock_waits

from .harness import (
    DEFAULT_FEATURES,
    MUSIC_PROVIDER,
    TEST_TRACK_URI,
    FakePlayerProvider,
    FakeProtocol,
    Scenario,
)
from .test_grouping import _player_id, _speakers

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

    async def _slow_command(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(seconds)
        await handle(speaker, *args, **kwargs)

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


async def _load_test_music(scenario: Scenario) -> None:
    """Load the server's test music provider, which play() loads on first use."""
    if scenario.mass.get_provider(MUSIC_PROVIDER) is None:
        await scenario.mass.config._create_provider_instance(MUSIC_PROVIDER, {})


def _synced_to(scenario: Scenario, player_id: str) -> str | None:
    """Return the player a player is synced to."""
    state = scenario.player(player_id)
    assert state is not None
    return state.synced_to


def _sequence(provider: FakePlayerProvider, *steps: tuple[FakeProtocol, str]) -> list[int]:
    """Return where each (device, command) first shows in the provider's timeline of commands."""
    timeline = [(protocol_id, command[0]) for protocol_id, command in provider._timeline]
    return [timeline.index((protocol.protocol_id, name)) for protocol, name in steps]


@pytest.mark.parametrize("operation", ["transfer", "hand-over"])
async def test_a_player_a_queue_moves_to_joins_another_group_once_the_move_is_done(
    scenario: Scenario, operation: str
) -> None:
    """
    A player that a queue moves to is added to another group only once it plays the queue.

    Den takes 40 s to stop. Kitchen, the player the queue of Den moves to (by a transfer, or
    as Den's group member when Den leaves its group), is added to Office's group meanwhile.
    The add must not take Kitchen from under the move, which would end with nobody playing.
    """
    sonos, (den, kitchen, office) = await _speakers(scenario, "Den", "Kitchen", "Office")
    den_id, kitchen_id, office_id = (_player_id(scenario, p) for p in (den, kitchen, office))
    players = scenario.mass.players
    if operation == "hand-over":
        await players.cmd_set_members(den_id, player_ids_to_add=[kitchen_id])
        await scenario.idle()
    await scenario.play(den_id)
    _slow(den, "stop", 40)
    move = await scenario.command(
        scenario.mass.player_queues.transfer_queue(den_id, kitchen_id)
        if operation == "transfer"
        else players.cmd_ungroup(den_id)
    )

    await scenario.advance(10)
    join = await scenario.command(
        players.cmd_set_members(office_id, player_ids_to_add=[kitchen_id])
    )
    assert not join.done()

    await scenario.advance(30)
    await move
    await join
    await scenario.idle()

    play, add = _sequence(sonos, (kitchen, "play_media"), (office, "set_members"))
    assert play < add
    assert _synced_to(scenario, kitchen_id) == office_id
    assert _plays(scenario, den_id) == (PlaybackState.IDLE, [])


async def test_a_player_that_is_still_starting_a_play_joins_a_group_after_it(
    scenario: Scenario,
) -> None:
    """A play that takes 40 s at Kitchen is done before Kitchen is added to Office's group."""
    sonos, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    kitchen_id, office_id = (_player_id(scenario, p) for p in (kitchen, office))
    await _load_test_music(scenario)
    _slow(kitchen, "play_media", 40)
    play = await scenario.command(
        scenario.mass.player_queues.play_media(kitchen_id, TEST_TRACK_URI)
    )

    await scenario.advance(10)
    join = await scenario.command(
        scenario.mass.players.cmd_set_members(office_id, player_ids_to_add=[kitchen_id])
    )
    assert not join.done()

    await scenario.advance(30)
    await play
    await join
    await scenario.idle()

    play_at, add_at = _sequence(sonos, (kitchen, "play_media"), (office, "set_members"))
    assert play_at < add_at
    assert _synced_to(scenario, kitchen_id) == office_id


async def test_two_groups_that_add_the_same_player_at_once_take_turns(
    scenario: Scenario,
) -> None:
    """
    Kitchen added to Office's group and to Den's at once ends in the group added last.

    Office takes 40 s to add it. The add to Den's group waits for that, then takes Kitchen
    over, rather than both changing Kitchen at the same time.
    """
    _sonos, (den, kitchen, office) = await _speakers(scenario, "Den", "Kitchen", "Office")
    den_id, kitchen_id, office_id = (_player_id(scenario, p) for p in (den, kitchen, office))
    players = scenario.mass.players
    _slow(office, "set_members", 40)
    to_office = await scenario.command(
        players.cmd_set_members(office_id, player_ids_to_add=[kitchen_id])
    )
    to_den = await scenario.command(players.cmd_set_members(den_id, player_ids_to_add=[kitchen_id]))

    await scenario.advance(10)
    assert not to_den.done()

    await scenario.advance(30)
    await to_office
    await to_den
    await scenario.idle()

    assert _synced_to(scenario, kitchen_id) == den_id
    office_state = scenario.player(office_id)
    assert office_state is not None
    assert kitchen_id not in office_state.group_members


async def test_adding_a_player_that_stays_busy_fails_and_changes_no_group(
    scenario: Scenario,
) -> None:
    """
    An add of a player busy with a play that never returns fails as busy after 120 s.

    No group changed: Office has no members and Kitchen is in no group.
    """
    _sonos, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    kitchen_id, office_id = (_player_id(scenario, p) for p in (kitchen, office))
    await _load_test_music(scenario)
    answered = asyncio.Event()

    async def _play_media_that_never_returns(media: PlayerMedia) -> None:
        await answered.wait()
        await FakeProtocol.play_media(kitchen, media)

    kitchen.play_media = _play_media_that_never_returns  # type: ignore[method-assign]
    scenario.expect_log("Gave up waiting 120s for the playback lock of player Kitchen")
    play = await scenario.command(
        scenario.mass.player_queues.play_media(kitchen_id, TEST_TRACK_URI)
    )
    join = await scenario.command(
        scenario.mass.players.cmd_set_members(office_id, player_ids_to_add=[kitchen_id])
    )

    await scenario.advance(120)
    with pytest.raises(ResourceBusyError) as busy:
        await join

    assert busy.value.translation_args == ["Kitchen"]
    assert [command for command in office.commands if command[0] == "set_members"] == []
    assert _synced_to(scenario, kitchen_id) is None

    answered.set()
    await scenario.settle()
    await play
