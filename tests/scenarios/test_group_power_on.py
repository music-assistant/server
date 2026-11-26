"""A group waits for the members it switches on at their power control, and then starts once."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import MediaType, PlaybackState, QueueOption
from music_assistant_models.errors import (
    InsufficientPermissions,
    MusicAssistantError,
    PlayerCommandFailed,
    PlayerUnavailableError,
    ResourceBusyError,
)
from music_assistant_models.media_items import AudioSource
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues.constants import DEFAULT_PAUSE_TIMEOUT
from music_assistant.controllers.players.audio_sources import AUDIO_SOURCE_CLAIM_TIMEOUT
from music_assistant.controllers.players.config_changes import CONFIG_CHANGE_RESUME_DELAY
from music_assistant.controllers.streams.announcements import (
    ANNOUNCEMENT_PCM_FORMAT,
    AnnouncementRender,
)
from music_assistant.models.media_capabilities import AudioSourceMixin
from music_assistant.models.plugin import PluginProvider
from tests.common import as_user, record_lock_waits

from .harness import TEST_TRACK_URI
from .test_commands import THIRD_TRACK_URI
from .test_grouping import (
    STARTUP_TIME,
    _come_back,
    _device,
    _group,
    _grouping_commands,
    _load_music,
    _player_id,
    _speakers,
    _start_up,
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
    from collections.abc import Awaitable, Callable, Coroutine

    from music_assistant_models.queue_item import QueueItem

    from .harness import FakePowerControl, FakeProtocol, Scenario

# the groups a scenario runs for: the type of group player, and whether its members can change
GROUPS = {
    "dynamic sync group": ("sync_group", True),
    "fixed sync group": ("sync_group", False),
    "universal group": ("universal_group", True),
}

# media that is not an item of a queue
MEDIA = PlayerMedia(uri="http://ma.local/stream/line_in.flac", source_id="line_in")
# a live source of a plugin
SOURCE_URI = "spotify_connect--den://audio_source/main"
# a track that the queues of these scenarios do not hold
ANOTHER_TRACK_URI = "test://track/3_3_3"


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
    plugs on and waits for the members to start up: no speaker gets a command meanwhile,
    also not once its device is back. When the start-up time is over the group starts once,
    on all members.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    away = speakers[-switched_off:]
    plugs = [await _switch_off(scenario, speaker) for speaker in away]

    play = await _play(scenario, group_id)

    assert not play.done(), play.exception()
    assert [plug.commands for plug in plugs] == [[True]] * switched_off
    for speaker in away:
        await scenario.advance(2)
        await scenario.register(speaker)
    await scenario.advance(2)
    assert not play.done(), play.exception()
    assert [speaker.commands for speaker in speakers] == [[], [], []]
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE

    await _start_up(scenario, player_ids[-1])
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
    A member whose device is not back when its start-up time is over is left out, logged once.

    Nothing starts while the group waits for it. Once the start-up time of the member is over
    the group starts on the other members, and the member, whose plug is on by then, shows as
    unavailable. A later play of the group, with the plug switched off again, tries again.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den = speakers[2]
    plug = await _switch_off(scenario, den)
    scenario.expect_log("could not be reached")
    _expect_the_form_without(scenario, group)

    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        play = await _play(scenario, group_id)
        await scenario.advance(STARTUP_TIME - 1)
        assert not play.done(), play.exception()
        assert [speaker.commands for speaker in speakers] == [[], [], []]
        await scenario.advance(2)
        await play
        await scenario.idle()

    assert _warnings_about(caplog, "Den") == [
        "Player Den is left out of Downstairs: "
        f"Player Den could not be reached {STARTUP_TIME}s after it was switched on"
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

    The play that waits fails, and nothing starts, also not on the members that are on. Den,
    whose plug stays on, keeps starting up, and stays idle and gets no command once it has
    started up. A group that is powered off reads as off itself while it waits.
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
    state = _state(scenario, den_id)
    assert (state.available, state.powered) == (True, True)
    await _come_back(scenario, den)
    await scenario.idle()

    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    assert den.commands == []
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert _state(scenario, den_id).playback_state == PlaybackState.IDLE
    assert _state(scenario, den_id).synced_to is None
    assert _state(scenario, den_id).active_group is None
    assert plug.commands == [True]


@pytest.mark.parametrize("command", ["stop", "power off"])
async def test_a_command_on_a_member_reaches_the_start_of_the_group_that_owns_its_playback(
    scenario: Scenario, command: str
) -> None:
    """
    A play on a member of a paused group waits for the members of the group, and a stop ends it.

    The sync group, which has a fixed member list, plays on Kitchen and Office: the plug of
    Den refused to switch on. The group is paused and the plug works again. The user presses
    play on Office, which goes to the group: Den's plug is switched on and the play waits.
    The user presses stop on Office, or powers it off: the play fails, the group is stopped,
    and Den stays idle when it has started up.

    :param command: The command Office gets while the play waits.
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
    stop = await scenario.command(
        players.cmd_stop(office_id) if command == "stop" else players.cmd_power(office_id, False)
    )
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
    group on all members once Den has started up.
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
    await scenario.advance(STARTUP_TIME / 2)
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

    The plug of Den is switched on, and the group is on once Den has started up.
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
    its plug: the plug is switched on, Bedroom plays on meanwhile, and once Den has started
    up the group plays the queue on all its members.
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
    once and wait for no lock. A second play of the group waits for the same start-up, not
    for a lock, and the group plays once Den has started up.
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
    A next on a paused group whose play waits for a member returns at once and holds no lock.

    The sync group, which has a fixed member list, plays two tracks on Kitchen and Office: the
    plug of Den refused to switch on. The group is paused and the plug works again. A play of
    the group switches the plug on and waits. A next meanwhile waits for no lock: it moves the
    queue on at once, and the next track starts on all three once Den has started up.
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
    assert _state(scenario, group_id).playback_state == PlaybackState.PAUSED
    assert not play.done(), play.exception()
    await _come_back(scenario, den)
    await play
    await scenario.idle()

    assert queue.current_index == 1
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    assert plug.commands == [True]
    await _stop(scenario, group_id)


async def _play_without_den(
    scenario: Scenario, group: str, group_id: str, den: FakeProtocol
) -> FakePowerControl:
    """
    Let a group play two tracks on its other members, with Den switched off.

    The plug of Den refused to switch on when the group started, and works again by now.

    :param group: Which kind of group, a key of GROUPS.
    :param group_id: The id of the group.
    :param den: The speaker Den, a member of the group.
    :return: The plug of Den.
    """
    plug = await _switch_off(scenario, den)
    control = scenario.mass.players.get_player_control(plug.control_id)
    assert control is not None
    power_on = control.power_on
    _refuse_to_switch_on(scenario, plug)
    _expect_the_form_without(scenario, group)
    # the speakers of a scenario take no next track, which the queue logs when it hands one over
    scenario.expect_log("does not support enqueueing")
    await scenario.play(group_id)
    await scenario.mass.player_queues.play_media(group_id, OTHER_TRACK_URI, option=QueueOption.ADD)
    control.power_on = power_on
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    return plug


