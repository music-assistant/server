"""Tests for a group that loses the AirPlay stream of its leader, on a running server."""

from __future__ import annotations

import asyncio
import re
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature
from zeroconf import ServiceStateChange

from music_assistant.constants import CONF_POWER_CONTROL, PLAYER_CONTROL_PROTOCOL
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.providers.airplay.control_player import AirPlayControlPlayer
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.stream import AirPlayStream
from music_assistant.providers.airplay.stream_session import AirPlayStreamSession
from music_assistant.providers.sync_group.player import SyncGroupPlayer
from tests.providers.airplay.test_lifecycle import (
    FIRST_HOMEPOD_ID,
    SECOND_PAIR_HOMEPOD_ID,
    _announce,
    _pair_homepod,
    _player_of,
    _service,
)
from tests.scenarios.harness import TEST_TRACK_URI

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Iterator

    from music_assistant.models.player import Player
    from music_assistant.providers.airplay.player import AirPlayPlayer
    from tests.scenarios.harness import Scenario

# past the first attempt of a lost player to join its group again
AFTER_REJOIN = 8
# past the moment a group that stopped playing gives up its members
AFTER_IDLE_GRACE = 15


class _Cli:
    """A cliairplay process that answers the commands of its stream, until it ends."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        """Initialize the process, whatever it is called with."""
        self.closed = False
        self.stdin_closed = False
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()
        self._ended = asyncio.Event()

    async def start(self) -> None:
        """Connect to a receiver that can play at once."""
        self._report("connected", "clock_ready mode=ntp state=ready", "audio buffered_ms=500")

    def attach_stderr_reader(self, _task: asyncio.Task[None]) -> None:
        """Take the task that reads what the process reports."""

    async def iter_stderr(self) -> AsyncGenerator[str]:
        """Yield what the process reports, until it ends."""
        while (line := await self._lines.get()) is not None:
            yield line

    async def read(self, _size: int) -> bytes:
        """Report nothing on stdout, until the process ends."""
        await self._ended.wait()
        return b""

    async def write_eof(self) -> None:
        """Take the end of the audio."""
        self.stdin_closed = True

    async def kill(self) -> None:
        """End the process."""
        self.end()

    async def close(self) -> None:
        """End the process."""
        self.end()

    async def wait_ended(self) -> None:
        """Wait until the process ends."""
        await self._ended.wait()

    def command(self, command: str) -> None:
        """
        Answer a command of the stream.

        :param command: The command, as written to the command pipe.
        """
        if "ACTION=START" in command:
            at = re.search(r"START_UNIX_MS=(\d+)", command)
            assert at is not None
            self._report(
                f"started requested_unix_ms={at[1]} at_unix_ms={at[1]}", "playing elapsed_ms=0"
            )
        elif "ACTION=PAUSE" in command:
            self._report("paused")
        elif "ACTION=FLUSH" in command:
            self._report("flushed")
        elif "ACTION=STOP" in command:
            self._report("stopped")

    def end(self, *lines: str) -> None:
        """
        End the process, as one that lost its receiver does.

        :param lines: What it reports before it ends.
        """
        if not self.closed:
            self.closed = True
            self._report(*lines)
            self._lines.put_nowait(None)
            self._ended.set()

    def _report(self, *lines: str) -> None:
        """Report status lines on stderr."""
        for line in lines:
            self._lines.put_nowait(f"[STATUS] {line}")


class _StuckFFmpeg:
    """The ffmpeg of a receiver that stopped reading: a write hangs until its process ends."""

    def __init__(self, process: _Cli) -> None:
        """
        Initialize the ffmpeg.

        :param process: The process it writes to.
        """
        self.closed = False
        self._process = process

    async def write(self, _chunk: bytes) -> None:
        """Write to the process, which never takes it."""
        await self._process.wait_ended()
        raise BrokenPipeError

    async def kill(self) -> None:
        """End the ffmpeg."""
        self.closed = True


async def _write_command(stream: AirPlayStream, command: str) -> bool:
    """Hand a command of a stream to its process."""
    process: Any = stream._cli_proc
    if process is None or process.closed:
        return False
    process.command(command)
    return True


async def _silence(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
    """Yield a second of silence every second."""
    while True:
        yield bytes(44100 * 2 * 2)
        await asyncio.sleep(1)


@pytest.fixture(autouse=True)
def cliairplay(scenario: Scenario) -> Iterator[None]:
    """
    Replace the cliairplay processes, and the audio and the metadata they are fed.

    :param scenario: The running server, whose clock the processes follow.
    """
    with (
        patch(
            "music_assistant.providers.airplay.stream_session.cli_clock_time",
            lambda _clock: scenario.wall_time(),
        ),
        patch("music_assistant.providers.airplay.stream.AsyncProcess", _Cli),
        patch.object(AsyncNamedPipeWriter, "create", AsyncMock()),
        patch.object(AsyncNamedPipeWriter, "remove", AsyncMock()),
        patch.object(AsyncNamedPipeWriter, "wait_for_reader", AsyncMock(return_value=True)),
        patch.object(AirPlayStream, "_build_cli_args", AsyncMock(return_value=[])),
        patch.object(AirPlayStream, "_write_cli_command", _write_command),
        patch.object(AirPlayStream, "send_metadata", AsyncMock()),
        patch.object(AirPlayStreamSession, "_start_player_ffmpeg", AsyncMock()),
        patch.object(AirPlayProvider, "wait_ptp_daemon_ready", AsyncMock(return_value=True)),
        patch.object(StreamsController, "get_stream", _silence),
    ):
        yield


@pytest.fixture(autouse=True)
def lost_stream_log(scenario: Scenario) -> None:
    """Allow the warning every lost stream logs."""
    scenario.expect_log("cliairplay process stopped unexpectedly")


async def _receivers(
    scenario: Scenario, provider: AirPlayProvider, letters: str = "ABC", *, native: bool = False
) -> list[AirPlayPlayer]:
    """
    Attach AirPlay receivers and return their protocols.

    :param letters: The letter of each player.
    :param native: Whether the devices have a protocol of their own as well.
    """
    devices = {letter: f"00:11:22:33:44:0{number}" for number, letter in enumerate(letters, 1)}
    if native:
        sonos = await scenario.add_provider("sonos")
        for letter, mac in devices.items():
            await scenario.register(
                sonos.protocol(
                    f"RINCON_{letter}",
                    f"Player {letter}",
                    identifiers={IdentifierType.MAC_ADDRESS: mac},
                )
            )
    protocols = []
    for number, (letter, mac) in enumerate(devices.items(), 1):
        protocol = await _announce(
            scenario, provider, _service(f"Player {letter}", mac, f"192.168.1.5{number}")
        )
        assert protocol is not None
        protocols.append(protocol)
    await scenario.idle()
    return protocols


async def _play_group(
    scenario: Scenario, protocols: list[AirPlayPlayer], *, dynamic: bool = True
) -> Player:
    """
    Play on a new sync group of players, led by the first one.

    :param protocols: The protocols of the players.
    :param dynamic: Whether the members of the group can change.
    """
    group = await scenario.mass.players.create_group_player(
        "sync_group", "Group Z", [_player_of(x).player_id for x in protocols], dynamic
    )
    await scenario.settle()
    await scenario.play(group.player_id)
    await scenario.idle()
    assert _led_by(group) is _player_of(protocols[0])
    assert _members(scenario, group) == [_player_of(x).name for x in protocols]
    assert _state(group) == PlaybackState.PLAYING
    return group


async def _play_again(scenario: Scenario, group: Player) -> None:
    """Play on a group that played before."""
    task = await scenario.command(
        scenario.mass.player_queues.play_media(group.player_id, TEST_TRACK_URI)
    )
    await scenario.idle()
    await task
    assert _state(group) == PlaybackState.PLAYING


@contextmanager
def _out_of_reach(protocol: AirPlayPlayer) -> Iterator[list[str]]:
    """
    Keep a receiver from answering new connections.

    :param protocol: The protocol of its player.
    :return: One entry for each attempt to connect to it.
    """
    attempts: list[str] = []
    connect = AirPlayStream.connect

    async def _connect(stream: AirPlayStream, use_shared_ptp: bool = False) -> None:
        if stream.player is not protocol:
            await connect(stream, use_shared_ptp)
            return
        attempts.append(protocol.protocol_id)
        with patch.object(_Cli, "start", AsyncMock()):
            await connect(stream, use_shared_ptp)

    with patch.object(AirPlayStream, "connect", _connect):
        yield attempts


async def _announce_again(
    scenario: Scenario, provider: AirPlayProvider, protocol: AirPlayPlayer
) -> None:
    """Announce a receiver again, as it does when it shows up on the network."""
    info = protocol.raop_discovery_info or protocol.airplay_discovery_info
    assert info is not None
    await provider.on_mdns_service_state_change(info.name, ServiceStateChange.Updated, info)
    await scenario.advance(2)


async def _stop(scenario: Scenario, group: Player) -> None:
    """Stop a group and let it give up its members."""
    await scenario.command(scenario.mass.players.cmd_stop(group.player_id))
    await scenario.advance(AFTER_IDLE_GRACE)
    assert _state(group) == PlaybackState.IDLE
    assert _led_by(group) is None


def _state(player: Player) -> PlaybackState:
    """Return what a player does now."""
    return player.playback_state


def _led_by(group: Player) -> Player | None:
    """Return the sync leader of a group."""
    assert isinstance(group.own_protocol, SyncGroupPlayer)
    return group.own_protocol.sync_leader


def _members(scenario: Scenario, player: Player) -> list[str]:
    """Return the names of the members of a (group) player, as clients see them."""
    state = scenario.player(player.player_id)
    assert state is not None
    members = [scenario.mass.players.get_player(player_id) for player_id in state.group_members]
    return sorted(member.name for member in members if member is not None)


def _cli(protocol: AirPlayPlayer) -> _Cli:
    """Return the process of the stream of a player."""
    assert protocol.stream is not None
    return cast("_Cli", protocol.stream._cli_proc)


@pytest.mark.parametrize("dynamic", [True, False])
async def test_the_remaining_members_play_on(
    scenario: Scenario, provider: AirPlayProvider, dynamic: bool
) -> None:
    """The members of a sync group play on when its leader is lost, which comes back to them."""
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c], dynamic=dynamic)
    queue = scenario.queue(group.player_id)
    assert queue is not None
    playing_item = queue.current_item
    session = b.session
    remaining = [_cli(b), _cli(c)]

    _cli(a).end()
    await scenario.settle()

    assert _state(group) == PlaybackState.PLAYING
    assert _led_by(group) is _player_of(b)
    assert _members(scenario, group) == ["Player B", "Player C"]
    assert a.playback_state == PlaybackState.IDLE
    # a fixed group keeps it
    assert _player_of(a).active_group == (None if dynamic else group.player_id)
    assert session is not None
    assert set(session.sync_clients) == {b, c}

    await scenario.advance(AFTER_REJOIN)

    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    assert set(session.sync_clients) == {a, b, c}
    await scenario.advance(AFTER_IDLE_GRACE)
    assert _state(group) == PlaybackState.PLAYING
    queue = scenario.queue(group.player_id)
    assert queue is not None
    assert queue.state == PlaybackState.PLAYING
    assert queue.current_item is playing_item
    # nothing was restarted
    assert [_cli(b), _cli(c)] == remaining
    assert not any(process.closed for process in remaining)
    await _stop(scenario, group)


async def test_members_that_joined_late_play_on(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A member that joined while the group played takes it over like any other."""
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a])
    await scenario.mass.players.cmd_set_members(
        group.player_id, player_ids_to_add=[_player_of(x).player_id for x in (b, c)]
    )
    await scenario.idle()
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]

    _cli(a).end()
    await scenario.settle()

    assert _state(group) == PlaybackState.PLAYING
    assert _members(scenario, group) == ["Player B", "Player C"]
    await scenario.advance(AFTER_REJOIN)
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    await _stop(scenario, group)


