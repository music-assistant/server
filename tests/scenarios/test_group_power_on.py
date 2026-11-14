"""A group waits for the members it switches on at their power control, and then starts once."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import PlaybackState, QueueOption
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError

from music_assistant.controllers.players.constants import OFFLINE_POWER_ON_TIMEOUT
from tests.common import record_lock_waits

from .harness import TEST_TRACK_URI
from .test_grouping import (
    _come_back,
    _group,
    _grouping_commands,
    _load_music,
    _player_id,
    _speakers,
    _state,
    _stop,
    _switch_off,
)
from .test_paused_queue import (
    OTHER_TRACK_URI,
    SETTLE_SECONDS,
    TRACK_SECONDS,
    _fetch_the_flow_stream,
)
from .test_stopped_queue import _DynamicSource, _other_track

if TYPE_CHECKING:
    import asyncio

    from .harness import FakePowerControl, FakeProtocol, Scenario

# the groups a scenario runs for: the type of group player, and whether its members can change
GROUPS = {
    "dynamic sync group": ("sync_group", True),
    "fixed sync group": ("sync_group", False),
    "universal group": ("universal_group", True),
}


async def _downstairs(
    scenario: Scenario, group: str, *names: str
) -> tuple[str, list[FakeProtocol], list[str]]:
    """
    Create the group Downstairs of the named speakers.

    :param group: Which kind of group, a key of GROUPS.
    :param names: The names of the speakers.
    :return: The id of the group, the speakers and the ids of their players.
    """
    _, speakers = await _speakers(scenario, *names)
    player_ids = [_player_id(scenario, speaker) for speaker in speakers]
    group_type, dynamic = GROUPS[group]
    group_id = await _group(scenario, group_type, *player_ids, dynamic=dynamic)
    return group_id, speakers, player_ids


async def _play(scenario: Scenario, player_id: str) -> asyncio.Task[None]:
    """Send a play request for a track to a player and return the command, which may wait."""
    await _load_music(scenario)
    return await scenario.command(scenario.mass.player_queues.play_media(player_id, TEST_TRACK_URI))


def _starts(speaker: FakeProtocol) -> int:
    """Return how often a speaker was told to play something."""
    return [command[0] for command in speaker.commands].count("play_media")


def _plays_along(scenario: Scenario, group_id: str, player_id: str) -> bool:
    """Return whether a player plays what a group plays."""
    state = _state(scenario, player_id)
    return (
        state.playback_state == PlaybackState.PLAYING
        and player_id in _state(scenario, group_id).group_members
        and state.active_group == group_id
    )


def _warnings_about(caplog: pytest.LogCaptureFixture, name: str) -> list[str]:
    """Return the warnings (and worse) that name a player, in order."""
    return [
        message
        for record in caplog.records
        if record.levelno >= logging.WARNING and name in (message := record.getMessage())
    ]


def _expect_the_form_without(scenario: Scenario, group: str) -> None:
    """Expect what a sync group logs when it forms without a member that can not play."""
    if GROUPS[group][0] == "sync_group":
        scenario.expect_log("is not available")


def _refuse_to_switch_on(scenario: Scenario, plug: FakePowerControl) -> None:
    """Let a plug fail every power on, which the scenario expects to be logged."""
    control = scenario.mass.players.get_player_control(plug.control_id)
    assert control is not None

    async def _refuse() -> None:
        raise PlayerCommandFailed("The plug does not answer")

    control.power_on = _refuse
    scenario.expect_log("The plug does not answer")


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("switched_off", [1, 3], ids=["one member off", "all members off"])
async def test_a_group_waits_for_its_switched_off_members_and_starts_once(
    scenario: Scenario, group: str, switched_off: int
) -> None:
    """
    A group that is played switches its members on, waits for them and starts once.

    One or all of its members are on a smart plug that is switched off. The play switches the
    plugs on and waits: no speaker gets a command until the last device is back. Then the
    group starts once, on all members.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    away = speakers[-switched_off:]
    plugs = [await _switch_off(scenario, speaker) for speaker in away]

    play = await _play(scenario, group_id)

    assert not play.done(), play.exception()
    assert [plug.commands for plug in plugs] == [[True]] * switched_off
    for speaker in away[:-1]:
        await scenario.advance(10)
        await _come_back(scenario, speaker)
    await scenario.advance(10)
    assert not play.done(), play.exception()
    assert [speaker.commands for speaker in speakers] == [[], [], []]
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE

    await _come_back(scenario, away[-1])
    await play
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    if GROUPS[group][0] == "sync_group":
        # the leader gets the stream, and the others are joined to it once
        assert sorted(_starts(speaker) for speaker in speakers) == [0, 0, 1]
        assert sum(len(_grouping_commands(speaker)) for speaker in speakers) == 1
    else:
        assert [_starts(speaker) for speaker in speakers] == [1, 1, 1]
    assert [plug.commands for plug in plugs] == [[True]] * switched_off
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", GROUPS)
async def test_a_member_that_does_not_come_online_is_left_out(
    scenario: Scenario, group: str, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A member whose device does not come back in time is left out, which is logged once.

    Nothing starts while the group waits for it. After the time a power on waits for a device
    the group starts on the other members, and the member, whose plug is on by then, shows as
    unavailable. A later play of the group, with the plug switched off again, tries again.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den = speakers[2]
    plug = await _switch_off(scenario, den)
    scenario.expect_log("did not come online")
    _expect_the_form_without(scenario, group)

    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        play = await _play(scenario, group_id)
        await scenario.advance(OFFLINE_POWER_ON_TIMEOUT - 1)
        assert not play.done(), play.exception()
        assert [speaker.commands for speaker in speakers] == [[], [], []]
        await scenario.advance(2)
        await play
        await scenario.idle()

    assert _warnings_about(caplog, "Den") == [
        "Player Den is left out of Downstairs: Player Den did not come online after powering it on"
    ]
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [
        True,
        True,
        False,
    ]
    assert _state(scenario, player_ids[2]).available is False
    assert plug.commands == [True]
    await _stop(scenario, group_id)

    if group == "dynamic sync group":
        # it let go of the member that is unavailable
        return
    await scenario.switch(plug, False)
    play = await _play(scenario, group_id)
    assert not play.done(), play.exception()
    assert plug.commands == [True, True]
    await _come_back(scenario, den)
    await play
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", GROUPS)
async def test_a_power_control_that_refuses_leaves_the_member_out(
    scenario: Scenario, group: str, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A member whose plug refuses to switch on is left out, which is logged once.

    The group starts on the other members, and the warning names the member and the group. A
    later play of the group, when the plug works again, waits for the member and starts on
    all of them.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den = speakers[2]
    plug = await _switch_off(scenario, den)
    control = scenario.mass.players.get_player_control(plug.control_id)
    assert control is not None
    power_on = control.power_on
    _refuse_to_switch_on(scenario, plug)
    _expect_the_form_without(scenario, group)

    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        await scenario.play(group_id)
        await scenario.idle()

    assert _warnings_about(caplog, "Den") == [
        "Player Den is left out of Downstairs: The plug does not answer"
    ]
    assert plug.commands == []
    assert _state(scenario, player_ids[2]).powered is False
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [
        True,
        True,
        False,
    ]
    await _stop(scenario, group_id)

    control.power_on = power_on
    play = await _play(scenario, group_id)
    assert not play.done(), play.exception()
    assert plug.commands == [True]
    await _come_back(scenario, den)
    await play
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    await _stop(scenario, group_id)


CALL_OFFS = ["stop", "queue stop", "pause", "queue pause", "power off"]


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("call_off", CALL_OFFS)
async def test_a_stop_of_the_group_calls_off_the_start_that_waits(
    scenario: Scenario, group: str, call_off: str
) -> None:
    """
    A stop, a pause or a power off of a group ends the start that waits for its members.

    The play that waits fails, and nothing starts, also not on the members that are on. The
    device that comes back afterwards stays idle and gets no command, and its plug stays on.
    A group that is powered off reads as off itself while it waits.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den, den_id = speakers[2], player_ids[2]
    if call_off == "power off":
        await scenario.configure_player(group_id, power_control=PLAYER_CONTROL_FAKE)
    plug = await _switch_off(scenario, den)
    play = await _play(scenario, group_id)
    assert not play.done(), play.exception()
    assert plug.commands == [True]
    players, queues = scenario.mass.players, scenario.mass.player_queues

    command = await scenario.command(
        {
            "stop": lambda: players.cmd_stop(group_id),
            "queue stop": lambda: queues.stop(group_id),
            "pause": lambda: players.cmd_pause(group_id),
            "queue pause": lambda: queues.pause(group_id),
            "power off": lambda: players.cmd_power(group_id, False),
        }[call_off]()
    )
    await scenario.advance(10)
    await command

    with pytest.raises(PlayerUnavailableError, match="was called off"):
        await play
    assert _state(scenario, den_id).available is False
    await _come_back(scenario, den)
    await scenario.idle()

    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    assert den.commands == []
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert _state(scenario, den_id).playback_state == PlaybackState.IDLE
    assert _state(scenario, den_id).synced_to is None
    assert _state(scenario, den_id).active_group is None
    assert plug.commands == [True]


async def test_a_command_on_a_member_reaches_the_start_of_the_group_that_owns_its_playback(
    scenario: Scenario,
) -> None:
    """
    A play on a member of a paused group waits for the members of the group, and a stop ends it.

    The sync group, which has a fixed member list, plays on Kitchen and Office: the plug of
    Den refused to switch on. The group is paused and the plug works again. The user presses
    play on Office, which goes to the group: Den's plug is switched on and the play waits.
    The user presses stop on Office: the play fails, the group is stopped, and Den stays idle
    when it is back.
    """
    group_id, speakers, player_ids = await _downstairs(
        scenario, "fixed sync group", "Kitchen", "Office", "Den"
    )
    den, (_, office_id, den_id) = speakers[2], player_ids
    plug = await _switch_off(scenario, den)
    control = scenario.mass.players.get_player_control(plug.control_id)
    assert control is not None
    power_on = control.power_on
    _refuse_to_switch_on(scenario, plug)
    _expect_the_form_without(scenario, "fixed sync group")
    players = scenario.mass.players
    await scenario.play(group_id)
    control.power_on = power_on
    pause = await scenario.command(players.cmd_pause(group_id))
    await scenario.advance(2)
    await pause
    assert _state(scenario, group_id).playback_state == PlaybackState.PAUSED
    assert plug.commands == []
    for speaker in speakers:
        speaker.commands.clear()

    play = await scenario.command(players.cmd_play(office_id))

    assert not play.done(), play.exception()
    assert plug.commands == [True]
    stop = await scenario.command(players.cmd_stop(office_id))
    await scenario.advance(10)
    await stop
    with pytest.raises(PlayerUnavailableError, match="was called off"):
        await play
    await _come_back(scenario, den)
    await scenario.idle()

    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert _state(scenario, den_id).playback_state == PlaybackState.IDLE
    assert plug.commands == [True]


STARTS = ["queue play", "queue resume", "queue play index", "play", "resume"]


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize("start", STARTS)
async def test_every_command_that_starts_a_group_waits_for_its_members(
    scenario: Scenario, group: str, start: str
) -> None:
    """
    Each command that starts a group whose queue holds music waits for its members.

    The group was stopped, and Den's plug is switched off. A play, a resume or a play of an
    item of its queue, through the queue or the player, switches the plug on and starts the
    group on all members once Den's device is back.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den = speakers[2]
    await scenario.play(group_id)
    await _stop(scenario, group_id)
    plug = await _switch_off(scenario, den)
    for speaker in speakers:
        speaker.commands.clear()
    players, queues = scenario.mass.players, scenario.mass.player_queues

    command = await scenario.command(
        {
            "queue play": lambda: queues.play(group_id),
            "queue resume": lambda: queues.resume(group_id),
            "queue play index": lambda: queues.play_index(group_id, 0),
            "play": lambda: players.cmd_play(group_id),
            "resume": lambda: players.cmd_resume(group_id),
        }[start]()
    )

    assert not command.done(), command.exception()
    assert plug.commands == [True]
    await scenario.advance(10)
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    await _come_back(scenario, den)
    await command
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
async def test_a_group_that_is_powered_on_waits_for_its_members(
    scenario: Scenario, group: str
) -> None:
    """
    A power on of a group with a power control of its own waits for its switched-off members.

    The plug of Den is switched on, and the group is on once Den's device is back.
    """
    group_id, speakers, _ = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    await scenario.configure_player(group_id, power_control=PLAYER_CONTROL_FAKE)
    plug = await _switch_off(scenario, speakers[2])

    power_on = await scenario.command(scenario.mass.players.cmd_power(group_id, True))

    assert not power_on.done(), power_on.exception()
    assert plug.commands == [True]
    assert _state(scenario, group_id).powered is False
    await _come_back(scenario, speakers[2])
    await power_on
    await scenario.idle()

    assert _state(scenario, group_id).powered is True
    power_off = await scenario.command(scenario.mass.players.cmd_power(group_id, False))
    await scenario.advance(10)
    await power_off


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
async def test_a_queue_that_is_transferred_to_a_group_waits_for_its_members(
    scenario: Scenario, group: str
) -> None:
    """
    A queue that is transferred to a group and plays on waits for the members of the group.

    Bedroom plays. Its queue is transferred to the group, whose member Den is switched off at
    its plug: the plug is switched on, Bedroom plays on meanwhile, and once Den's device is
    back the group plays the queue on all its members.
    """
    _, speakers = await _speakers(scenario, "Kitchen", "Office", "Den", "Bedroom")
    player_ids = [_player_id(scenario, speaker) for speaker in speakers]
    bedroom_id = player_ids.pop()
    group_type, dynamic = GROUPS[group]
    group_id = await _group(scenario, group_type, *player_ids, dynamic=dynamic)
    plug = await _switch_off(scenario, speakers[2])
    await scenario.play(bedroom_id)

    transfer = await scenario.command(
        scenario.mass.player_queues.transfer_queue(bedroom_id, group_id)
    )

    assert not transfer.done(), transfer.exception()
    assert plug.commands == [True]
    assert _state(scenario, bedroom_id).playback_state == PlaybackState.PLAYING
    await _come_back(scenario, speakers[2])
    await scenario.advance(10)
    await transfer
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    assert _state(scenario, bedroom_id).playback_state == PlaybackState.IDLE
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize(
    "command", ["volume", "queue pause", "stop", "second play", "play on a member"]
)
async def test_a_group_that_waits_for_a_member_holds_no_player_meanwhile(
    scenario: Scenario, group: str, command: str
) -> None:
    """
    A start of a group that waits for a member holds no player meanwhile.

    A volume command, a pause or a stop of the group and a play on a member that is on run at
    once and wait for no lock. A second play of the group waits for the same device, not for
    a lock, and the group plays once the device is back.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den, kitchen_id = speakers[2], player_ids[0]
    plug = await _switch_off(scenario, den)
    play = await _play(scenario, group_id)
    assert not play.done(), play.exception()
    players, queues = scenario.mass.players, scenario.mass.player_queues
    waits = record_lock_waits(players)

    other = await scenario.command(
        {
            "volume": lambda: players.cmd_volume_set(group_id, 30),
            "queue pause": lambda: queues.pause(group_id),
            "stop": lambda: players.cmd_stop(group_id),
            "second play": lambda: queues.play_media(group_id, OTHER_TRACK_URI),
            "play on a member": lambda: queues.play_media(kitchen_id, TEST_TRACK_URI),
        }[command]()
    )

    assert waits == []
    assert plug.commands == [True]
    if command == "second play":
        assert not other.done(), other.exception()
    else:
        assert other.done()
        await other
    if command == "play on a member":
        assert _state(scenario, kitchen_id).playback_state == PlaybackState.PLAYING
    await _come_back(scenario, den)
    await scenario.advance(10)
    if command in ("queue pause", "stop"):
        with pytest.raises(PlayerUnavailableError, match="was called off"):
            await play
        await scenario.idle()
        assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
        return
    await play
    await other
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    await _stop(scenario, group_id)


async def test_a_next_on_a_group_that_waits_for_a_member_runs_at_once(scenario: Scenario) -> None:
    """
    A next on a paused group whose play waits for a member runs at once, on the members that are on.

    The sync group, which has a fixed member list, plays two tracks on Kitchen and Office: the
    plug of Den refused to switch on. The group is paused and the plug works again. A play of
    the group switches the plug on and waits. A next meanwhile waits for no lock and for no
    device: the next track plays on Kitchen and Office, and on Den as well once it is back.
    """
    group_id, speakers, player_ids = await _downstairs(
        scenario, "fixed sync group", "Kitchen", "Office", "Den"
    )
    den = speakers[2]
    plug = await _switch_off(scenario, den)
    control = scenario.mass.players.get_player_control(plug.control_id)
    assert control is not None
    power_on = control.power_on
    _refuse_to_switch_on(scenario, plug)
    _expect_the_form_without(scenario, "fixed sync group")
    players, queues = scenario.mass.players, scenario.mass.player_queues
    await scenario.play(group_id)
    await queues.play_media(group_id, OTHER_TRACK_URI, option=QueueOption.ADD)
    control.power_on = power_on
    pause = await scenario.command(players.cmd_pause(group_id))
    await scenario.advance(2)
    await pause
    play = await scenario.command(players.cmd_play(group_id))
    assert not play.done(), play.exception()
    assert plug.commands == [True]
    waits = record_lock_waits(players)

    skip = await scenario.command(queues.next(group_id))

    assert skip.done()
    await skip
    assert waits == []
    await scenario.advance(5)
    queue = scenario.queue(group_id)
    assert queue is not None
    assert queue.current_index == 1
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert not play.done(), play.exception()
    await _come_back(scenario, den)
    await play
    await scenario.idle()

    assert queue.current_index == 1
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    await _stop(scenario, group_id)


OWNERS = ["leader", "dynamic sync group", "universal group"]


async def _playing_owner(
    scenario: Scenario, owner: str
) -> tuple[str, list[FakeProtocol], list[str], FakePowerControl]:
    """
    Let Kitchen and Office play as a leader with its member, or as a group, with Den switched off.

    :param owner: ``"leader"`` for Kitchen leading Office, else the kind of group of the two
        (a key of GROUPS).
    :return: The id of the leader or group, the three speakers, the ids of their players and
        the plug of Den.
    """
    _, speakers = await _speakers(scenario, "Kitchen", "Office", "Den")
    player_ids = [_player_id(scenario, speaker) for speaker in speakers]
    if owner == "leader":
        owner_id = player_ids[0]
        await scenario.mass.players.cmd_set_members(owner_id, player_ids_to_add=[player_ids[1]])
        await scenario.idle()
    else:
        group_type, dynamic = GROUPS[owner]
        owner_id = await _group(scenario, group_type, *player_ids[:2], dynamic=dynamic)
    plug = await _switch_off(scenario, speakers[2])
    await scenario.play(owner_id)
    for speaker in speakers:
        speaker.commands.clear()
    return owner_id, speakers, player_ids, plug


def _joined(scenario: Scenario, owner_id: str, den: FakeProtocol, den_id: str) -> bool:
    """Return whether Den is a member of a leader or group and plays what that plays."""
    state = _state(scenario, den_id)
    owner = _state(scenario, owner_id)
    plays_along = (
        state.playback_state == PlaybackState.PLAYING
        if state.synced_to is None
        else _state(scenario, state.synced_to).playback_state == PlaybackState.PLAYING
    )
    return den_id in owner.group_members and plays_along and bool(den.commands or state.synced_to)


@pytest.mark.parametrize("owner", OWNERS)
async def test_a_switched_off_speaker_that_is_added_is_waited_for_and_added_once(
    scenario: Scenario, owner: str
) -> None:
    """
    A switched-off speaker that is added to a playing leader or group is switched on first.

    The command switches its plug on and waits: the leader or group plays on and gets no
    command for it. Once the device is back the speaker is added, once, and plays along.
    """
    owner_id, speakers, player_ids, plug = await _playing_owner(scenario, owner)
    den, den_id = speakers[2], player_ids[2]

    add = await scenario.command(
        scenario.mass.players.cmd_set_members(owner_id, player_ids_to_add=[den_id])
    )

    assert not add.done(), add.exception()
    assert plug.commands == [True]
    await scenario.advance(10)
    assert [_grouping_commands(speaker) for speaker in speakers] == [[], [], []]
    assert den_id not in _state(scenario, owner_id).group_members
    await _come_back(scenario, den)
    await add
    await scenario.idle()

    assert _joined(scenario, owner_id, den, den_id)
    if owner == "universal group":
        assert _starts(den) == 1
    else:
        assert _grouping_commands(speakers[0]) == [("set_members", ["sonos_den"], None)]
    assert plug.commands == [True]
    await _stop(scenario, owner_id)


@pytest.mark.parametrize("owner", OWNERS)
async def test_a_speaker_that_is_added_and_does_not_come_online_is_left_out(
    scenario: Scenario, owner: str, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A switched-off speaker that is added and whose device does not come back is not added.

    That is logged once, with the speaker and the leader or group it is left out of, which
    plays on.
    """
    owner_id, speakers, player_ids, plug = await _playing_owner(scenario, owner)
    den, den_id = speakers[2], player_ids[2]
    scenario.expect_log("did not come online")

    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        add = await scenario.command(
            scenario.mass.players.cmd_set_members(owner_id, player_ids_to_add=[den_id])
        )
        await scenario.advance(OFFLINE_POWER_ON_TIMEOUT + 1)
        await add
        await scenario.idle()

    assert _warnings_about(caplog, "Den") == [
        f"Player Den is left out of {_state(scenario, owner_id).name}: "
        "Player Den did not come online after powering it on"
    ]
    assert den_id not in _state(scenario, owner_id).group_members
    assert [_grouping_commands(speaker) for speaker in speakers] == [[], [], []]
    assert den.commands == []
    assert _state(scenario, owner_id).playback_state == PlaybackState.PLAYING
    assert plug.commands == [True]
    await _stop(scenario, owner_id)


@pytest.mark.parametrize("owner", OWNERS)
@pytest.mark.parametrize("ended_by", ["removing the speaker", "stopping the owner"])
async def test_an_addition_that_waits_for_a_speaker_can_be_ended(
    scenario: Scenario, owner: str, ended_by: str
) -> None:
    """
    Removing a speaker that is still switched on to be added, or stopping its owner, adds nothing.

    The addition ends at once. The device that comes back afterwards gets no command and is
    in no group, and its plug stays on.
    """
    owner_id, speakers, player_ids, plug = await _playing_owner(scenario, owner)
    den, den_id = speakers[2], player_ids[2]
    players = scenario.mass.players
    add = await scenario.command(players.cmd_set_members(owner_id, player_ids_to_add=[den_id]))
    assert not add.done(), add.exception()

    if ended_by == "removing the speaker":
        await players.cmd_set_members(owner_id, player_ids_to_remove=[den_id])
        # the power on looks at its player every 0.5 s
        await scenario.advance(1)
        await add
    else:
        await _stop(scenario, owner_id)
        with pytest.raises(PlayerUnavailableError, match="was called off"):
            await add
    await _come_back(scenario, den)
    await scenario.idle()

    assert den.commands == []
    assert den_id not in _state(scenario, owner_id).group_members
    assert _state(scenario, den_id).synced_to is None
    assert _state(scenario, den_id).active_group is None
    assert [
        command for command in _grouping_commands(speakers[0]) if "sonos_den" in str(command)
    ] == []
    assert plug.commands == [True]
    if ended_by == "removing the speaker":
        await _stop(scenario, owner_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize(
    "continuation", ["music added while idle", "dynamic source", "flow stream that ended"]
)
async def test_a_queue_that_the_server_plays_on_switches_no_member_on(
    scenario: Scenario, group: str, continuation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A queue of a group that the server plays on by itself leaves a switched-off member off.

    The queue of the group played out, and someone switches Den's plug off. The queue then
    plays on without a command: music was added to it, its dynamic source hands back new
    tracks, or a track was added behind its flow stream. The group plays on Kitchen and
    Office without waiting, and Den's plug stays off.

    :param continuation: What makes the queue play on.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    kitchen, _, den = speakers
    _expect_the_form_without(scenario, group)
    plug = await scenario.add_power_control("switch.den_plug", "Den plug", powered=True)
    await scenario.configure_player(player_ids[2], power_control=plug.control_id)
    queues = scenario.mass.player_queues
    await scenario.play(group_id)
    queue = scenario.queue(group_id)
    assert queue is not None
    assert queue.current_item is not None
    if continuation == "music added while idle":
        queues.mark_item_served(group_id, queue.current_item.queue_item_id)
        await scenario.report(
            kitchen, elapsed_time=0, elapsed_time_last_updated=scenario.wall_time()
        )
    else:
        await _fetch_the_flow_stream(scenario, kitchen, group_id)
    if continuation == "dynamic source":
        _DynamicSource(scenario, group_id, [await _other_track(scenario)], monkeypatch)
    if continuation == "flow stream that ended":
        await queues.play_media(group_id, OTHER_TRACK_URI, option=QueueOption.ADD)
    await scenario.advance(TRACK_SECONDS)
    for speaker in speakers:
        await scenario.report(speaker, playback_state=PlaybackState.IDLE, current_media=None)
    if GROUPS[group][0] == "universal_group":
        await scenario.mass.players.get_player(group_id).own_protocol.poll()  # type: ignore[union-attr]
    for _ in range(16):
        if queue.state == PlaybackState.IDLE:
            break
        # the queue follows its player with a short delay
        await scenario.advance(0.125)
    assert queue.state == PlaybackState.IDLE

    await scenario.switch(plug, False)
    await scenario.unregister(den)
    await scenario.settle()
    kitchen.commands.clear()
    if continuation == "music added while idle":
        await queues.play_media(group_id, OTHER_TRACK_URI, option=QueueOption.ADD)
    await scenario.advance(SETTLE_SECONDS + 2)

    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert queue.current_index == 1
    assert _starts(kitchen) == 1
    assert plug.commands == []
    assert _state(scenario, player_ids[2]).powered is False
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", GROUPS)
async def test_a_member_that_waited_for_its_group_is_not_powered_off_as_idle(
    scenario: Scenario, group: str
) -> None:
    """
    A member with an idle power off is not switched off between its power on and the start.

    Den is powered off after one idle minute. The group starts as soon as Den's device is
    back, so Den plays, and its plug is not switched off while it does.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    await scenario.configure_player(player_ids[2], idle_power_off=1)
    plug = await _switch_off(scenario, speakers[2])
    play = await _play(scenario, group_id)
    await scenario.advance(30)

    await _come_back(scenario, speakers[2])
    await play
    await scenario.advance(180)

    assert plug.commands == [True]
    assert _plays_along(scenario, group_id, player_ids[2])
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", GROUPS)
async def test_a_member_its_group_never_started_is_powered_off_as_idle(
    scenario: Scenario, group: str
) -> None:
    """
    A member that was switched on for a start that was called off is powered off as idle.

    Den is powered off after one idle minute. The group is stopped while its start waits for
    Den, whose plug stays on. Den's device comes back and stays idle, and a minute later Den
    is powered off as any idle player is.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    await scenario.configure_player(player_ids[2], idle_power_off=1)
    plug = await _switch_off(scenario, speakers[2])
    play = await _play(scenario, group_id)
    await _stop(scenario, group_id)
    with pytest.raises(PlayerUnavailableError, match="was called off"):
        await play

    await _come_back(scenario, speakers[2])
    await scenario.advance(50)
    assert plug.commands == [True]
    await scenario.advance(30)

    assert plug.commands == [True, False]
    assert _state(scenario, player_ids[2]).powered is False


@pytest.mark.parametrize("group", GROUPS)
async def test_a_new_start_after_a_stop_waits_again_for_a_member_that_still_boots(
    scenario: Scenario, group: str
) -> None:
    """
    A group that is stopped while it waits, and played again, waits again for the same device.

    The stop leaves Den's plug on, so its device still boots and Den shows as unavailable. The
    new play does not start without Den: Den shows as available and powered on again, and
    the group starts on all members once its device is back. The plug is not switched again.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den, den_id = speakers[2], player_ids[2]
    plug = await _switch_off(scenario, den)
    play = await _play(scenario, group_id)
    await _stop(scenario, group_id)
    with pytest.raises(PlayerUnavailableError, match="was called off"):
        await play
    assert _state(scenario, den_id).available is False

    play = await _play(scenario, group_id)

    assert not play.done(), play.exception()
    assert (_state(scenario, den_id).available, _state(scenario, den_id).powered) == (True, True)
    await scenario.advance(10)
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    await _come_back(scenario, den)
    await play
    await scenario.idle()

    assert plug.commands == [True]
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    await _stop(scenario, group_id)