async def _playing_without_den(
    scenario: Scenario, group: str
) -> tuple[str, list[FakeProtocol], list[str], FakePowerControl]:
    """
    Let the group Downstairs play two tracks on Kitchen and Office, with Den switched off.

    The plug of Den refused to switch on when the group started, and works again by now.

    :param group: Which kind of group, a key of GROUPS.
    :return: The id of the group, the speakers, the ids of their players and the plug of Den.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    plug = await _play_without_den(scenario, group, group_id, speakers[2])
    return group_id, speakers, player_ids, plug


STEPS = ["next", "previous", "seek", "skip"]


def _step(scenario: Scenario, queue_id: str, step: str) -> Coroutine[Any, Any, None]:
    """
    Return a step within a queue or a seek within its current item, a key of STEPS.

    :param queue_id: The id of the queue.
    :param step: The command.
    """
    queues = scenario.mass.player_queues
    steps = {
        "next": lambda: queues.next(queue_id),
        "previous": lambda: queues.previous(queue_id),
        "seek": lambda: queues.seek(queue_id, 20),
        "skip": lambda: queues.skip(queue_id, 15),
    }
    return steps[step]()


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("command", ["play", *STEPS])
async def test_a_play_a_step_or_a_seek_on_a_group_that_plays_switches_no_member_on(
    scenario: Scenario, group: str, command: str
) -> None:
    """
    A play, a next, a previous and a seek on a group that plays leave a switched-off member off.

    The group plays on Kitchen and Office, without Den. A play is ignored, as on any player
    that plays already, and a next, a previous, a seek and a skip move within what plays:
    they switch nothing on and wait for nothing, and the group plays on.

    :param command: The command the group gets.
    """
    group_id, _, player_ids, plug = await _playing_without_den(scenario, group)

    sent = await scenario.command(
        scenario.mass.players.cmd_play(group_id)
        if command == "play"
        else _step(scenario, group_id, command)
    )
    await scenario.advance(5)

    assert sent.done()
    await sent
    assert plug.commands == []
    assert _state(scenario, player_ids[2]).powered is False
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    await _stop(scenario, group_id)


STARTS_OF_A_PLAYING_GROUP = [
    "queue play",
    "queue resume",
    "queue play index",
    "queue play media",
    "resume",
    "play media",
]


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("start", STARTS_OF_A_PLAYING_GROUP)
async def test_a_start_of_a_group_that_plays_waits_for_its_members(
    scenario: Scenario, group: str, start: str
) -> None:
    """
    A command that starts a group waits for its switched-off members, also while it plays.

    The group plays on Kitchen and Office, without Den. A play, a resume or a play of an item
    of its queue, a play of other media, and a resume or a play of media through the player
    switch Den's plug on and wait, while Kitchen and Office play on. Once Den has started
    up the group starts again, once, on all three. A sync group whose members can change
    let Den go when it started without it: while it plays it has no member to switch on.

    :param start: The command the group gets.
    """
    group_id, speakers, player_ids, plug = await _playing_without_den(scenario, group)
    players, queues = scenario.mass.players, scenario.mass.player_queues
    for speaker in speakers:
        speaker.commands.clear()
    starts = {
        "queue play": lambda: queues.play(group_id),
        "queue resume": lambda: queues.resume(group_id),
        "queue play index": lambda: queues.play_index(group_id, 1),
        "queue play media": lambda: queues.play_media(group_id, OTHER_TRACK_URI),
        "resume": lambda: players.cmd_resume(group_id),
        "play media": lambda: players.play_media(group_id, MEDIA),
    }

    command = await scenario.command(starts[start]())
    await scenario.advance(5)

    if group == "dynamic sync group":
        assert command.done()
        await command
        assert plug.commands == []
        assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [
            True,
            True,
            False,
        ]
        await _stop(scenario, group_id)
        return
    assert not command.done(), command.exception()
    assert plug.commands == [True]
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    await _come_back(scenario, speakers[2])
    await command
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    if GROUPS[group][0] == "sync_group":
        # the leader gets the stream again, and Den is joined to it
        assert sorted(_starts(speaker) for speaker in speakers) == [0, 0, 1]
    else:
        assert [_starts(speaker) for speaker in speakers] == [1, 1, 1]
    assert plug.commands == [True]
    await _stop(scenario, group_id)


async def _bedroom(scenario: Scenario) -> str:
    """Attach the speaker Bedroom, which is in no group, and return the id of its player."""
    bedroom = scenario.provider("sonos").protocol(
        "sonos_bedroom", "Bedroom", identifiers=_device(20)
    )
    await scenario.register(bedroom)
    await scenario.idle()
    return _player_id(scenario, bedroom)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
async def test_a_queue_that_is_transferred_to_a_group_that_plays_waits_for_its_members(
    scenario: Scenario, group: str
) -> None:
    """
    A queue that is transferred to a group that plays waits for the members of the group.

    The group plays on Kitchen and Office, without Den, and Bedroom plays a queue of its own.
    That queue is transferred to the group: Den's plug is switched on, and Bedroom and the
    group play on meanwhile. Once Den has started up the group plays the queue of Bedroom
    on all three.
    """
    group_id, speakers, player_ids, plug = await _playing_without_den(scenario, group)
    bedroom_id = await _bedroom(scenario)
    await scenario.play(bedroom_id)

    transfer = await scenario.command(
        scenario.mass.player_queues.transfer_queue(bedroom_id, group_id)
    )
    await scenario.advance(5)

    assert not transfer.done(), transfer.exception()
    assert plug.commands == [True]
    assert _state(scenario, bedroom_id).playback_state == PlaybackState.PLAYING
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    await _come_back(scenario, speakers[2])
    await scenario.advance(10)
    await transfer
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    assert _state(scenario, bedroom_id).playback_state == PlaybackState.IDLE
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize("after", ["its members played out", "a member plays something else"])
async def test_a_resume_of_a_group_that_still_reads_as_playing_waits_for_its_members(
    scenario: Scenario, group: str, after: str
) -> None:
    """
    A resume of a group that reads as playing while its queue does not play waits for its members.

    The group plays on Kitchen and Office, without Den. Kitchen and Office play the track
    out, or Kitchen plays something of its own instead: the group still reads as playing. A
    resume of its queue starts it: Den's plug is switched on, and the group plays its queue
    on all three once Den has started up.

    :param after: What happened to the playback of the group.
    """
    group_id, speakers, player_ids, plug = await _playing_without_den(scenario, group)
    if after == "its members played out":
        for speaker in speakers[:2]:
            await scenario.report(speaker, playback_state=PlaybackState.IDLE, current_media=None)
    else:
        await scenario.report(
            speakers[0],
            playback_state=PlaybackState.PLAYING,
            active_source="line_in",
            current_media=MEDIA,
        )
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING

    resume = await scenario.command(scenario.mass.player_queues.resume(group_id))
    await scenario.advance(1)

    assert not resume.done(), resume.exception()
    assert plug.commands == [True]
    await _come_back(scenario, speakers[2])
    await resume
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", GROUPS)
async def test_a_group_none_of_whose_members_can_play_fails_and_waits_for_them_at_its_next_start(
    scenario: Scenario, group: str
) -> None:
    """
    A play of a group none of whose members can play fails, and the next play waits for them.

    Kitchen, Office and Den are switched off and their plugs do not answer: the play fails, and
    Downstairs and its queue stay idle with all three as members. The plugs work again and the
    user plays another track: the plugs are switched on, and the group plays on all three once
    they have started up.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    plugs = [await _switch_off(scenario, speaker) for speaker in speakers]
    controls = [scenario.mass.players.get_player_control(plug.control_id) for plug in plugs]
    power_ons = [control.power_on for control in controls if control is not None]
    for plug in plugs:
        _refuse_to_switch_on(scenario, plug)
    _expect_the_form_without(scenario, group)

    play = await _play(scenario, group_id)
    with pytest.raises((PlayerUnavailableError, PlayerCommandFailed)):
        await play
    await scenario.idle()

    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert list(_state(scenario, group_id).group_members) == player_ids
    queue = scenario.queue(group_id)
    assert queue is not None
    assert queue.state == PlaybackState.IDLE

    for control, power_on in zip(controls, power_ons, strict=True):
        assert control is not None
        control.power_on = power_on
    play = await scenario.command(scenario.mass.player_queues.play_media(group_id, OTHER_TRACK_URI))
    await scenario.advance(1)

    assert not play.done(), play.exception()
    assert [plug.commands for plug in plugs] == [[True]] * 3
    for speaker in speakers:
        await scenario.register(speaker)
    await _start_up(scenario, player_ids[0])
    await play
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    await _stop(scenario, group_id)


