"""Unit tests for AirPlay stream session late-join logic."""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import AudioError, PlayerCommandFailed

from music_assistant.helpers.timeline_clock import TimelineClock
from music_assistant.models.media_capabilities import AudioSourceRefusedError
from music_assistant.providers.airplay.constants import (
    AIRPLAY_ANCHOR_SEAT_LEAD_MS,
    AIRPLAY_CLOCK_READY_LEAD_MS,
    AIRPLAY_CLOCK_READY_TIMEOUT_MS,
    AIRPLAY_COLD_GROUP_START_LEAD_MS,
    AIRPLAY_GROUP_START_LEAD_MS,
    AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS,
    AIRPLAY_LATE_JOIN_RING_MARGIN_SECONDS,
    AIRPLAY_LATE_JOIN_RING_MAX_BYTES,
    AIRPLAY_LATE_JOIN_RING_MIN_SECONDS,
    AIRPLAY_PCM_FORMAT,
    AIRPLAY_SILENT_MEMBER_RESET_WINDOW,
    AIRPLAY_SPLICE_LEAD_MARGIN_MS,
    AIRPLAY_STALL_RESYNC_HOLDOFF,
    AIRPLAY_STALL_SETTLE_SECONDS,
    AIRPLAY_START_LEAD_MS,
    ClockReadiness,
    StreamingProtocol,
)
from music_assistant.providers.airplay.stream_session import AirPlayStreamSession

PCM_SAMPLE_SIZE = 176400  # 44.1kHz / 16-bit / 2ch


async def _ack_commanded_instant(start_unix_ms: int = 0, *_args: Any, **_kwargs: Any) -> int:
    """Ack a START at exactly the instant it was commanded, as a feasible one is."""
    return start_unix_ms


def _stream_defaults(stream: MagicMock) -> MagicMock:
    """
    Apply the verified-start API defaults to a mocked stream.

    Every START is acked at the commanded instant, with no warm-lead constraint
    and no receiver clock projection, so the tests assert the commanded values
    directly.
    """
    stream.start = AsyncMock(side_effect=_ack_commanded_instant)
    # on the real stream this follows `running` until the audio EOF is written
    stream.accepts_audio = bool(stream.running)
    stream.wait_clock_ready = AsyncMock(return_value=(ClockReadiness.UNREPORTED, 0))
    stream.warm_lead_ms = 0
    stream.flushed_head_unix_ms = 0
    stream.buffered_route = False
    return stream


def _make_session(
    start_time: float,
    seconds_streamed: float,
) -> AirPlayStreamSession:
    """
    Create a stream session for testing.

    :param start_time: The wall-clock time when the stream was started.
    :param seconds_streamed: How many seconds of audio have been streamed.
    """
    prov = MagicMock()
    # a fresh clock reads as wall-clock time on the cliairplay timeline
    prov.mass.timeline_clock = TimelineClock()

    pcm_format = MagicMock()
    pcm_format.pcm_sample_size = PCM_SAMPLE_SIZE
    pcm_format.sample_rate = 44100
    pcm_format.bit_depth = 16
    pcm_format.channels = 2

    leader = MagicMock()
    leader.protocol_id = "leader"
    leader.protocol = StreamingProtocol.RAOP
    # a joinable session has a reference member that is actually playing
    leader.playback_state = PlaybackState.PLAYING
    leader.stream = _stream_defaults(MagicMock())
    leader.stream.running = True
    leader.stream.connected = True
    leader.stream.wait_audio_present = AsyncMock(return_value=True)
    leader.stream.cumulative_shift_seconds = 0.0
    leader.sync_offset_ms = 0

    # media without a queue session: nothing can be mid-handover to this session,
    # so a source that ends is by default the end of the stream
    media = MagicMock(elapsed_time=0, source_id=None, queue_session_id=None)
    session = AirPlayStreamSession(prov, [leader], pcm_format, media)
    session.start_time = start_time
    session.seconds_streamed = seconds_streamed
    session.start_unix_ms = 1  # dummy

    return session


def _make_late_joiner() -> MagicMock:
    """Create a mock AirPlay player for late-join testing."""
    player = MagicMock()
    player.protocol_id = "late_joiner"
    player.protocol = StreamingProtocol.RAOP
    player.stream = None
    player.sync_offset_ms = 0
    return player


def _setup_stream(player: MagicMock) -> Any:
    """Return a side_effect callable that sets up the stream mock on the player."""

    def _side_effect(*_args: Any, **_kwargs: Any) -> None:
        player.stream = _stream_defaults(MagicMock())
        player.stream.running = True
        player.stream.connected = True
        player.stream.wait_for_connection = AsyncMock()
        player.stream.wait_audio_present = AsyncMock(return_value=True)
        player.stream.flush = AsyncMock(return_value=True)
        player.stream.cumulative_shift_seconds = 0.0

    return _side_effect


def _captured_start_at(player: MagicMock) -> float:
    """Return the start time passed to the player's START command."""
    start_unix_ms = player.stream.start.call_args[0][0]
    assert isinstance(start_unix_ms, int)
    return start_unix_ms / 1000


@pytest.mark.asyncio
async def test_initial_client_failure_stops_started_clients() -> None:
    """A partial group startup failure cancels siblings before session teardown."""
    session = _make_session(time.time(), 0)
    first_player: Any = session.sync_clients[0]
    second_player = MagicMock()
    session.sync_clients.append(second_player)
    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()

    async def audio_source() -> AsyncGenerator[bytes]:
        yield b""

    async def start_client(player: Any, *_args: Any) -> None:
        if player is first_player:
            first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                first_cancelled.set()
                raise
        await first_started.wait()
        raise OSError("process failed")

    with (
        patch.object(
            session,
            "_start_client",
            new_callable=AsyncMock,
            side_effect=start_client,
        ),
        patch.object(session, "stop", new_callable=AsyncMock) as stop_session,
        pytest.raises(PlayerCommandFailed, match="Playback failed to start"),
    ):
        await session.start(audio_source())

    assert first_cancelled.is_set()
    stop_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_protocol", "second_protocol"),
    [
        (StreamingProtocol.RAOP, StreamingProtocol.RAOP),
        (StreamingProtocol.AIRPLAY2, StreamingProtocol.AIRPLAY2),
        (StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2),
    ],
)
async def test_initial_group_waits_for_every_member_before_shared_start(
    first_protocol: StreamingProtocol,
    second_protocol: StreamingProtocol,
) -> None:
    """Every member connects before one shared START anchors the group."""
    session = _make_session(0, 0)
    first_player: Any = session.sync_clients[0]
    first_player.protocol_id = "first"
    first_player.protocol = first_protocol
    first_player.sync_offset_ms = 0
    second_player = MagicMock(protocol_id="second")
    second_player.protocol = second_protocol
    second_player.sync_offset_ms = 0
    session.sync_clients.append(second_player)
    session.media.elapsed_time = 12
    operations: list[str] = []

    async def start_client(player: MagicMock, _use_shared_ptp: bool) -> None:
        stream = _stream_defaults(MagicMock(running=True))

        async def wait_for_connection() -> None:
            operations.append(f"connected:{player.protocol_id}")

        async def start(start_unix_ms: int, position_ms: int) -> int:
            assert position_ms == 12_000
            operations.append(f"started:{player.protocol_id}:{start_unix_ms}")
            return start_unix_ms

        stream.wait_for_connection = AsyncMock(side_effect=wait_for_connection)
        stream.wait_audio_present = AsyncMock(return_value=True)
        stream.start = AsyncMock(side_effect=start)
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "_resolve_shared_ptp", new_callable=AsyncMock, return_value=False),
        patch(
            "music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=100.0
        ),
    ):
        await session.start(MagicMock())

    last_connected = max(i for i, op in enumerate(operations) if op.startswith("connected:"))
    first_start = min(i for i, op in enumerate(operations) if op.startswith("started:"))
    assert last_connected < first_start
    # start = now (100_000 ms) + the cold group start lead, one shared instant
    starts = {int(op.rsplit(":", 1)[1]) for op in operations if op.startswith("started:")}
    assert starts == {100_000 + AIRPLAY_COLD_GROUP_START_LEAD_MS}


@pytest.mark.asyncio
async def test_group_start_never_anchors_before_every_receiver_clock_is_usable() -> None:
    """A member whose clock lands past the group lead pushes the shared anchor out."""
    now = 100.0
    session = _make_session(0, 0)
    first: Any = session.sync_clients[0]
    first.protocol = StreamingProtocol.AIRPLAY2
    first.sync_offset_ms = 0
    second = MagicMock(protocol_id="second", protocol=StreamingProtocol.AIRPLAY2)
    second.sync_offset_ms = 0
    session.sync_clients.append(second)
    # The slower member's clock lands past the cold group lead, so anchoring on
    # that lead alone would leave it seated at a different instant to the first.
    ready_at = int(now * 1000) + AIRPLAY_COLD_GROUP_START_LEAD_MS + 400

    async def start_client(player: MagicMock, _use_shared_ptp: bool) -> None:
        stream = _stream_defaults(MagicMock(running=True))
        stream.wait_for_connection = AsyncMock()
        stream.wait_audio_present = AsyncMock(return_value=True)
        stream.wait_clock_ready = AsyncMock(
            return_value=(
                ClockReadiness.PROJECTED,
                ready_at if player is second else int(now * 1000),
            )
        )
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "_resolve_shared_ptp", new_callable=AsyncMock, return_value=False),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        await session.start(MagicMock())

    expected = ready_at + AIRPLAY_CLOCK_READY_LEAD_MS
    for player in (first, second):
        assert player.stream.start.await_args.args[0] == expected


@pytest.mark.asyncio
async def test_solo_start_waits_for_the_receiver_clock_projection() -> None:
    """A lone receiver on a cold clock renders silence, so its anchor waits for it too."""
    now = 100.0
    ready_at_unix_ms = int(now * 1000) + 5_000
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.protocol = StreamingProtocol.AIRPLAY2
    player.sync_offset_ms = 0

    async def start_client(_player: MagicMock, _use_shared_ptp: bool) -> None:
        stream = _stream_defaults(MagicMock(running=True))
        stream.wait_for_connection = AsyncMock()
        stream.wait_audio_present = AsyncMock(return_value=True)
        stream.wait_clock_ready = AsyncMock(
            return_value=(ClockReadiness.PROJECTED, ready_at_unix_ms)
        )
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "_resolve_shared_ptp", new_callable=AsyncMock, return_value=False),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        await session.start(MagicMock())

    player.stream.wait_clock_ready.assert_awaited_once()
    # the projection is further out than the solo lead, so it carries the anchor
    assert player.stream.start.await_args.args[0] == ready_at_unix_ms + AIRPLAY_CLOCK_READY_LEAD_MS


@pytest.mark.asyncio
async def test_solo_start_with_a_usable_clock_keeps_the_short_lead() -> None:
    """A warm clock reports ready with a past instant, so waiting for it costs nothing."""
    now = 100.0
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.protocol = StreamingProtocol.AIRPLAY2
    player.sync_offset_ms = 0

    async def start_client(_player: MagicMock, _use_shared_ptp: bool) -> None:
        stream = _stream_defaults(MagicMock(running=True))
        stream.wait_for_connection = AsyncMock()
        stream.wait_audio_present = AsyncMock(return_value=True)
        # already usable: the projection sits a second in the past
        stream.wait_clock_ready = AsyncMock(
            return_value=(ClockReadiness.PROJECTED, int(now * 1000) - 1_000)
        )
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "_resolve_shared_ptp", new_callable=AsyncMock, return_value=False),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        await session.start(MagicMock())

    assert player.stream.start.await_args.args[0] == int(now * 1000) + AIRPLAY_START_LEAD_MS


async def _start_group(
    accept_delays: dict[str, float],
    *,
    buffered: bool = True,
    ready_at: list[int] | None = None,
    now: float = 100.0,
) -> tuple[AirPlayStreamSession, dict[str, list[int]], dict[str, int]]:
    """
    Cold-start a group against members behaving like the binary.

    Audio leaves for a receiver from one send window before its anchor, and a
    re-START puts the next unsent sample on the new anchor, so whatever had
    already left is never heard. A member's accept delay is the time its first
    anchor takes to be accepted, moving the instant if that outlasts it.

    :param ready_at: Clock readiness every member projects, one value per wait.
    :return: The session, the instants each member was commanded and the
        milliseconds of audio each one lost to a re-START.
    """
    send_window_ms = 1750
    clock = [now]
    commanded: dict[str, list[int]] = {name: [] for name in accept_delays}
    lost_ms: dict[str, int] = dict.fromkeys(accept_delays, 0)
    session = _make_session(0, 0)
    players: list[Any] = [
        MagicMock(protocol_id=name, protocol=StreamingProtocol.AIRPLAY2) for name in accept_delays
    ]
    for player in players:
        player.sync_offset_ms = 0
    session.sync_clients = players

    def member_stream(name: str) -> MagicMock:
        async def start(start_unix_ms: int, _position_ms: int) -> int:
            first = not commanded[name]
            commanded[name].append(start_unix_ms)
            now_ms = int(clock[0] * 1000)
            if not first:
                sent_ms = now_ms + send_window_ms - commanded[name][-2]
                lost_ms[name] += max(0, sent_ms)
                return start_unix_ms
            clock[0] += accept_delays[name]
            return max(start_unix_ms, int(clock[0] * 1000) + 500)

        stream = _stream_defaults(MagicMock(running=True))
        stream.buffered_route = buffered
        stream.wait_for_connection = AsyncMock()
        stream.wait_audio_present = AsyncMock(return_value=True)
        stream.start = AsyncMock(side_effect=start)
        if ready_at:
            stream.wait_clock_ready = AsyncMock(
                side_effect=[(ClockReadiness.PROJECTED, at) for at in ready_at]
            )
        return stream

    async def start_client(player: MagicMock, _use_shared_ptp: bool) -> None:
        player.stream = member_stream(player.protocol_id)

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "_resolve_shared_ptp", new_callable=AsyncMock, return_value=False),
        patch(
            "music_assistant.providers.airplay.stream_session.cli_clock_time",
            side_effect=lambda _clock: clock[0],
        ),
    ):
        await session.start(MagicMock())
    return session, commanded, lost_ms


@pytest.mark.asyncio
async def test_late_acceptor_costs_the_buffered_group_no_audio() -> None:
    """A member that accepts its anchor late makes no one lose audio to a re-START."""
    session, commanded, lost_ms = await _start_group({"quick": 0.0, "slow": 3.0})

    assert lost_ms == {"quick": 0, "slow": 0}
    assert commanded["quick"][-1] == commanded["slow"][-1] == session.start_unix_ms
    # The planned instant passed while the slow member was accepting its seat.
    assert session.start_unix_ms == 103_000 + AIRPLAY_GROUP_START_LEAD_MS


@pytest.mark.asyncio
async def test_prompt_buffered_group_keeps_the_planned_instant() -> None:
    """When every member accepts at once, the seat costs the group no start time."""
    session, commanded, _ = await _start_group({"first": 0.0, "second": 0.0})

    seat = 100_000 + AIRPLAY_ANCHOR_SEAT_LEAD_MS
    instant = 100_000 + AIRPLAY_COLD_GROUP_START_LEAD_MS
    assert commanded == {"first": [seat, instant], "second": [seat, instant]}
    assert session.start_unix_ms == instant


@pytest.mark.asyncio
async def test_seated_group_start_waits_for_fresh_clock_readiness() -> None:
    """The real instant after the seat is never earlier than the members' clocks allow."""
    session, commanded, _ = await _start_group(
        {"first": 0.0, "second": 0.0}, ready_at=[100_500, 104_000]
    )

    instant = 104_000 + AIRPLAY_CLOCK_READY_LEAD_MS
    assert commanded["first"][-1] == commanded["second"][-1] == instant
    assert session.start_unix_ms == instant


@pytest.mark.asyncio
async def test_unbuffered_group_start_commands_a_single_instant() -> None:
    """Without a buffered route audio may leave at once, so no anchor can be rehearsed."""
    _, commanded, _ = await _start_group({"first": 0.0, "second": 0.0}, buffered=False)

    assert commanded == {
        "first": [100_000 + AIRPLAY_COLD_GROUP_START_LEAD_MS],
        "second": [100_000 + AIRPLAY_COLD_GROUP_START_LEAD_MS],
    }