@pytest.mark.parametrize("dynamic", [True, False])
async def test_a_lost_leader_is_part_of_the_next_formation(
    scenario: Scenario, provider: AirPlayProvider, dynamic: bool
) -> None:
    """A leader that was lost and not back yet stays a member, and plays with the group again."""
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c], dynamic=dynamic)
    _cli(a).end()
    await scenario.settle()
    assert _members(scenario, group) == ["Player B", "Player C"]

    await _stop(scenario, group)

    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    await _play_again(scenario, group)
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    assert [x.playback_state for x in (a, b, c)] == [PlaybackState.PLAYING] * 3
    await _stop(scenario, group)


@pytest.mark.parametrize("dynamic", [True, False])
async def test_a_lost_leader_gets_its_volume_on_its_own_protocol_again(
    scenario: Scenario, provider: AirPlayProvider, dynamic: bool
) -> None:
    """A leader that fell out of its group is at rest, while its members play on over AirPlay."""
    a, b = await _receivers(scenario, provider, "AB", native=True)
    group = await _play_group(scenario, [a, b], dynamic=dynamic)
    assert _player_of(a).active_output_protocol == a.protocol_id

    _cli(a).end()
    await scenario.settle()

    assert _player_of(a).active_output_protocol in (None, "native")
    assert _player_of(b).active_output_protocol == b.protocol_id
    native = scenario.protocol("RINCON_A")
    native.commands.clear()
    await scenario.mass.players.cmd_volume_set(_player_of(a).player_id, 25)
    assert ("volume_set", 25) in native.commands
    await _stop(scenario, group)


