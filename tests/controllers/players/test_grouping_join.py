"""
The join of a member change and the regroup at a fresh start, call by call.

The players controller is a fake over a world built by hand (see test_grouping_plan.py): the
change reads the world as it is at that moment, so a test changes it while a device call runs,
as a device reports while it is called.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState, ProtocolRole
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.controllers.players import grouping
from music_assistant.controllers.players.grouping_plan import (
    ChangeKind,
    GroupChange,
    GroupingSnapshot,
)
from tests.controllers.players.test_grouping_plan import (
    PLAYING,
    PLAYS,
    _leader_with_members,
    _left_on_bridge,
    _mixed,
    _switching,
    _World,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

PAUSED = PlaybackState.PAUSED


class _Protocol:
    """A protocol object whose calls the controller records."""

    def __init__(self, devices: _Devices, protocol_id: str) -> None:
        """Initialize the protocol of the fake controller."""
        self.devices = devices
        self.protocol_id = protocol_id
        self.token: object | None = None

    async def set_members(self, **lists: Any) -> None:
        """Record the call with the lists in the shape they were passed."""
        await self.devices.call("set_members", self.protocol_id, lists)

    async def stop(self) -> None:
        """Record the stop."""
        await self.devices.call("stop", self.protocol_id)

    async def on_group_content_takeover(self) -> object | None:
        """Record the hook and return the token of the protocol."""
        await self.devices.call("takeover", self.protocol_id)
        return self.token

    async def on_group_content_takeover_aborted(self, token: object) -> None:
        """Record the abort."""
        await self.devices.call("takeover_aborted", self.protocol_id, token)


class _Player:
    """A player object: clearing its output is recorded and reaches the world."""

    def __init__(self, devices: _Devices, player_id: str) -> None:
        """Initialize the player of the fake controller."""
        self.devices = devices
        self.player_id = player_id

    def set_active_output_protocol(self, output_id: str | None) -> None:
        """Record the clear and apply it to the world."""
        self.devices.calls.append(("clear_output", self.player_id))
        self.devices.set_output(self.player_id, output_id)


class _Devices:
    """
    The players controller over a world, every call recorded in order.

    A call runs the change a test set for it (``during``) or fails as a test said (``failing``),
    both by the call's name and the id of what it is called on.
    """

    def __init__(self, world: _World) -> None:
        """Initialize the controller over a world."""
        self.world = world
        self.calls: list[tuple[Any, ...]] = []
        self.during: dict[tuple[str, str], Callable[[], None]] = {}
        self.failing: dict[tuple[str, str], Exception] = {}
        self.logger = MagicMock()
        self.translation_owner = "players"
        self.protocol_objects: dict[str, _Protocol] = {}
        self.player_objects: dict[str, _Player] = {}

    def get_protocol(self, protocol_id: str) -> _Protocol | None:
        """Return the protocol object of an id in the world."""
        if protocol_id not in self.world.protocols:
            return None
        return self.protocol_objects.setdefault(protocol_id, _Protocol(self, protocol_id))

    def get_player(self, player_id: str) -> _Player | None:
        """Return the player object of an id in the world."""
        if player_id not in self.world.players:
            return None
        return self.player_objects.setdefault(player_id, _Player(self, player_id))

    async def call(self, *call: Any) -> None:
        """Record a device call; run the change set for it, then fail as set."""
        self.calls.append(call)
        if (change := self.during.pop(call[:2], None)) is not None:
            change()
        if (error := self.failing.get(call[:2])) is not None:
            raise error

    def set_output(self, player_id: str, output_id: str | None) -> None:
        """Set the active output of a player in the world, as the player normalizes it."""
        player = self.world.players[player_id]
        if output_id is not None and output_id == player.own_protocol:
            output_id = "native"
        self.world.players[player_id] = replace(player, active_output=output_id)

    def change(self, player_id: str, **values: Any) -> Callable[[], None]:
        """Return a change of a player in the world, to run while a call runs."""

        def _change() -> None:
            self.world.players[player_id] = replace(self.world.players[player_id], **values)

        return _change

    async def _activate_output_protocol(self, player: _Player, output_id: str) -> None:
        await self.call("set_output", player.player_id, output_id)
        self.set_output(player.player_id, output_id)

    async def cmd_resume(self, player_id: str) -> None:
        await self.call("resume", player_id)

    async def _handle_set_members(self, player: _Player, **lists: Any) -> None:
        await self.call("member_change", player.player_id, lists)

    async def _stop_player_or_its_queue(self, player: _Player) -> None:
        await self.call("stop_leader", player.player_id)

    async def _power_on_for_playback(self, player: _Player) -> None:
        self.calls.append(("prepare", player.player_id))


@contextmanager
def _reading() -> Iterator[None]:
    """Let the change read the world of the fake controller as it is at each read."""

    def _snapshot(players: _Devices, *_args: Any, **_kwargs: Any) -> GroupingSnapshot:
        return players.world.snapshot()

    with patch.object(grouping, "grouping_snapshot", _snapshot):
        yield


async def _join(
    devices: _Devices,
    target: str,
    add: tuple[str, ...] | None = None,
    remove: tuple[str, ...] | None = None,
    *,
    new_content: bool = False,
) -> Any:
    """Run a member change on the fake controller and return what it hands back."""
    change = GroupChange(
        kind=ChangeKind.MEMBERS, target_id=target, add=add, remove=remove, new_content=new_content
    )
    with _reading():
        return await grouping.apply_group_change(devices, change)  # type: ignore[arg-type]


def _device_calls(devices: _Devices) -> list[tuple[Any, ...]]:
    """Return the recorded calls without the admission's preparation of the members."""
    return [call for call in devices.calls if call[0] != "prepare"]