@pytest.mark.asyncio
async def test_group_start_that_never_converges_anchors_where_members_landed() -> None:
    """A group that keeps correcting is anchored at the members' own last instant."""
    session = _make_session(0, 0)
    first: Any = session.sync_clients[0]
    first.sync_offset_ms = 0
    second = MagicMock(protocol_id="second")
    second.protocol = StreamingProtocol.RAOP
    second.sync_offset_ms = 0
    session.sync_clients.append(second)
    commanded: list[int] = []

    async def correcting_start(start_unix_ms: int, _position_ms: int) -> int:
        # Correct every round, so the loop exhausts without ever converging.
        commanded.append(start_unix_ms)
        return start_unix_ms + 100

    async def honoring_start(start_unix_ms: int, _position_ms: int) -> int:
        return start_unix_ms

    first.stream = _stream_defaults(MagicMock(running=True))
    first.stream.start = AsyncMock(side_effect=correcting_start)
    second.stream = _stream_defaults(MagicMock(running=True))
    second.stream.start = AsyncMock(side_effect=honoring_start)

    await session._start_members(0, 100_000)

    # The retry after the final round is never commanded, so it must not become
    # the session anchor: that would map every later joiner behind the group.
    assert session.start_unix_ms == commanded[-1] + 100
    assert session.start_unix_ms not in commanded


@pytest.mark.asyncio
async def test_corrected_solo_start_adopts_the_instant_without_reanchoring() -> None:
    """A corrected solo start is anchored at the binary's instant with no re-START."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.sync_offset_ms = 0
    stream = _stream_defaults(MagicMock(running=True))
    # The binary corrects the commanded instant forward; a re-START would only
    # re-base reported position on the raw one, so exactly one START may be
    # commanded and its corrected ack becomes the anchor.
    stream.start = AsyncMock(return_value=101_500)
    player.stream = stream

    await session._start_members(0, 100_000)

    stream.start.assert_awaited_once_with(100_000, 0)
    assert session.start_unix_ms == 101_500


@pytest.mark.asyncio
async def test_group_start_fails_when_a_member_never_acknowledges() -> None:
    """An unacknowledged member start fails the session instead of recording its instant."""
    session = _make_session(0, 0)
    first_player: Any = session.sync_clients[0]
    first_player.protocol = StreamingProtocol.RAOP
    second_player = MagicMock(protocol_id="silent", protocol=StreamingProtocol.RAOP)
    second_player.sync_offset_ms = 0
    session.sync_clients.append(second_player)
    anchor_before = (session.start_unix_ms, session.start_time)

    async def start_client(player: MagicMock, _use_shared_ptp: bool) -> None:
        stream = _stream_defaults(MagicMock(running=True))
        stream.wait_for_connection = AsyncMock()
        stream.wait_audio_present = AsyncMock(return_value=True)
        if player is second_player:
            stream.start = AsyncMock(
                side_effect=PlayerCommandFailed(
                    "AirPlay player Player B did not acknowledge its start within 2.0s"
                )
            )
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "stop", new_callable=AsyncMock) as stop_session,
        pytest.raises(PlayerCommandFailed, match="did not acknowledge its start"),
    ):
        await session.start(MagicMock())

    # Nothing may be recorded from a round that had no verified answer: an
    # anchor the group never played is what every later joiner aligns against.
    assert (session.start_unix_ms, session.start_time) == anchor_before
    stop_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol",
    [StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2],
)
async def test_initial_single_player_starts_after_connect(
    protocol: StreamingProtocol,
) -> None:
    """A standalone player is anchored with a single START once connected."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.protocol_id = "solo"
    player.protocol = protocol
    player.sync_offset_ms = 0
    stream = _stream_defaults(MagicMock(running=True))
    stream.wait_for_connection = AsyncMock()
    stream.wait_audio_present = AsyncMock(return_value=True)
    stream.flush = AsyncMock(return_value=True)

    async def start_client(_player: MagicMock, _use_shared_ptp: bool) -> None:
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_resolve_shared_ptp", new_callable=AsyncMock, return_value=False),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch(
            "music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=200.0
        ),
    ):
        await session.start(MagicMock())

    # start = now (200_000 ms) + the solo start lead, position 0
    stream.start.assert_awaited_once_with(200_000 + AIRPLAY_START_LEAD_MS, 0)


@pytest.mark.asyncio
async def test_initial_connection_failure_never_starts_partial_group() -> None:
    """If any member fails to connect, no member receives START and the group is stopped."""
    session = _make_session(0, 0)
    first_player: Any = session.sync_clients[0]
    first_player.protocol = StreamingProtocol.RAOP
    second_player = MagicMock(protocol_id="second")
    second_player.protocol = StreamingProtocol.RAOP
    session.sync_clients.append(second_player)

    async def start_client(player: MagicMock, _use_shared_ptp: bool) -> None:
        stream = _stream_defaults(MagicMock(running=True))
        if player is second_player:
            stream.wait_for_connection = AsyncMock(side_effect=TimeoutError("connect timeout"))
        else:
            stream.wait_for_connection = AsyncMock()
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "stop", new_callable=AsyncMock) as stop_session,
        pytest.raises(PlayerCommandFailed, match="Playback failed to start"),
    ):
        await session.start(MagicMock())

    for player in session.sync_clients:
        stream: Any = player.stream
        stream.start.assert_not_awaited()
    stop_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_connection_failure_names_the_member_that_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log has to say WHICH speaker failed, and what its binary reported."""
    session = _make_session(0, 0)
    prov_logger = logging.getLogger("test.airplay.session")
    session.prov.logger = prov_logger
    first_player: Any = session.sync_clients[0]
    first_player.display_name = "Player A"
    first_player.protocol = StreamingProtocol.RAOP
    second_player = MagicMock(protocol_id="second", display_name="Player B")
    second_player.protocol = StreamingProtocol.RAOP
    session.sync_clients.append(second_player)

    async def start_client(player: MagicMock, _use_shared_ptp: bool) -> None:
        stream = _stream_defaults(MagicMock(running=True))
        if player is second_player:
            stream.wait_for_connection = AsyncMock(
                side_effect=TimeoutError("cliairplay did not connect to Player B: no route")
            )
        else:
            stream.wait_for_connection = AsyncMock()
        player.stream = stream

    with (
        caplog.at_level(logging.WARNING),
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "stop", new_callable=AsyncMock),
        pytest.raises(PlayerCommandFailed),
    ):
        await session.start(MagicMock())

    assert "Player B failed to connect to its device" in caplog.text
    assert "no route" in caplog.text
    assert "Player A failed" not in caplog.text


@pytest.mark.asyncio
async def test_unconfirmed_audio_feed_names_the_silent_members() -> None:
    """A member whose binary never reported audio flowing is named in the failure."""
    session = _make_session(0, 0)
    first_player: Any = session.sync_clients[0]
    first_player.display_name = "Player A"
    first_player.protocol = StreamingProtocol.RAOP
    first_player.stream.wait_audio_present = AsyncMock(return_value=True)
    second_player = MagicMock(protocol_id="second", display_name="Player B")
    second_player.protocol = StreamingProtocol.RAOP
    second_player.stream = _stream_defaults(MagicMock(running=True))
    second_player.stream.wait_audio_present = AsyncMock(return_value=False)
    session.sync_clients.append(second_player)

    with pytest.raises(PlayerCommandFailed, match="not confirmed by Player B"):
        await session._wait_members_audio_present()


@pytest.mark.asyncio
async def test_initial_connection_cancellation_never_starts_group() -> None:
    """Cancellation while connecting cleans up without anchoring playback."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.protocol_id = "solo"
    player.protocol = StreamingProtocol.RAOP
    connection_waiting = asyncio.Event()
    stream = _stream_defaults(MagicMock(running=True))

    async def wait_for_connection() -> None:
        connection_waiting.set()
        await asyncio.Event().wait()

    stream.wait_for_connection = AsyncMock(side_effect=wait_for_connection)

    async def start_client(_player: MagicMock, _use_shared_ptp: bool) -> None:
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "stop", new_callable=AsyncMock) as stop_session,
    ):
        start_task = asyncio.create_task(session.start(MagicMock()))
        await connection_waiting.wait()
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task

    stream.start.assert_not_awaited()
    stop_session.assert_awaited_once()


@pytest.mark.parametrize(
    "protocols",
    [
        (StreamingProtocol.RAOP,),
        (StreamingProtocol.AIRPLAY2,),
        (StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2),
    ],
)
def test_warm_replace_supports_every_streaming_protocol(
    protocols: tuple[StreamingProtocol, ...],
) -> None:
    """Connected legacy RAOP, AirPlay 2 and mixed sessions can replace warm."""
    session = _make_session(0, 0)
    players: Any = []
    for index, protocol in enumerate(protocols):
        player = MagicMock()
        player.protocol_id = f"player-{index}"
        player.protocol = protocol
        player.stream = _stream_defaults(MagicMock(running=True, connected=True))
        players.append(player)
    session.sync_clients = players

    assert session.can_replace(players, session.pcm_format)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocols",
    [
        (StreamingProtocol.AIRPLAY2,),
        (StreamingProtocol.RAOP,),
        (StreamingProtocol.AIRPLAY2, StreamingProtocol.AIRPLAY2),
        (StreamingProtocol.RAOP, StreamingProtocol.RAOP),
        (StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2),
    ],
)
async def test_standby_supports_every_connected_streaming_protocol(
    protocols: tuple[StreamingProtocol, ...],
) -> None:
    """Connected legacy RAOP, AirPlay 2 and mixed sessions can enter standby."""
    session = _make_session(0, 0)
    players: Any = []
    for index, protocol in enumerate(protocols):
        player = MagicMock()
        player.protocol_id = f"player-{index}"
        player.protocol = protocol
        player.stream = _stream_defaults(MagicMock(running=True, connected=True))
        player.stream.send_cli_command = AsyncMock(return_value=True)
        players.append(player)
    session.sync_clients = players

    assert await session.standby()
    # the park is carried by the session, so it survives the group shrinking
    assert session.parked is True
    for player in players:
        player.stream.send_cli_command.assert_awaited_once_with("ACTION=STANDBY")
        player.set_state_from_stream.assert_called_once_with(
            state=PlaybackState.PAUSED, stream=player.stream
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocols",
    [
        (StreamingProtocol.RAOP,),
        (StreamingProtocol.AIRPLAY2,),
        (StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2),
    ],
)
async def test_standby_resumes_warm_on_existing_streams(
    protocols: tuple[StreamingProtocol, ...],
) -> None:
    """RAOP, AirPlay 2 and mixed sessions resume warm via flush-refill on parked streams."""
    session = _make_session(0, 0)
    players: Any = []
    original_streams: dict[str, MagicMock] = {}
    for index, protocol in enumerate(protocols):
        player = MagicMock()
        player.protocol_id = f"player-{index}"
        player.protocol = protocol
        player.sync_offset_ms = 0
        player.stream = _stream_defaults(MagicMock(running=True, connected=True))
        player.stream.send_cli_command = AsyncMock(return_value=True)
        player.stream.wait_audio_present = AsyncMock(return_value=True)
        player.stream.flush = AsyncMock(return_value=True)
        players.append(player)
        original_streams[player.protocol_id] = player.stream
    session.sync_clients = players

    assert await session.standby()
    assert session.can_replace(players, session.pcm_format)
    media = MagicMock(elapsed_time=10)
    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch(
            "music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=100.0
        ),
    ):
        assert await session.replace(MagicMock(), media)

    # the re-anchor ends the park, so the members are playing again
    assert session.parked is False
    # start = now (100_000 ms) + solo/group start lead, position 10s
    expected_start = 100_000 + (
        AIRPLAY_START_LEAD_MS if len(protocols) == 1 else AIRPLAY_GROUP_START_LEAD_MS
    )
    for player in players:
        stream = original_streams[player.protocol_id]
        assert player.stream is stream
        stream.send_cli_command.assert_awaited_once_with("ACTION=STANDBY")
        stream.flush.assert_awaited_once_with()
        stream.start.assert_awaited_once_with(expected_start, 10_000)


@pytest.mark.asyncio
async def test_warm_replace_flushes_all_before_starting_any() -> None:
    """A group flushes every member and awaits all acks before any shared START."""
    session = _make_session(0, 0)
    players: list[Any] = []
    release_delayed = asyncio.Event()
    delayed_waiting = asyncio.Event()

    for player_id in ("first", "delayed"):
        player = MagicMock(protocol_id=player_id, protocol=StreamingProtocol.AIRPLAY2)
        player.sync_offset_ms = 0
        stream = _stream_defaults(MagicMock(running=True, connected=True))

        async def flush(*, current_id: str = player_id) -> bool:
            if current_id == "delayed":
                delayed_waiting.set()
                await release_delayed.wait()
            return True

        stream.flush = AsyncMock(side_effect=flush)
        stream.wait_audio_present = AsyncMock(return_value=True)
        player.stream = stream
        players.append(player)
    session.sync_clients = players

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch(
            "music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=100.0
        ),
    ):
        replace_task = asyncio.create_task(session.replace(MagicMock(), MagicMock(elapsed_time=0)))
        await delayed_waiting.wait()
        # one member's flush is still pending: no member may have been started yet
        for player in players:
            player.stream.start.assert_not_awaited()
        release_delayed.set()
        assert await replace_task

    for player in players:
        player.stream.flush.assert_awaited_once_with()
        # start = now (100_000 ms) + the group start lead (500 ms), position 0
        player.stream.start.assert_awaited_once_with(100_000 + AIRPLAY_GROUP_START_LEAD_MS, 0)


@pytest.mark.asyncio
async def test_warm_replace_stops_old_audio_and_ffmpeg_before_flush() -> None:
    """The old audio feed and ffmpeg are torn down before any FLUSH is sent."""
    session = _make_session(0, 0)
    operations: list[str] = []
    player: Any = session.sync_clients[0]
    player.sync_offset_ms = 0
    stream = player.stream
    stream.running = True
    stream.connected = True

    async def flush() -> bool:
        operations.append("flush")
        return True

    stream.flush = AsyncMock(side_effect=flush)
    old_ffmpeg = MagicMock(closed=False)

    async def kill_ffmpeg() -> None:
        operations.append("ffmpeg-killed")

    old_ffmpeg.kill = AsyncMock(side_effect=kill_ffmpeg)
    session._player_ffmpeg[player.protocol_id] = old_ffmpeg

    async def old_audio() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            operations.append("audio-cancelled")
            raise

    session._audio_source_task = asyncio.create_task(old_audio())
    # let old_audio reach its await so the cancellation lands inside its try/except
    await asyncio.sleep(0)

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch(
            "music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=100.0
        ),
    ):
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=0))

    assert operations.index("audio-cancelled") < operations.index("flush")
    assert operations.index("ffmpeg-killed") < operations.index("flush")
    old_ffmpeg.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_warm_replace_flush_failure_falls_back_to_cold() -> None:
    """A member that never acknowledges its flush makes the whole replace fall back."""
    session = _make_session(0, 0)
    players: list[Any] = []
    for player_id, acked in (("ok", True), ("failed", False)):
        player = MagicMock(protocol_id=player_id, protocol=StreamingProtocol.RAOP)
        player.sync_offset_ms = 0
        stream = _stream_defaults(MagicMock(running=True, connected=True))
        stream.flush = AsyncMock(return_value=acked)
        player.stream = stream
        players.append(player)
    session.sync_clients = players

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock) as start_ffmpeg,
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
    ):
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=0)) is False

    # no member is started and no fresh ffmpeg is wired once a flush is unacknowledged
    for player in players:
        player.stream.start.assert_not_awaited()
    start_ffmpeg.assert_not_awaited()