@pytest.mark.parametrize("lost", [True, False])
async def test_a_re_joined_member_keeps_its_volume_when_it_leads(
    scenario: Scenario, provider: AirPlayProvider, lost: bool
) -> None:
    """
    A member that was lost and came back leads at the volume it plays at.

    The user selected "auto" as the volume and mute control of the players.

    :param lost: Whether the leader before it was lost too, or was taken out by the user.
    """
    a, b = await _receivers(scenario, provider, "AB", native=True)
    for protocol in (a, b):
        await scenario.configure_player(
            _player_of(protocol).player_id,
            volume_control=PLAYER_CONTROL_PROTOCOL,
            mute_control=PLAYER_CONTROL_PROTOCOL,
        )
    for letter, volume in (("A", 10), ("B", 20)):
        await scenario.report(scenario.protocol(f"RINCON_{letter}"), volume_level=volume)
    group = await _play_group(scenario, [a, b])
    _cli(b).end()
    await scenario.advance(AFTER_REJOIN)
    await scenario.mass.players.cmd_volume_set(_player_of(b).player_id, 55)
    await scenario.settle()

    if lost:
        _cli(a).end()
    else:
        await scenario.mass.players.cmd_set_members(
            group.player_id, player_ids_to_remove=[_player_of(a).player_id]
        )
    await scenario.settle()

    assert _led_by(group) is _player_of(b)
    assert _state(group) == PlaybackState.PLAYING
    assert _player_of(b).active_output_protocol == b.protocol_id
    assert _player_of(b).volume_level == 55
    assert b.volume_level == 55
    await _stop(scenario, group)