def _joins(protocol_id: str, *add: str) -> tuple[Any, ...]:
    """Return the join call of a protocol group that adds members."""
    return (
        "set_members",
        protocol_id,
        {"player_ids_to_add": list(add), "player_ids_to_remove": None},
    )


def _leaves(protocol_id: str, *remove: str) -> tuple[Any, ...]:
    """Return a call of a protocol group that removes members, passing no additions."""
    return (
        "set_members",
        protocol_id,
        {"player_ids_to_add": None, "player_ids_to_remove": list(remove)},
    )


# -- the protocol's join call --


async def test_a_member_over_a_protocol_points_at_it_before_the_join_call() -> None:
    """The member's output moves to the protocol, then the join call, then the leader's output."""
    world = _World()
    _mixed(world)
    devices = _Devices(world)
    await _join(devices, "s", ("w",))
    assert _device_calls(devices) == [
        ("set_output", "w", "airplay_w"),
        _joins("airplay_s", "airplay_w"),
        ("set_output", "s", "airplay_s"),
    ]


async def test_each_further_member_is_checked_after_the_one_before_moved() -> None:
    """A member whose output points at the protocol by the time it is checked is not moved."""
    world = _World()
    _mixed(world)
    world.protocol("airplay_v", "v", "airplay")
    world.player("v", "airplay_v")
    world.players["s"] = replace(world.players["s"], can_group_with=frozenset({"w", "v"}))
    world.protocols["airplay_s"] = replace(
        world.protocols["airplay_s"], can_group_with=frozenset({"airplay_w", "airplay_v"})
    )
    devices = _Devices(world)
    devices.during["set_output", "w"] = devices.change("v", active_output="airplay_v")
    await _join(devices, "s", ("w", "v"))
    assert _device_calls(devices) == [
        ("set_output", "w", "airplay_w"),
        _joins("airplay_s", "airplay_w", "airplay_v"),
        ("set_output", "s", "airplay_s"),
    ]


def _with_member_w(world: _World) -> None:
    """Make w a member of s's AirPlay group, s rendering on AirPlay."""
    _mixed(world, active_output="airplay_s")
    world.protocols["airplay_s"] = replace(
        world.protocols["airplay_s"], group_members=("airplay_s", "airplay_w")
    )
    world.players["s"] = replace(world.players["s"], group_members=("s", "w"))
    world.players["w"] = replace(world.players["w"], synced_to="s", active_output="airplay_w")


async def test_a_member_that_leaves_the_protocol_drops_its_output_after_the_call() -> None:
    """The removal call, then its output is cleared; the leader's output stays."""
    world = _World()
    _with_member_w(world)
    devices = _Devices(world)
    await _join(devices, "s", None, ("w",))
    assert devices.calls == [_leaves("airplay_s", "airplay_w"), ("clear_output", "w")]


