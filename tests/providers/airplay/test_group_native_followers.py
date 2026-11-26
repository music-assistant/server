"""Tests for a leader on AirPlay that leaves its group while speakers follow it natively."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import IdentifierType, PlaybackState

from music_assistant.constants import CONF_POWER_CONTROL
from music_assistant.providers.sync_group.player import SyncGroupPlayer
from tests.providers.airplay.test_group_leader_loss import (
    AFTER_IDLE_GRACE,
    _Cli,
    _cli,
    _led_by,
    _members,
    _play_again,
    _remove_command,
    _state,
    _stop,
    _switch_off_command,
    _ungroup_command,
    cliairplay,  # noqa: F401
)
from tests.providers.airplay.test_lifecycle import _announce, _player_of, _service
from tests.scenarios.test_grouping import GROUPING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable

    from music_assistant.models.player import Player
    from music_assistant.providers.airplay.player import AirPlayPlayer
    from music_assistant.providers.airplay.provider import AirPlayProvider
    from tests.scenarios.harness import FakeProtocol, Scenario

    # takes the group command of a speaker, which it is given to carry out
    type Answer = Callable[[Callable[[], Awaitable[None]]], Awaitable[None]]

# a group command is not answered within this time, and fails
COMMAND_TIMEOUT = 10


class _Group(NamedTuple):
    """A playing group: A leads over AirPlay, R plays along over AirPlay."""

    player: Player
    a: AirPlayPlayer
    r: AirPlayPlayer


async def _airplay(scenario: Scenario, provider: AirPlayProvider, letter: str) -> AirPlayPlayer:
    """
    Attach the AirPlay receiver of a player.

    :param letter: The letter of the player.
    """
    protocol = await _announce(
        scenario,
        provider,
        _service(f"Player {letter}", f"00:11:22:33:44:{ord(letter)}", f"192.168.1.{ord(letter)}"),
    )
    assert protocol is not None
    await scenario.idle()
    return protocol


async def _play_group(
    scenario: Scenario, provider: AirPlayProvider, members: str = "ABR"
) -> _Group:
    """
    Play on a dynamic sync group, whose members other than A and R follow A natively.

    :param members: The letters of the players, in the order they join. A is the first.
    """
    sonos = await scenario.add_provider("sonos")
    natives = [f"RINCON_{letter}" for letter in members if letter != "R"]
    for protocol_id in natives:
        letter = protocol_id[-1]
        await scenario.register(
            sonos.protocol(
                protocol_id,
                f"Player {letter}",
                identifiers={IdentifierType.MAC_ADDRESS: f"00:11:22:33:44:{ord(letter)}"},
                supported_features=set(GROUPING),
                can_group_with=set(natives) - {protocol_id},
            )
        )
    a, r = [await _airplay(scenario, provider, letter) for letter in "AR"]
    leader = _player_of(a)
    await scenario.configure_player(leader.player_id, **{CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE})
    group = await scenario.mass.players.create_group_player(
        "sync_group", "Group Z", [leader.player_id], True
    )
    await scenario.settle()
    await scenario.play(group.player_id)
    for letter in members[1:]:
        task = await scenario.command(
            scenario.mass.players.cmd_set_members(
                group.player_id, player_ids_to_add=[_player(scenario, letter).player_id]
            )
        )
        await scenario.idle()
        await task
    assert _led_by(group) is leader
    assert _members(scenario, group) == [f"Player {letter}" for letter in sorted(members)]
    assert leader.active_output_protocol == a.protocol_id
    assert a.group_members == [a.protocol_id, r.protocol_id]
    for letter in members:
        assert _player(scenario, letter).playback_state == PlaybackState.PLAYING
    _stops_with(_cli(a), scenario.protocol("RINCON_A"))
    scenario.timeline.clear()
    return _Group(group, a, r)


def _stops_with(process: _Cli, native: FakeProtocol) -> None:
    """
    Let a device report that it stopped once the AirPlay stream it plays ends, as a Sonos does.

    :param process: The process of the stream.
    :param native: The native protocol of the device.
    """
    end = process.end

    def _end(*lines: str) -> None:
        end(*lines)
        native._set_reported(playback_state=PlaybackState.IDLE)
        native.update_state()

    process.end = _end  # type: ignore[method-assign]


def _player(scenario: Scenario, letter: str) -> Player:
    """
    Return a player.

    :param letter: The letter of the player.
    """
    return next(x for x in scenario.mass.players.all_players() if x.name == f"Player {letter}")


def _tracked(group: _Group) -> list[str]:
    """Return the ids of the members the group plays on the next time it forms."""
    assert isinstance(group.player.own_protocol, SyncGroupPlayer)
    return group.player.own_protocol._attr_group_members


def _on_group_command(native: FakeProtocol, answer: Answer) -> None:
    """
    Let a speaker take its group commands in its own way.

    :param native: The native protocol of the speaker.
    :param answer: What it does with a command.
    """
    set_members = native.set_members

    async def _set_members(
        player_ids_to_add: list[str] | None = None, player_ids_to_remove: list[str] | None = None
    ) -> None:
        await answer(partial(set_members, player_ids_to_add, player_ids_to_remove))

    native.set_members = _set_members  # type: ignore[method-assign]


async def _silent(_carry_out: Callable[[], Awaitable[None]]) -> None:
    """Answer nothing, and do nothing."""
    await asyncio.sleep(COMMAND_TIMEOUT)
    raise TimeoutError


async def _answers_late(carry_out: Callable[[], Awaitable[None]]) -> None:
    """Carry the command out just before it would fail."""
    await asyncio.sleep(COMMAND_TIMEOUT - 1)
    await carry_out()


def _assert_plays_on(
    scenario: Scenario, group: _Group, process: Any, members: Iterable[str]
) -> None:
    """
    Assert that the group plays on without a gap, led by R and without A.

    :param process: The process of the stream of R before A left.
    :param members: The letters of the members.
    """
    assert _led_by(group.player) is _player_of(group.r)
    assert _state(group.player) == PlaybackState.PLAYING
    assert _members(scenario, group.player) == [f"Player {letter}" for letter in members]
    assert _cli(group.r) is process
    assert not process.closed
    leader = _player_of(group.a)
    assert leader.playback_state == PlaybackState.IDLE
    assert leader.active_group is None


@pytest.mark.parametrize("command", [_remove_command, _ungroup_command, _switch_off_command])
async def test_a_follower_that_can_not_play_along_leaves_with_the_leader(
    scenario: Scenario,
    provider: AirPlayProvider,
    caplog: pytest.LogCaptureFixture,
    command: Callable[[Scenario, Player, Player], Coroutine[Any, Any, None]],
) -> None:
    """
    A speaker without AirPlay is let go of before the stream it follows ends, and is free then.

    :param command: How the user takes the leader out.
    """
    group = await _play_group(scenario, provider)
    process, session = _cli(group.r), group.r.session
    queue = scenario.queue(group.player.player_id)
    assert queue is not None
    playing_item = queue.current_item
    leader_process = _cli(group.a)
    leader_streams: list[bool] = []

    async def _note(carry_out: Callable[[], Awaitable[None]]) -> None:
        leader_streams.append(not leader_process.closed)
        await carry_out()

    _on_group_command(scenario.protocol("RINCON_A"), _note)
    scenario.expect_log("can not be grouped with")

    task = await scenario.command(command(scenario, group.player, _player_of(group.a)))
    await scenario.advance(AFTER_IDLE_GRACE)

    await task
    follower = _player(scenario, "B")
    assert follower.synced_to is None
    assert follower.active_group is None
    assert follower.playback_state == PlaybackState.IDLE
    assert _player_of(group.a).group_members == []
    _assert_plays_on(scenario, group, process, "R")
    assert _tracked(group) == [_player_of(group.r).player_id]
    assert group.r.session is session
    queue = scenario.queue(group.player.player_id)
    assert queue is not None
    assert queue.state == PlaybackState.PLAYING
    assert queue.current_item is playing_item
    assert scenario.timeline == [("RINCON_A", ("set_members", None, ["RINCON_B"]))]
    assert leader_streams == [True]
    assert leader_process.closed
    assert "Removing Player B from group Group Z: it can not be grouped with Player R" in (
        caplog.text
    )
    await _stop(scenario, group.player)


@pytest.mark.parametrize("command", [_remove_command, _ungroup_command, _switch_off_command])
@pytest.mark.parametrize("members", ["ABR", "ARB"])
async def test_a_follower_that_can_play_along_joins_the_session_that_plays_on(
    scenario: Scenario,
    provider: AirPlayProvider,
    command: Callable[[Scenario, Player, Player], Coroutine[Any, Any, None]],
    members: str,
) -> None:
    """
    A speaker whose AirPlay came back while it followed natively plays on over AirPlay.

    :param command: How the user takes the leader out.
    :param members: The members, in the order they joined.
    """
    group = await _play_group(scenario, provider, members)
    b = await _airplay(scenario, provider, "B")
    process, session = _cli(group.r), group.r.session
    assert session is not None

    task = await scenario.command(command(scenario, group.player, _player_of(group.a)))
    await scenario.advance(AFTER_IDLE_GRACE)

    await task
    follower = _player_of(b)
    assert follower.synced_to == _player_of(group.r).player_id
    assert follower.playback_state == PlaybackState.PLAYING
    assert follower.active_output_protocol == b.protocol_id
    _assert_plays_on(scenario, group, process, "BR")
    assert set(session.sync_clients) == {group.r, b}
    assert scenario.timeline == [("RINCON_A", ("set_members", None, ["RINCON_B"]))]
    assert _tracked(group) == [_player_of(group.r).player_id, follower.player_id]
    await _stop(scenario, group.player)


async def test_each_follower_goes_its_own_way(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """Followers are let go of in one command: one joins the session, the other is free."""
    group = await _play_group(scenario, provider, "ABCR")
    c = await _airplay(scenario, provider, "C")
    process, session = _cli(group.r), group.r.session
    assert session is not None
    scenario.expect_log("Removing Player B from group")

    await scenario.mass.players.cmd_ungroup(_player_of(group.a).player_id)
    await scenario.advance(AFTER_IDLE_GRACE)

    assert _player(scenario, "B").synced_to is None
    assert _player(scenario, "B").playback_state == PlaybackState.IDLE
    assert _player_of(c).synced_to == _player_of(group.r).player_id
    _assert_plays_on(scenario, group, process, "CR")
    assert set(session.sync_clients) == {group.r, c}
    assert scenario.timeline == [("RINCON_A", ("set_members", None, ["RINCON_B", "RINCON_C"]))]
    await _stop(scenario, group.player)


async def test_a_follower_that_is_taken_out_with_the_leader_is_free(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A follower that leaves along with the leader does not join the session that plays on."""
    group = await _play_group(scenario, provider)
    b = await _airplay(scenario, provider, "B")
    process, session = _cli(group.r), group.r.session
    assert session is not None

    await scenario.mass.players.cmd_set_members(
        group.player.player_id,
        player_ids_to_remove=[_player_of(x).player_id for x in (group.a, b)],
    )
    await scenario.advance(AFTER_IDLE_GRACE)

    _assert_plays_on(scenario, group, process, "R")
    assert set(session.sync_clients) == {group.r}
    assert _player_of(b).synced_to is None
    assert _player_of(b).playback_state == PlaybackState.IDLE
    assert _tracked(group) == [_player_of(group.r).player_id]
    await _stop(scenario, group.player)