async def test_the_group_outlives_one_leader_after_the_other(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A group whose leaders are lost in turn plays on with whoever is left or came back."""
    a, b, c = await _receivers(scenario, provider, native=True)
    group = await _play_group(scenario, [a, b, c])
    for lost, leader in ((a, b), (b, c)):
        _cli(lost).end()
        await scenario.settle()
        assert _led_by(group) is _player_of(leader)
        assert _state(group) == PlaybackState.PLAYING
    assert _members(scenario, group) == ["Player C"]
    await scenario.advance(AFTER_REJOIN)
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]

    _cli(c).end()
    await scenario.settle()

    assert _led_by(group) in (_player_of(a), _player_of(b))
    assert _state(group) == PlaybackState.PLAYING
    await scenario.advance(AFTER_REJOIN + AFTER_IDLE_GRACE)
    assert _state(group) == PlaybackState.PLAYING
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    await _stop(scenario, group)


async def test_a_member_lost_along_with_the_leader_does_not_take_over(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """The group plays on with the member that still plays when two streams die at once."""
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c])

    for lost in (a, b):
        _cli(lost).end()
    scenario.expect_log("did not produce a running stream")
    await scenario.settle()

    assert _led_by(group) is _player_of(c)
    assert _state(group) == PlaybackState.PLAYING
    await scenario.advance(AFTER_IDLE_GRACE)
    assert _state(group) == PlaybackState.PLAYING
    await _stop(scenario, group)
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]


@pytest.mark.parametrize("dynamic", [True, False])
async def test_a_new_leader_lost_during_the_handover_is_passed_over(
    scenario: Scenario, provider: AirPlayProvider, dynamic: bool
) -> None:
    """The member that is still heard leads when the new leader is lost before it leads."""
    a, b, c = await _receivers(scenario, provider, native=True)
    group = await _play_group(scenario, [a, b, c], dynamic=dynamic)
    queue = scenario.queue(group.player_id)
    assert queue is not None
    playing_item = queue.current_item
    session = c.session
    assert session is not None
    remaining = _cli(c)

    async with session._lock:
        for lost in (a, b):
            _cli(lost).end()
            await scenario.settle()
        # the handover made its choice before it got to the session
        assert _led_by(group) is _player_of(b)
        # long enough for what follows on a group that came to rest
        await scenario.advance(AFTER_IDLE_GRACE)
    await scenario.settle()

    assert _led_by(group) is _player_of(c)
    assert _state(group) == PlaybackState.PLAYING
    # a player that is not heard has its volume on its own protocol again
    assert _player_of(b).active_output_protocol in (None, "native")
    scenario.expect_log("did not produce a running stream")
    await scenario.advance(40)
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    assert _state(group) == PlaybackState.PLAYING
    queue = scenario.queue(group.player_id)
    assert queue is not None
    assert queue.current_item is playing_item
    assert _cli(c) is remaining
    assert not remaining.closed
    await _stop(scenario, group)


@pytest.mark.parametrize("dynamic", [True, False])
@pytest.mark.parametrize("process_ends", [True, False])
async def test_a_new_leader_that_stopped_reading_is_passed_over(
    scenario: Scenario, provider: AirPlayProvider, process_ends: bool, dynamic: bool
) -> None:
    """
    The handover outlasts a new leader that is off the network, along with the lost one.

    :param process_ends: Whether its process ends, or the session gives up on it.
    :param dynamic: Whether the members of the group can change.
    """
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c], dynamic=dynamic)
    session = c.session
    assert session is not None
    remaining = _cli(c)
    session._player_ffmpeg[b.protocol_id] = cast("Any", _StuckFFmpeg(_cli(b)))
    await scenario.advance(2)
    assert session._lock.locked()

    _cli(a).end()
    await scenario.advance(2)
    if process_ends:
        _cli(b).end()
        scenario.expect_log("due to write error")
    else:
        scenario.expect_log("stopped reading data")
    scenario.expect_log("did not produce a running stream")
    await scenario.advance(35 + AFTER_IDLE_GRACE)

    assert _led_by(group) is _player_of(c)
    assert _state(group) == PlaybackState.PLAYING
    assert "Player C" in _members(scenario, group)
    assert _cli(c) is remaining
    assert not remaining.closed
    await _stop(scenario, group)


async def test_a_group_that_loses_the_rest_during_the_handover_ends(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A group with nobody left once the leader is out comes to rest, and plays again later."""
    protocols = await _receivers(scenario, provider)
    group = await _play_group(scenario, protocols)
    session = protocols[0].session
    assert session is not None

    async with session._lock:
        for protocol in protocols:
            _cli(protocol).end()
            await scenario.settle()
    await scenario.advance(AFTER_IDLE_GRACE)

    assert _state(group) == PlaybackState.IDLE
    assert _led_by(group) is None
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    await _play_again(scenario, group)
    assert [x.playback_state for x in protocols] == [PlaybackState.PLAYING] * 3
    await _stop(scenario, group)


async def test_a_group_with_nobody_left_to_play_ends(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A group that lost every stream at once comes to rest, and plays again when asked to."""
    protocols = await _receivers(scenario, provider)
    group = await _play_group(scenario, protocols)

    for protocol in reversed(protocols):
        _cli(protocol).end()
    await scenario.advance(AFTER_IDLE_GRACE)

    assert _state(group) == PlaybackState.IDLE
    assert _led_by(group) is None
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    await _play_again(scenario, group)
    assert [x.playback_state for x in protocols] == [PlaybackState.PLAYING] * 3
    await _stop(scenario, group)


async def test_a_paused_group_that_loses_its_leader_ends(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A group that is not playing has nothing to hand over: it ends, and plays again later."""
    a, b = await _receivers(scenario, provider, "AB")
    group = await _play_group(scenario, [a, b])
    await scenario.command(scenario.mass.players.cmd_pause(group.player_id))
    await scenario.advance(2)
    assert _state(group) == PlaybackState.PAUSED

    _cli(a).end()
    await scenario.advance(AFTER_IDLE_GRACE)

    assert _state(group) == PlaybackState.IDLE
    assert _led_by(group) is None
    await _play_again(scenario, group)
    assert _members(scenario, group) == ["Player A", "Player B"]
    await _stop(scenario, group)


@pytest.mark.parametrize("leader", [True, False], ids=["leader", "member"])
async def test_a_taken_over_player_leaves_a_dynamic_group_and_is_not_taken_back(
    scenario: Scenario, provider: AirPlayProvider, leader: bool
) -> None:
    """A receiver that another sender took over is left to that sender, the others play on."""
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c])
    taken, others = (a, [b, c]) if leader else (c, [a, b])
    process = _cli(taken)
    remaining = [_cli(x) for x in others]

    process.end("receiver_closed")
    await scenario.advance(60)

    assert _state(group) == PlaybackState.PLAYING
    assert _members(scenario, group) == [_player_of(x).name for x in others]
    assert _cli(taken) is process
    assert [_cli(x) for x in others] == remaining
    assert not any(x.closed for x in remaining)
    await _stop(scenario, group)
    # nor is it when the group plays again, until the user adds it
    await _play_again(scenario, group)
    assert _members(scenario, group) == [_player_of(x).name for x in others]
    await _stop(scenario, group)