@pytest.mark.asyncio
async def test_warm_replace_start_failure_falls_back_to_cold() -> None:
    """A failed shared START after flush returns False so the caller restarts cold."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.sync_offset_ms = 0
    stream = player.stream
    stream.running = True
    stream.connected = True
    stream.flush = AsyncMock(return_value=True)
    stream.start = AsyncMock(side_effect=OSError("start failed"))

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch(
            "music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=100.0
        ),
    ):
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=0)) is False


@pytest.mark.parametrize(
    ("member_specs", "expected_requirement_ms"),
    [
        # the largest member requirement wins, wherever that member sits
        (((4000, 0), (1500, 0)), 4000),
        (((1500, 0), (4000, 0)), 4000),
        # a positive sync_adjust widens that member's own requirement by exactly
        # the amount it moves that member's commanded instant earlier
        (((4000, 0), (3600, 600)), 4200),
    ],
)
def test_warm_anchor_clears_the_receivers_queued_audio(
    member_specs: tuple[tuple[int, int], ...],
    expected_requirement_ms: int,
) -> None:
    """
    A warm re-anchor lands beyond the audio the receivers still have queued.

    A splice-timeline member honors the commanded instant only when it lands
    past that member's own queued audio, so the shared anchor has to clear the
    largest member requirement: anchoring short leaves that member behind the
    group and every warm start pays a corrective round.
    """
    session = _make_session(0, 0)
    members: list[Any] = [session.sync_clients[0]]
    second = MagicMock(protocol_id="second", protocol=StreamingProtocol.AIRPLAY2)
    second.stream = _stream_defaults(MagicMock(running=True, connected=True))
    session.sync_clients.append(second)
    members.append(second)
    for player, (warm_lead_ms, sync_adjust) in zip(members, member_specs, strict=True):
        player.stream.warm_lead_ms = warm_lead_ms
        player.sync_offset_ms = -sync_adjust

    with patch(
        "music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=100.0
    ):
        anchor = session._anchor_start_unix_ms(warm=True)

    assert anchor == 100_000 + expected_requirement_ms + AIRPLAY_SPLICE_LEAD_MARGIN_MS


def test_warm_anchor_clears_the_head_every_flushed_member_froze() -> None:
    """
    A warm re-anchor lands beyond the frozen head the flushed members reported.

    A member renders from its own commanded instant (the anchor minus its
    sync_adjust), so it is that instant, not the bare anchor, that has to clear
    the head the member froze at flush, with margin for the command round-trip.
    """
    session = _make_session(0, 0)
    first: Any = session.sync_clients[0]
    first.stream.flushed_head_unix_ms = 102_000
    first.sync_offset_ms = 0
    second = MagicMock(protocol_id="second", protocol=StreamingProtocol.AIRPLAY2)
    second.stream = _stream_defaults(MagicMock(running=True, connected=True))
    second.stream.flushed_head_unix_ms = 105_000
    second.sync_offset_ms = -300
    session.sync_clients.append(second)

    with patch(
        "music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=100.0
    ):
        anchor = session._anchor_start_unix_ms(warm=True)

    # the later head wins, and the earlier start its member takes is added to it
    assert anchor == 105_000 + 300 + AIRPLAY_SPLICE_LEAD_MARGIN_MS


@pytest.mark.asyncio
async def test_start_player_ffmpeg_wires_persistent_cli_stdin() -> None:
    """The per-seek ffmpeg is wired to the member's persistent cli stdin fd and tracked."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    old_ffmpeg = MagicMock()
    old_ffmpeg.close = AsyncMock()
    session._player_ffmpeg[player.protocol_id] = old_ffmpeg

    stream = _stream_defaults(MagicMock())
    stream.pcm_format = session.pcm_format
    cli_proc = MagicMock()
    cli_proc.proc.stdin.transport.get_extra_info.return_value.fileno.return_value = 77
    stream._cli_proc = cli_proc
    player.stream = stream
    new_ffmpeg = MagicMock()
    new_ffmpeg.start = AsyncMock(return_value=None)

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.get_final_output_format",
            return_value=MagicMock(),
        ),
        patch(
            "music_assistant.providers.airplay.stream_session.get_media_session_id",
            return_value="session-id",
        ),
        patch(
            "music_assistant.providers.airplay.stream_session.FFMpeg", return_value=new_ffmpeg
        ) as ffmpeg_factory,
    ):
        await session._start_player_ffmpeg(player, MagicMock(), stream)

    # the old ffmpeg is closed, never killing the shared cli stdin
    old_ffmpeg.close.assert_awaited_once()
    # the fresh ffmpeg writes into the cli process stdin fd (77)
    assert ffmpeg_factory.call_args.kwargs["audio_output"] == 77
    new_ffmpeg.start.assert_awaited_once()
    assert session._player_ffmpeg[player.protocol_id] is new_ffmpeg


@pytest.mark.asyncio
async def test_start_player_ffmpeg_applies_the_output_plan_the_player_names() -> None:
    """The member's ffmpeg runs the output plan of the protocol its output settings belong to."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.output_plan_protocol_id = "plan_owner"
    stream = _stream_defaults(MagicMock())
    stream.pcm_format = session.pcm_format
    stream._cli_proc = MagicMock()
    get_plan = cast("MagicMock", session.mass.streams.audio.get_player_output_plan)

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.get_final_output_format",
            return_value=MagicMock(),
        ),
        patch(
            "music_assistant.providers.airplay.stream_session.get_media_session_id",
            return_value="session-id",
        ),
        patch(
            "music_assistant.providers.airplay.stream_session.FFMpeg",
            return_value=MagicMock(start=AsyncMock()),
        ) as ffmpeg_factory,
    ):
        await session._start_player_ffmpeg(player, MagicMock(), stream)

    assert get_plan.call_args.args[0] == "plan_owner"
    assert ffmpeg_factory.call_args.kwargs["filter_params"] is get_plan.return_value.filter_params


@pytest.mark.asyncio
async def test_start_player_ffmpeg_uses_the_passed_stream() -> None:
    """The ffmpeg is wired to the stream passed in, never a re-read of player.stream."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]

    passed_stream = _stream_defaults(MagicMock())
    passed_stream.pcm_format = session.pcm_format
    passed_cli = MagicMock()
    passed_cli.proc.stdin.transport.get_extra_info.return_value.fileno.return_value = 77
    passed_stream._cli_proc = passed_cli

    # a displacement has swapped player.stream onto a different cli process
    stale_stream = _stream_defaults(MagicMock())
    stale_stream.pcm_format = session.pcm_format
    stale_cli = MagicMock()
    stale_cli.proc.stdin.transport.get_extra_info.return_value.fileno.return_value = 99
    stale_stream._cli_proc = stale_cli
    player.stream = stale_stream

    new_ffmpeg = MagicMock()
    new_ffmpeg.start = AsyncMock(return_value=None)

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.get_final_output_format",
            return_value=MagicMock(),
        ),
        patch(
            "music_assistant.providers.airplay.stream_session.get_media_session_id",
            return_value="session-id",
        ),
        patch(
            "music_assistant.providers.airplay.stream_session.FFMpeg", return_value=new_ffmpeg
        ) as ffmpeg_factory,
    ):
        await session._start_player_ffmpeg(player, MagicMock(), passed_stream)

    # wired to the passed stream's cli stdin fd (77), never the stale 99
    assert ffmpeg_factory.call_args.kwargs["audio_output"] == 77


@pytest.mark.asyncio
async def test_audio_confirmation_waits_for_the_source_to_feed() -> None:
    """
    A binary is only judged silent once it has actually been handed audio.

    A seek can land seconds ahead of what the source has produced, and giving up
    on the member there would restart the session into the very same wait.
    """
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.stream.wait_audio_present = AsyncMock(return_value=True)
    feeding = asyncio.Event()

    async def _source() -> None:
        await feeding.wait()

    session._audio_source_task = asyncio.create_task(_source())

    waiter = asyncio.create_task(session._wait_members_audio_present())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not waiter.done()
    player.stream.wait_audio_present.assert_not_awaited()

    session._feed_settled.set()
    await waiter

    player.stream.wait_audio_present.assert_awaited_once()
    feeding.set()
    await session._audio_source_task


@pytest.mark.asyncio
async def test_a_source_that_dies_settles_the_feed_question() -> None:
    """A source that fails never leaves a start waiting for audio it will not get."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.stream.write_audio_eof = AsyncMock()
    session.media = MagicMock(source_id=None, queue_session_id=None)
    no_chunks: list[bytes] = []

    async def failing_source() -> AsyncGenerator[bytes]:
        for chunk in no_chunks:
            yield chunk
        raise AudioError("source died")

    await session._audio_streamer(failing_source())

    assert session._feed_settled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "logged"),
    [
        (AudioSourceRefusedError("No AirPlay client is streaming to this receiver"), False),
        (AudioError("source died"), True),
    ],
    ids=["refused", "failed"],
)
async def test_only_a_source_that_fails_is_logged_as_an_error(
    error: AudioError, logged: bool
) -> None:
    """Only a failure is logged here: the streams controller logs a source refusing the player."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.stream.write_audio_eof = AsyncMock()
    no_chunks: list[bytes] = []

    async def failing_source() -> AsyncGenerator[bytes]:
        for chunk in no_chunks:
            yield chunk
        raise error

    await session._audio_streamer(failing_source())

    logger = cast("MagicMock", session.prov.logger)
    assert logger.error.called is logged
    assert logger.warning.called is logged


@pytest.mark.asyncio
async def test_a_source_refusing_the_members_ends_the_start_at_once_and_quietly() -> None:
    """
    A refusal ends the start as soon as the source gives it, and is not logged here.

    The streams controller logs the refusal itself, and no member can report audio the
    source never handed over, so none is waited for. The stop that ends the start is what
    ends the members' ffmpeg, also one that would not exit by itself.
    """
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.stream.wait_for_connection = AsyncMock()
    player.stream.session = session
    player.stream.stop = AsyncMock()
    refusal = AudioSourceRefusedError("No AirPlay client is streaming to this receiver")
    no_chunks: list[bytes] = []

    async def refusing_source() -> AsyncGenerator[bytes]:
        for chunk in no_chunks:
            yield chunk
        raise refusal

    async def never_exits(*_args: Any) -> int:
        await asyncio.Event().wait()
        return 0

    ffmpeg = MagicMock(closed=False, kill=AsyncMock(), write_eof=AsyncMock())
    ffmpeg.wait_with_timeout = AsyncMock(side_effect=never_exits)

    async def start_client(airplay_player: Any, _use_shared_ptp: bool) -> None:
        session._player_ffmpeg[airplay_player.protocol_id] = ffmpeg

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        pytest.raises(AudioSourceRefusedError) as raised,
    ):
        await session.start(refusing_source())

    assert raised.value is refusal
    player.stream.wait_audio_present.assert_not_awaited()
    ffmpeg.write_eof.assert_not_awaited()
    ffmpeg.kill.assert_awaited_once()
    player.stream.stop.assert_awaited_once_with(force=True)
    assert not session.sync_clients
    logger = cast("MagicMock", session.prov.logger)
    logger.warning.assert_not_called()
    logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_a_source_refusing_a_warm_replacement_stops_the_session_quietly() -> None:
    """A refused replacement is not answered with a cold restart, which would be refused too."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.sync_offset_ms = 0
    player.stream.flush = AsyncMock(return_value=True)
    refusal = AudioSourceRefusedError("No AirPlay client is streaming to this receiver")
    no_chunks: list[bytes] = []

    async def refusing_source() -> AsyncGenerator[bytes]:
        for chunk in no_chunks:
            yield chunk
        raise refusal

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "stop", new_callable=AsyncMock) as stop_session,
        pytest.raises(AudioSourceRefusedError) as raised,
    ):
        await session.replace(
            refusing_source(), MagicMock(elapsed_time=0, source_id=None, queue_session_id=None)
        )

    assert raised.value is refusal
    stop_session.assert_awaited_once()
    player.stream.wait_audio_present.assert_not_awaited()
    player.stream.start.assert_not_awaited()
    cast("MagicMock", session.prov.logger).warning.assert_not_called()
    assert session._audio_source_task is not None
    await session._audio_source_task


@pytest.mark.asyncio
async def test_warm_replace_rearms_the_feed_question() -> None:
    """The new source answers for its own feed; what the old one delivered does not count."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.sync_offset_ms = 0
    player.stream.flush = AsyncMock(return_value=True)
    session._feed_settled.set()
    # nor does the old one refusing to play
    session._source_refusal = AudioSourceRefusedError("No AirPlay client is streaming")

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch(
            "music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=100.0
        ),
    ):
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=0))

    assert not session._feed_settled.is_set()