async def test_the_outputs_of_the_leaving_members_are_read_after_the_join_call() -> None:
    """A member whose output moved on while the call ran keeps it."""
    world = _World()
    _with_member_w(world)
    devices = _Devices(world)
    devices.during["set_members", "airplay_s"] = devices.change("w", active_output="native")
    await _join(devices, "s", None, ("w",))
    assert devices.calls == [_leaves("airplay_s", "airplay_w")]


# -- the hand-over of the leader's output --


async def test_an_idle_switch_moves_the_leaders_output_and_hands_nothing_over() -> None:
    """Idle, the old protocol is neither stopped nor left, and nothing resumes."""
    world = _World()
    _switching(world, PlaybackState.IDLE)
    devices = _Devices(world)
    await _join(devices, "l", ("y",))
    assert _device_calls(devices) == [
        ("set_output", "y", "sendspin_y"),
        _joins("sendspin_l", "sendspin_y"),
        ("set_output", "l", "sendspin_l"),
    ]


def _switch_with_x_on_sendspin(state: PlaybackState) -> _World:
    """Return leader l on AirPlay with x, which can follow to Sendspin, and y to add."""
    world = _World()
    _switching(world, state)
    world.protocol("sendspin_x", "x", "sendspin")
    world.player("x", "airplay_x", "sendspin_x", synced_to="l", active_output="airplay_x")
    return world


async def test_a_playing_switch_stops_the_old_protocol_resumes_and_regroups() -> None:
    """The old protocol stops and lets its members go, the leader resumes, they join again."""
    world = _switch_with_x_on_sendspin(PLAYING)
    devices = _Devices(world)
    await _join(devices, "l", ("y",))
    assert _device_calls(devices) == [
        ("set_output", "y", "sendspin_y"),
        _joins("sendspin_l", "sendspin_y"),
        ("set_output", "l", "sendspin_l"),
        ("stop", "airplay_l"),
        _leaves("airplay_l", "airplay_x"),
        ("clear_output", "x"),
        ("resume", "l"),
        ("member_change", "l", {"player_ids_to_add": ["x"]}),
    ]
    devices.logger.log.assert_any_call(
        20,
        "Handing the output of %s over to the %s protocol%s",
        "State l",
        "sendspin",
        " and resuming playback",
    )


async def test_a_pause_during_the_join_call_hands_over_without_a_resume() -> None:
    """Whether the leader plays is read after the join call: paused, it is not resumed."""
    world = _switch_with_x_on_sendspin(PLAYING)
    devices = _Devices(world)
    devices.during["set_members", "sendspin_l"] = devices.change("l", playback_state=PAUSED)
    await _join(devices, "l", ("y",))
    assert ("stop", "airplay_l") in devices.calls
    assert ("resume", "l") not in devices.calls
    assert ("member_change", "l", {"player_ids_to_add": ["x"]}) in devices.calls


async def test_a_pause_after_the_leaders_output_moved_still_resumes() -> None:
    """Whether the leader played is taken before its output moves, so it resumes."""
    world = _switch_with_x_on_sendspin(PLAYING)
    devices = _Devices(world)
    devices.during["set_output", "l"] = devices.change("l", playback_state=PAUSED)
    await _join(devices, "l", ("y",))
    assert ("resume", "l") in devices.calls


async def test_the_old_members_are_the_ones_captured_before_the_stop() -> None:
    """The stopped protocol's members are removed and added back as they were before its stop."""
    world = _switch_with_x_on_sendspin(PLAYING)
    devices = _Devices(world)

    def _stopped_without_members() -> None:
        world.protocols["airplay_l"] = replace(
            world.protocols["airplay_l"], group_members=("airplay_l",)
        )

    devices.during["stop", "airplay_l"] = _stopped_without_members
    await _join(devices, "l", ("y",))
    assert _leaves("airplay_l", "airplay_x") in devices.calls
    assert devices.calls[-1] == ("member_change", "l", {"player_ids_to_add": ["x"]})


async def test_the_old_members_outputs_are_read_after_their_removal() -> None:
    """An old member whose output moved on while it was removed from the old protocol keeps it."""
    world = _switch_with_x_on_sendspin(PLAYING)
    devices = _Devices(world)
    devices.during["set_members", "airplay_l"] = devices.change("x", active_output="native")
    await _join(devices, "l", ("y",))
    assert ("clear_output", "x") not in devices.calls
    assert world.players["x"].active_output == "native"
    assert devices.calls[-1] == ("member_change", "l", {"player_ids_to_add": ["x"]})