@pytest.mark.parametrize("leader", [True, False], ids=["leader", "member"])
async def test_a_taken_over_player_ends_a_fixed_group(
    scenario: Scenario, provider: AirPlayProvider, leader: bool
) -> None:
    """A fixed group plays on all of its members or not at all: another sender ends it."""
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c], dynamic=False)
    taken = a if leader else c
    process = _cli(taken)

    process.end("receiver_closed")
    await scenario.settle()

    assert _state(group) == PlaybackState.IDLE
    assert _led_by(group) is None
    await scenario.advance(60)
    assert [x.playback_state for x in (a, b, c)] == [PlaybackState.IDLE] * 3
    # it is left to the other sender
    assert taken.stream is None or _cli(taken) is process
    await _play_again(scenario, group)
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    await _stop(scenario, group)


async def test_a_fixed_group_that_was_started_again_outlives_the_takeover(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A takeover that is handled after the group was started again does not end it."""
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c], dynamic=False)
    taken_over = asyncio.Event()

    async def _run() -> None:
        async with scenario.mass.players.get_player_lock(group.player_id):
            await taken_over.wait()
            await scenario.mass.player_queues.play_media(group.player_id, TEST_TRACK_URI)

    task = await scenario.command(_run())
    _cli(c).end("receiver_closed")
    await scenario.settle()
    taken_over.set()
    await scenario.advance(AFTER_IDLE_GRACE)

    await task
    assert _state(group) == PlaybackState.PLAYING
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    await _stop(scenario, group)


async def test_a_lost_member_still_comes_back_to_its_leader(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A member that is lost leaves the leader in place."""
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c])

    _cli(b).end()
    await scenario.settle()

    assert _led_by(group) is _player_of(a)
    assert a.group_members == [a.protocol_id, c.protocol_id]
    await scenario.advance(AFTER_REJOIN)
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    assert _state(group) == PlaybackState.PLAYING
    await _stop(scenario, group)


async def test_an_ad_hoc_group_moves_its_queue_to_a_member(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """Without a group player the queue of the lost leader is handed to a member, as before."""
    a, b, c = await _receivers(scenario, provider)
    await scenario.mass.players.cmd_set_members(
        _player_of(a).player_id, player_ids_to_add=[_player_of(x).player_id for x in (b, c)]
    )
    await scenario.settle()
    await scenario.play(_player_of(a).player_id)
    await scenario.idle()
    assert _members(scenario, _player_of(a)) == ["Player A", "Player B", "Player C"]

    _cli(a).end()
    await scenario.advance(AFTER_REJOIN)

    queue = scenario.queue(_player_of(b).player_id)
    assert queue is not None
    assert queue.state == PlaybackState.PLAYING
    assert {"Player B", "Player C"} <= set(_members(scenario, _player_of(b)))
    await scenario.command(scenario.mass.players.cmd_stop(_player_of(b).player_id))
    await scenario.idle()


async def test_a_pair_that_loses_a_speaker_hands_the_group_over_as_a_whole(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A stereo pair that leads a group leaves it with both speakers, and the others play on."""
    with patch.object(
        AirPlayControlPlayer, "_connect_control_services", AsyncMock(return_value=False)
    ):
        for record in (
            _pair_homepod(FIRST_HOMEPOD_ID, 0),
            _pair_homepod(SECOND_PAIR_HOMEPOD_ID, 1),
        ):
            await _announce(scenario, provider, record)
        pair = provider.get_player("apaabbcc003301")
        assert pair is not None
        b, c = await _receivers(scenario, provider, "BC")
        group = await _play_group(scenario, [pair, b, c])
        (speaker,) = pair.speakers
        carrier_process = _cli(pair)

        _cli(speaker).end()
        await scenario.settle()

        assert _state(group) == PlaybackState.PLAYING
        assert _led_by(group) is _player_of(b)
        assert _members(scenario, group) == ["Player B", "Player C"]
        assert carrier_process.closed
        await scenario.advance(AFTER_REJOIN)
        assert _members(scenario, group) == ["Loft", "Player B", "Player C"]
        await _stop(scenario, group)


async def test_a_handover_waits_for_a_command_on_the_group(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A loss while another command works on the group is handled once that is done."""
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c])

    async with scenario.mass.players.get_player_lock(group.player_id):
        _cli(a).end()
        await scenario.settle()
        assert _led_by(group) is _player_of(a)
    await scenario.settle()

    assert _led_by(group) is _player_of(b)
    assert _state(group) == PlaybackState.PLAYING
    await scenario.advance(AFTER_REJOIN)
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    await _stop(scenario, group)


async def test_a_leader_comes_back_after_a_handover_that_had_to_wait(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """The attempts of a lost leader to join again start once the group is handed over."""
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c])
    scenario.expect_log("is held by another command")

    async with scenario.mass.players.get_player_lock(group.player_id):
        _cli(a).end()
        await scenario.advance(27)
    await scenario.settle()

    assert _led_by(group) is _player_of(b)
    assert _state(group) == PlaybackState.PLAYING
    assert _members(scenario, group) == ["Player B", "Player C"]
    await scenario.advance(AFTER_REJOIN)
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    await _stop(scenario, group)


async def _remove_command(scenario: Scenario, group: Player, leader: Player) -> None:
    """Take the leader out of the group."""
    await scenario.mass.players.cmd_set_members(
        group.player_id, player_ids_to_remove=[leader.player_id]
    )


async def _ungroup_command(scenario: Scenario, _group: Player, leader: Player) -> None:
    """Ungroup the leader."""
    await scenario.mass.players.cmd_ungroup(leader.player_id)


async def _switch_off_command(scenario: Scenario, _group: Player, leader: Player) -> None:
    """Switch the leader off."""
    await scenario.mass.players.cmd_power(leader.player_id, False)