@pytest.mark.asyncio
async def test_audio_confirmation_is_released_by_a_source_that_never_feeds() -> None:
    """A source that ends without handing anything over never holds up the start."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.display_name = "Kantoor"
    player.stream.wait_audio_present = AsyncMock(return_value=False)
    session._audio_source_task = asyncio.create_task(asyncio.sleep(0))
    await session._audio_source_task

    with pytest.raises(PlayerCommandFailed, match="audio feed was not confirmed"):
        await session._wait_members_audio_present()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transitioning", "live_session_id", "ends_stream"),
    [
        # a seek/next: the queue rotates its session while it loads the new stream
        (True, "session-2", False),
        # the queue played out, or the flow broke off for the queue to restart once
        # the player reports idle - which only the audio EOF can bring about
        (False, "session-1", True),
        # not this session's transition (the queue is starting the one we play)
        (True, "session-1", True),
        # a transition that already finished cannot be waiting on this session
        (False, "session-2", True),
    ],
)
async def test_source_end_only_keeps_stdin_open_for_a_pending_replacement(
    transitioning: bool, live_session_id: str, ends_stream: bool
) -> None:
    """
    The binary's stdin is closed unless the queue is mid-handover to a new stream.

    Closing it ends the stream for good - it cannot be reopened - so a seek would
    be left with a cold restart as its only option. Everywhere else the EOF is
    what makes the binary end and the player report idle.
    """
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.stream.write_audio_eof = AsyncMock()
    session.media = MagicMock(source_id="queue-1", queue_session_id="session-1")
    queues: Any = session.mass.player_queues
    queues.queue_data_or_none = MagicMock(
        return_value=MagicMock(session_id=live_session_id, transitioning=transitioning)
    )
    ffmpeg = MagicMock(closed=False)
    ffmpeg.write_eof = AsyncMock()
    ffmpeg.wait_with_timeout = AsyncMock()
    ffmpeg.kill = AsyncMock()
    session._player_ffmpeg[player.protocol_id] = ffmpeg

    no_chunks: list[bytes] = []

    async def exhausted_source() -> AsyncGenerator[bytes]:
        for chunk in no_chunks:
            yield chunk

    with patch.object(
        session, "_end_stream_if_no_replacement_lands", new_callable=AsyncMock
    ) as backstop:
        await session._audio_streamer(exhausted_source())

    assert player.protocol_id not in session._player_ffmpeg
    assert player.stream.write_audio_eof.await_count == (1 if ends_stream else 0)
    # a withheld EOF is never simply dropped: it is held for the replacement
    assert backstop.await_count == (0 if ends_stream else 1)
    if ends_stream:
        # the audio ffmpeg still holds is handed over before the binary is told
        ffmpeg.write_eof.assert_awaited_once()
        ffmpeg.wait_with_timeout.assert_awaited_once()
        ffmpeg.kill.assert_not_awaited()
    else:
        # the replacement flushes that audio away, and nothing may reach the
        # binary between the old ffmpeg dying and that flush
        ffmpeg.kill.assert_awaited_once()
        ffmpeg.write_eof.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_replacement_that_never_arrives_still_ends_the_stream() -> None:
    """
    A predicted replacement that never lands must not leave the session hanging.

    The queue clears its transition on any failure between rotating its stream
    session and the play_media that carries the replacement, and nothing else
    ends this stream: the withheld EOF is what makes the binary play out, report
    eof and the player report idle.
    """
    session = _make_session(0, 0)
    logger = MagicMock()
    session.prov.logger = logger
    player: Any = session.sync_clients[0]
    player.stream.write_audio_eof = AsyncMock()
    session.media = MagicMock(source_id="queue-1", queue_session_id="session-1")
    queues: Any = session.mass.player_queues
    queues.queue_data_or_none = MagicMock(
        return_value=MagicMock(session_id="session-2", transitioning=True)
    )
    ffmpeg = MagicMock(closed=False)
    ffmpeg.write_eof = AsyncMock()
    ffmpeg.kill = AsyncMock()
    session._player_ffmpeg[player.protocol_id] = ffmpeg

    no_chunks: list[bytes] = []

    async def exhausted_source() -> AsyncGenerator[bytes]:
        for chunk in no_chunks:
            yield chunk

    with patch(
        "music_assistant.providers.airplay.stream_session.AIRPLAY_REPLACEMENT_EOF_TIMEOUT", 0
    ):
        await session._audio_streamer(exhausted_source())

    # the ffmpeg is still dropped rather than drained - the audio it held belongs
    # to a flush that is not coming either
    ffmpeg.kill.assert_awaited_once()
    ffmpeg.write_eof.assert_not_awaited()
    player.stream.write_audio_eof.assert_awaited_once()
    logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_a_queue_that_gives_up_releases_the_withheld_eof_early() -> None:
    """
    The queue ending its transition is what says no replacement is coming.

    It clears the flag on any failure between rotating its stream session and
    the play_media that carries the replacement, so the EOF follows that rather
    than waiting out the cap, which only covers a transition that hangs.
    """
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.stream.write_audio_eof = AsyncMock()
    session.media = MagicMock(source_id="queue-1", queue_session_id="session-1")
    queues: Any = session.mass.player_queues
    queue_data = MagicMock(session_id="session-2", transitioning=True)
    queues.queue_data_or_none = MagicMock(return_value=queue_data)

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.AIRPLAY_REPLACEMENT_EOF_TIMEOUT", 30
        ),
        patch(
            "music_assistant.providers.airplay.stream_session.AIRPLAY_REPLACEMENT_POLL_INTERVAL",
            0.01,
        ),
    ):
        backstop = asyncio.create_task(session._end_stream_if_no_replacement_lands())
        await asyncio.sleep(0.02)
        # the load is still running, so the EOF stays withheld
        assert not backstop.done()
        player.stream.write_audio_eof.assert_not_awaited()
        queue_data.transitioning = False
        await asyncio.wait_for(backstop, timeout=5)

    player.stream.write_audio_eof.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_withheld_eof_drops_any_ffmpeg_still_on_the_stdin() -> None:
    """
    Nothing may still own the cli stdin when a withheld EOF is finally sent.

    A member that joins while the EOF is held gets an ffmpeg of its own writing
    into that same stdin, so closing only this end would leave the binary waiting
    on a pipe that never ends.
    """
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    session.media = MagicMock(source_id="queue-1", queue_session_id="session-1")
    queues: Any = session.mass.player_queues
    queues.queue_data_or_none = MagicMock(
        return_value=MagicMock(session_id="session-2", transitioning=True)
    )
    order: list[str] = []
    player.stream.write_audio_eof = AsyncMock(side_effect=lambda: order.append("eof"))
    retired = MagicMock(closed=False)
    retired.kill = AsyncMock(side_effect=lambda: order.append("retired-kill"))
    session._player_ffmpeg[player.protocol_id] = retired

    no_chunks: list[bytes] = []

    async def exhausted_source() -> AsyncGenerator[bytes]:
        for chunk in no_chunks:
            yield chunk

    with patch(
        "music_assistant.providers.airplay.stream_session.AIRPLAY_REPLACEMENT_EOF_TIMEOUT", 0.05
    ):
        streamer = asyncio.create_task(session._audio_streamer(exhausted_source()))
        # the source is out and its ffmpeg retired; the EOF is now being held
        await asyncio.sleep(0.01)
        joined = MagicMock(closed=False)
        joined.kill = AsyncMock(side_effect=lambda: order.append("joiner-kill"))
        session._player_ffmpeg[player.protocol_id] = joined
        await streamer

    assert order == ["retired-kill", "joiner-kill", "eof"]
    assert not session._player_ffmpeg


@pytest.mark.asyncio
async def test_a_replacement_claiming_the_session_cancels_the_withheld_eof() -> None:
    """A session taken over warm never delivers the EOF it held for that takeover."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.stream.write_audio_eof = AsyncMock()
    player.stream.flush = AsyncMock(return_value=True)
    player.sync_offset_ms = 0
    session.media = MagicMock(source_id="queue-1", queue_session_id="session-1")
    queues: Any = session.mass.player_queues
    queues.queue_data_or_none = MagicMock(
        return_value=MagicMock(session_id="session-2", transitioning=True)
    )
    ffmpeg = MagicMock(closed=False)
    ffmpeg.kill = AsyncMock()
    session._player_ffmpeg[player.protocol_id] = ffmpeg

    no_chunks: list[bytes] = []

    async def exhausted_source() -> AsyncGenerator[bytes]:
        for chunk in no_chunks:
            yield chunk

    with patch(
        "music_assistant.providers.airplay.stream_session.AIRPLAY_REPLACEMENT_EOF_TIMEOUT", 0.05
    ):
        streamer = asyncio.create_task(session._audio_streamer(exhausted_source()))
        session._audio_source_task = streamer
        # the source runs out over mocked awaits alone, so the streamer is holding
        # the withheld EOF well before the wait itself could expire
        await asyncio.sleep(0.01)
        assert not streamer.done()

        with (
            patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
            patch.object(session, "_audio_streamer", new_callable=AsyncMock),
            patch(
                "music_assistant.providers.airplay.stream_session.cli_clock_time",
                return_value=100.0,
            ),
        ):
            assert await session.replace(MagicMock(), MagicMock(elapsed_time=0))

        assert streamer.cancelled()
        # well past the point where a wait left running would have delivered it
        await asyncio.sleep(0.1)

    player.stream.write_audio_eof.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_state", ["missing", "stopped", "disconnected", "audio_ended"])
async def test_standby_requires_every_member_running_and_connected(stream_state: str) -> None:
    """Standby is unavailable when any member lacks a reusable connected session."""
    session = _make_session(0, 0)
    ready_player = MagicMock(protocol_id="ready", protocol=StreamingProtocol.AIRPLAY2)
    ready_player.stream = _stream_defaults(MagicMock(running=True, connected=True))
    ready_player.stream.send_cli_command = AsyncMock()
    unavailable_player = MagicMock(protocol_id="unavailable", protocol=StreamingProtocol.RAOP)
    unavailable_player.stream = None
    if stream_state != "missing":
        unavailable_player.stream = MagicMock(
            running=stream_state != "stopped",
            # a stream that was sent its audio EOF is still running, but its
            # stdin is closed for good so it can never be refilled
            accepts_audio=stream_state not in ("stopped", "audio_ended"),
            connected=stream_state != "disconnected",
        )
        unavailable_player.stream.send_cli_command = AsyncMock()
    players: Any = [ready_player, unavailable_player]
    session.sync_clients = players

    assert await session.standby() is False
    assert session.can_replace(players, session.pcm_format) is False
    ready_player.stream.send_cli_command.assert_not_awaited()
    ready_player.set_state_from_stream.assert_not_called()
    if unavailable_player.stream:
        unavailable_player.stream.send_cli_command.assert_not_awaited()
        unavailable_player.set_state_from_stream.assert_not_called()


@pytest.mark.asyncio
async def test_standby_returns_false_when_command_is_not_delivered() -> None:
    """Standby fails without changing state for a member that misses the command."""
    session = _make_session(0, 0)
    logger = MagicMock()
    session.prov.logger = logger
    delivered_player = MagicMock(protocol_id="delivered", protocol=StreamingProtocol.AIRPLAY2)
    delivered_player.stream = _stream_defaults(MagicMock(running=True, connected=True))
    delivered_player.stream.send_cli_command = AsyncMock(return_value=True)
    dropped_player = MagicMock(protocol_id="dropped", protocol=StreamingProtocol.RAOP)
    dropped_player.stream = _stream_defaults(MagicMock(running=True, connected=True))
    dropped_player.stream.send_cli_command = AsyncMock(return_value=False)
    pending_player = MagicMock(protocol_id="pending", protocol=StreamingProtocol.AIRPLAY2)
    pending_player.stream = _stream_defaults(MagicMock(running=True, connected=True))
    pending_player.stream.send_cli_command = AsyncMock(return_value=True)
    session.sync_clients = [delivered_player, dropped_player, pending_player]

    assert await session.standby() is False
    delivered_player.stream.send_cli_command.assert_awaited_once_with("ACTION=STANDBY")
    delivered_player.set_state_from_stream.assert_called_once_with(
        state=PlaybackState.PAUSED, stream=delivered_player.stream
    )
    dropped_player.stream.send_cli_command.assert_awaited_once_with("ACTION=STANDBY")
    dropped_player.set_state_from_stream.assert_not_called()
    pending_player.stream.send_cli_command.assert_not_awaited()
    pending_player.set_state_from_stream.assert_not_called()
    logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_standby_returns_false_when_command_raises() -> None:
    """Standby fails without changing state when command delivery raises."""
    session = _make_session(0, 0)
    logger = MagicMock()
    session.prov.logger = logger
    player = MagicMock(protocol_id="failed", protocol=StreamingProtocol.AIRPLAY2)
    player.stream = _stream_defaults(MagicMock(running=True, connected=True))
    player.stream.send_cli_command = AsyncMock(side_effect=OSError("command pipe failed"))
    session.sync_clients = [player]

    assert await session.standby() is False
    player.set_state_from_stream.assert_not_called()
    logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_late_join_empty_buffer() -> None:
    """Test that with an empty buffer, start_at = start_time + seconds_streamed."""
    now = time.time()
    start_time = now - 8
    seconds_streamed = 12.5
    session = _make_session(start_time, seconds_streamed)
    player = _make_late_joiner()

    with patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start:
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    assert mock_start.called, "_start_client was never called"
    expected = start_time + seconds_streamed
    assert abs(_captured_start_at(player) - expected) < 0.1


@pytest.mark.asyncio
async def test_late_join_caps_prime_at_whole_ring_when_position_predates_it() -> None:
    """A due position older than the ring caps the prime at the whole buffer."""
    now = 1_000_000.0
    # Synthetic anchor in the future forces the due position behind the oldest
    # buffered sample: only the whole ring can prime and the anchor is pulled
    # to the ring's first sample so content and anchor stay exactly aligned.
    seconds_streamed = 12.5
    buffer_seconds = 3
    start_time = now + 5.0
    session = _make_session(start_time, seconds_streamed)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * buffer_seconds)
    player = _make_late_joiner()

    written_chunks: list[bytes] = []

    async def capture_write(_player: Any, chunk: bytes) -> None:
        written_chunks.append(chunk)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    assert mock_start.called, "_start_client was never called"
    # the whole ring is primed, nothing is skipped, and the anchor maps to the
    # ring's first sample: start_at = start_time + (seconds_streamed - buffer)
    assert session._client_skip_bytes[player.protocol_id] == 0
    assert written_chunks, "expected the whole ring to be primed"
    assert len(written_chunks[0]) / PCM_SAMPLE_SIZE == pytest.approx(buffer_seconds, abs=0.01)
    expected = start_time + (seconds_streamed - buffer_seconds)
    assert _captured_start_at(player) == pytest.approx(expected, abs=0.02)


@pytest.mark.asyncio
async def test_late_join_adds_to_sync_clients() -> None:
    """Test that the late joiner is added to sync_clients."""
    now = time.time()
    session = _make_session(now - 10, 12.5)
    player = _make_late_joiner()

    with patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start:
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    assert player in session.sync_clients


@pytest.mark.asyncio
async def test_late_join_start_failure_stops_client() -> None:
    """A late joiner whose START fails is torn down before joining the session."""
    session = _make_session(time.time() - 10, 12.5)
    player = _make_late_joiner()

    def setup_failing_start(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()
        player.stream.start = AsyncMock(side_effect=OSError("start failed"))

    with (
        patch.object(session, "_start_client", side_effect=setup_failing_start),
        patch.object(session, "stop_client", new_callable=AsyncMock) as stop_client,
    ):
        await session.add_client(player)

    player.stream.start.assert_awaited_once()
    # START precedes the prime feed and the sync_clients append, so a failed
    # joiner is stopped without ever having joined the session.
    assert player not in session.sync_clients
    stop_client.assert_awaited_once_with(player, reason="late joiner start/prime failed")


@pytest.mark.asyncio
async def test_late_join_unacknowledged_start_stops_client() -> None:
    """A joiner whose START is never acked is torn down, never mapped onto that instant."""
    session = _make_session(time.time() - 10, 12.5)
    player = _make_late_joiner()

    def setup_unacknowledged_start(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()
        player.stream.start = AsyncMock(
            side_effect=PlayerCommandFailed(
                "AirPlay player Player B did not acknowledge its start within 5.0s"
            )
        )

    with (
        patch.object(session, "_start_client", side_effect=setup_unacknowledged_start),
        patch.object(session, "stop_client", new_callable=AsyncMock) as stop_client,
    ):
        await session.add_client(player)

    assert player not in session.sync_clients
    assert player.protocol_id not in session._client_skip_bytes
    player.stream.rebase_position.assert_not_called()
    stop_client.assert_awaited_once_with(player, reason="late joiner start/prime failed")


@pytest.mark.asyncio
async def test_late_join_refuses_a_session_that_was_sent_its_audio_eof() -> None:
    """
    A stream on its way out after an EOF cannot absorb a joiner.

    It keeps reporting itself running for as long as it plays out, so a joiner
    would otherwise be anchored onto a session that exits moments later, with a
    stdin of its own that nothing is left to close.
    """
    session = _make_session(time.time() - 10, 12.5)
    reference: Any = session.sync_clients[0]
    reference.stream.accepts_audio = False
    player = _make_late_joiner()

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "stop_client", new_callable=AsyncMock) as stop_client,
    ):
        await session.add_client(player)

    mock_start.assert_not_called()
    stop_client.assert_not_awaited()
    assert player not in session.sync_clients


@pytest.mark.asyncio
async def test_late_join_refuses_a_parked_session() -> None:
    """A parked (standby) session has no live timeline, so it cannot absorb a joiner."""
    session = _make_session(time.time() - 10, 12.5)
    reference: Any = session.sync_clients[0]
    reference.stream.send_cli_command = AsyncMock(return_value=True)
    reference.set_state_from_stream = MagicMock(
        side_effect=lambda **kwargs: setattr(reference, "playback_state", kwargs["state"])
    )
    assert await session.standby()
    assert reference.playback_state == PlaybackState.PAUSED
    player = _make_late_joiner()

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "stop_client", new_callable=AsyncMock) as stop_client,
    ):
        await session.add_client(player)

    # The parked session zeroed seconds_streamed while start_time stayed put, so
    # anchoring here maps the joiner onto a timeline nothing is playing.
    mock_start.assert_not_called()
    stop_client.assert_not_awaited()
    assert player not in session.sync_clients


@pytest.mark.asyncio
async def test_late_join_no_running_session() -> None:
    """Test that add_client is a no-op when no session is running."""
    now = time.time()
    session = _make_session(now - 10, 12.5)
    # Make the leader's stream not running
    leader = session.sync_clients[0]
    leader.stream = _stream_defaults(MagicMock(running=False))
    player = _make_late_joiner()

    with patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start:
        await session.add_client(player)
        mock_start.assert_not_called()
        assert player not in session.sync_clients