async def test_the_leader_plays_until_it_answered(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A leader that is slow to let go of its follower leads and plays on until it did."""
    group = await _play_group(scenario, provider)
    b = await _airplay(scenario, provider, "B")
    process, leader_process = _cli(group.r), _cli(group.a)
    _on_group_command(scenario.protocol("RINCON_A"), _answers_late)

    task = await scenario.command(scenario.mass.players.cmd_ungroup(_player_of(group.a).player_id))
    await scenario.advance(COMMAND_TIMEOUT - 2)

    assert _led_by(group.player) is _player_of(group.a)
    assert _members(scenario, group.player) == ["Player A", "Player B", "Player R"]
    assert not leader_process.closed
    await scenario.advance(AFTER_IDLE_GRACE)
    await task
    _assert_plays_on(scenario, group, process, "BR")
    assert _player_of(b).playback_state == PlaybackState.PLAYING
    await _stop(scenario, group.player)


async def test_a_follower_joins_once_it_reports_that_it_is_free(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """The stream of the leader ends, and the follower joins, after the follower's report."""
    group = await _play_group(scenario, provider)
    b = await _airplay(scenario, provider, "B")
    process, leader_process = _cli(group.r), _cli(group.a)

    async def _reports_late(carry_out: Callable[[], Awaitable[None]]) -> None:
        scenario.mass.call_later(3, carry_out)

    _on_group_command(scenario.protocol("RINCON_A"), _reports_late)

    task = await scenario.command(scenario.mass.players.cmd_ungroup(_player_of(group.a).player_id))
    await scenario.advance(2)

    assert not leader_process.closed
    assert b.session is None
    await scenario.advance(AFTER_IDLE_GRACE)
    await task
    _assert_plays_on(scenario, group, process, "BR")
    assert b.session is group.r.session
    await _stop(scenario, group.player)


@pytest.mark.parametrize("has_airplay", [True, False])
async def test_a_leader_that_does_not_let_go_does_not_hold_up_the_group(
    scenario: Scenario,
    provider: AirPlayProvider,
    caplog: pytest.LogCaptureFixture,
    has_airplay: bool,
) -> None:
    """
    The group plays on when the leader does not answer, without the follower it holds.

    :param has_airplay: Whether the follower could play along over AirPlay.
    """
    group = await _play_group(scenario, provider)
    b = await _airplay(scenario, provider, "B") if has_airplay else None
    process, session = _cli(group.r), group.r.session
    assert session is not None
    native = scenario.protocol("RINCON_A")
    _on_group_command(native, _silent)
    for log in ("Could not release", "can not be grouped with", "is not free"):
        scenario.expect_log(log)

    task = await scenario.command(scenario.mass.players.cmd_ungroup(_player_of(group.a).player_id))
    await scenario.advance(COMMAND_TIMEOUT)

    # it took one command, and the follower is not waited for
    assert task.done()
    await task
    _assert_plays_on(scenario, group, process, "R")
    assert set(session.sync_clients) == {group.r}
    follower = _player(scenario, "B")
    assert follower.synced_to == _player_of(group.a).player_id
    assert "Could not release" in caplog.text
    member_ids = [_player_of(group.r).player_id]
    if b is not None:
        assert "Player B is not free" in caplog.text
        member_ids.append(follower.player_id)
    assert _tracked(group) == member_ids

    # the speaker lets go of it later by itself: a follower that can play along is in the next
    # formation
    await scenario.report(native, group_members=[])
    await _stop(scenario, group.player)
    await _play_again(scenario, group.player)
    await scenario.advance(2)
    assert _members(scenario, group.player) == (["Player B", "Player R"] if b else ["Player R"])
    await _stop(scenario, group.player)


async def test_a_follower_that_reports_nothing_is_not_waited_for_forever(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """The group plays on without a follower that does not report that it is free."""
    group = await _play_group(scenario, provider, "ABCR")
    for letter in "BC":
        await _airplay(scenario, provider, letter)
    process, session = _cli(group.r), group.r.session
    assert session is not None
    scenario.expect_log("is not free")

    async def _does_nothing(_carry_out: Callable[[], Awaitable[None]]) -> None:
        pass

    _on_group_command(scenario.protocol("RINCON_A"), _does_nothing)

    task = await scenario.command(scenario.mass.players.cmd_ungroup(_player_of(group.a).player_id))
    # the followers are waited for together
    await scenario.advance(6)

    assert task.done()
    await task
    _assert_plays_on(scenario, group, process, "R")
    assert set(session.sync_clients) == {group.r}
    await _stop(scenario, group.player)


async def test_a_member_that_is_fed_takes_over_when_the_first_one_is_lost_meanwhile(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """The next member takes over when the first one is lost while the old leader lets go."""
    group = await _play_group(scenario, provider)
    s = await _airplay(scenario, provider, "S")
    await scenario.mass.players.cmd_set_members(
        group.player.player_id, player_ids_to_add=[_player_of(s).player_id]
    )
    await scenario.idle()
    session, process = s.session, _cli(s)
    assert session is not None
    _on_group_command(scenario.protocol("RINCON_A"), _answers_late)
    for log in (
        "cliairplay process stopped unexpectedly",
        "did not produce a running stream",
        "can not be grouped with",
    ):
        scenario.expect_log(log)

    task = await scenario.command(scenario.mass.players.cmd_ungroup(_player_of(group.a).player_id))
    await scenario.advance(COMMAND_TIMEOUT - 2)
    _cli(group.r).end()
    await scenario.advance(AFTER_IDLE_GRACE)

    await task
    assert _led_by(group.player) is _player_of(s)
    assert _state(group.player) == PlaybackState.PLAYING
    assert s.session is session
    assert _cli(s) is process
    assert not process.closed
    await _stop(scenario, group.player)


async def test_a_new_leader_that_is_lost_meanwhile_does_not_take_over(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """The group starts again when the new leader is lost while the old one lets go."""
    group = await _play_group(scenario, provider)
    b = await _airplay(scenario, provider, "B")
    process = _cli(group.r)
    _on_group_command(scenario.protocol("RINCON_A"), _answers_late)
    for log in ("cliairplay process stopped unexpectedly", "did not produce a running stream"):
        scenario.expect_log(log)

    task = await scenario.command(scenario.mass.players.cmd_ungroup(_player_of(group.a).player_id))
    await scenario.advance(COMMAND_TIMEOUT - 2)
    process.end()
    await scenario.advance(AFTER_IDLE_GRACE)

    await task
    assert _state(group.player) == PlaybackState.PLAYING
    assert _members(scenario, group.player) == ["Player B", "Player R"]
    for protocol in (b, group.r):
        assert protocol.playback_state == PlaybackState.PLAYING
        assert not _cli(protocol).closed
    await scenario.advance(AFTER_IDLE_GRACE)
    assert _state(group.player) == PlaybackState.PLAYING
    await _stop(scenario, group.player)