@pytest.mark.parametrize("command", [_remove_command, _ungroup_command])
async def test_a_leader_that_was_taken_out_shows_what_its_device_plays_by_itself(
    scenario: Scenario,
    provider: AirPlayProvider,
    command: Callable[[Scenario, Player, Player], Coroutine[Any, Any, None]],
) -> None:
    """
    A leader that the user takes out of its group plays on its own protocol again.

    :param command: How the user takes the leader out.
    """
    a, b, c = await _receivers(scenario, provider, native=True)
    group = await _play_group(scenario, [a, b, c])
    leader = _player_of(a)

    await command(scenario, group, leader)
    await scenario.settle()

    assert _led_by(group) is _player_of(b)
    assert leader.active_output_protocol is None
    # another app plays on its device
    await scenario.report(
        scenario.protocol("RINCON_A"), playback_state=PlaybackState.PLAYING, active_source="spotify"
    )
    assert leader.playback_state == PlaybackState.PLAYING
    assert leader.active_source == "spotify"
    await _stop(scenario, group)


@pytest.mark.parametrize("leader", [True, False], ids=["leader", "member"])
async def test_a_lost_player_the_user_plays_on_is_not_taken_back(
    scenario: Scenario, provider: AirPlayProvider, leader: bool
) -> None:
    """A lost player that the user plays something else on keeps playing that."""
    a, b, c = await _receivers(scenario, provider, native=True)
    group = await _play_group(scenario, [a, b, c])
    lost = a if leader else c
    player = _player_of(lost)

    _cli(lost).end()
    await scenario.advance(1)
    await scenario.command(scenario.mass.player_queues.play_media(player.player_id, TEST_TRACK_URI))
    await scenario.advance(AFTER_REJOIN)

    assert player.playback_state == PlaybackState.PLAYING
    assert player.active_group is None
    assert b.session is not None
    assert lost not in b.session.sync_clients
    assert _state(group) == PlaybackState.PLAYING
    await scenario.command(scenario.mass.players.cmd_stop(player.player_id))
    await _stop(scenario, group)


@pytest.mark.parametrize("leader", [True, False], ids=["leader", "member"])
async def test_a_lost_player_the_user_switches_off_stays_off(
    scenario: Scenario, provider: AirPlayProvider, leader: bool
) -> None:
    """A lost player that the user switches off does not come back to its group."""
    a, b, c = await _receivers(scenario, provider)
    lost = a if leader else c
    player = _player_of(lost)
    await scenario.configure_player(player.player_id, **{CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE})
    group = await _play_group(scenario, [a, b, c])

    _cli(lost).end()
    await scenario.advance(2)
    task = await scenario.command(scenario.mass.players.cmd_power(player.player_id, False))
    await scenario.advance(AFTER_REJOIN)
    await task

    assert player.powered is False
    assert player.playback_state == PlaybackState.IDLE
    assert b.session is not None
    assert lost not in b.session.sync_clients
    assert _state(group) == PlaybackState.PLAYING
    await _stop(scenario, group)


@pytest.mark.parametrize("leader", [True, False], ids=["leader", "member"])
async def test_a_fixed_group_ends_when_the_user_plays_on_a_lost_player(
    scenario: Scenario, provider: AirPlayProvider, leader: bool
) -> None:
    """Media played on a member of a fixed group ends the group, also while that member is out."""
    a, b, c = await _receivers(scenario, provider, native=True)
    group = await _play_group(scenario, [a, b, c], dynamic=False)
    lost, others = (a, [b, c]) if leader else (c, [a, b])
    player = _player_of(lost)

    _cli(lost).end()
    await scenario.advance(1)
    await scenario.command(scenario.mass.player_queues.play_media(player.player_id, TEST_TRACK_URI))
    await scenario.advance(2)

    assert _state(group) == PlaybackState.IDLE
    assert _led_by(group) is None
    await scenario.advance(AFTER_REJOIN)
    assert player.playback_state == PlaybackState.PLAYING
    assert [_player_of(x).playback_state for x in others] == [PlaybackState.IDLE] * 2
    await scenario.command(scenario.mass.players.cmd_stop(player.player_id))
    await scenario.idle()


@pytest.mark.parametrize("leader", [True, False], ids=["leader", "member"])
async def test_a_fixed_group_ends_when_the_user_switches_a_lost_player_off(
    scenario: Scenario, provider: AirPlayProvider, leader: bool
) -> None:
    """A member of a fixed group that is switched off ends the group, also while it is out."""
    a, b, c = await _receivers(scenario, provider)
    lost = a if leader else c
    player = _player_of(lost)
    await scenario.configure_player(player.player_id, **{CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE})
    group = await _play_group(scenario, [a, b, c], dynamic=False)

    _cli(lost).end()
    await scenario.advance(1)
    task = await scenario.command(scenario.mass.players.cmd_power(player.player_id, False))
    await scenario.advance(2)
    await task

    assert _state(group) == PlaybackState.IDLE
    assert _led_by(group) is None
    await scenario.advance(AFTER_REJOIN)
    assert player.powered is False
    assert [x.playback_state for x in (a, b, c)] == [PlaybackState.IDLE] * 3