@pytest.mark.asyncio
async def test_late_join_primes_from_ring_tail_at_headroom() -> None:
    """A due position inside the ring primes from the tail and anchors at now + headroom."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    start_time = now - 0.5
    seconds_streamed = 5.0
    session = _make_session(start_time, seconds_streamed)
    # Fill ring buffer with 5 seconds of non-silent PCM.
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 5)
    player = _make_late_joiner()

    written_chunks: list[bytes] = []

    async def capture_write(_player: Any, chunk: bytes) -> None:
        written_chunks.append(chunk)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    # start_at is now + min_headroom (the late-join floor); fed_pos_due = 2.0s
    assert mock_start.called, "_start_client was never called"
    expected_headroom = AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000
    assert _captured_start_at(player) - now == pytest.approx(expected_headroom, abs=0.01), (
        f"start_at should be at now + min_headroom, "
        f"got offset {_captured_start_at(player) - now:.4f}s"
    )

    # The last 3.0s of the ring is primed (positions 2.0s..5.0s), nothing skipped.
    assert session._client_skip_bytes[player.protocol_id] == 0
    assert written_chunks, "No data was written to the player"
    remaining_seconds = len(written_chunks[0]) / PCM_SAMPLE_SIZE
    expected_primed = seconds_streamed - (expected_headroom + (now - start_time))
    assert remaining_seconds == pytest.approx(expected_primed, abs=0.01), (
        f"expected {expected_primed:.2f}s primed, got {remaining_seconds:.4f}s"
    )


@pytest.mark.asyncio
async def test_late_join_skips_live_feed_when_anchor_ahead_of_write_head() -> None:
    """When the due position is ahead of the write head, skip that many live bytes."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    # Diagnosed clamp case: now - start_time = 8.84s, seconds_streamed = 10.0s,
    # min_headroom = 2.5s (the late-join floor, no readiness projection) and no
    # group shift, so the anchor is due at 11.34s of feed, past the 10.0s write
    # head.
    start_time = now - 8.84
    seconds_streamed = 10.0
    session = _make_session(start_time, seconds_streamed)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 10)
    player = _make_late_joiner()

    written_chunks: list[bytes] = []

    async def capture_write(_player: Any, chunk: bytes) -> None:
        written_chunks.append(chunk)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    # anchor is now + min_headroom and nothing is primed (position past the head)
    expected_headroom = AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000
    assert _captured_start_at(player) - now == pytest.approx(expected_headroom, abs=0.01)
    assert written_chunks == []
    skip_seconds = session._client_skip_bytes[player.protocol_id] / PCM_SAMPLE_SIZE
    assert skip_seconds == pytest.approx(1.34, abs=0.01)