def _switch_with_native_n() -> _World:
    """Return the playing switch of l with x, and n that joins l natively."""
    world = _switch_with_x_on_sendspin(PLAYING)
    world.protocol("sonos_n", "n", "sonos", role=ProtocolRole.NATIVE)
    world.player("n", "sonos_n")
    world.players["l"] = replace(world.players["l"], can_group_with=frozenset({"n", "y"}))
    world.protocols["sonos_l"] = replace(
        world.protocols["sonos_l"], can_group_with=frozenset({"sonos_n"})
    )
    return world


async def test_the_native_call_is_decided_after_the_resume_and_the_re_add() -> None:
    """An own protocol that stopped grouping while the leader resumed gets no native call."""
    world = _switch_with_native_n()
    devices = _Devices(world)
    await _join(devices, "l", ("n", "y"))
    assert devices.calls[-1] == _joins("sonos_l", "sonos_n")
    world = _switch_with_native_n()
    devices = _Devices(world)

    def _no_grouping() -> None:
        world.protocols["sonos_l"] = replace(world.protocols["sonos_l"], features=PLAYS)

    devices.during["resume", "l"] = _no_grouping
    await _join(devices, "l", ("n", "y"))
    assert devices.calls[-1] == ("member_change", "l", {"player_ids_to_add": ["x"]})
    assert not any(call[:2] == ("set_members", "sonos_l") for call in devices.calls)


async def test_the_old_protocol_is_read_after_the_leaders_output_moved() -> None:
    """A protocol detached from the leader while its output moved is left alone."""
    world = _switch_with_x_on_sendspin(PLAYING)
    devices = _Devices(world)

    def _detached() -> None:
        world.protocols["airplay_l"] = replace(world.protocols["airplay_l"], player_id=None)

    devices.during["set_output", "l"] = _detached
    await _join(devices, "l", ("y",))
    assert ("stop", "airplay_l") not in devices.calls
    assert devices.calls[-1] == ("resume", "l")


async def test_the_removal_from_the_old_protocol_is_read_after_its_stop() -> None:
    """An old protocol that lost SET_MEMBERS in its stop is not called to let its members go."""
    world = _switch_with_x_on_sendspin(PLAYING)
    devices = _Devices(world)

    def _no_grouping() -> None:
        world.protocols["airplay_l"] = replace(world.protocols["airplay_l"], features=PLAYS)

    devices.during["stop", "airplay_l"] = _no_grouping
    await _join(devices, "l", ("y",))
    assert _device_calls(devices)[3:] == [
        ("stop", "airplay_l"),
        ("resume", "l"),
        ("member_change", "l", {"player_ids_to_add": ["x"]}),
    ]


# -- members stranded by a switch off the leader's own stream --


def _own_stream_leader(world: _World, state: PlaybackState = PlaybackState.IDLE) -> None:
    """Add AirPlay leader l on its own stream holding n, both with Sendspin, and s on Sendspin."""
    world.protocol(
        "airplay_l",
        "l",
        "airplay",
        role=ProtocolRole.NATIVE,
        own_stream=True,
        members=("airplay_l", "airplay_n"),
    )
    world.protocol("sendspin_l", "l", "sendspin")
    world.player(
        "l",
        "airplay_l",
        "sendspin_l",
        can_group_with=frozenset({"s"}),
        group_members=("l", "n"),
        playback_state=state,
        active_output="native" if state != PlaybackState.IDLE else None,
    )
    world.protocol("airplay_n", "n", "airplay", role=ProtocolRole.NATIVE)
    world.protocol("sendspin_n", "n", "sendspin")
    world.player("n", "airplay_n", "sendspin_n", synced_to="l")
    world.protocol("sendspin_s", "s", "sendspin")
    world.player("s", "sendspin_s")


async def test_stranded_members_move_along_and_are_released_from_the_own_protocol() -> None:
    """They join the protocol in the same call, and are released once the leader moved."""
    world = _World()
    _own_stream_leader(world)
    devices = _Devices(world)
    await _join(devices, "l", ("s",))
    assert _device_calls(devices) == [
        ("set_output", "s", "sendspin_s"),
        ("set_output", "n", "sendspin_n"),
        _joins("sendspin_l", "sendspin_s", "sendspin_n"),
        ("set_output", "l", "sendspin_l"),
        ("set_members", "airplay_l", {"player_ids_to_remove": ["airplay_n"]}),
    ]