@pytest.mark.parametrize("leader", [True, False], ids=["leader", "member"])
async def test_a_lost_player_does_not_join_a_member_that_plays_alone_after_the_group(
    scenario: Scenario, provider: AirPlayProvider, leader: bool
) -> None:
    """A lost player does not come back to a player that plays alone once its group stopped."""
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c])
    lost, other = (a, b) if leader else (c, a)

    _cli(lost).end()
    await scenario.advance(1)
    await scenario.command(scenario.mass.players.cmd_stop(group.player_id))
    await scenario.advance(AFTER_IDLE_GRACE)
    await scenario.play(_player_of(other).player_id)
    await scenario.advance(300)

    assert lost.playback_state == PlaybackState.IDLE
    assert other.group_members == []
    assert _player_of(other).playback_state == PlaybackState.PLAYING
    await scenario.command(scenario.mass.players.cmd_stop(_player_of(other).player_id))
    await scenario.idle()


async def test_a_lost_player_the_user_removes_does_not_come_back(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A lost player that the user takes out while it tries to come back stays out."""
    scenario.expect_log("cliairplay did not connect")
    scenario.expect_log("did not produce a running stream")
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c])

    with _out_of_reach(a):
        _cli(a).end()
        await scenario.advance(AFTER_REJOIN)
        # it is listed during an attempt
        assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
        remove = await scenario.command(
            scenario.mass.players.cmd_set_members(
                group.player_id, player_ids_to_remove=[_player_of(a).player_id]
            )
        )
        await scenario.advance(12)
        await remove
    await scenario.advance(300)

    assert a.playback_state == PlaybackState.IDLE
    assert _members(scenario, group) == ["Player B", "Player C"]
    assert _state(group) == PlaybackState.PLAYING
    await _stop(scenario, group)


@pytest.mark.parametrize("dynamic", [True, False])
@pytest.mark.parametrize("leader", [True, False], ids=["leader", "member"])
async def test_a_player_that_was_away_for_long_comes_back_once_it_announces_itself(
    scenario: Scenario, provider: AirPlayProvider, leader: bool, dynamic: bool
) -> None:
    """A lost player that can not be reached for minutes is back as soon as it shows up again."""
    scenario.expect_log("cliairplay did not connect")
    scenario.expect_log("did not produce a running stream")
    protocols = await _receivers(scenario, provider)
    a, b, c = protocols
    group = await _play_group(scenario, [a, b, c], dynamic=dynamic)
    lost, other = (a, b) if leader else (c, a)

    with _out_of_reach(lost) as attempts:
        _cli(lost).end()
        await scenario.advance(600)
        # the attempts that are not known to make sense are bounded
        assert len(attempts) == 5
        # it announces itself, but can not be reached yet
        await _announce_again(scenario, provider, lost)
        await scenario.advance(600)
        assert len(attempts) == 6
    await scenario.advance(600)
    assert other.session is not None
    assert lost not in other.session.sync_clients
    await _announce_again(scenario, provider, lost)

    assert lost in other.session.sync_clients
    assert _members(scenario, group) == ["Player A", "Player B", "Player C"]
    assert _state(group) == PlaybackState.PLAYING
    await _stop(scenario, group)


async def test_a_lost_player_comes_back_to_a_group_whose_members_changed(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A lost player comes back to its group, whoever plays in it by then."""
    scenario.expect_log("cliairplay did not connect")
    scenario.expect_log("did not produce a running stream")
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b])
    with _out_of_reach(b):
        _cli(b).end()
        await scenario.advance(AFTER_REJOIN)
        for change in (
            {"player_ids_to_add": [_player_of(c).player_id]},
            {"player_ids_to_remove": [_player_of(a).player_id]},
        ):
            task = await scenario.command(
                scenario.mass.players.cmd_set_members(group.player_id, **change)
            )
            await scenario.advance(20)
            await task
        assert _led_by(group) is _player_of(c)
    await scenario.advance(130)

    assert _members(scenario, group) == ["Player B", "Player C"]
    assert b.playback_state == PlaybackState.PLAYING
    assert _state(group) == PlaybackState.PLAYING
    await _stop(scenario, group)


async def test_a_player_that_announces_itself_at_each_attempt_is_not_tried_all_the_time(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A receiver whose record changes with an attempt that fails is tried once in two minutes."""
    scenario.expect_log("cliairplay did not connect")
    scenario.expect_log("did not produce a running stream")
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c])

    with _out_of_reach(c) as attempts:
        _cli(c).end()
        for _ in range(100):
            await _announce_again(scenario, provider, c)
            await scenario.advance(18)

    # 2000 seconds: five attempts in the first 230, and then one each 120 seconds at most
    assert 5 + 10 <= len(attempts) <= 5 + 15
    assert _state(group) == PlaybackState.PLAYING
    await _stop(scenario, group)


async def test_a_lost_player_the_user_groups_elsewhere_is_not_taken_back(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A lost player that follows another player by now stays there when it announces itself."""
    scenario.expect_log("cliairplay did not connect")
    scenario.expect_log("did not produce a running stream")
    a, b, c, d = await _receivers(scenario, provider, "ABCD", native=True)
    group = await _play_group(scenario, [a, b, c])
    with _out_of_reach(c):
        _cli(c).end()
        await scenario.advance(400)
    native = scenario.protocol("RINCON_D")
    native.supported_features.add(PlayerFeature.SET_MEMBERS)
    await scenario.report(native, can_group_with={"RINCON_C"})
    await scenario.mass.players.cmd_group(_player_of(c).player_id, _player_of(d).player_id)
    await scenario.advance(3)
    assert _player_of(c).synced_to == _player_of(d).player_id

    await _announce_again(scenario, provider, c)

    assert a.session is not None
    assert c not in a.session.sync_clients
    assert _player_of(c).synced_to == _player_of(d).player_id
    await _stop(scenario, group)


async def test_a_player_that_shows_up_after_its_group_stopped_stays_idle(
    scenario: Scenario, provider: AirPlayProvider
) -> None:
    """A lost player that announces itself once its group has ended does nothing."""
    scenario.expect_log("cliairplay did not connect")
    scenario.expect_log("did not produce a running stream")
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c])
    with _out_of_reach(c):
        _cli(c).end()
        await scenario.advance(400)
    await _stop(scenario, group)
    await scenario.play(_player_of(a).player_id)
    await scenario.idle()

    await _announce_again(scenario, provider, c)

    assert c.playback_state == PlaybackState.IDLE
    assert a.group_members == []
    await scenario.command(scenario.mass.players.cmd_stop(_player_of(a).player_id))
    await scenario.idle()