@pytest.mark.asyncio
async def test_late_join_primes_from_ring_under_group_shift() -> None:
    """A reference member that re-anchored later pulls the due position back into the ring."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    # Same base as the clamp case, but the reference member accumulated a
    # +3.039s starvation shift (134020 frames @44100) so the group's effective
    # anchor is later: fed_pos_due = 8.30s, back inside the ring. The joiner is
    # primed with ~1.7s from the ring tail and skips nothing.
    start_time = now - 8.84
    seconds_streamed = 10.0
    session = _make_session(start_time, seconds_streamed)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 10)
    reference: Any = session.sync_clients[0]
    reference.stream.cumulative_shift_seconds = 134020 / 44100
    player = _make_late_joiner()

    written_chunks: list[bytes] = []

    async def capture_write(_player: Any, chunk: bytes) -> None:
        written_chunks.append(chunk)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    expected_headroom = AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000
    assert _captured_start_at(player) - now == pytest.approx(expected_headroom, abs=0.01)
    assert session._client_skip_bytes[player.protocol_id] == 0
    assert written_chunks, "expected a prime write from the ring tail"
    primed_seconds = len(written_chunks[0]) / PCM_SAMPLE_SIZE
    assert primed_seconds == pytest.approx(1.699, abs=0.01)


@pytest.mark.asyncio
async def test_late_join_anchors_on_the_reported_clock_readiness(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A projected readiness instant anchors the join, just past the receiver's clock."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    session = _make_session(now - 5.0, 5.0)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 5)
    session.prov.logger = logging.getLogger("test.airplay.session")
    player = _make_late_joiner()
    # A cold receiver: its clock is projected usable 3.0s out, well past the floor.
    ready_at_unix_ms = int((now + 3.0) * 1000)

    def setup_with_projection(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()
        player.stream.wait_clock_ready = AsyncMock(
            return_value=(ClockReadiness.PROJECTED, ready_at_unix_ms)
        )

    with (
        caplog.at_level(logging.DEBUG),
        patch.object(session, "_start_client", side_effect=setup_with_projection),
        patch.object(session, "_write_chunk_to_player", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        await session.add_client(player)

    expected_lead = AIRPLAY_CLOCK_READY_LEAD_MS / 1000
    assert _captured_start_at(player) - now == pytest.approx(3.0 + expected_lead, abs=0.01)
    player.stream.wait_clock_ready.assert_awaited_once_with(
        timeout=AIRPLAY_CLOCK_READY_TIMEOUT_MS / 1000
    )
    assert "receiver clock usable in 3.00s; anchoring no earlier than that" in caplog.text


@pytest.mark.asyncio
async def test_late_join_floor_wins_over_a_clock_that_is_already_ready(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A receiver whose clock is already locked still gets the join floor as its anchor."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    session = _make_session(now - 5.0, 5.0)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 5)
    session.prov.logger = logging.getLogger("test.airplay.session")
    player = _make_late_joiner()
    # A warm receiver reports a readiness instant that has already passed.
    ready_at_unix_ms = int((now - 1.0) * 1000)

    def setup_with_projection(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()
        player.stream.wait_clock_ready = AsyncMock(
            return_value=(ClockReadiness.PROJECTED, ready_at_unix_ms)
        )

    with (
        caplog.at_level(logging.DEBUG),
        patch.object(session, "_start_client", side_effect=setup_with_projection),
        patch.object(session, "_write_chunk_to_player", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        await session.add_client(player)

    expected_headroom = AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000
    assert _captured_start_at(player) - now == pytest.approx(expected_headroom, abs=0.01)
    assert "receiver clock became usable 1.00s ago; anchoring on the join floor" in caplog.text


@pytest.mark.asyncio
async def test_late_join_falls_back_to_the_floor_without_a_clock_projection() -> None:
    """No projection (NTP timing or a silent receiver) anchors on the floor."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    session = _make_session(now - 5.0, 5.0)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 5)
    player = _make_late_joiner()

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    # every fallback shape surfaces as "no projection" to the session
    player.stream.wait_clock_ready.assert_awaited_once_with(
        timeout=AIRPLAY_CLOCK_READY_TIMEOUT_MS / 1000
    )
    expected_headroom = AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000
    assert _captured_start_at(player) - now == pytest.approx(expected_headroom, abs=0.01)
    assert player in session.sync_clients


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "readiness",
    [ClockReadiness.UNREPORTED, ClockReadiness.NOT_APPLICABLE],
    ids=["unreported", "ntp"],
)
async def test_late_join_without_a_projection_still_joins(readiness: ClockReadiness) -> None:
    """A device with no clock to wait for is a fallback, not a reason to refuse it."""
    now = 1_000_000.0
    session = _make_session(now - 5.0, 5.0)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 5)
    player = _make_late_joiner()

    def setup_without_projection(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()
        player.stream.wait_clock_ready = AsyncMock(return_value=(readiness, 0))

    with (
        patch.object(session, "_start_client", side_effect=setup_without_projection),
        patch.object(session, "_write_chunk_to_player", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        await session.add_client(player)

    assert player in session.sync_clients


@pytest.mark.asyncio
async def test_stalled_late_joiner_is_removed_while_the_group_plays_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A joiner whose receiver never answered our clock renders silence, so it leaves the group."""
    session = _make_session(time.time() - 5.0, 5.0)
    session.prov.logger = logging.getLogger("test.airplay.session")
    leader = session.sync_clients[0]
    player = _make_late_joiner()
    player.display_name = "Late Joiner"
    _setup_stream(player)()
    player.stream.session = session
    session.sync_clients.append(player)

    with (
        patch.object(session, "stop_client", new_callable=AsyncMock) as stop_client,
        patch.object(session, "stop", new_callable=AsyncMock) as stop_session,
        caplog.at_level(logging.WARNING),
    ):
        await session.remove_stalled_joiner(player)

    assert session.sync_clients == [leader]
    stop_client.assert_awaited_once_with(player, reason="receiver clock stalled")
    player.set_state_from_stream.assert_called_once_with(PlaybackState.IDLE, stream=player.stream)
    stop_session.assert_not_awaited()
    assert len(caplog.records) == 1
    assert "Removed late joiner Late Joiner from the group" in caplog.text


@pytest.mark.asyncio
async def test_stall_of_a_player_no_longer_in_the_session_changes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stall reported after the joiner already left the session neither warns nor tears down."""
    session = _make_session(time.time() - 5.0, 5.0)
    session.prov.logger = logging.getLogger("test.airplay.session")
    player = _make_late_joiner()

    with (
        patch.object(session, "stop_client", new_callable=AsyncMock) as stop_client,
        caplog.at_level(logging.WARNING),
    ):
        await session.remove_stalled_joiner(player)

    stop_client.assert_not_awaited()
    assert not caplog.records


@pytest.mark.asyncio
async def test_late_join_feed_keeps_flowing_while_waiting_for_clock_readiness() -> None:
    """The group keeps being fed while a joiner's receiver clock projection is pending."""
    session = _make_session(time.time() - 5, 5.0)
    player = _make_late_joiner()
    readiness_pending = asyncio.Event()
    readiness_released = asyncio.Event()

    def setup_pending_projection(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()

        async def wait_clock_ready(*_args: Any, **_kwargs: Any) -> tuple[ClockReadiness, int]:
            readiness_pending.set()
            await readiness_released.wait()
            return (ClockReadiness.UNREPORTED, 0)

        player.stream.wait_clock_ready = AsyncMock(side_effect=wait_clock_ready)

    with (
        patch.object(session, "_start_client", side_effect=setup_pending_projection),
        patch.object(session, "_write_chunk_to_player", new_callable=AsyncMock),
    ):
        join = asyncio.create_task(session.add_client(player))
        await asyncio.wait_for(readiness_pending.wait(), timeout=5)
        assert await asyncio.wait_for(
            session._write_chunk_to_all_players(b"\x02" * PCM_SAMPLE_SIZE), timeout=5
        )
        readiness_released.set()
        await asyncio.wait_for(join, timeout=5)

    assert session.seconds_streamed == pytest.approx(6.0)
    assert player in session.sync_clients


@pytest.mark.asyncio
async def test_late_join_feed_keeps_flowing_while_start_ack_is_outstanding() -> None:
    """The group keeps being fed while a join's START ack is outstanding."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    start_time = now - 5.0
    session = _make_session(start_time, 5.0)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 5)
    player = _make_late_joiner()
    ack_outstanding = asyncio.Event()
    ack_released = asyncio.Event()

    def setup_deferred_ack(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()

        async def start(start_unix_ms: int, *_args: Any, **_kwargs: Any) -> int:
            # the binary holds its ack until the receiver clock is verified
            ack_outstanding.set()
            await ack_released.wait()
            return start_unix_ms

        player.stream.start = AsyncMock(side_effect=start)

    writes: list[tuple[str, int]] = []

    async def capture_write(target: Any, chunk: bytes) -> None:
        writes.append((target.protocol_id, len(chunk)))

    with (
        patch.object(session, "_start_client", side_effect=setup_deferred_ack),
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        join = asyncio.create_task(session.add_client(player))
        await asyncio.wait_for(ack_outstanding.wait(), timeout=5)
        assert await asyncio.wait_for(
            session._write_chunk_to_all_players(b"\x02" * PCM_SAMPLE_SIZE), timeout=5
        )
        ack_released.set()
        await asyncio.wait_for(join, timeout=5)

    # that second of feed reached the leader and moved the write head to 6.0s
    assert ("leader", PCM_SAMPLE_SIZE) in writes
    assert session.seconds_streamed == pytest.approx(6.0)
    # the anchor (now + the 2.5s floor) is due at 7.5s of feed, so the joiner
    # skips only the 1.5s still to come, not the 2.5s due at the commanded
    # mapping: the content is mapped against the head the feed actually reached
    skip_seconds = session._client_skip_bytes[player.protocol_id] / PCM_SAMPLE_SIZE
    assert skip_seconds == pytest.approx(1.5, abs=0.01)
    assert player in session.sync_clients


@pytest.mark.asyncio
async def test_late_join_cancelled_while_ack_outstanding_stops_the_client() -> None:
    """A join cancelled while its START ack is outstanding never half-joins the session."""
    session = _make_session(time.time() - 5, 5.0)
    player = _make_late_joiner()
    ack_outstanding = asyncio.Event()

    def setup_pending_ack(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()

        async def start(*_args: Any, **_kwargs: Any) -> None:
            ack_outstanding.set()
            await asyncio.Event().wait()

        player.stream.start = AsyncMock(side_effect=start)

    with (
        patch.object(session, "_start_client", side_effect=setup_pending_ack),
        patch.object(session, "stop_client", new_callable=AsyncMock) as stop_client,
    ):
        join = asyncio.create_task(session.add_client(player))
        await asyncio.wait_for(ack_outstanding.wait(), timeout=5)
        join.cancel()
        with pytest.raises(asyncio.CancelledError):
            await join

    assert player not in session.sync_clients
    stop_client.assert_awaited_once_with(player, reason="late joiner start cancelled")


@pytest.mark.asyncio
async def test_late_join_maps_content_from_the_acked_instant() -> None:
    """A binary that acks later than commanded gets its content mapped to the acked instant."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    start_time = now - 5.0
    session = _make_session(start_time, 5.0)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 5)
    player = _make_late_joiner()
    # A sync_adjust rides on top of the commanded instant, so it has to be taken
    # back out of the ack before the content is mapped onto it.
    sync_adjust = 200
    player.sync_offset_ms = -sync_adjust
    deferral_ms = 1500

    def setup_deferred_ack(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()

        async def start(start_unix_ms: int, _position_ms: int, *, join: bool = False) -> int:
            assert join is True
            return start_unix_ms + deferral_ms

        player.stream.start = AsyncMock(side_effect=start)

    written_chunks: list[bytes] = []

    async def capture_write(_player: Any, chunk: bytes) -> None:
        written_chunks.append(chunk)

    with (
        patch.object(session, "_start_client", side_effect=setup_deferred_ack),
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        await session.add_client(player)

    # a positive sync adjust plays earlier
    commanded_ms = int((now + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000) * 1000) - sync_adjust
    assert player.stream.start.await_args.args[0] == commanded_ms
    # The ack lands 1.5s later than commanded, so the due position is 9.0s of
    # feed instead of 7.5s: the joiner skips 4.0s of the live feed.
    assert written_chunks == []
    skip_seconds = session._client_skip_bytes[player.protocol_id] / PCM_SAMPLE_SIZE
    assert skip_seconds == pytest.approx(4.0, abs=0.01)
    # Progress is reported against the sample that lands on the acked instant,
    # not the one that would have landed on the commanded instant.
    player.stream.rebase_position.assert_called_once_with(9000)


@pytest.mark.asyncio
async def test_write_chunk_drains_skip_counter_across_chunks() -> None:
    """A per-client skip is consumed across chunks, slicing the partial one."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    ffmpeg = MagicMock(closed=False)
    ffmpeg.write = AsyncMock()
    session._player_ffmpeg[player.protocol_id] = ffmpeg
    # skip 1.5s of a 1s-per-chunk feed
    session._client_skip_bytes[player.protocol_id] = PCM_SAMPLE_SIZE * 3 // 2

    chunk_one = b"\x01" * PCM_SAMPLE_SIZE
    chunk_two = b"\x02" * PCM_SAMPLE_SIZE
    chunk_three = b"\x03" * PCM_SAMPLE_SIZE
    for chunk in (chunk_one, chunk_two, chunk_three):
        await session._write_chunk_to_player(player, chunk)

    written = [call_args.args[0] for call_args in ffmpeg.write.await_args_list]
    # first chunk fully consumed, second chunk sliced in half, third whole
    assert written == [b"\x02" * (PCM_SAMPLE_SIZE // 2), chunk_three]
    assert session._client_skip_bytes[player.protocol_id] == 0


def test_effective_start_time_adds_reference_member_shift() -> None:
    """The effective anchor adds the first sync client's accumulated shift."""
    session = _make_session(100.0, 0)
    reference: Any = session.sync_clients[0]
    reference.stream.cumulative_shift_seconds = 1.539
    assert session.effective_start_time == pytest.approx(101.539)

    # the reference transfers to whichever client is first
    other = MagicMock()
    other.stream.cumulative_shift_seconds = 0.5
    session.sync_clients.insert(0, other)
    assert session.effective_start_time == pytest.approx(100.5)

    # a missing stream falls back to the raw anchor
    other.stream = None
    assert session.effective_start_time == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_stop_client_clears_skip_and_shift_state() -> None:
    """Tearing a client down drops its skip counter and resets its playout shift."""
    session = _make_session(0, 0)
    player = _make_late_joiner()
    player.stream = _stream_defaults(MagicMock())
    player.stream.session = session
    player.stream.stop = AsyncMock()
    session._client_skip_bytes[player.protocol_id] = 12_345

    await session.stop_client(player)

    assert player.protocol_id not in session._client_skip_bytes
    player.stream.reset_reanchor_shift.assert_called_once_with()
    player.stream.stop.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_replace_clears_skip_and_shift_state() -> None:
    """A warm replace re-anchors everyone, so skip counters and shifts reset."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.sync_offset_ms = 0
    stream = player.stream
    stream.running = True
    stream.connected = True
    stream.flush = AsyncMock(return_value=True)
    stream.wait_audio_present = AsyncMock(return_value=True)
    session._client_skip_bytes[player.protocol_id] = 999

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch(
            "music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=100.0
        ),
    ):
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=0))

    assert session._client_skip_bytes == {}
    stream.reset_reanchor_shift.assert_called()


@pytest.mark.asyncio
async def test_cleanup_after_removal_skips_idle_when_player_has_new_session_stream() -> None:
    """Cleanup must not idle a player that was already re-added to another session."""
    now = time.time()
    session = _make_session(now - 10, 12.5)
    player = _make_late_joiner()
    other_session = object()
    player.set_state_from_stream = MagicMock()
    player.stream = _stream_defaults(MagicMock())
    player.stream.session = other_session
    session.sync_clients.clear()

    with (
        patch.object(session, "stop_client", new_callable=AsyncMock),
        patch.object(session, "stop", new_callable=AsyncMock),
    ):
        await session._cleanup_after_removal(player)

    player.set_state_from_stream.assert_not_called()


@pytest.mark.asyncio
async def test_late_join_pads_with_silence_when_the_ring_ran_out_under_a_committed_anchor() -> None:
    """A due position lost to the ring after the START is covered with silence, not a moved anchor."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    start_time = now - 100.0
    session = _make_session(start_time, 110.0)
    session._pcm_total_fed = int(110.0 * PCM_SAMPLE_SIZE)
    # An 8s ring against a 10s write-head lead: wide enough when the anchor is
    # planned, too narrow by the time the binary owns the instant.
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 8)
    session._pcm_buffer_max = PCM_SAMPLE_SIZE * 8
    # Freeze the adaptive sizing so this test covers the cap, not the growth.
    session._peak_lead_seconds = 1e6
    logger = MagicMock()
    session.prov.logger = logger
    player = _make_late_joiner()

    written: list[tuple[str, bytes]] = []

    async def capture_write(target: Any, chunk: bytes) -> None:
        written.append((target.protocol_id, chunk))

    def setup_with_feed(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()

        async def start(start_unix_ms: int, _position_ms: int, *, join: bool = False) -> int:
            assert join is True
            # The group keeps being fed while the START ack is outstanding: this
            # is what pushes the joiner's due position off the back of the ring.
            await session._write_chunk_to_all_players(b"\x02" * int(2.0 * PCM_SAMPLE_SIZE))
            return start_unix_ms

        player.stream.start = AsyncMock(side_effect=start)

    with (
        patch.object(session, "_start_client", side_effect=setup_with_feed),
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        await session.add_client(player)

    # The anchor is the one that was commanded: an acked instant is never moved.
    headroom = AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000
    assert _captured_start_at(player) == pytest.approx(now + headroom, abs=0.001)
    prime = next(chunk for pid, chunk in written if pid == player.protocol_id)
    # due is 102.5s of feed and the write head reached 112.0s, so 9.5s is owed
    # while only 8s survives in the ring: the missing 1.5s opens as silence.
    assert len(prime) / PCM_SAMPLE_SIZE == pytest.approx(9.5, abs=0.001)
    pad = int(1.5 * PCM_SAMPLE_SIZE)
    assert prime[:pad] == bytes(pad), "the missing head must be silence"
    assert set(prime[pad:]) == {1, 2}, "the rest must be the buffered feed"
    assert pad % session._pcm_frame_size == 0
    assert session._client_skip_bytes[player.protocol_id] == 0
    # Position still reports where the GROUP is at that instant, because the
    # real content lands exactly where it would have without the shortfall.
    player.stream.rebase_position.assert_not_called()
    logger.warning.assert_called_once()
    assert "silence" in logger.warning.call_args.args[0]


@pytest.mark.asyncio
async def test_late_join_ring_shortfall_keeps_a_misaligned_ring_head_frame_aligned() -> None:
    """A silence pad over a mid-frame ring head still lands the feed on frame boundaries."""
    now = 1_000_000.0
    session = _make_session(now - 100.0, 110.0)
    # Both the write head and the ring head sit mid-frame.
    session._pcm_total_fed = int(110.0 * PCM_SAMPLE_SIZE) + 3
    session._pcm_buffer = bytearray(b"\x01" * (PCM_SAMPLE_SIZE * 8 + 2))
    session._pcm_buffer_max = len(session._pcm_buffer)
    session._peak_lead_seconds = 1e6
    player = _make_late_joiner()

    written: list[tuple[str, bytes]] = []

    async def capture_write(target: Any, chunk: bytes) -> None:
        written.append((target.protocol_id, chunk))

    def setup_with_feed(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()

        async def start(start_unix_ms: int, _position_ms: int, *, join: bool = False) -> int:
            assert join is True
            await session._write_chunk_to_all_players(b"\x02" * (int(2.0 * PCM_SAMPLE_SIZE) + 1))
            return start_unix_ms

        player.stream.start = AsyncMock(side_effect=start)

    with (
        patch.object(session, "_start_client", side_effect=setup_with_feed),
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        await session.add_client(player)

    prime = next(chunk for pid, chunk in written if pid == player.protocol_id)
    frame_size = session._pcm_frame_size
    # The prime ends exactly at the write head, so its start - and therefore the
    # whole padded prime - has to sit on an absolute frame boundary.
    assert (session._pcm_total_fed - len(prime)) % frame_size == 0
    pad_len = len(prime) - len(prime.lstrip(b"\x00"))
    assert pad_len % frame_size == 0


def test_ring_grows_to_the_observed_write_head_lead() -> None:
    """The ring tracks the largest lead a session shows, above a floor and under a byte cap."""
    now = 1_000_000.0
    session = _make_session(now - 100.0, 100.0)
    assert session._pcm_buffer_max == int(AIRPLAY_LATE_JOIN_RING_MIN_SECONDS * PCM_SAMPLE_SIZE)

    with patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now):
        # A 4s lead stays under the floor, which keeps the ring where it was.
        session.seconds_streamed = 104.0
        session._observe_write_head_lead()
        assert session._pcm_buffer_max == int(AIRPLAY_LATE_JOIN_RING_MIN_SECONDS * PCM_SAMPLE_SIZE)

        # A 15s lead carries the ring past the floor, with the margin on top.
        session.seconds_streamed = 115.0
        session._observe_write_head_lead()
        assert session._peak_lead_seconds == pytest.approx(15.0, abs=0.001)
        expected = int((15.0 + AIRPLAY_LATE_JOIN_RING_MARGIN_SECONDS) * PCM_SAMPLE_SIZE)
        assert session._pcm_buffer_max == expected

        # A lead that falls back never shrinks the ring: the history a joiner
        # still needs is already in it.
        session.seconds_streamed = 108.0
        session._observe_write_head_lead()
        assert session._pcm_buffer_max == expected

        # Growth is bounded in bytes, so a hi-res rate cannot multiply it out.
        session.seconds_streamed = 100.0 + 3600.0
        session._observe_write_head_lead()
        assert session._pcm_buffer_max == AIRPLAY_LATE_JOIN_RING_MAX_BYTES


def test_write_head_lead_is_not_measured_before_the_anchor_arrives() -> None:
    """Audio fed inside the start lead is not counted as pipeline depth."""
    now = 1_000_000.0
    # Anchored 2.5s into the future: nothing is audible yet.
    session = _make_session(now + 2.5, 8.0)
    with patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now):
        session._observe_write_head_lead()
    assert session._peak_lead_seconds == 0.0
    assert session._pcm_buffer_max == int(AIRPLAY_LATE_JOIN_RING_MIN_SECONDS * PCM_SAMPLE_SIZE)

    unanchored = _make_session(0.0, 8.0)
    with patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now):
        unanchored._observe_write_head_lead()
    assert unanchored._peak_lead_seconds == 0.0


@pytest.mark.asyncio
async def test_late_join_silence_pad_is_bounded_and_reports_the_residual() -> None:
    """An implausible ack is padded only up to the ring bound, and says so."""
    now = 1_000_000.0
    session = _make_session(now - 400.0, 420.0)
    session._pcm_total_fed = int(420.0 * PCM_SAMPLE_SIZE)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 2)
    session._pcm_buffer_max = PCM_SAMPLE_SIZE * 2
    session._peak_lead_seconds = 1e6
    logger = MagicMock()
    session.prov.logger = logger
    player = _make_late_joiner()

    written: list[tuple[str, bytes]] = []

    async def capture_write(target: Any, chunk: bytes) -> None:
        written.append((target.protocol_id, chunk))

    def setup_with_stale_ack(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()
        # The binary reports an instant far behind the commanded one, mapping
        # the joiner back near the start of the session. 320s of head is owed.
        player.stream.start = AsyncMock(return_value=int((now - 300.0) * 1000))

    with (
        patch.object(session, "_start_client", side_effect=setup_with_stale_ack),
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        await session.add_client(player)

    prime = next(chunk for pid, chunk in written if pid == player.protocol_id)
    # Bounded at one ring of silence plus the ring itself - never the 320s owed.
    assert len(prime) / PCM_SAMPLE_SIZE == pytest.approx(4.0, abs=0.01)
    assert (session._pcm_total_fed - len(prime)) % session._pcm_frame_size == 0
    # The joiner cannot be placed exactly, so the log must not claim sync.
    logger.warning.assert_called_once()
    tail = logger.warning.call_args.args[-1]
    assert "ahead of the group" in tail
    assert "in sync" not in tail


@pytest.mark.asyncio
async def test_start_client_releases_a_foreign_mute_latch() -> None:
    """A client joining the session gets its foreign mute latch released on start."""
    session = _make_session(start_time=0.0, seconds_streamed=0.0)
    player = _make_late_joiner()

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.AirPlayStream",
            return_value=MagicMock(connect=AsyncMock()),
        ),
        patch.object(session, "_start_player_ffmpeg", AsyncMock()),
    ):
        await session._start_client(player, use_shared_ptp=False)

    player.release_foreign_mute_latch.assert_called_once_with()


@pytest.mark.asyncio
async def test_start_client_displaces_a_stopping_stream_under_the_spawn_lock() -> None:
    """
    Displacing a member's cli is one claim: the stop, the connect and the wiring.

    A stream stops reporting itself running the moment its own stop() begins,
    while its process can still be on the receiver, so the old one is stopped
    whatever it reports. Holding the spawn lock across the connect and the ffmpeg
    keeps a Sendspin bridge start from putting a second process on the same
    receiver in between.
    """
    session = _make_session(start_time=0.0, seconds_streamed=0.0)
    player = _make_late_joiner()
    player.stream_spawn_lock = asyncio.Lock()
    steps: list[str] = []

    def record(step: str) -> None:
        assert player.stream_spawn_lock.locked()
        steps.append(step)

    old_stream = MagicMock(running=False)
    old_stream.stop = AsyncMock(side_effect=lambda: record("stop"))
    player.stream = old_stream
    new_stream = MagicMock(connect=AsyncMock(side_effect=lambda *_args: record("connect")))

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.AirPlayStream",
            return_value=new_stream,
        ),
        patch.object(
            session, "_start_player_ffmpeg", AsyncMock(side_effect=lambda *_args: record("ffmpeg"))
        ),
    ):
        await session._start_client(player, use_shared_ptp=False)

    assert steps == ["stop", "connect", "ffmpeg"]
    assert player.stream is new_stream
    assert new_stream.session is session


def _make_group_session(*shifts: float) -> AirPlayStreamSession:
    """
    Create a playing group session whose members report the given starvation shifts.

    :param shifts: Accumulated shift in seconds of each member, the leader first.
    """
    session = _make_session(time.time() - 60, 60.0)
    leader: Any = session.sync_clients[0]
    leader.display_name = "Leader"
    leader.owner = leader
    leader.player.player_id = "leader_player"
    leader.player.synced_to = None
    for index in range(1, len(shifts)):
        member = MagicMock()
        member.protocol_id = f"member_{index}"
        member.display_name = f"Member {index}"
        member.owner = member
        member.player.player_id = f"member_{index}_player"
        member.player.synced_to = "leader_player"
        member.stream = _stream_defaults(MagicMock())
        member.stream.running = True
        session.sync_clients.append(member)
    clients: list[Any] = list(session.sync_clients)
    for client, shift in zip(clients, shifts, strict=True):
        client.stream.cumulative_shift_seconds = shift
    return session


def test_stall_with_diverged_shifts_restarts_the_group_once() -> None:
    """A realtime member that shifted next to a buffered one that did not restarts the group."""
    # the reported case: the HomePods shifted 1.758 s, the buffered Beam kept its timeline
    session = _make_group_session(1.758, 1.758, 0.0)
    mass: Any = session.mass

    session.on_member_reanchored()

    mass.call_later.assert_called_once_with(
        AIRPLAY_STALL_SETTLE_SECONDS,
        session._check_member_shifts,
        task_id=session._shift_check_task_id,
    )
    session._check_member_shifts()
    # the restart goes to the leader, whose playback owns the group
    mass.players.cmd_resume.assert_called_once_with("leader_player")
    mass.create_task.assert_called_once()


@pytest.mark.parametrize(
    "shifts",
    [
        (1.758, 1.7585, 1.74),  # same queue depth: equal shifts within jitter
        (0.0, 0.0),  # nobody shifted
    ],
)
def test_stall_with_equal_shifts_keeps_playing(shifts: tuple[float, ...]) -> None:
    """Members that shifted alike are still in sync, so nothing restarts."""
    session = _make_group_session(*shifts)
    mass: Any = session.mass

    session._check_member_shifts()

    mass.players.cmd_resume.assert_not_called()


def test_stall_restart_backs_off_instead_of_looping() -> None:
    """A source that stalls again right after a restart does not restart the group again."""
    session = _make_group_session(1.758, 0.0)
    mass: Any = session.mass
    monotonic = "music_assistant.providers.airplay.stream_session.time.monotonic"

    with patch(monotonic, return_value=1000.0):
        session._check_member_shifts()
    assert mass.players.cmd_resume.call_count == 1

    # another stall pulls the members apart again 10 s later: the restart waits out the
    # hold-off rather than following straight away
    mass.call_later.reset_mock()
    with patch(monotonic, return_value=1010.0):
        session._check_member_shifts()
    assert mass.players.cmd_resume.call_count == 1
    mass.call_later.assert_called_once_with(
        pytest.approx(AIRPLAY_STALL_RESYNC_HOLDOFF - 10),
        session._check_member_shifts,
        task_id=session._shift_check_task_id,
    )

    # still out of sync once it has passed: restarted, and the next hold-off is twice as long
    with patch(monotonic, return_value=1000.0 + AIRPLAY_STALL_RESYNC_HOLDOFF):
        session._check_member_shifts()
    assert mass.players.cmd_resume.call_count == 2
    assert session._resync_not_before == pytest.approx(1000.0 + 3 * AIRPLAY_STALL_RESYNC_HOLDOFF)


def test_stall_check_skips_a_group_that_is_not_playing() -> None:
    """A solo session needs no check, and a parked group has no timeline to restart."""
    solo = _make_session(time.time() - 60, 60.0)
    solo_mass: Any = solo.mass
    solo.on_member_reanchored()
    solo_mass.call_later.assert_not_called()

    parked = _make_group_session(1.758, 0.0)
    parked_mass: Any = parked.mass
    leader: Any = parked.sync_clients[0]
    leader.playback_state = PlaybackState.PAUSED
    parked._check_member_shifts()
    parked_mass.players.cmd_resume.assert_not_called()


@pytest.mark.asyncio
async def test_stop_cancels_a_pending_stall_check() -> None:
    """A stopped session drops the comparison it scheduled after a stall."""
    session = _make_group_session(1.758, 0.0)
    mass: Any = session.mass
    clients: list[Any] = list(session.sync_clients)
    for client in clients:
        client.stream.session = session
        client.stream.stop = AsyncMock()

    await session.stop()

    mass.cancel_timer.assert_any_call(session._shift_check_task_id)


@pytest.mark.asyncio
async def test_late_joiner_counts_its_shift_from_the_group_timeline() -> None:
    """A joiner mapped onto a shifted group stays in sync when everyone shifts alike again."""
    now = 1_000_000.0
    session = _make_session(now - 8.84, 10.0)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 10)
    mass: Any = session.mass
    reference: Any = session.sync_clients[0]
    reference.stream.cumulative_shift_seconds = 1.758
    player = _make_late_joiner()

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=now),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    assert player in session.sync_clients
    # the next stall shifts both members by the same 1.758 s
    reference.stream.cumulative_shift_seconds = 2 * 1.758
    player.stream.cumulative_shift_seconds = 1.758
    session._check_member_shifts()

    mass.players.cmd_resume.assert_not_called()