async def test_a_live_own_session_is_stopped_after_its_stranded_members_left() -> None:
    """Rendering on its own stream, the leader's own protocol stops after the release."""
    world = _World()
    _own_stream_leader(world, PLAYING)
    devices = _Devices(world)
    await _join(devices, "l", ("s",))
    assert _device_calls(devices)[4:] == [
        ("set_members", "airplay_l", {"player_ids_to_remove": ["airplay_n"]}),
        ("stop", "airplay_l"),
        ("resume", "l"),
    ]


async def test_stranded_members_are_not_released_from_an_own_protocol_that_went_away() -> None:
    """The own protocol is read after the leader's output moved: gone, nothing is released."""
    world = _World()
    _own_stream_leader(world)
    devices = _Devices(world)
    devices.during["set_output", "l"] = devices.change("l", own_protocol=None)
    await _join(devices, "l", ("s",))
    assert _device_calls(devices)[-1] == ("set_output", "l", "sendspin_l")


# -- the native call, the takeover and the end of a join --


async def test_the_native_call_follows_the_protocol_leg_and_is_decided_after_it() -> None:
    """Native members join after the protocol's members, if the own protocol still groups."""
    world = _World()
    _mixed(world)
    devices = _Devices(world)
    await _join(devices, "s", ("b", "w"))
    assert _device_calls(devices) == [
        ("set_output", "w", "airplay_w"),
        _joins("airplay_s", "airplay_w"),
        ("set_output", "s", "airplay_s"),
        _joins("sonos_s", "sonos_b"),
    ]
    devices = _Devices(world := _World())
    _mixed(world)

    def _no_grouping() -> None:
        world.protocols["sonos_s"] = replace(world.protocols["sonos_s"], features=PLAYS)

    devices.during["set_members", "airplay_s"] = _no_grouping
    await _join(devices, "s", ("b", "w"))
    assert _device_calls(devices)[-1] == ("set_output", "s", "airplay_s")


async def test_a_dissolved_leader_is_stopped_after_its_members_left() -> None:
    """The members leave the own protocol, then the leader stops."""
    world = _World()
    _leader_with_members(world, None)
    devices = _Devices(world)
    await _join(devices, "a", None, ("a",))
    assert devices.calls == [_leaves("sonos_a", "sonos_b", "sonos_c"), ("stop_leader", "a")]


async def test_a_takeover_is_opened_for_new_content_over_a_protocol_only() -> None:
    """The hook runs before anything else and its token goes to the caller; never natively."""
    world = _World()
    _mixed(world)
    devices = _Devices(world)
    protocol = devices.get_protocol("airplay_s")
    assert protocol is not None
    protocol.token = "token"
    assert await _join(devices, "s", ("w",), new_content=True) == (protocol, "token")
    assert _device_calls(devices)[0] == ("takeover", "airplay_s")
    devices = _Devices(world := _World())
    _mixed(world)
    assert await _join(devices, "s", ("b",), new_content=True) is None
    assert _device_calls(devices) == [_joins("sonos_s", "sonos_b")]


async def test_the_join_is_decided_again_after_the_takeover_hook() -> None:
    """A leader that stopped during the hook switches protocols without a refusal."""
    world = _World()
    _switching(world, PLAYING)
    devices = _Devices(world)
    protocol = devices.get_protocol("sendspin_l")
    assert protocol is not None
    protocol.token = "token"
    devices.during["takeover", "sendspin_l"] = devices.change(
        "l", playback_state=PlaybackState.IDLE
    )
    assert await _join(devices, "l", ("y",), new_content=True) == (protocol, "token")
    assert _device_calls(devices) == [
        ("takeover", "sendspin_l"),
        ("set_output", "y", "sendspin_y"),
        _joins("sendspin_l", "sendspin_y"),
        ("set_output", "l", "sendspin_l"),
    ]