@pytest.mark.parametrize("command", [_remove_command, _ungroup_command, _switch_off_command])
async def test_a_leader_the_user_takes_out_is_handed_over_without_a_gap(
    scenario: Scenario,
    provider: AirPlayProvider,
    command: Callable[[Scenario, Player, Player], Coroutine[Any, Any, None]],
) -> None:
    """
    The members play on when the user takes the leader out of the group, which stays out.

    :param command: What the user does.
    """
    a, b, c = await _receivers(scenario, provider)
    leader = _player_of(a)
    await scenario.configure_player(leader.player_id, **{CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE})
    group = await _play_group(scenario, [a, b, c])
    queue = scenario.queue(group.player_id)
    assert queue is not None
    playing_item = queue.current_item
    session = b.session
    assert session is not None
    remaining = [_cli(b), _cli(c)]

    task = await scenario.command(command(scenario, group, leader))
    await scenario.advance(AFTER_REJOIN + AFTER_IDLE_GRACE)

    await task
    assert _led_by(group) is _player_of(b)
    assert _state(group) == PlaybackState.PLAYING
    assert _members(scenario, group) == ["Player B", "Player C"]
    assert set(session.sync_clients) == {b, c}
    assert a.playback_state == PlaybackState.IDLE
    queue = scenario.queue(group.player_id)
    assert queue is not None
    assert queue.state == PlaybackState.PLAYING
    assert queue.current_item is playing_item
    # nothing was restarted
    assert [_cli(b), _cli(c)] == remaining
    assert not any(process.closed for process in remaining)
    await _stop(scenario, group)


@pytest.mark.parametrize(
    ("taken_out", "leader"), [("BC", "Player A"), ("AB", "Player C"), ("ABC", "Player D")]
)
async def test_members_the_user_takes_out_at_once_stop_playing(
    scenario: Scenario, provider: AirPlayProvider, taken_out: str, leader: str
) -> None:
    """
    Members the user takes out of the group in one command stop, while the others play on.

    :param taken_out: The letters of the players the user takes out.
    :param leader: The player that leads the group after it.
    """
    protocols = dict(zip("ABCD", await _receivers(scenario, provider, "ABCD"), strict=True))
    group = await _play_group(scenario, list(protocols.values()))
    session = protocols["D"].session
    assert session is not None
    staying = [protocol for letter, protocol in protocols.items() if letter not in taken_out]
    remaining = [_cli(protocol) for protocol in staying]

    await scenario.mass.players.cmd_set_members(
        group.player_id,
        player_ids_to_remove=[_player_of(protocols[letter]).player_id for letter in taken_out],
    )
    await scenario.advance(2)

    led_by = _led_by(group)
    assert led_by is not None
    assert led_by.name == leader
    assert _state(group) == PlaybackState.PLAYING
    assert _members(scenario, group) == [_player_of(protocol).name for protocol in staying]
    assert set(session.sync_clients) == set(staying)
    for letter in taken_out:
        assert protocols[letter].playback_state == PlaybackState.IDLE
    # nothing was restarted
    assert [_cli(protocol) for protocol in staying] == remaining
    assert not any(process.closed for process in remaining)
    await _stop(scenario, group)


async def _stop_command(scenario: Scenario, group: Player, _leader: Player) -> None:
    """Stop the group."""
    await scenario.mass.players.cmd_stop(group.player_id)


async def _play_command(scenario: Scenario, group: Player, _leader: Player) -> None:
    """Play a track on the group."""
    await scenario.mass.player_queues.play_media(group.player_id, TEST_TRACK_URI)


@pytest.mark.parametrize(
    ("command", "state", "leader", "members"),
    [
        (_stop_command, PlaybackState.IDLE, None, ["Player A", "Player B", "Player C"]),
        (
            _play_command,
            PlaybackState.PLAYING,
            "Player A",
            ["Player A", "Player B", "Player C"],
        ),
        (_remove_command, PlaybackState.PLAYING, "Player B", ["Player B", "Player C"]),
    ],
)
async def test_a_command_that_came_first_stands(
    scenario: Scenario,
    provider: AirPlayProvider,
    command: Callable[[Scenario, Player, Player], Awaitable[None]],
    state: PlaybackState,
    leader: str | None,
    members: list[str],
) -> None:
    """
    The loss of the leader waits for a command on the group, and leaves what that made of it.

    :param command: The command.
    :param state: The state the command leaves the group in.
    :param leader: The player that leads the group after it.
    :param members: The members of the group after it.
    """
    a, b, c = await _receivers(scenario, provider)
    group = await _play_group(scenario, [a, b, c])
    lost = asyncio.Event()

    async def _run() -> None:
        async with scenario.mass.players.get_player_lock(group.player_id):
            await lost.wait()
            await command(scenario, group, _player_of(a))

    task = await scenario.command(_run())
    _cli(a).end()
    await scenario.settle()
    assert _led_by(group) is _player_of(a)

    lost.set()
    await scenario.advance(2)

    await task
    led_by = _led_by(group)
    assert (led_by.name if led_by else None) == leader
    assert _members(scenario, group) == members
    assert _state(group) == state
    await _stop(scenario, group)