def test_a_stall_of_the_speakers_of_a_set_restarts_the_set() -> None:
    """Speakers of one set that came out of a stall apart restart the player of their set."""
    session = _make_group_session(1.758, 0.0)
    airplay_set = MagicMock()
    airplay_set.player.player_id = "set_player"
    airplay_set.player.synced_to = None
    for speaker in session.sync_clients:
        cast("MagicMock", speaker).owner = airplay_set
    mass: Any = session.mass

    session._check_member_shifts()

    mass.players.cmd_resume.assert_called_once_with("set_player")


# --- The Apple TV of a set: a member that is not heard ---


def _make_set_session() -> tuple[AirPlayStreamSession, Any, Any]:
    """
    Create a playing session of the speaker of a set, with the Apple TV of the set.

    :return: The session, its speaker and the Apple TV, which is not in the session yet.
    """
    session = _make_session(time.time(), 0)
    speaker: Any = session.sync_clients[0]
    speaker.protocol_id = "apspeaker"
    speaker.renders_audio = True
    speaker.stream.flush = AsyncMock(return_value=True)
    speaker.stream.send_cli_command = AsyncMock(return_value=True)
    apple_tv = MagicMock(protocol_id="apappletv", renders_audio=False, sync_offset_ms=0)
    apple_tv.protocol = StreamingProtocol.AIRPLAY2
    apple_tv.endpoints = [speaker]
    apple_tv.silent_member = apple_tv
    apple_tv.stream = _stream_defaults(MagicMock(running=True, connected=True))
    apple_tv.stream.flush = AsyncMock(return_value=True)
    apple_tv.stream.send_cli_command = AsyncMock(return_value=True)
    speaker.owner = apple_tv
    mass: Any = session.mass
    mass.create_task = lambda coro: asyncio.get_running_loop().create_task(coro)
    return session, speaker, apple_tv


async def _settle_silent_joins(session: AirPlayStreamSession) -> None:
    """Let the joins of the members that are not heard run."""
    for _ in range(5):
        await asyncio.sleep(0)
    for task in list(session._silent_member_tasks.values()):
        with contextlib.suppress(asyncio.CancelledError):
            await task


_SILENT_POLL = (
    "music_assistant.providers.airplay.stream_session.AIRPLAY_SILENT_MEMBER_JOIN_POLL_INTERVAL"
)
_SILENT_TIMEOUT = (
    "music_assistant.providers.airplay.stream_session.AIRPLAY_SILENT_MEMBER_JOIN_TIMEOUT"
)


@pytest.mark.asyncio
async def test_the_apple_tv_of_a_set_joins_once_its_speakers_play() -> None:
    """
    The start of a set holds its speakers; the Apple TV joins as a late joiner once they play.

    An Apple TV whose SETUP goes out before the one of its HomePod resets its session.
    """
    session, speaker, apple_tv = _make_set_session()
    speaker.playback_state = PlaybackState.IDLE
    order: list[str] = []
    speaker.stream.start = AsyncMock(
        side_effect=lambda *_args: order.append("speaker start") or 0  # type: ignore[func-returns-value]
    )
    speaker.stream.wait_for_connection = AsyncMock()

    async def join(player: Any) -> None:
        order.append(f"join {player.protocol_id}")
        session.sync_clients.append(player)

    with (
        patch(_SILENT_POLL, 0),
        patch.object(session, "_start_client", new_callable=AsyncMock),
        patch.object(session, "_resolve_shared_ptp", new_callable=AsyncMock, return_value=False),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "add_client", side_effect=join) as add_client,
    ):
        await session.start(MagicMock())
        for _ in range(5):
            await asyncio.sleep(0)
        # started, but not playing yet: there is no timeline to join
        assert order == ["speaker start"]
        speaker.playback_state = PlaybackState.PLAYING
        await _settle_silent_joins(session)

    assert order == ["speaker start", "join apappletv"]
    add_client.assert_awaited_once_with(apple_tv)


@pytest.mark.asyncio
async def test_an_apple_tv_that_found_no_music_to_join_joins_at_the_next_warm_start() -> None:
    """A set paused before it played takes its Apple TV along when it resumes."""
    session, speaker, apple_tv = _make_set_session()
    speaker.playback_state = PlaybackState.PAUSED
    speaker.stream.wait_audio_present = AsyncMock(return_value=True)

    async def join(player: Any) -> None:
        session.sync_clients.append(player)

    with (
        patch(_SILENT_POLL, 0),
        patch(_SILENT_TIMEOUT, 0),
        patch.object(session, "add_client", side_effect=join) as add_client,
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
    ):
        session._join_silent_members()
        await _settle_silent_joins(session)
        add_client.assert_not_awaited()

        speaker.playback_state = PlaybackState.PLAYING
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=0))
        await _settle_silent_joins(session)

    add_client.assert_awaited_once_with(apple_tv)


@pytest.mark.asyncio
async def test_an_apple_tv_that_fails_to_join_is_added_once_more_and_never_fails_the_set() -> None:
    """The music plays on whatever the Apple TV does: it is added once more, then left out."""
    session, speaker, apple_tv = _make_set_session()

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.AIRPLAY_SILENT_MEMBER_REJOIN_DELAY",
            0,
        ),
        patch.object(session, "add_client", new_callable=AsyncMock) as add_client,
        patch.object(session, "stop", new_callable=AsyncMock) as stop,
    ):
        session._join_silent_members()
        await _settle_silent_joins(session)
        await _settle_silent_joins(session)

    assert add_client.await_args_list == [call(apple_tv), call(apple_tv)]
    stop.assert_not_awaited()
    assert session.sync_clients == [speaker]


@pytest.mark.asyncio
@pytest.mark.parametrize("rejoin", [True, False])
async def test_an_apple_tv_whose_stream_ended_leaves_the_music_playing(rejoin: bool) -> None:
    """An Apple TV that lost its stream leaves; it is added once more unless it was ended."""
    session, speaker, apple_tv = _make_set_session()
    session.sync_clients.append(apple_tv)
    session._silent_member_joins[apple_tv.protocol_id] = 1

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.AIRPLAY_SILENT_MEMBER_REJOIN_DELAY",
            0,
        ),
        patch.object(session, "stop_client", new_callable=AsyncMock),
        patch.object(session, "add_client", new_callable=AsyncMock) as add_client,
        patch.object(session, "stop", new_callable=AsyncMock) as stop,
    ):
        await session.remove_silent_member(apple_tv, rejoin=rejoin)
        await _settle_silent_joins(session)

    assert session.sync_clients == [speaker]
    stop.assert_not_awaited()
    assert add_client.await_count == (1 if rejoin else 0)


@pytest.mark.asyncio
async def test_a_session_with_only_the_apple_tv_left_ends() -> None:
    """Once its last speaker left, the session of a set ends: nothing of it is heard any more."""
    session, speaker, apple_tv = _make_set_session()
    session.sync_clients.append(apple_tv)

    with (
        patch.object(session, "stop_client", new_callable=AsyncMock),
        patch.object(session, "stop", new_callable=AsyncMock) as stop,
    ):
        await session.remove_client(speaker)

    stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_seek_and_resume_of_a_set_go_on_without_its_apple_tv() -> None:
    """
    A missing or failing Apple TV never sends a seek, next or resume of its set the cold way.

    It is not waited for either: its START follows the instant its speakers start at.
    """
    session, speaker, apple_tv = _make_set_session()
    # missing from the session: the speakers alone still replace warm
    assert session.can_replace([speaker], session.pcm_format)
    session.sync_clients.append(apple_tv)
    assert session.can_replace([speaker], session.pcm_format)
    apple_tv.stream.wait_audio_present = AsyncMock(return_value=False)
    apple_tv.stream.start = AsyncMock(side_effect=lambda at, _pos: at + 5000)
    speaker.stream.wait_audio_present = AsyncMock(return_value=True)

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "stop_client", new_callable=AsyncMock),
        patch(
            "music_assistant.providers.airplay.stream_session.cli_clock_time", return_value=100.0
        ),
    ):
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=10))
        # the speaker plays alone after the next one, whose flush the Apple TV fails
        apple_tv.stream.flush = AsyncMock(return_value=False)
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=20))

    speaker.stream.start.assert_any_await(100_000 + AIRPLAY_START_LEAD_MS, 10_000)
    assert speaker.stream.start.await_count == 2
    apple_tv.stream.start.assert_awaited_once_with(100_000 + AIRPLAY_START_LEAD_MS, 10_000)
    assert session.sync_clients == [speaker]


@pytest.mark.asyncio
async def test_a_set_parks_without_an_apple_tv_that_does_not_answer() -> None:
    """A pause of a set parks its speakers, whatever its Apple TV does."""
    session, speaker, apple_tv = _make_set_session()
    session.sync_clients.append(apple_tv)
    apple_tv.stream.send_cli_command = AsyncMock(return_value=False)

    with patch.object(session, "stop_client", new_callable=AsyncMock):
        assert await session.standby()

    speaker.stream.send_cli_command.assert_awaited_once_with("ACTION=STANDBY")
    assert session.sync_clients == [speaker]


@pytest.mark.asyncio
async def test_the_apple_tv_of_a_set_is_fed_silence() -> None:
    """What the Apple TV is fed is zeroed and leaves out the output settings of the set."""
    session, speaker, apple_tv = _make_set_session()
    mass: Any = session.mass
    mass.streams.audio.get_player_output_plan.return_value = MagicMock(filter_params=["dsp"])
    for player in (speaker, apple_tv):
        player.output_plan_protocol_id = "apappletv"
        player.stream.pcm_format = AIRPLAY_PCM_FORMAT
        player.stream._cli_proc.proc.stdin.transport.get_extra_info.return_value.fileno.return_value = 9

    with patch("music_assistant.providers.airplay.stream_session.FFMpeg") as ffmpeg_cls:
        ffmpeg_cls.return_value.start = AsyncMock()
        for player in (speaker, apple_tv):
            await session._start_player_ffmpeg(player, MagicMock(), player.stream)

    filters = [kwargs["filter_params"] for _, kwargs in ffmpeg_cls.call_args_list]
    assert filters == [["dsp"], ["volume=0"]]


_SILENT_BOUND = "music_assistant.providers.airplay.stream_session.AIRPLAY_SILENT_MEMBER_TIMEOUT"


async def _never(*_args: Any, **_kwargs: Any) -> Any:
    """Never answer, as an Apple TV that hangs does."""
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_an_apple_tv_that_takes_no_audio_never_holds_the_speakers_for_long() -> None:
    """The speakers are fed while the Apple TV does not take its audio; it leaves the session."""
    session, speaker, apple_tv = _make_set_session()
    session.sync_clients.append(apple_tv)
    session._started_silent_members.add(apple_tv.protocol_id)
    speaker_ffmpeg = MagicMock(closed=False, write=AsyncMock())
    session._player_ffmpeg = {
        speaker.protocol_id: speaker_ffmpeg,
        apple_tv.protocol_id: MagicMock(closed=False, write=AsyncMock(side_effect=_never)),
    }

    with (
        patch(_SILENT_BOUND, 0.05),
        patch.object(session, "_cleanup_after_removal", new_callable=AsyncMock),
    ):
        assert await asyncio.wait_for(session._write_chunk_to_all_players(b"\0" * 4), 1.0)

    speaker_ffmpeg.write.assert_awaited_once()
    assert session.sync_clients == [speaker]


@pytest.mark.asyncio
async def test_seek_and_pause_of_a_set_never_wait_long_on_an_apple_tv_that_does_not_answer() -> (
    None
):
    """An Apple TV that answers neither its flush, its START nor its park is left behind."""
    session, speaker, apple_tv = _make_set_session()
    session.sync_clients.append(apple_tv)
    speaker.stream.wait_audio_present = AsyncMock(return_value=True)
    for step in ("flush", "start", "send_cli_command"):
        setattr(apple_tv.stream, step, AsyncMock(side_effect=_never))

    with (
        patch(_SILENT_BOUND, 0.05),
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "stop_client", new_callable=AsyncMock),
    ):
        # once for the flush it does not answer, once for its START
        assert await asyncio.wait_for(session.replace(MagicMock(), MagicMock(elapsed_time=0)), 1)
        assert session.sync_clients == [speaker]
        apple_tv.stream.flush = AsyncMock(return_value=True)
        session.sync_clients.append(apple_tv)
        assert await asyncio.wait_for(session.replace(MagicMock(), MagicMock(elapsed_time=0)), 1)
        assert session.sync_clients == [speaker]
        session.sync_clients.append(apple_tv)
        assert await asyncio.wait_for(session.standby(), 1)

    assert speaker.stream.start.await_count == 2
    speaker.stream.send_cli_command.assert_awaited_once_with("ACTION=STANDBY")
    assert session.sync_clients == [speaker]


@pytest.mark.asyncio
async def test_an_apple_tv_left_out_while_it_joins_never_ends_up_in_the_session() -> None:
    """Switched to play on the HomePods only while the Apple TV joins, the join is called off."""
    session, speaker, apple_tv = _make_set_session()
    joining = asyncio.Event()

    async def join(player: Any) -> None:
        joining.set()
        await asyncio.sleep(0.05)
        session.sync_clients.append(player)

    with (
        patch(_SILENT_POLL, 0),
        patch.object(session, "add_client", side_effect=join),
        patch.object(session, "stop_client", new_callable=AsyncMock),
    ):
        session._join_silent_members()
        await asyncio.wait_for(joining.wait(), 1)
        apple_tv.silent_member = None
        await session.remove_silent_member(apple_tv, rejoin=False, reason="left out")
        await asyncio.sleep(0.1)

    assert session.sync_clients == [speaker]


@pytest.mark.asyncio
async def test_an_apple_tv_that_waits_for_its_start_is_not_taken_for_a_stalled_one() -> None:
    """
    In a seek or a resume the Apple TV backs up the feed until it is started, like any member.

    It is not dropped for that while the speakers start; once started, a write it does not take
    in time is a stall.
    """
    session, speaker, apple_tv = _make_set_session()
    session.sync_clients.append(apple_tv)
    # started in the cycle before this seek
    session._started_silent_members.add(apple_tv.protocol_id)
    speaker.stream.wait_audio_present = AsyncMock(return_value=True)

    async def backed_up(*_args: Any) -> None:
        await asyncio.sleep(0.2)

    def wire_ffmpegs() -> None:
        session._player_ffmpeg = {
            speaker.protocol_id: MagicMock(closed=False, write=AsyncMock(), kill=AsyncMock()),
            apple_tv.protocol_id: MagicMock(
                closed=False, write=AsyncMock(side_effect=backed_up), kill=AsyncMock()
            ),
        }

    async def speaker_start(at: int, *_args: Any) -> int:
        # the feed runs while the speakers start, the Apple TV not started yet
        wire_ffmpegs()
        assert await session._write_chunk_to_all_players(b"\0" * 4)
        return at

    speaker.stream.start = AsyncMock(side_effect=speaker_start)

    with (
        patch(_SILENT_BOUND, 0.05),
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "_cleanup_after_removal", new_callable=AsyncMock),
    ):
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=0))
        assert apple_tv in session.sync_clients
        assert apple_tv.protocol_id in session._started_silent_members
        wire_ffmpegs()
        assert await session._write_chunk_to_all_players(b"\0" * 4)

    assert session.sync_clients == [speaker]