async def _halt(scenario: Scenario, group_id: str, halted: str) -> None:
    """
    Pause or stop a group that plays.

    :param group_id: The id of the group.
    :param halted: ``"paused"`` or ``"stopped"``.
    """
    if halted == "stopped":
        await _stop(scenario, group_id)
        return
    pause = await scenario.command(scenario.mass.players.cmd_pause(group_id))
    await scenario.advance(2)
    await pause


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("halted", ["paused", "stopped"])
@pytest.mark.parametrize("step", STEPS)
async def test_a_step_or_a_seek_on_a_group_that_does_not_play_waits_for_its_members(
    scenario: Scenario, group: str, halted: str, step: str
) -> None:
    """
    A next, a previous or a seek that makes a group play waits for its switched-off members.

    The group played on Kitchen and Office, without Den, and is paused or stopped. A next, a
    previous, a seek or a skip makes it play again, which is a start: Den's plug is switched
    on and nothing plays yet. A next and a previous return at once and move the queue on, a
    seek and a skip wait. Once Den has started up the group plays the item, at the position
    that was asked for, on all three. A sync group whose members can change let Den go when
    it started without it: while it is paused it has no member to switch on, and once it is
    stopped it has Den back.

    :param halted: Whether the group is paused or stopped.
    :param step: The command the queue of the group gets.
    """
    group_id, speakers, player_ids, plug = await _playing_without_den(scenario, group)
    await _halt(scenario, group_id, halted)
    queue = scenario.queue(group_id)
    assert queue is not None
    assert queue.state != PlaybackState.PLAYING
    position = int(queue.corrected_elapsed_time)
    for speaker in speakers:
        speaker.commands.clear()

    command = await scenario.command(_step(scenario, group_id, step))
    # a next and a previous play a second after they moved the queue on
    await scenario.advance(2)

    if group == "dynamic sync group" and halted == "paused":
        assert command.done()
        await command
        await scenario.idle()
        assert plug.commands == []
        assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [
            True,
            True,
            False,
        ]
        await _stop(scenario, group_id)
        return
    assert command.done() == (step in ("next", "previous"))
    assert plug.commands == [True]
    assert _state(scenario, group_id).playback_state != PlaybackState.PLAYING
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    await _come_back(scenario, speakers[2])
    await command
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    item = queue.current_item
    assert item is not None
    assert item.streamdetails is not None
    assert (queue.current_index, item.streamdetails.seek_position) == {
        "next": (1, 0),
        "previous": (0, 0),
        "seek": (0, 20),
        "skip": (0, position + 15),
    }[step]
    if GROUPS[group][0] == "sync_group":
        assert sorted(_starts(speaker) for speaker in speakers) == [0, 0, 1]
    else:
        assert [_starts(speaker) for speaker in speakers] == [1, 1, 1]
    assert plug.commands == [True]
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize("command", ["next", "previous", "seek"])
async def test_a_step_or_a_seek_on_a_paused_group_player_holds_no_lock_while_it_waits(
    scenario: Scenario, group: str, command: str
) -> None:
    """
    A next, a previous or a seek on a paused group player waits for its members without a lock.

    The group played on Kitchen and Office, without Den, and its queue is paused. A next, a
    previous or a seek on the group player switches Den's plug on and waits for its start-up,
    and no lock of a player is held meanwhile. Once Den has started up the group plays on
    all three.

    :param command: The command the group player gets.
    """
    group_id, speakers, player_ids, plug = await _playing_without_den(scenario, group)
    players = scenario.mass.players
    pause = await scenario.command(scenario.mass.player_queues.pause(group_id))
    await scenario.advance(2)
    await pause
    for speaker in speakers:
        speaker.commands.clear()

    sent = await scenario.command(
        {
            "next": lambda: players.cmd_next_track(group_id),
            "previous": lambda: players.cmd_previous_track(group_id),
            "seek": lambda: players.cmd_seek(group_id, 10),
        }[command]()
    )
    await scenario.advance(2)

    assert sent.done() == (command != "seek")
    assert plug.commands == [True]
    assert not any(lock.locked() for lock in players._player_command_locks.values())
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    await _come_back(scenario, speakers[2])
    await sent
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    queue = scenario.queue(group_id)
    assert queue is not None
    assert queue.current_item is not None
    assert queue.current_item.streamdetails is not None
    assert (queue.current_index, queue.current_item.streamdetails.seek_position) == {
        "next": (1, 0),
        "previous": (0, 0),
        "seek": (0, 10),
    }[command]
    assert plug.commands == [True]
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize("call_off", ["stop", "pause", "power off"])
async def test_a_stop_during_the_wait_of_a_next_starts_nothing(
    scenario: Scenario, group: str, call_off: str, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A stop, a pause or a power off ends the wait of a next for the members of a group.

    The group played on Kitchen and Office, without Den, and is stopped. A next switches
    Den's plug on and waits. The user stops the group, pauses it or powers it off: nothing
    starts, also not when Den has started up, the plug stays on, and nothing is logged
    above the debug level.

    :param call_off: The command the group gets while the next waits.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    if call_off == "power off":
        await scenario.configure_player(group_id, power_control=PLAYER_CONTROL_FAKE)
    plug = await _play_without_den(scenario, group, group_id, speakers[2])
    await _stop(scenario, group_id)
    for speaker in speakers:
        speaker.commands.clear()
    players = scenario.mass.players
    step = await scenario.command(scenario.mass.player_queues.next(group_id))
    await scenario.advance(2)
    assert step.done()
    await step
    assert plug.commands == [True]

    with caplog.at_level(logging.DEBUG, logger="music_assistant"):
        command = await scenario.command(
            {
                "stop": lambda: players.cmd_stop(group_id),
                "pause": lambda: players.cmd_pause(group_id),
                "power off": lambda: players.cmd_power(group_id, False),
            }[call_off]()
        )
        await scenario.advance(10)
        await command
        await _come_back(scenario, speakers[2])
        await scenario.idle()

    called_off = [record for record in caplog.records if "called off" in record.getMessage()]
    assert {record.levelno for record in called_off} == {logging.DEBUG}
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert _state(scenario, player_ids[2]).playback_state == PlaybackState.IDLE
    queue = scenario.queue(group_id)
    assert queue is not None
    assert queue.current_index == 1
    assert plug.commands == [True]


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize("step", ["seek", "skip"])
async def test_a_stop_during_the_wait_of_a_seek_ends_the_seek_and_starts_nothing(
    scenario: Scenario, group: str, step: str
) -> None:
    """
    A stop ends the wait of a seek for the members of a group, and the seek with it.

    The group played on Kitchen and Office, without Den, and is paused. A seek or a skip
    switches Den's plug on and waits. The user stops the group: the command fails as called
    off and nothing starts, also not when Den has started up. A skip that was called off
    does not add to a later one: a skip of 15 seconds then moves 15 seconds on.

    :param step: The command the queue of the group gets.
    """
    group_id, speakers, player_ids, plug = await _playing_without_den(scenario, group)
    await _halt(scenario, group_id, "paused")
    for speaker in speakers:
        speaker.commands.clear()
    command = await scenario.command(_step(scenario, group_id, step))
    await scenario.advance(2)
    assert not command.done(), command.exception()
    assert plug.commands == [True]

    await _stop(scenario, group_id, idle=False)

    with pytest.raises(PlayerUnavailableError, match="was called off"):
        await command
    await _come_back(scenario, speakers[2])
    await scenario.idle()
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert plug.commands == [True]
    if step == "seek":
        return
    queue = scenario.queue(group_id)
    assert queue is not None
    position = int(queue.corrected_elapsed_time)
    skip = await scenario.command(_step(scenario, group_id, "skip"))
    await scenario.advance(5)
    await skip
    await scenario.idle()
    assert queue.current_item is not None
    assert queue.current_item.streamdetails is not None
    assert queue.current_item.streamdetails.seek_position == position + 15
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
async def test_two_nexts_during_one_wait_start_a_group_once_with_the_last_item(
    scenario: Scenario, group: str
) -> None:
    """
    Two presses of next on a group that does not play leave one wait and one start.

    The group holds three tracks, played the first on Kitchen and Office, without Den, and
    is stopped. A next switches Den's plug on and waits. A second next, while it waits, moves
    the queue on once more and joins that wait: the plug is switched once, and once Den
    has started up the group starts once, with the third track, on all three.
    """
    group_id, speakers, player_ids, plug = await _playing_without_den(scenario, group)
    players, queues = scenario.mass.players, scenario.mass.player_queues
    await queues.play_media(group_id, THIRD_TRACK_URI, option=QueueOption.ADD)
    await _stop(scenario, group_id)
    for speaker in speakers:
        speaker.commands.clear()

    waits = []
    for _ in range(2):
        step = await scenario.command(queues.next(group_id))
        await scenario.advance(2)
        assert step.done()
        await step
        waits.append(list(players._member_power_ons))

    # the second next joined the wait of the first
    assert len(waits[0]) == 1
    assert waits[1] == waits[0]
    assert plug.commands == [True]
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    await _come_back(scenario, speakers[2])
    await scenario.idle()

    queue = scenario.queue(group_id)
    assert queue is not None
    assert queue.current_index == 2
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    if GROUPS[group][0] == "sync_group":
        assert sorted(_starts(speaker) for speaker in speakers) == [0, 0, 1]
    else:
        assert [_starts(speaker) for speaker in speakers] == [1, 1, 1]
    assert plug.commands == [True]
    await _stop(scenario, group_id)


@pytest.mark.parametrize("step", STEPS)
async def test_a_switched_off_player_refuses_a_step_and_a_seek(
    scenario: Scenario, step: str
) -> None:
    """
    A single player that is switched off at its plug refuses a next, a previous and a seek.

    Den, which is in no group, holds two tracks and is stopped, and its plug is switched off.
    A next, a previous, a seek and a skip are refused, and the plug stays off.

    :param step: The command the queue of Den gets.
    """
    _, (den,) = await _speakers(scenario, "Den")
    den_id = _player_id(scenario, den)
    await scenario.play(den_id)
    await scenario.mass.player_queues.play_media(den_id, OTHER_TRACK_URI, option=QueueOption.ADD)
    await _stop(scenario, den_id)
    plug = await _switch_off(scenario, den)

    command = await scenario.command(_step(scenario, den_id, step))
    await scenario.advance(2)

    assert command.done()
    with pytest.raises(PlayerUnavailableError, match="is switched off"):
        await command
    assert plug.commands == []


def _live_source(scenario: Scenario, monkeypatch: pytest.MonkeyPatch) -> str:
    """
    Let the server know a live source of a plugin, and return its uri.

    Only the lookup of the source is a stand-in: selecting and playing it is the real code.
    """
    instance_id = SOURCE_URI.split("://", maxsplit=1)[0]
    source = AudioSource(
        item_id="main",
        provider=instance_id,
        name="Spotify Connect",
        provider_mappings={
            ProviderMapping(
                item_id="main", provider_domain="spotify_connect", provider_instance=instance_id
            )
        },
    )
    provider = MagicMock(spec=PluginProvider, instance_id=instance_id)
    resolve = scenario.mass.players._resolve_audio_source_uri

    async def _resolve(uri: str) -> tuple[AudioSource, AudioSourceMixin] | None:
        return (source, provider) if uri == SOURCE_URI else await resolve(uri)

    monkeypatch.setattr(scenario.mass.players, "_resolve_audio_source_uri", _resolve)
    return SOURCE_URI


async def _group_with_den_switched_off(
    scenario: Scenario, group: str, state: str
) -> tuple[str, list[FakeProtocol], list[str], FakePowerControl]:
    """
    Let the group Downstairs play on Kitchen and Office or be idle, with Den switched off.

    :param group: Which kind of group, a key of GROUPS.
    :param state: ``"plays"`` or ``"is idle"``.
    :return: The id of the group, the speakers, the ids of their players and the plug of Den.
    """
    if state == "plays":
        return await _playing_without_den(scenario, group)
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    plug = await _switch_off(scenario, speakers[2])
    return group_id, speakers, player_ids, plug


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize("state", ["plays", "is idle"])
@pytest.mark.parametrize("source", ["its own queue", "the queue of another player"])
async def test_a_selection_of_a_queue_switches_no_member_on(
    scenario: Scenario, group: str, state: str, source: str
) -> None:
    """
    A queue that is selected as the source of a group starts nothing and switches no member on.

    Den is switched off at its plug, and the group plays on Kitchen and Office or is idle.
    Its own queue, or the queue of Bedroom, is selected as its source. That only names what
    the group plays next: Den's plug stays off and nothing waits.

    :param state: Whether the group plays or is idle.
    :param source: The queue that is selected.
    """
    group_id, _, player_ids, plug = await _group_with_den_switched_off(scenario, group, state)
    bedroom_id = await _bedroom(scenario)

    select = await scenario.command(
        scenario.mass.players.select_source(
            group_id, None if source == "its own queue" else bedroom_id
        )
    )
    await scenario.advance(5)

    assert select.done()
    await select
    assert plug.commands == []
    assert _state(scenario, player_ids[2]).powered is False
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize("state", ["plays", "is idle"])
@pytest.mark.parametrize("through", ["the player", "its queue"])
async def test_a_selection_of_a_source_that_plays_waits_for_the_members_of_a_group(
    scenario: Scenario, group: str, state: str, through: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A source that plays and is selected on a group starts the group, which waits for its members.

    Den is switched off at its plug, and the group plays on Kitchen and Office or is idle. A
    live source of a plugin is selected on the group, or played on its queue, which hands it
    to the player: Den's plug is switched on and the command waits. Once Den has started
    up the source plays on all three.

    :param state: Whether the group plays or is idle.
    :param through: Whether the source is selected on the player or played on its queue.
    """
    group_id, speakers, player_ids, plug = await _group_with_den_switched_off(
        scenario, group, state
    )
    source = _live_source(scenario, monkeypatch)
    for speaker in speakers:
        speaker.commands.clear()

    select = await scenario.command(
        scenario.mass.players.select_source(group_id, source)
        if through == "the player"
        else scenario.mass.player_queues.play_media(group_id, source)
    )
    await scenario.advance(5)

    assert not select.done(), select.exception()
    assert plug.commands == [True]
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    await _come_back(scenario, speakers[2])
    await select
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    assert _state(scenario, group_id).active_source == source
    assert plug.commands == [True]
    stop = await scenario.command(scenario.mass.players.cmd_stop(group_id))
    # no fake speaker fetches the stream of the source, so the server lets go of it as
    # never streamed
    await scenario.advance(AUDIO_SOURCE_CLAIM_TIMEOUT)
    await stop
    await scenario.idle()


async def _halted_with_three_tracks(
    scenario: Scenario, group: str, halted: str
) -> tuple[str, list[FakeProtocol], list[str], FakePowerControl]:
    """
    Let the group Downstairs hold three tracks and be paused or stopped, with Den switched off.

    It played the first track on Kitchen and Office for some seconds: the plug of Den refused
    to switch on when it started, and works again by now.

    :param group: Which kind of group, a key of GROUPS.
    :param halted: ``"paused"`` or ``"stopped"``.
    :return: The id of the group, the speakers, the ids of their players and the plug of Den.
    """
    group_id, speakers, player_ids, plug = await _playing_without_den(scenario, group)
    await scenario.mass.player_queues.play_media(group_id, THIRD_TRACK_URI, option=QueueOption.ADD)
    # the first track gets a position to resume from
    await scenario.advance(7)
    await _halt(scenario, group_id, halted)
    for speaker in speakers:
        speaker.commands.clear()
    return group_id, speakers, player_ids, plug


def _streams(group: str, speakers: list[FakeProtocol]) -> bool:
    """
    Return whether a group started once on its speakers since their commands were cleared.

    A sync group sends one stream to its leader, a universal group one to each member.

    :param group: Which kind of group, a key of GROUPS.
    :param speakers: The speakers of the group.
    """
    starts = [_starts(speaker) for speaker in speakers]
    if GROUPS[group][0] == "sync_group":
        return sorted(starts) == [0, 0, 1]
    return starts == [1] * len(speakers)


# what takes the place of a next to the second track of a queue that still waits: the track
# the group then plays, and the position it plays from (None: where another queue was)
TAKES_OVER = {
    "queue resume": (OTHER_TRACK_URI, 0),
    "queue play": (OTHER_TRACK_URI, 0),
    "resume": (OTHER_TRACK_URI, 0),
    "play": (OTHER_TRACK_URI, 0),
    "other media": (ANOTHER_TRACK_URI, 0),
    "another item": (THIRD_TRACK_URI, 0),
    "seek": (OTHER_TRACK_URI, 20),
    "transfer": (TEST_TRACK_URI, None),
}


def _take_over(
    scenario: Scenario, group_id: str, bedroom_id: str, start: str
) -> Coroutine[Any, Any, None]:
    """
    Return a command that starts a group, a key of TAKES_OVER.

    :param group_id: The id of the group.
    :param bedroom_id: The id of the player whose queue a transfer moves to the group.
    :param start: The command.
    """
    players, queues = scenario.mass.players, scenario.mass.player_queues
    starts = {
        "queue resume": lambda: queues.resume(group_id),
        "queue play": lambda: queues.play(group_id),
        "resume": lambda: players.cmd_resume(group_id),
        "play": lambda: players.cmd_play(group_id),
        "other media": lambda: queues.play_media(group_id, ANOTHER_TRACK_URI),
        "another item": lambda: queues.play_index(group_id, 2),
        "seek": lambda: queues.seek(group_id, 20),
        "transfer": lambda: queues.transfer_queue(bedroom_id, group_id),
    }
    return starts[start]()


# how long after a next a start arrives: while the next still holds its item back for a second,
# or while it waits for the members
ARRIVES = {"in its debounce": 0.5, "in its wait": 2}


@pytest.mark.parametrize(
    ("group", "halted"),
    [
        ("fixed sync group", "paused"),
        ("fixed sync group", "stopped"),
        ("universal group", "stopped"),
    ],
)
@pytest.mark.parametrize("arrives", ARRIVES)
@pytest.mark.parametrize("start", TAKES_OVER)
async def test_a_start_during_the_wait_of_a_next_takes_its_place(
    scenario: Scenario, group: str, halted: str, arrives: str, start: str
) -> None:
    """
    A start that arrives while a next waits for the members of a group takes its place.

    The group holds three tracks, played the first on Kitchen and Office, without Den, and is
    paused or stopped. A next moves its queue to the second track, switches Den's plug on and
    waits. Then the user resumes or plays the group or its queue, plays other media or the
    third track on it, seeks, or transfers the queue of Bedroom to it: that start waits for
    Den as well, and the next is gone. Once Den has started up every speaker gets one
    stream, of what the start asked for: the second track for a resume and a play, from its
    beginning, and for a seek, from that position; else the media, the track or the queue.
    The plug is switched once, and the queue stays on what plays.

    :param halted: Whether the group is paused or stopped.
    :param arrives: When the start arrives, a key of ARRIVES.
    :param start: The command that starts the group, a key of TAKES_OVER.
    """
    group_id, speakers, player_ids, plug = await _halted_with_three_tracks(scenario, group, halted)
    bedroom_id = ""
    if start == "transfer":
        bedroom_id = await _bedroom(scenario)
        await scenario.play(bedroom_id)
    step = await scenario.command(scenario.mass.player_queues.next(group_id))
    await scenario.advance(ARRIVES[arrives])
    await step

    sent = await scenario.command(_take_over(scenario, group_id, bedroom_id, start))
    await scenario.advance(3)

    assert not sent.done(), sent.exception()
    assert plug.commands == [True]
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    await _come_back(scenario, speakers[2])
    await sent
    # what a next that was still there would play on top of it has started by now
    await scenario.advance(5)
    await scenario.idle()

    assert _streams(group, speakers)
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    queue = scenario.queue(group_id)
    assert queue is not None
    assert queue.current_item is not None
    assert queue.current_item.streamdetails is not None
    uri, position = TAKES_OVER[start]
    assert queue.current_item.uri == uri
    if position is not None:
        assert queue.current_item.streamdetails.seek_position == position
    assert plug.commands == [True]
    await _stop(scenario, group_id)


@pytest.mark.parametrize("start", ["queue resume", "another item", "seek"])
async def test_a_user_without_access_to_a_group_leaves_a_next_that_waits_in_place(
    scenario: Scenario, start: str
) -> None:
    """
    A user who may not use a group does not take the place of a next that waits on it.

    The sync group, which has a fixed member list, holds three tracks and is stopped, and a
    next to its second track waits for Den. A user who may use Kitchen only resumes the queue
    of the group, plays its third track or seeks: each is refused. Once Den has started up
    the second track plays, on all three.

    :param start: The command the user sends, a key of TAKES_OVER.
    """
    group = "fixed sync group"
    group_id, speakers, player_ids, plug = await _halted_with_three_tracks(
        scenario, group, "stopped"
    )
    step = await scenario.command(scenario.mass.player_queues.next(group_id))
    await scenario.advance(2)
    await step

    with as_user([player_ids[0]]):
        sent = await scenario.command(_take_over(scenario, group_id, "", start))
    await scenario.advance(1)

    assert sent.done()
    assert isinstance(sent.exception(), InsufficientPermissions)
    await _come_back(scenario, speakers[2])
    await scenario.advance(5)
    await scenario.idle()
    assert _streams(group, speakers)
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    queue = scenario.queue(group_id)
    assert queue is not None
    assert queue.current_item is not None
    assert queue.current_item.uri == OTHER_TRACK_URI
    assert plug.commands == [True]
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize(
    "media", ["a live source on its queue", "a live source on the player", "media of a player"]
)
async def test_media_that_is_not_of_the_queue_takes_the_place_of_a_next_that_waits(
    scenario: Scenario, group: str, media: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Media that is not of the queue and starts a group takes the place of a next that waits.

    The group holds three tracks and is stopped, and a next waits for Den. A live source of a
    plugin is played on the queue of the group or selected on the group player, or media is
    played on the group player itself: that start waits for Den as well, and the next is
    gone. Once Den has started up every speaker gets one stream, and the queue of the group
    plays nothing on top of it.

    :param media: What is played on the group.
    """
    group_id, speakers, player_ids, plug = await _halted_with_three_tracks(
        scenario, group, "stopped"
    )
    players, queues = scenario.mass.players, scenario.mass.player_queues
    source = _live_source(scenario, monkeypatch)
    step = await scenario.command(queues.next(group_id))
    await scenario.advance(2)
    await step

    starts = {
        "a live source on its queue": lambda: queues.play_media(group_id, source),
        "a live source on the player": lambda: players.select_source(group_id, source),
        "media of a player": lambda: players.play_media(group_id, MEDIA),
    }
    sent = await scenario.command(starts[media]())
    await scenario.advance(3)

    assert not sent.done(), sent.exception()
    assert plug.commands == [True]
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    await _come_back(scenario, speakers[2])
    await sent
    await scenario.advance(5)
    await scenario.idle()

    assert _streams(group, speakers)
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    if media != "media of a player":
        assert _state(scenario, group_id).active_source == source
    assert plug.commands == [True]
    stop = await scenario.command(players.cmd_stop(group_id))
    # no fake speaker fetches the stream of a live source, so the server lets go of it as
    # never streamed
    await scenario.advance(AUDIO_SOURCE_CLAIM_TIMEOUT)
    await stop
    await scenario.idle()


@pytest.mark.parametrize("play", ["queue play", "play"])
async def test_a_play_right_after_a_next_on_a_paused_speaker_starts_the_next_track(
    scenario: Scenario, play: str
) -> None:
    """
    A play in the second after a next on a paused speaker starts the next track, once.

    Bedroom, which is in no group, holds two tracks and is paused on the first. A next moves
    its queue to the second track and holds that back for a second, for a further press. A
    play of the queue or of the speaker within that second takes the place of the next: the
    second track starts from its beginning, once, and the speaker is not unpaused on the
    first.

    :param play: Whether the queue or the speaker is played.
    """
    _, speakers = await _speakers(scenario, "Bedroom")
    bedroom = speakers[0]
    bedroom_id = _player_id(scenario, bedroom)
    players, queues = scenario.mass.players, scenario.mass.player_queues
    # the speakers of a scenario take no next track, which the queue logs when it hands one over
    scenario.expect_log("does not support enqueueing")
    await scenario.play(bedroom_id)
    await queues.play_media(bedroom_id, OTHER_TRACK_URI, option=QueueOption.ADD)
    await scenario.advance(7)
    await _halt(scenario, bedroom_id, "paused")
    bedroom.commands.clear()

    await queues.next(bedroom_id)
    await scenario.advance(0.5)
    await (queues.play(bedroom_id) if play == "queue play" else players.cmd_play(bedroom_id))
    # what a next that was still there would play on top of it has started by now
    await scenario.advance(5)
    await scenario.idle()

    assert [command[0] for command in bedroom.commands] == ["play_media"]
    assert _state(scenario, bedroom_id).playback_state == PlaybackState.PLAYING
    queue = scenario.queue(bedroom_id)
    assert queue is not None
    assert queue.current_item is not None
    assert queue.current_item.streamdetails is not None
    assert queue.current_item.uri == OTHER_TRACK_URI
    assert queue.current_item.streamdetails.seek_position == 0
    await _stop(scenario, bedroom_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize("start", ["another item", "seek"])
async def test_a_next_during_the_wait_of_a_start_plays_nothing_on_top_of_it(
    scenario: Scenario, group: str, start: str
) -> None:
    """
    A next that still waits when a start of its queue plays, plays nothing on top of it.

    The group holds three tracks and is stopped, and Den is switched off. A play of its third
    track, or a seek, switches Den's plug on and waits. A next meanwhile moves the queue to
    the second track and waits as well. When Den has started up the start goes first: the
    third track plays, or the second one from the position of the seek, on all
    three. Every speaker gets one stream, and the next plays nothing on top of it.

    :param start: The command that waits, a key of TAKES_OVER.
    """
    group_id, speakers, player_ids, plug = await _halted_with_three_tracks(
        scenario, group, "stopped"
    )
    sent = await scenario.command(_take_over(scenario, group_id, "", start))
    await scenario.advance(2)
    assert not sent.done(), sent.exception()
    assert plug.commands == [True]

    step = await scenario.command(scenario.mass.player_queues.next(group_id))
    await scenario.advance(2)
    await step
    await _come_back(scenario, speakers[2])
    await sent
    await scenario.advance(5)
    await scenario.idle()

    assert _streams(group, speakers)
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    queue = scenario.queue(group_id)
    assert queue is not None
    assert queue.current_item is not None
    assert queue.current_item.streamdetails is not None
    uri, position = TAKES_OVER[start]
    assert queue.current_item.uri == uri
    assert queue.current_item.streamdetails.seek_position == position
    assert plug.commands == [True]
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
async def test_a_second_next_in_the_last_moment_of_the_wait_starts_a_group_once(
    scenario: Scenario, group: str
) -> None:
    """
    A second next in the last moment of the wait of the first leaves one start, of the last item.

    The group holds three tracks and is stopped, and a next to its second track waits for
    Den. Half a second before Den's start-up time is over, the user presses next once more.
    The group starts once, with the third track, on all three.
    """
    group_id, speakers, player_ids, plug = await _halted_with_three_tracks(
        scenario, group, "stopped"
    )
    queues = scenario.mass.player_queues
    first = await scenario.command(queues.next(group_id))
    await scenario.advance(3)
    await first
    assert plug.commands == [True]

    await scenario.register(speakers[2])
    den = scenario.mass.players.get_player(player_ids[2])
    assert den is not None
    assert den.startup_ends_at is not None
    await scenario.advance(den.startup_ends_at - scenario.mass.loop.time() - 0.5)
    await queues.next(group_id)
    await scenario.advance(5)
    await scenario.idle()

    assert _streams(group, speakers)
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    queue = scenario.queue(group_id)
    assert queue is not None
    assert queue.current_item is not None
    assert queue.current_item.uri == THIRD_TRACK_URI
    assert plug.commands == [True]
    await _stop(scenario, group_id)


MEMBER_PLAYS = ["queue resume", "queue play index", "play media"]


async def _office_with_a_queue_in_a_group_without_den(
    scenario: Scenario, group: str
) -> tuple[str, list[FakeProtocol], list[str], FakePowerControl]:
    """
    Let the group Downstairs play on Kitchen and Office, whose own queue holds a track.

    Den is switched off: its plug refused to switch on when the group started, and works
    again by now.

    :param group: Which kind of group, a key of GROUPS.
    :return: The id of the group, the speakers, the ids of their players and the plug of Den.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    await _load_music(scenario)
    await scenario.mass.player_queues.play_media(
        player_ids[1], TEST_TRACK_URI, option=QueueOption.ADD
    )
    plug = await _play_without_den(scenario, group, group_id, speakers[2])
    await scenario.idle()
    assert _plays_along(scenario, group_id, player_ids[1])
    for speaker in speakers:
        speaker.commands.clear()
    return group_id, speakers, player_ids, plug


def _play_on_office(scenario: Scenario, office_id: str, play: str) -> Coroutine[Any, Any, None]:
    """
    Return a play command for Office, a key of MEMBER_PLAYS.

    :param office_id: The id of the player of Office.
    :param play: The command.
    """
    players, queues = scenario.mass.players, scenario.mass.player_queues
    plays = {
        "queue resume": lambda: queues.resume(office_id),
        "queue play index": lambda: queues.play_index(office_id, 0),
        "play media": lambda: players.play_media(office_id, MEDIA),
    }
    return plays[play]()


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize("play", MEMBER_PLAYS)
async def test_a_play_on_a_member_that_goes_to_its_group_waits_for_the_members_of_the_group(
    scenario: Scenario, group: str, play: str
) -> None:
    """
    A play on a member that goes to its group waits for the switched-off members of the group.

    The group plays on Kitchen and Office, without Den. Office sends what is played on it to
    its group. A resume or a play of an item of the queue of Office, or media played on
    Office, starts the group: Den's plug is switched on and the command waits, and once Den
    has started up the group plays on all three.

    :param play: The command Office gets.
    """
    group_id, speakers, player_ids, plug = await _office_with_a_queue_in_a_group_without_den(
        scenario, group
    )
    office_id = player_ids[1]
    await scenario.configure_player(office_id, play_media_overrides_group=False)

    command = await scenario.command(_play_on_office(scenario, office_id, play))
    await scenario.advance(5)

    assert not command.done(), command.exception()
    assert plug.commands == [True]
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    await _come_back(scenario, speakers[2])
    await command
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    assert plug.commands == [True]
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize("play", MEMBER_PLAYS)
async def test_a_play_on_a_member_that_leaves_its_group_switches_no_member_on(
    scenario: Scenario, group: str, play: str
) -> None:
    """
    A play on a member that is taken out of its group switches no member of the group on.

    The group plays on Kitchen and Office, without Den. Office plays what is played on it by
    itself, as a player does unless it is set otherwise. A resume or a play of an item of the
    queue of Office, or media played on Office, takes Office out of the group and plays on
    Office alone: Den's plug stays off and nothing waits.

    :param play: The command Office gets.
    """
    group_id, _, player_ids, plug = await _office_with_a_queue_in_a_group_without_den(
        scenario, group
    )
    office_id = player_ids[1]

    command = await scenario.command(_play_on_office(scenario, office_id, play))
    await scenario.advance(15)

    assert command.done()
    await command
    assert plug.commands == []
    office = _state(scenario, office_id)
    assert office.playback_state == PlaybackState.PLAYING
    assert (office.active_group, office.synced_to) == (None, None)
    assert _state(scenario, player_ids[2]).powered is False
    await _stop(scenario, office_id)
    await _stop(scenario, group_id)


async def test_a_play_on_a_speaker_that_a_group_took_meanwhile_switches_no_member_on(
    scenario: Scenario,
) -> None:
    """
    A play on a speaker that a group took during that play fails as busy and wakes nobody.

    Downstairs, a universal group of Kitchen and Den, plays on Kitchen: Den is switched off.
    Office sends what is played on it to its group. Its own queue is started, and the music
    takes a while to load. Meanwhile Office is added to Downstairs, so the play now goes to
    Downstairs, whose lock it can not wait for while it holds Office: it fails as busy at
    once. Den's plug stays off, and Office is added to the group and plays along.
    """
    _, speakers = await _speakers(scenario, "Kitchen", "Office", "Den")
    kitchen_id, office_id, den_id = (_player_id(scenario, speaker) for speaker in speakers)
    group_id = await _group(scenario, "universal_group", kitchen_id, den_id)
    players, queues = scenario.mass.players, scenario.mass.player_queues
    await scenario.configure_player(office_id, play_media_overrides_group=False)
    await _load_music(scenario)
    await queues.play_media(office_id, TEST_TRACK_URI, option=QueueOption.ADD)
    plug = await _play_without_den(scenario, "universal group", group_id, speakers[2])
    loads, loaded = asyncio.Event(), asyncio.Event()
    load_item = queues._load_item

    async def _slow_load(item: QueueItem, *args: Any, **kwargs: Any) -> None:
        """Load an item of the queue of Office only once the music is there."""
        if item.queue_id == office_id:
            loads.set()
            await loaded.wait()
        await load_item(item, *args, **kwargs)

    with patch.object(queues, "_load_item", _slow_load):
        play = await scenario.command(queues.play_index(office_id, 0))
        assert loads.is_set()
        addition = await scenario.command(
            players.cmd_set_members(group_id, player_ids_to_add=[office_id])
        )
        await scenario.advance(2)
        assert _state(scenario, office_id).active_group == group_id
        loaded.set()
        await scenario.advance(1)

    assert play.done()
    assert isinstance(play.exception(), ResourceBusyError)
    assert plug.commands == []
    await addition
    await scenario.idle()
    assert _plays_along(scenario, group_id, office_id)
    assert _state(scenario, den_id).powered is False
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
@pytest.mark.parametrize("setting", ["of the player", "of the queue"])
async def test_a_restart_for_a_changed_setting_switches_no_member_on(
    scenario: Scenario, group: str, setting: str
) -> None:
    """
    A queue of a group that is restarted for a changed setting leaves a switched-off member off.

    The group plays on Kitchen and Office, without Den. A setting of the player or of its
    queue changes that only applies to a new stream, so the server stops the queue and resumes
    it. That is no start of the group: it plays on Kitchen and Office again without waiting,
    and Den's plug stays off.

    :param setting: Whose setting changes.
    """
    group_id, _, player_ids, plug = await _playing_without_den(scenario, group)
    players = scenario.mass.players
    if setting == "of the player":
        group_player = players.get_player(group_id)
        assert group_player is not None
        await players._restart_playback_after_config_change(group_player, requires_restart=True)
    else:
        entry = next(
            entry
            for entry in scenario.mass.player_queues.get_queue_config_entries()
            if entry.requires_reload and entry.options
        )
        value = next(o.value for o in entry.options if o.value != entry.default_value)
        await scenario.mass.config.save_player_queue_config(group_id, {entry.key: value})
    assert _state(scenario, group_id).playback_state != PlaybackState.PLAYING
    await scenario.advance(CONFIG_CHANGE_RESUME_DELAY + 5)

    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert plug.commands == []
    assert _state(scenario, player_ids[2]).powered is False
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", ["fixed sync group", "universal group"])
async def test_a_restart_for_changed_dsp_settings_switches_no_member_on(
    scenario: Scenario, group: str
) -> None:
    """
    A group that plays no queue and is restarted for changed DSP settings leaves a member off.

    The group plays media that is no item of its queue on Kitchen and Office, without Den:
    the plug of Den refused when it started, and works again. Its DSP settings change, so the
    server stops it and plays it again. That is no start of the group: Den's plug stays off
    and nothing waits.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    plug = await _switch_off(scenario, speakers[2])
    control = scenario.mass.players.get_player_control(plug.control_id)
    assert control is not None
    power_on = control.power_on
    _refuse_to_switch_on(scenario, plug)
    _expect_the_form_without(scenario, group)
    players = scenario.mass.players
    play = await scenario.command(players.play_media(group_id, MEDIA))
    await scenario.advance(5)
    await play
    control.power_on = power_on
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING

    restart = await scenario.command(players.on_player_dsp_change(group_id))
    await scenario.advance(5)

    assert restart.done()
    with contextlib.suppress(MusicAssistantError):
        # a group that plays no queue may fail to play again, with or without a member that
        # is switched off
        await restart
    assert plug.commands == []
    assert _state(scenario, player_ids[2]).powered is False
    await _stop(scenario, group_id)


@pytest.mark.parametrize("command", ["resume", "play index", "play media", "play", "power on"])
async def test_a_user_without_access_to_a_group_switches_no_member_on(
    scenario: Scenario, command: str
) -> None:
    """
    A user who may not use a group can not have its members switched on.

    Den, a member of the sync group, is switched off at its plug. A user who may use Kitchen
    only sends the queue of the group a resume or a play of an item or of media, or the group
    a play or a power on. Each is refused, and the plug stays off.

    :param command: The command the user sends.
    """
    group_id, speakers, player_ids = await _downstairs(
        scenario, "fixed sync group", "Kitchen", "Office", "Den"
    )
    plug = await _switch_off(scenario, speakers[2])
    await _load_music(scenario)
    players, queues = scenario.mass.players, scenario.mass.player_queues
    commands = {
        "resume": lambda: queues.resume(group_id),
        "play index": lambda: queues.play_index(group_id, 0),
        "play media": lambda: queues.play_media(group_id, TEST_TRACK_URI),
        "play": lambda: players.cmd_play(group_id),
        "power on": lambda: players.cmd_power(group_id, True),
    }

    with as_user([player_ids[0]]):
        sent = await scenario.command(commands[command]())
    await scenario.advance(1)

    assert sent.done()
    assert isinstance(sent.exception(), InsufficientPermissions)
    assert plug.commands == []


@pytest.mark.parametrize("play", MEMBER_PLAYS)
async def test_a_user_without_access_to_the_group_of_a_member_switches_no_member_on(
    scenario: Scenario, play: str
) -> None:
    """
    A play on a member that goes to its group switches nothing on without access to the group.

    The sync group, which has a fixed member list, plays on Kitchen and Office, without Den.
    Office sends what is played on it to its group. A user who may use Office only resumes
    the queue of Office, plays an item of it or plays media on Office. Each is refused, and
    Den's plug stays off.

    :param play: The command the user sends to Office.
    """
    group_id, _, player_ids, plug = await _office_with_a_queue_in_a_group_without_den(
        scenario, "fixed sync group"
    )
    office_id = player_ids[1]
    await scenario.configure_player(office_id, play_media_overrides_group=False)

    with as_user([office_id]):
        sent = await scenario.command(_play_on_office(scenario, office_id, play))
    await scenario.advance(1)

    assert sent.done()
    assert isinstance(sent.exception(), InsufficientPermissions)
    assert plug.commands == []
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
@pytest.mark.parametrize("command", ["set members", "group"])
async def test_a_switched_off_speaker_that_is_added_is_waited_for_and_added_once(
    scenario: Scenario, owner: str, command: str
) -> None:
    """
    A switched-off speaker that is added to a playing leader or group is switched on first.

    The command switches its plug on and waits: the leader or group plays on and gets no
    command for it. Once the speaker has started up it is added, once, and plays along.

    :param command: The command that adds the speaker.
    """
    owner_id, speakers, player_ids, plug = await _playing_owner(scenario, owner)
    den, den_id = speakers[2], player_ids[2]
    players = scenario.mass.players

    add = await scenario.command(
        players.cmd_set_members(owner_id, player_ids_to_add=[den_id])
        if command == "set members"
        else players.cmd_group(den_id, owner_id)
    )

    assert not add.done(), add.exception()
    assert plug.commands == [True]
    await scenario.advance(STARTUP_TIME / 2)
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
    A switched-off speaker that is added and is not back after its start-up time is not added.

    That is logged once, with the speaker and the leader or group it is left out of, which
    plays on.
    """
    owner_id, speakers, player_ids, plug = await _playing_owner(scenario, owner)
    den, den_id = speakers[2], player_ids[2]
    scenario.expect_log("could not be reached")

    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        add = await scenario.command(
            scenario.mass.players.cmd_set_members(owner_id, player_ids_to_add=[den_id])
        )
        await scenario.advance(STARTUP_TIME + 1)
        await add
        await scenario.idle()

    assert _warnings_about(caplog, "Den") == [
        f"Player Den is left out of {_state(scenario, owner_id).name}: "
        f"Player Den could not be reached {STARTUP_TIME}s after it was switched on"
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

    The addition ends at once. The speaker that has started up afterwards gets no command
    and is in no group, and its plug stays on.
    """
    owner_id, speakers, player_ids, plug = await _playing_owner(scenario, owner)
    den, den_id = speakers[2], player_ids[2]
    players = scenario.mass.players
    add = await scenario.command(players.cmd_set_members(owner_id, player_ids_to_add=[den_id]))
    assert not add.done(), add.exception()

    if ended_by == "removing the speaker":
        await players.cmd_set_members(owner_id, player_ids_to_remove=[den_id])
        await scenario.settle()
        await add
    else:
        await _stop(scenario, owner_id, idle=False)
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

    Den is powered off after one idle minute. The group starts as soon as Den has started
    up, so Den plays, and its plug is not switched off while it does.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    await scenario.configure_player(player_ids[2], idle_power_off=1)
    plug = await _switch_off(scenario, speakers[2])
    play = await _play(scenario, group_id)
    await scenario.advance(STARTUP_TIME / 2)

    await _come_back(scenario, speakers[2])
    await play
    await scenario.advance(180)

    assert plug.commands == [True]
    assert _plays_along(scenario, group_id, player_ids[2])
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", GROUPS)
async def test_a_member_that_is_back_early_is_not_powered_off_while_its_group_waits(
    scenario: Scenario, group: str
) -> None:
    """
    A member that started up before another one is not switched off as idle while the start waits.

    Office and Den are switched off, and Office is powered off after one idle minute. Office
    has started up after half a minute, Den needs two minutes. Office stays on meanwhile,
    and the group starts on all three when Den has started up.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    await scenario.configure_player(player_ids[1], idle_power_off=1)
    office_plug = await _switch_off(scenario, speakers[1])
    den_plug = await _switch_off(scenario, speakers[2])
    await scenario.configure_player(player_ids[2], power_on_startup_time=120)
    play = await _play(scenario, group_id)
    await scenario.advance(3)
    await _come_back(scenario, speakers[1])

    await scenario.advance(70)

    assert not play.done(), play.exception()
    assert office_plug.commands == [True]
    await _come_back(scenario, speakers[2])
    await play
    await scenario.idle()
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    assert office_plug.commands == den_plug.commands == [True]
    await _stop(scenario, group_id)


@pytest.mark.parametrize("group", GROUPS)
async def test_a_member_its_group_never_started_is_powered_off_as_idle(
    scenario: Scenario, group: str
) -> None:
    """
    A member that was switched on for a start that was called off is powered off as idle.

    Den is powered off after one idle minute. The group is stopped while its start waits for
    Den, whose plug stays on. Den starts up and stays idle, and a minute later Den is
    powered off as any idle player is.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    await scenario.configure_player(player_ids[2], idle_power_off=1)
    plug = await _switch_off(scenario, speakers[2])
    play = await _play(scenario, group_id)
    await _stop(scenario, group_id, idle=False)
    with pytest.raises(PlayerUnavailableError, match="was called off"):
        await play

    await _come_back(scenario, speakers[2])
    await scenario.advance(50)
    assert plug.commands == [True]
    await scenario.advance(30)

    assert plug.commands == [True, False]
    assert _state(scenario, player_ids[2]).powered is False


@pytest.mark.parametrize("group", GROUPS)
async def test_a_new_start_after_a_stop_waits_for_what_is_left_of_the_startup(
    scenario: Scenario, group: str
) -> None:
    """
    A group that is stopped while it waits, and played again, waits for the same start-up.

    Den needs a minute to start up. The stop leaves Den's plug on and its start-up time runs
    on: Den shows as available and powered on. The new play does not start without Den and
    does not switch the plug again: the group starts on all members once what was left of
    Den's start-up time is over.
    """
    group_id, speakers, player_ids = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den, den_id = speakers[2], player_ids[2]
    plug = await _switch_off(scenario, den)
    await scenario.configure_player(den_id, power_on_startup_time=60)
    play = await _play(scenario, group_id)
    switched_on_at = scenario.mass.loop.time()
    await _stop(scenario, group_id)
    with pytest.raises(PlayerUnavailableError, match="was called off"):
        await play
    assert (_state(scenario, den_id).available, _state(scenario, den_id).powered) == (True, True)

    play = await _play(scenario, group_id)

    assert not play.done(), play.exception()
    assert (_state(scenario, den_id).available, _state(scenario, den_id).powered) == (True, True)
    await scenario.advance(10)
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    await scenario.register(den)
    await scenario.advance(switched_on_at + 59 - scenario.mass.loop.time())
    assert not play.done(), play.exception()
    await scenario.advance(2)
    await play
    await scenario.idle()

    assert plug.commands == [True]
    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    await _stop(scenario, group_id)


async def _paused_without_den(
    scenario: Scenario,
) -> tuple[str, list[FakeProtocol], list[str], FakePowerControl]:
    """
    Let the sync group Downstairs be paused on Kitchen and Office, with Den switched off.

    The group, which has a fixed member list, played on Kitchen and Office: the plug of Den
    refused to switch on when it started, and works again by now. Den needs the default time
    to start up, and the queue has the default pause timeout. Two seconds have passed since
    the pause.

    :return: The id of the group, the speakers, the ids of their players and the plug of Den.
    """
    group_id, speakers, player_ids, plug = await _playing_without_den(scenario, "fixed sync group")
    await _halt(scenario, group_id, "paused")
    assert _state(scenario, group_id).playback_state == PlaybackState.PAUSED
    for speaker in speakers:
        speaker.commands.clear()
    return group_id, speakers, player_ids, plug


async def test_a_play_of_a_paused_group_that_waits_for_a_member_outlasts_the_pause_timeout(
    scenario: Scenario,
) -> None:
    """
    A play of a paused group waits for its member past the pause timeout, and then starts.

    The sync group is paused on Kitchen and Office, without Den. Ten seconds later the user
    presses play: Den's plug is switched on and the play waits for Den to start up, which
    takes longer than what is left of the pause timeout. The group is not stopped when that
    timeout ends: it stays paused and the play keeps waiting. Once Den has started up the
    group starts once, on all three, and it is not stopped afterwards either.
    """
    group_id, speakers, player_ids, plug = await _paused_without_den(scenario)
    await scenario.advance(8)

    play = await scenario.command(scenario.mass.players.cmd_play(group_id))

    assert plug.commands == [True]
    await scenario.register(speakers[2])
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT - 10 + 5)
    assert not play.done(), play.exception()
    assert _state(scenario, group_id).playback_state == PlaybackState.PAUSED
    assert [speaker.commands for speaker in speakers] == [[], [], []]
    await _start_up(scenario, player_ids[2])
    await play
    await scenario.advance(DEFAULT_PAUSE_TIMEOUT)
    await scenario.idle()

    assert [_plays_along(scenario, group_id, player_id) for player_id in player_ids] == [True] * 3
    assert _streams("fixed sync group", speakers)
    assert [command[0] for speaker in speakers for command in speaker.commands].count("stop") == 0
    assert plug.commands == [True]
    await _stop(scenario, group_id)


async def test_a_paused_group_that_nobody_plays_is_stopped_at_the_pause_timeout(
    scenario: Scenario,
) -> None:
    """
    A paused group that gets no play is stopped when the pause timeout ends.

    The sync group is paused on Kitchen and Office, without Den. Nobody plays it: it is
    still paused a second before the pause timeout ends and stopped after it, and Den's plug
    stays off.
    """
    group_id, _, _, plug = await _paused_without_den(scenario)

    await scenario.advance(DEFAULT_PAUSE_TIMEOUT - 3)
    assert _state(scenario, group_id).playback_state == PlaybackState.PAUSED
    await scenario.advance(10)

    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert plug.commands == []
    await scenario.idle()


@pytest.mark.parametrize("call_off", ["stop", "pause"])
async def test_a_play_of_a_paused_group_that_is_called_off_leaves_the_pause_timeout_in_force(
    scenario: Scenario, call_off: str
) -> None:
    """
    A play that waits and is called off leaves a paused group that the pause timeout still stops.

    The sync group is paused on Kitchen and Office, without Den. The user presses play,
    which switches Den's plug on and waits, and ten seconds later stops or pauses the group:
    the play fails as called off. After the stop the group is stopped. After the pause it is
    still paused, and it is stopped when the pause timeout has passed since that pause.
    Nothing starts once Den has started up, and its plug stays on.

    :param call_off: The command the group gets while the play waits.
    """
    group_id, speakers, _, plug = await _paused_without_den(scenario)
    players = scenario.mass.players
    await scenario.advance(8)
    play = await scenario.command(players.cmd_play(group_id))
    await scenario.advance(10)
    assert not play.done(), play.exception()

    command = await scenario.command(
        players.cmd_stop(group_id) if call_off == "stop" else players.cmd_pause(group_id)
    )
    await scenario.advance(2)
    await command

    with pytest.raises(PlayerUnavailableError, match="was called off"):
        await play
    if call_off == "pause":
        assert _state(scenario, group_id).playback_state == PlaybackState.PAUSED
        await scenario.advance(DEFAULT_PAUSE_TIMEOUT - 3)
        assert _state(scenario, group_id).playback_state == PlaybackState.PAUSED
        await scenario.advance(10)
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    await _come_back(scenario, speakers[2])
    await scenario.idle()
    assert [_starts(speaker) for speaker in speakers] == [0, 0, 0]
    assert _state(scenario, group_id).playback_state == PlaybackState.IDLE
    assert plug.commands == [True]


async def _rendered_clip(render: AnnouncementRender) -> None:
    """Stand in for the rendering of an announcement: one sample of silence, ready at once."""
    render._chunks = [b"\0" * ANNOUNCEMENT_PCM_FORMAT.pcm_sample_size]
    render._total_bytes = len(render._chunks[0])
    render._ready.set()
    render._finished.set()


async def _announce(
    scenario: Scenario, player_id: str, group_id: str | None, speakers: list[FakeProtocol]
) -> None:
    """
    Play an announcement on a player with the real controller, and let the clip play out.

    Only the rendering of the audio is stubbed, and each speaker stops by itself a second after
    it was told to play while the announcement of the group or of the player runs.

    :param player_id: The player the announcement is sent to.
    :param group_id: The group player of the speakers, None when they have none.
    :param speakers: The speakers that may play the clip.
    """

    def _stops_after(speaker: FakeProtocol) -> Callable[[PlayerMedia], Awaitable[None]]:
        play_media = speaker.play_media

        async def _play(media: PlayerMedia) -> None:
            await play_media(media)
            group = scenario.mass.players.get_player(group_id) if group_id else None
            if media.media_type == MediaType.ANNOUNCEMENT or (
                group is not None and group.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)
            ):
                asyncio.get_running_loop().call_later(
                    1, lambda: speaker._apply("stop", playback_state=PlaybackState.IDLE)
                )

        return _play

    with contextlib.ExitStack() as patches:
        patches.enter_context(patch.object(AnnouncementRender, "_render", new=_rendered_clip))
        for speaker in speakers:
            patches.enter_context(
                patch.object(speaker, "play_media", side_effect=_stops_after(speaker))
            )
        clip = await scenario.command(
            scenario.mass.players.play_announcement(
                player_id, url="http://clip.invalid/announcement.wav", pre_announce=False
            )
        )
        await scenario.advance(20)
        assert clip.done()
        await clip


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("before", ["idle", "playing", "paused"])
async def test_an_announcement_to_a_group_switches_no_member_on(
    scenario: Scenario, group: str, before: str
) -> None:
    """
    An announcement to a group plays on the members that are on and switches nothing on.

    Den's plug is switched off. The group is idle, plays or is paused on Kitchen and Office:
    the plug of Den refused when it started, and works again. The announcement switches the
    plug on on no path, also not when it resumes the group afterwards, and waits for nothing.
    """
    group_id, speakers, _ = await _downstairs(scenario, group, "Kitchen", "Office", "Den")
    den = speakers[2]
    plug = await _switch_off(scenario, den)
    _expect_the_form_without(scenario, group)
    if before != "idle":
        control = scenario.mass.players.get_player_control(plug.control_id)
        assert control is not None
        power_on = control.power_on
        _refuse_to_switch_on(scenario, plug)
        await scenario.play(group_id)
        control.power_on = power_on
    if before == "paused":
        pause = await scenario.command(scenario.mass.player_queues.pause(group_id))
        await scenario.advance(1)
        await pause

    await _announce(scenario, group_id, group_id, speakers)

    assert plug.commands == []
    assert den.commands == []
    await _stop(scenario, group_id)


async def test_an_announcement_to_a_member_switches_no_other_member_on(
    scenario: Scenario,
) -> None:
    """
    The group that plays on after an announcement to one of its members switches nothing on.

    The sync group, which has a fixed member list, plays on Kitchen and Office: the plug of
    Den refused when it started, and works again. An announcement to Kitchen takes Kitchen out
    of the group and restores it afterwards: Den's plug stays off.
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
    await scenario.play(group_id)
    control.power_on = power_on

    await _announce(scenario, player_ids[0], group_id, speakers)

    assert plug.commands == []
    assert den.commands == []
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    await _stop(scenario, group_id)


@pytest.mark.parametrize("target", ["switched-off player", "group with a switched-off member"])
@pytest.mark.parametrize("default", ["add", "next"])
async def test_media_that_is_only_added_by_default_switches_nothing_on(
    scenario: Scenario, target: str, default: str
) -> None:
    """
    A play request without an option that only adds its media switches no power control on.

    The user's default for tracks is to add or to play next. A track is played without an option
    on Den, switched off at its plug, or on a group with Den as a member: nothing waits, Den's
    plug stays off, and the track is in the queue.

    :param target: Whether the request names Den or its group.
    :param default: The user's default option for tracks.
    """
    await scenario.mass.config.save_core_config(
        "player_queues", {"default_enqueue_option_track": default}
    )
    await scenario.settle()
    group_id, speakers, player_ids = await _downstairs(
        scenario, "universal group", "Kitchen", "Den"
    )
    plug = await _switch_off(scenario, speakers[1])
    queue_id = player_ids[1] if target == "switched-off player" else group_id

    request = await _play(scenario, queue_id)
    await scenario.advance(5)

    assert request.done(), "the request waits for the plug"
    await request
    assert plug.commands == []
    queue = scenario.queue(queue_id)
    assert queue is not None
    assert queue.items == 1
    assert _state(scenario, queue_id).playback_state != PlaybackState.PLAYING


@pytest.mark.parametrize("target", ["switched-off player", "group with a switched-off member"])
async def test_media_that_plays_by_default_switches_its_power_control_on(
    scenario: Scenario, target: str
) -> None:
    """
    A play request without an option that plays its media waits for the power control.

    With the user's default for tracks to play, the track is played without an option on Den,
    switched off at its plug, or on a group with Den as a member: Den's plug is switched on and
    the request waits for Den to start up.

    :param target: Whether the request names Den or its group.
    """
    group_id, speakers, player_ids = await _downstairs(
        scenario, "universal group", "Kitchen", "Den"
    )
    plug = await _switch_off(scenario, speakers[1])
    queue_id = player_ids[1] if target == "switched-off player" else group_id

    request = await _play(scenario, queue_id)
    await scenario.advance(5)

    assert not request.done(), request.exception()
    assert plug.commands == [True]
    await _come_back(scenario, speakers[1])
    await request
    await scenario.idle()
    assert _state(scenario, queue_id).playback_state == PlaybackState.PLAYING
    await _stop(scenario, queue_id)