async def test_a_native_only_join_opens_no_takeover_while_the_group_is_on_a_protocol() -> None:
    """New content, the group on AirPlay, a member that joins natively: no takeover."""
    world = _World()
    _mixed(world, active_output="airplay_s")
    world.protocol("sonos_c", "c", "sonos", role=ProtocolRole.NATIVE)
    world.player("c", "sonos_c")
    world.protocols["sonos_s"] = replace(
        world.protocols["sonos_s"], can_group_with=frozenset({"sonos_b", "sonos_c"})
    )
    world.players["s"] = replace(world.players["s"], can_group_with=frozenset({"b", "w", "c"}))
    devices = _Devices(world)
    await _join(devices, "s", ("c",), new_content=True)
    assert _device_calls(devices) == [_joins("sonos_s", "sonos_c")]


@pytest.mark.parametrize(("token", "aborted"), [("token", True), (None, False)])
async def test_a_failure_after_the_hook_aborts_the_takeover_it_opened(
    token: str | None, aborted: bool
) -> None:
    """The takeover is aborted before the error propagates; a hook without a token opened none."""
    world = _World()
    _mixed(world)
    devices = _Devices(world)
    protocol = devices.get_protocol("airplay_s")
    assert protocol is not None
    protocol.token = token
    devices.failing["set_members", "airplay_s"] = PlayerCommandFailed("refused")
    with pytest.raises(PlayerCommandFailed, match="refused"):
        await _join(devices, "s", ("w",), new_content=True)
    assert (("takeover_aborted", "airplay_s", "token") in devices.calls) is aborted


async def test_a_refused_switch_calls_no_device_and_aborts_the_takeover() -> None:
    """The refusal comes before any grouping call, and aborts a takeover the hook opened."""
    world = _World()
    _switching(world, PLAYING)
    devices = _Devices(world)
    with pytest.raises(PlayerCommandFailed) as err:
        await _join(devices, "l", ("y",))
    assert err.value.translation_key == "member_cannot_follow_protocol"
    assert _device_calls(devices) == []
    devices = _Devices(world)
    protocol = devices.get_protocol("sendspin_l")
    assert protocol is not None
    protocol.token = "token"
    with pytest.raises(PlayerCommandFailed):
        await _join(devices, "l", ("y",), new_content=True)
    assert _device_calls(devices) == [
        ("takeover", "sendspin_l"),
        ("takeover_aborted", "sendspin_l", "token"),
    ]


async def test_the_join_calls_the_protocol_object_it_holds() -> None:
    """The group's protocol object is held from the start, also when replaced while joining."""
    world = _World()
    _mixed(world)
    devices = _Devices(world)
    held = devices.get_protocol("airplay_s")

    def _replaced() -> None:
        devices.protocol_objects["airplay_s"] = _Protocol(devices, "airplay_s")

    devices.during["set_output", "w"] = _replaced
    calls: list[Any] = []

    async def _set_members(**lists: Any) -> None:
        calls.append(lists)

    assert held is not None
    held.set_members = _set_members  # type: ignore[method-assign]
    await _join(devices, "s", ("w",))
    assert calls == [{"player_ids_to_add": ["airplay_w"], "player_ids_to_remove": None}]


# -- the regroup at a fresh start --


async def _regroup(devices: _Devices, leader: str) -> None:
    """Run the regroup of a leader at a fresh start on the fake controller."""
    change = GroupChange(kind=ChangeKind.REGROUP_FOR_START, target_id=leader)
    with _reading():
        await grouping.apply_group_change(devices, change)  # type: ignore[arg-type]


async def test_a_regroup_lets_the_members_go_and_joins_them_afresh() -> None:
    """Removed from the protocol, their outputs and the leader's cleared, joined natively."""
    world = _World()
    _left_on_bridge(world)
    devices = _Devices(world)
    await _regroup(devices, "a")
    assert devices.calls == [
        _leaves("airplay_a", "airplay_b"),
        ("clear_output", "b"),
        ("clear_output", "a"),
        _joins("sonos_a", "sonos_b"),
    ]


async def test_a_regroup_reads_the_outputs_after_the_removal_call() -> None:
    """A member whose output moved on while the removal ran keeps it."""
    world = _World()
    _left_on_bridge(world)
    devices = _Devices(world)
    devices.during["set_members", "airplay_a"] = devices.change("b", active_output="native")
    await _regroup(devices, "a")
    assert ("clear_output", "b") not in devices.calls
    assert devices.calls[-1] == _joins("sonos_a", "sonos_b")
