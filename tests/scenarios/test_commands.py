"""Commands that meet on one player: each waits for its turn, or fails when it does not come."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    QueueOption,
)
from music_assistant_models.errors import PlayerCommandFailed, ResourceBusyError
from music_assistant_models.player import PlayerMedia, PlayerSource

from music_assistant.constants import ATTR_PLAY_ACTION_IN_PROGRESS
from tests.common import record_lock_waits

from .harness import DEFAULT_FEATURES, TEST_TRACK_URI, FakeProtocol, Scenario
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
    await scenario.register(speaker)
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
    await scenario.register(den)
    await scenario.register(kitchen)
    await scenario.idle()
    return (
        den,
        scenario.single_player(den).player_id,
        kitchen,
        scenario.single_player(kitchen).player_id,
    )


def _slow(speaker: FakeProtocol, command: str, seconds: float, *, once: bool = False) -> None:
    """
    Let a command of the device take the given time before it does what it does.

    :param once: Only the first time the device gets the command.
    """
    handle = getattr(FakeProtocol, command)
    slowed = False

    async def _slow_command(*args: object, **kwargs: object) -> None:
        nonlocal slowed
        if not (once and slowed):
            slowed = True
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
    await scenario.register(pi)
    bathroom_id = scenario.single_player(pi).player_id
    plug = await scenario.add_power_control("switch.bathroom_plug", "Bathroom plug", powered=True)
    await scenario.configure_player(bathroom_id, power_control=plug.control_id)
    await scenario.switch(plug, False)
    await scenario.unregister(pi)
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
    await scenario.register(pi)
    # the power on looks for the device every 0.5 s
    await scenario.advance(1)
    await transfer
    await scenario.idle()

    assert _plays(scenario, bathroom_id) == (PlaybackState.PLAYING, [TEST_TRACK_URI])
    assert _plays(scenario, den_id) == (PlaybackState.IDLE, [])


# what a client sees of a player: its playback, the uris its queue holds, the uri of what it
# plays, the name of the player it is synced to and of the group holding it
type _Seen = tuple[PlaybackState, list[str | None], str | None, str | None, str | None]


def _seen(scenario: Scenario, names: dict[str, str]) -> dict[str, _Seen]:
    """
    Return what a client sees of each player, by name.

    :param names: The name of each player, by its id.
    """
    seen: dict[str, _Seen] = {}
    for player_id, name in names.items():
        state = scenario.player(player_id)
        assert state is not None
        media = state.current_media.uri if state.current_media else None
        synced_to = names.get(state.synced_to) if state.synced_to else None
        group = names.get(state.active_group) if state.active_group else None
        seen[name] = (state.playback_state, _plays(scenario, player_id)[1], media, synced_to, group)
    return seen


def _rendered(*speakers: FakeProtocol) -> dict[str, tuple[PlaybackState, str | None]]:
    """Return what each device renders, by its name."""
    return {
        speaker.name or speaker.protocol_id: (
            speaker.playback_state,
            speaker.current_media.uri if speaker.current_media else None,
        )
        for speaker in speakers
    }


IDLE: _Seen = (PlaybackState.IDLE, [], None, None, None)
PLAYS_MOVED: _Seen = (PlaybackState.PLAYING, [TEST_TRACK_URI], TEST_TRACK_URI, None, None)


@pytest.mark.parametrize("operation", ["transfer", "hand-over"])
async def test_a_transfer_whose_target_joins_another_group_meanwhile_still_completes(
    scenario: Scenario, operation: str
) -> None:
    """
    A player a queue moves to that joins another group meanwhile plays the queue once moved.

    Den takes 40 s to stop. Kitchen, the player the queue of Den moves to (by a transfer, or as
    Den's group member when Den leaves its group), joins Office's group meanwhile. Both commands
    succeed: Kitchen leaves Office's group again and plays the moved queue, Den is idle with an
    empty queue, and Office is idle.
    """
    _sonos, (den, kitchen, office) = await _speakers(scenario, "Den", "Kitchen", "Office")
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
    await scenario.advance(50)
    await move
    await join
    await scenario.idle()

    names = {den_id: "Den", kitchen_id: "Kitchen", office_id: "Office"}
    assert _seen(scenario, names) == {"Den": IDLE, "Kitchen": PLAYS_MOVED, "Office": IDLE}
    assert _rendered(den, kitchen, office) == {
        "Den": (PlaybackState.IDLE, None),
        "Kitchen": (PlaybackState.PLAYING, TEST_TRACK_URI),
        "Office": (PlaybackState.IDLE, None),
    }


_GROUP_PLAYS_OTHER: dict[str, _Seen] = {
    "Office": (PlaybackState.PLAYING, [], OTHER_TRACK_URI, None, "Downstairs"),
    "Downstairs": (PlaybackState.PLAYING, [OTHER_TRACK_URI], OTHER_TRACK_URI, None, None),
}
_SYNC_GROUP_RENDERS: dict[str, tuple[PlaybackState, str | None]] = {
    "Den": (PlaybackState.IDLE, None),
    "Kitchen": (PlaybackState.PLAYING, TEST_TRACK_URI),
    "Office": (PlaybackState.PLAYING, OTHER_TRACK_URI),
}
_ONLY_KITCHEN_RENDERS: dict[str, tuple[PlaybackState, str | None]] = {
    "Den": (PlaybackState.IDLE, None),
    "Kitchen": (PlaybackState.PLAYING, TEST_TRACK_URI),
    "Office": (PlaybackState.IDLE, None),
}
_UNIVERSAL_GROUP_END: tuple[dict[str, _Seen], dict[str, tuple[PlaybackState, str | None]]] = (
    {"Den": IDLE, "Kitchen": PLAYS_MOVED, "Office": IDLE, "Downstairs": IDLE},
    _ONLY_KITCHEN_RENDERS,
)
# what the players show and the devices render at the end, by group type and order
CAPTURED_END = {
    ("sync_group", "transfer first"): (
        {"Den": IDLE, "Kitchen": PLAYS_MOVED, **_GROUP_PLAYS_OTHER},
        _SYNC_GROUP_RENDERS,
    ),
    ("sync_group", "capture first"): (
        {
            "Den": IDLE,
            "Kitchen": (
                PlaybackState.PLAYING,
                [TEST_TRACK_URI],
                OTHER_TRACK_URI,
                "Office",
                "Downstairs",
            ),
            **_GROUP_PLAYS_OTHER,
        },
        _SYNC_GROUP_RENDERS,
    ),
    ("universal_group", "transfer first"): _UNIVERSAL_GROUP_END,
    ("universal_group", "capture first"): _UNIVERSAL_GROUP_END,
}


@pytest.mark.parametrize("group_type", ["sync_group", "universal_group"])
@pytest.mark.parametrize("order", ["transfer first", "capture first"])
async def test_a_transfer_whose_target_a_group_takes_meanwhile_still_completes(
    scenario: Scenario, group_type: str, order: str
) -> None:
    """
    A player a queue moves to that a group takes meanwhile plays the moved queue.

    The queue of Den moves to Kitchen while Downstairs, a group of Office and Kitchen, takes
    Kitchen: a sync group that starts to play another track, or a universal group that powers
    on. Either comes while Den takes 40 s to stop, or first, with Office taking 40 s to do its
    part. Both commands succeed, Den is idle with an empty queue and Kitchen holds the moved
    queue and plays it:
    - the sync group plays the other track on Office; Kitchen has left it when the queue moved
      first, and is still listed in it, at the other track, when the group came first;
    - the universal group, which plays nothing, is idle with Office.
    """
    _sonos, (den, kitchen, office) = await _speakers(scenario, "Den", "Kitchen", "Office")
    den_id, kitchen_id, office_id = (_player_id(scenario, p) for p in (den, kitchen, office))
    players = scenario.mass.players
    queues = scenario.mass.player_queues
    if group_type == "universal_group":
        await scenario.mass.config._create_provider_instance(group_type, {})
    group = await players.create_group_player(
        group_type, "Downstairs", [office_id, kitchen_id], dynamic=True
    )
    group_id = group.player_id
    await scenario.idle()
    await scenario.play(den_id)
    if group_type == "universal_group":
        await scenario.configure_player(group_id, power_control=PLAYER_CONTROL_FAKE)
        if order == "capture first":
            await scenario.report(office, powered=False)

    async def _capture() -> None:
        if group_type == "sync_group":
            await queues.play_media(group_id, OTHER_TRACK_URI)
        else:
            await players.cmd_power(group_id, True)

    if order == "transfer first":
        _slow(den, "stop", 40, once=True)
        transfer = await scenario.command(queues.transfer_queue(den_id, kitchen_id))
        await scenario.advance(10)
        capture = await scenario.command(_capture())
    else:
        _slow(office, "set_members" if group_type == "sync_group" else "power", 40, once=True)
        capture = await scenario.command(_capture())
        await scenario.advance(10)
        transfer = await scenario.command(queues.transfer_queue(den_id, kitchen_id))
    await scenario.advance(100)
    await transfer
    await capture
    await scenario.idle()

    names = {den_id: "Den", kitchen_id: "Kitchen", office_id: "Office", group_id: "Downstairs"}
    seen, rendered = CAPTURED_END[group_type, order]
    assert _seen(scenario, names) == seen
    assert _rendered(den, kitchen, office) == rendered
    stop = await scenario.command(players.cmd_stop(group_id))
    await scenario.advance(20)
    await stop
    await scenario.idle()


@pytest.mark.parametrize("step", ["regroup", "resume"])
async def test_a_hand_over_whose_new_leader_joins_another_group_meanwhile_completes(
    scenario: Scenario, step: str
) -> None:
    """
    A new leader that joins another group while the hand-over regroups or resumes it.

    Den leads Kitchen and Office and plays its own queue. Den leaves its group: its queue moves
    to Kitchen, the new leader, which takes 40 s to regroup Office, or to start the queue.
    Meanwhile Kitchen joins Patio's group. Both commands succeed and Den is idle with an empty
    queue:
    - during the regroup, Kitchen leaves Patio's group again, leads Office and plays the moved
      queue, and Patio is idle;
    - during the start, Kitchen's device plays the moved queue while Kitchen is listed in
      Patio's group, idle, with Office still following Kitchen.
    """
    _sonos, (den, kitchen, office, patio) = await _speakers(
        scenario, "Den", "Kitchen", "Office", "Patio"
    )
    ids = [_player_id(scenario, p) for p in (den, kitchen, office, patio)]
    den_id, kitchen_id, office_id, patio_id = ids
    players = scenario.mass.players
    await players.cmd_set_members(den_id, player_ids_to_add=[kitchen_id, office_id])
    await scenario.idle()
    await scenario.play(den_id)
    _slow(kitchen, "set_members" if step == "regroup" else "play_media", 40, once=True)
    hand_over = await scenario.command(players.cmd_ungroup(den_id))

    await scenario.advance(10)
    assert not hand_over.done()
    await players.cmd_set_members(patio_id, player_ids_to_add=[kitchen_id])
    await scenario.report(kitchen)
    await scenario.advance(200)
    await hand_over
    await scenario.idle()

    names = dict(zip(ids, ("Den", "Kitchen", "Office", "Patio"), strict=True))
    if step == "regroup":
        assert _seen(scenario, names) == {
            "Den": IDLE,
            "Kitchen": PLAYS_MOVED,
            "Office": (PlaybackState.PLAYING, [], TEST_TRACK_URI, "Kitchen", None),
            "Patio": IDLE,
        }
    else:
        assert _seen(scenario, names) == {
            "Den": IDLE,
            "Kitchen": (PlaybackState.IDLE, [TEST_TRACK_URI], None, "Patio", None),
            "Office": (PlaybackState.IDLE, [], None, "Kitchen", None),
            "Patio": IDLE,
        }
    assert _rendered(den, kitchen, office, patio) == {
        "Den": (PlaybackState.IDLE, None),
        "Kitchen": (PlaybackState.PLAYING, TEST_TRACK_URI),
        "Office": (PlaybackState.IDLE, None),
        "Patio": (PlaybackState.IDLE, None),
    }


def _never_resolving(scenario: Scenario, uri: str) -> None:
    """
    Let the lookup of what a play request plays never return for one track.

    :param uri: The uri of the track whose lookup hangs.
    """
    resolver = scenario.mass.player_queues._media_resolver
    resolve = resolver._resolve_media_items

    async def _resolve(media_item: Any, *args: Any, **kwargs: Any) -> Any:
        if media_item.uri == uri:
            await asyncio.Event().wait()
        return await resolve(media_item, *args, **kwargs)

    resolver._resolve_media_items = _resolve  # type: ignore[method-assign]


async def test_a_play_request_that_is_given_up_frees_the_player_and_its_leader(
    scenario: Scenario,
) -> None:
    """
    A play request whose lookup takes longer than a minute fails, and changes nothing.

    Kitchen is grouped to Den, which plays. A play on Kitchen holds Kitchen and Den while it
    looks up what to play, so a stop of Den sent behind it waits. After 60 s the play fails:
    Kitchen is still in the group of Den with an empty queue, neither queue shows an action in
    progress, and the stop reaches Den at once.
    """
    _, (den, kitchen) = await _speakers(scenario, "Den", "Kitchen")
    den_id, kitchen_id = _player_id(scenario, den), _player_id(scenario, kitchen)
    await scenario.mass.players.cmd_set_members(den_id, player_ids_to_add=[kitchen_id])
    await scenario.idle()
    await scenario.play(den_id)
    _never_resolving(scenario, OTHER_TRACK_URI)

    play = await scenario.command(
        scenario.mass.player_queues.play_media(kitchen_id, OTHER_TRACK_URI, QueueOption.REPLACE)
    )
    stop = await scenario.command(scenario.mass.player_queues.stop(den_id))
    await scenario.advance(59)
    assert not play.done()
    assert not stop.done()
    assert _plays(scenario, den_id) == (PlaybackState.PLAYING, [TEST_TRACK_URI])

    await scenario.advance(1)
    assert play.done()
    with pytest.raises(PlayerCommandFailed) as given_up:
        await play
    assert stop.done()
    await stop
    await scenario.idle()

    assert given_up.value.translation_key == "play_request_timeout"
    assert given_up.value.translation_owner == "core.player_queues"
    kitchen_state = scenario.player(kitchen_id)
    assert kitchen_state is not None
    assert kitchen_state.synced_to == den_id
    assert _plays(scenario, kitchen_id) == (PlaybackState.IDLE, [])
    assert _plays(scenario, den_id) == (PlaybackState.IDLE, [TEST_TRACK_URI])
    assert "ungroup" not in _names(kitchen)
    for player_id in (den_id, kitchen_id):
        queue = scenario.queue(player_id)
        assert queue is not None
        assert not queue.extra_attributes.get(ATTR_PLAY_ACTION_IN_PROGRESS)


async def test_a_play_request_that_waits_longer_than_a_minute_for_its_turn_still_plays(
    scenario: Scenario,
) -> None:
    """
    The minute a play request has to look up what to play does not run while it waits.

    A play takes 90 s at the Den speaker. A play of another track sent right behind it waits
    for it all that time, then looks its track up and plays it.
    """
    den, den_id, _kitchen, _kitchen_id = await _two_speakers(scenario)
    await scenario.play(den_id)
    _slow(den, "play_media", 90, once=True)
    queues = scenario.mass.player_queues
    first = await scenario.command(queues.play_media(den_id, TEST_TRACK_URI, QueueOption.REPLACE))
    second = await scenario.command(queues.play_media(den_id, OTHER_TRACK_URI, QueueOption.REPLACE))

    await scenario.advance(89)
    assert not first.done()
    assert not second.done()

    await scenario.advance(1)
    assert first.done()
    assert second.done()
    await first
    await second
    await scenario.idle()

    assert _plays(scenario, den_id) == (PlaybackState.PLAYING, [OTHER_TRACK_URI])