def _late_join_stream(session: AirPlayStreamSession) -> MagicMock:
    """Let a late join in the session connect a stream mock, and return it."""
    session._shared_ptp_resolved = True
    stream = _stream_defaults(MagicMock(running=True, connected=True))
    stream.wait_for_connection = AsyncMock()

    async def start_client(player: Any, _use_shared_ptp: bool) -> None:
        player.stream = stream

    session._start_client = AsyncMock(side_effect=start_client)  # type: ignore[method-assign]
    return stream


def _plain_speaker() -> MagicMock:
    """Return a playing member of a session that is no speaker of a set."""
    plain = MagicMock(
        protocol_id="applain", renders_audio=True, playback_state=PlaybackState.PLAYING, owner=None
    )
    plain.stream.accepts_audio = True
    plain.stream.cumulative_shift_seconds = 0.0
    return plain


@pytest.mark.asyncio
async def test_a_join_cancelled_while_it_waits_for_the_feed_stops_the_apple_tv() -> None:
    """A join called off while it waits to map its anchor stops what it started."""
    session, _, apple_tv = _make_set_session()
    stream = _late_join_stream(session)

    async def take_the_lock(**_kwargs: Any) -> tuple[ClockReadiness, int]:
        # the feed holds the lock: the join waits for it after its connect
        await session._lock.acquire()
        return ClockReadiness.UNREPORTED, 0

    stream.wait_clock_ready = AsyncMock(side_effect=take_the_lock)

    with patch.object(session, "stop_client", new_callable=AsyncMock) as stop_client:
        join = asyncio.create_task(session.add_client(apple_tv))
        for _ in range(5):
            await asyncio.sleep(0)
        assert session._lock.locked()
        join.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await join
        session._lock.release()

    stop_client.assert_awaited_once_with(apple_tv, reason="late joiner start cancelled")


@pytest.mark.asyncio
async def test_an_apple_tv_whose_room_left_while_it_joined_stays_out() -> None:
    """The Apple TV does not end up in a session its room left while it joined."""
    session, speaker, apple_tv = _make_set_session()
    stream = _late_join_stream(session)

    async def room_leaves(at: int, *_args: Any, **_kwargs: Any) -> int:
        # the room left the group of a plain speaker, which plays on
        session.sync_clients.append(_plain_speaker())
        session.sync_clients.remove(speaker)
        return at

    stream.start = AsyncMock(side_effect=room_leaves)

    with patch.object(session, "stop_client", new_callable=AsyncMock) as stop_client:
        await session.add_client(apple_tv)

    assert apple_tv not in session.sync_clients
    stop_client.assert_awaited_once_with(apple_tv, reason="set left during late join")


@pytest.mark.asyncio
async def test_an_apple_tv_joins_again_with_its_room_that_joins_a_group_again() -> None:
    """The Apple TV that left a group with its room is taken along when the room is back."""
    session, speaker, apple_tv = _make_set_session()
    # the Apple TV joined the group with its room, and left it with the room
    session.sync_clients = [_plain_speaker()]
    session._silent_member_joins[apple_tv.protocol_id] = 1
    _late_join_stream(session)

    async def join(player: Any) -> None:
        session.sync_clients.append(player)

    with patch(_SILENT_POLL, 0):
        await session.add_client(speaker)
        with patch.object(session, "add_client", side_effect=join) as add_client:
            await _settle_silent_joins(session)

    add_client.assert_awaited_once_with(apple_tv)


@pytest.mark.asyncio
async def test_the_apple_tv_of_a_pair_joins_once_both_of_its_speakers_play() -> None:
    """
    The Apple TV of a set whose speakers join one after the other waits for the last one.

    An Apple TV whose SETUP goes out before the one of a speaker resets its session.
    """
    session, speaker, apple_tv = _make_set_session()
    second = MagicMock(protocol_id="apsecond", renders_audio=True)
    apple_tv.endpoints = [speaker, second]
    joined: list[Any] = []

    async def join(player: Any) -> None:
        joined.append(player)
        session.sync_clients.append(player)

    with patch(_SILENT_POLL, 0), patch.object(session, "add_client", side_effect=join):
        session._join_silent_members()
        for _ in range(5):
            await asyncio.sleep(0)
        assert joined == []
        session.sync_clients.append(second)
        await _settle_silent_joins(session)

    assert joined == [apple_tv]


@pytest.mark.asyncio
async def test_the_apple_tv_of_a_pair_joins_the_speaker_that_plays_once_the_other_stays_out() -> (
    None
):
    """A speaker that does not join keeps the Apple TV of its set out for a bounded time only."""
    session, speaker, apple_tv = _make_set_session()
    apple_tv.endpoints = [speaker, MagicMock(protocol_id="apsecond", renders_audio=True)]

    async def join(player: Any) -> None:
        session.sync_clients.append(player)

    with (
        patch(_SILENT_POLL, 0),
        patch(_SILENT_TIMEOUT, 0),
        patch.object(session, "add_client", side_effect=join) as add_client,
    ):
        session._join_silent_members()
        await _settle_silent_joins(session)

    add_client.assert_awaited_once_with(apple_tv)


@pytest.mark.asyncio
async def test_an_apple_tv_reset_by_a_speaker_that_joined_is_added_once_more() -> None:
    """
    A speaker that sets up resets the session of its Apple TV, which reports another sender.

    Right after a speaker joined, the Apple TV is added once more all the same; another sender
    taking it over later on is final.
    """
    session, speaker, apple_tv = _make_set_session()
    session.sync_clients.append(apple_tv)
    session._silent_member_joins[apple_tv.protocol_id] = 1

    async def join(player: Any) -> None:
        session.sync_clients.append(player)

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.AIRPLAY_SILENT_MEMBER_REJOIN_DELAY",
            0,
        ),
        patch(_SILENT_POLL, 0),
        patch.object(session, "stop_client", new_callable=AsyncMock),
        patch.object(session, "add_client", side_effect=join) as add_client,
    ):
        # the speaker heals back into the session the Apple TV plays in
        session._join_silent_members(joined=speaker)
        await session.end_silent_member(apple_tv, taken_over=True)
        await _settle_silent_joins(session)
        add_client.assert_awaited_once_with(apple_tv)

        session._speaker_joined_at[apple_tv.protocol_id] -= AIRPLAY_SILENT_MEMBER_RESET_WINDOW
        await session.end_silent_member(apple_tv, taken_over=True)
        await _settle_silent_joins(session)

    add_client.assert_awaited_once()
    assert session.sync_clients == [speaker]


@pytest.mark.asyncio
@pytest.mark.parametrize("missed", ["flush", "start"])
async def test_an_apple_tv_that_misses_a_flush_or_its_start_is_added_once_more(missed: str) -> None:
    """An Apple TV left behind by a seek, next or resume of its set is added once more after it."""
    session, speaker, apple_tv = _make_set_session()
    session.sync_clients.append(apple_tv)
    # it joined the session once already
    session._silent_member_joins[apple_tv.protocol_id] = 1
    speaker.stream.wait_audio_present = AsyncMock(return_value=True)
    if missed == "flush":
        apple_tv.stream.flush = AsyncMock(return_value=False)
    else:
        apple_tv.stream.start = AsyncMock(side_effect=_never)

    async def join(player: Any) -> None:
        session.sync_clients.append(player)

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.AIRPLAY_SILENT_MEMBER_REJOIN_DELAY",
            0,
        ),
        patch(_SILENT_BOUND, 0.05),
        patch(_SILENT_POLL, 0),
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "stop_client", new_callable=AsyncMock),
        patch.object(session, "add_client", side_effect=join) as add_client,
    ):
        assert await asyncio.wait_for(session.replace(MagicMock(), MagicMock(elapsed_time=0)), 1)
        await _settle_silent_joins(session)

    add_client.assert_awaited_once_with(apple_tv)


@pytest.mark.asyncio
async def test_an_apple_tv_that_misses_the_park_joins_again_once_the_music_plays_again() -> None:
    """An Apple TV that does not park along is out while its set is paused, and back after."""
    session, speaker, apple_tv = _make_set_session()
    session.sync_clients.append(apple_tv)
    session._silent_member_joins[apple_tv.protocol_id] = 1
    apple_tv.stream.send_cli_command = AsyncMock(return_value=False)
    speaker.stream.wait_audio_present = AsyncMock(return_value=True)

    async def join(player: Any) -> None:
        session.sync_clients.append(player)

    with (
        patch(_SILENT_POLL, 0),
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "stop_client", new_callable=AsyncMock),
        patch.object(session, "add_client", side_effect=join) as add_client,
    ):
        assert await session.standby()
        assert session.sync_clients == [speaker]
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=0))
        await _settle_silent_joins(session)

    add_client.assert_awaited_once_with(apple_tv)


@pytest.mark.asyncio
async def test_a_speaker_of_a_set_dropped_for_its_writes_is_lost_as_when_its_stream_dies() -> None:
    """
    A speaker whose audio write failed is lost as when its stream dies: it takes its set along.

    A set plays on all of its speakers or not at all. A player of its own is only dropped.
    """
    session, speaker, apple_tv = _make_set_session()
    second = MagicMock(protocol_id="apsecond", renders_audio=True, owner=apple_tv)
    second.stream = _stream_defaults(MagicMock(running=True, connected=True))
    second.stream.session = session
    plain = _plain_speaker()
    plain.owner = plain
    plain.speakers = []
    session.sync_clients += [second, plain]

    def ffmpeg(write: AsyncMock) -> MagicMock:
        return MagicMock(closed=False, write=write, kill=AsyncMock())

    session._player_ffmpeg = {
        speaker.protocol_id: ffmpeg(AsyncMock()),
        second.protocol_id: ffmpeg(AsyncMock(side_effect=OSError("broken pipe"))),
        plain.protocol_id: ffmpeg(AsyncMock(side_effect=OSError("broken pipe"))),
    }

    with patch.object(session, "_cleanup_after_removal", new_callable=AsyncMock):
        assert await session._write_chunk_to_all_players(b"\0" * 4)

    second.stream.handle_stream_loss.assert_called_once_with(rejoin=True)
    plain.stream.handle_stream_loss.assert_not_called()
    speaker.stream.handle_stream_loss.assert_not_called()
    assert session.sync_clients == [speaker]


@pytest.mark.asyncio
async def test_an_apple_tv_dropped_for_its_writes_is_added_once_more() -> None:
    """An Apple TV that stopped taking audio leaves the music playing and is added once more."""
    session, speaker, apple_tv = _make_set_session()
    session.sync_clients.append(apple_tv)
    session._silent_member_joins[apple_tv.protocol_id] = 1
    session._player_ffmpeg = {
        speaker.protocol_id: MagicMock(closed=False, write=AsyncMock(), kill=AsyncMock()),
        apple_tv.protocol_id: MagicMock(
            closed=False, write=AsyncMock(side_effect=TimeoutError()), kill=AsyncMock()
        ),
    }

    async def join(player: Any) -> None:
        session.sync_clients.append(player)

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.AIRPLAY_SILENT_MEMBER_REJOIN_DELAY",
            0,
        ),
        patch(_SILENT_POLL, 0),
        patch.object(session, "_cleanup_after_removal", new_callable=AsyncMock),
        patch.object(session, "add_client", side_effect=join) as add_client,
    ):
        assert await session._write_chunk_to_all_players(b"\0" * 4)
        await _settle_silent_joins(session)

    add_client.assert_awaited_once_with(apple_tv)


@pytest.mark.asyncio
@pytest.mark.parametrize("missed", ["flush", "start"])
async def test_an_apple_tv_that_misses_every_seek_comes_back_after_each(missed: str) -> None:
    """Each seek, next or resume the Apple TV misses gives it a retry of its own."""
    session, speaker, apple_tv = _make_set_session()
    session.sync_clients.append(apple_tv)
    # it joined the session once already
    session._silent_member_joins[apple_tv.protocol_id] = 1
    speaker.stream.wait_audio_present = AsyncMock(return_value=True)
    if missed == "flush":
        apple_tv.stream.flush = AsyncMock(return_value=False)
    else:
        apple_tv.stream.start = AsyncMock(side_effect=_never)

    async def join(player: Any) -> None:
        session.sync_clients.append(player)

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.AIRPLAY_SILENT_MEMBER_REJOIN_DELAY",
            0,
        ),
        patch(_SILENT_BOUND, 0.05),
        patch(_SILENT_POLL, 0),
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "stop_client", new_callable=AsyncMock),
        patch.object(session, "add_client", side_effect=join) as add_client,
    ):
        for _ in range(2):
            assert await asyncio.wait_for(
                session.replace(MagicMock(), MagicMock(elapsed_time=0)), 1
            )
            await _settle_silent_joins(session)

    assert add_client.await_args_list == [call(apple_tv), call(apple_tv)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["connect", "start"])
async def test_a_speaker_of_a_set_that_fails_its_late_join_takes_the_set_along(
    failing: str,
) -> None:
    """
    A speaker whose late join fails takes its set along, as a start of the set fails as a whole.

    It is not lined up to join again: a start that failed is not tried again either.
    """
    session, _, apple_tv = _make_set_session()
    second = MagicMock(protocol_id="apsecond", renders_audio=True, owner=apple_tv, sync_offset_ms=0)
    second.protocol = StreamingProtocol.RAOP
    stream = _late_join_stream(session)
    stream.session = session
    if failing == "connect":
        stream.wait_for_connection = AsyncMock(side_effect=OSError("connection refused"))
    else:
        stream.start = AsyncMock(side_effect=OSError("START rejected"))

    with patch.object(session, "stop_client", new_callable=AsyncMock) as stop_client:
        await session.add_client(second)

    assert second not in session.sync_clients
    stop_client.assert_awaited_once()
    stream.handle_stream_loss.assert_called_once_with(rejoin=False)


@pytest.mark.asyncio
async def test_a_speaker_back_in_the_music_of_its_set_that_fails_to_join_is_only_left_out() -> None:
    """
    A speaker that comes back while its set plays and cannot join leaves the set playing.

    The set plays on what is there, as when the speaker was not on the network.
    """
    session, speaker, apple_tv = _make_set_session()
    second = MagicMock(protocol_id="apsecond", renders_audio=True, owner=apple_tv, sync_offset_ms=0)
    second.protocol = StreamingProtocol.RAOP
    stream = _late_join_stream(session)
    stream.session = session
    stream.wait_for_connection = AsyncMock(side_effect=OSError("connection refused"))

    with patch.object(session, "stop_client", new_callable=AsyncMock) as stop_client:
        await session.add_client(second, returning=True)

    stop_client.assert_awaited_once()
    stream.handle_stream_loss.assert_not_called()
    assert session.sync_clients == [speaker]


@pytest.mark.asyncio
async def test_the_homepod_that_carries_a_pair_dropped_for_its_writes_takes_the_pair_along() -> (
    None
):
    """The HomePod that carries a stereo pair is a speaker of it: losing it stops the pair too."""
    session, speaker, _ = _make_set_session()
    carrier = _plain_speaker()
    carrier.owner = carrier
    carrier.speakers = [speaker]
    speaker.owner = carrier
    carrier.stream.session = session
    session.sync_clients.append(carrier)
    session._player_ffmpeg = {
        speaker.protocol_id: MagicMock(closed=False, write=AsyncMock(), kill=AsyncMock()),
        carrier.protocol_id: MagicMock(
            closed=False, write=AsyncMock(side_effect=OSError("broken pipe")), kill=AsyncMock()
        ),
    }

    with patch.object(session, "_cleanup_after_removal", new_callable=AsyncMock):
        assert await session._write_chunk_to_all_players(b"\0" * 4)

    carrier.stream.handle_stream_loss.assert_called_once_with(rejoin=True)


@pytest.mark.asyncio
async def test_an_apple_tv_that_fails_its_late_join_never_takes_its_room_along() -> None:
    """The Apple TV of a set is not heard: its failed join leaves its speakers playing."""
    session, speaker, apple_tv = _make_set_session()
    apple_tv.speakers = [speaker]
    stream = _late_join_stream(session)
    stream.session = session
    stream.wait_for_connection = AsyncMock(side_effect=OSError("connection refused"))

    with patch.object(session, "stop_client", new_callable=AsyncMock):
        await session.add_client(apple_tv)

    stream.handle_stream_loss.assert_not_called()
