"""Unit tests for AirPlay stream session late-join logic."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.airplay.constants import (
    AIRPLAY_CLOCK_READY_LEAD_MS,
    AIRPLAY_CLOCK_READY_TIMEOUT_MS,
    AIRPLAY_COLD_GROUP_START_LEAD_MS,
    AIRPLAY_GROUP_START_LEAD_MS,
    AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS,
    AIRPLAY_LATE_JOIN_RING_MARGIN_SECONDS,
    AIRPLAY_LATE_JOIN_RING_MAX_BYTES,
    AIRPLAY_LATE_JOIN_RING_MIN_SECONDS,
    AIRPLAY_SPLICE_LEAD_MARGIN_MS,
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
    stream.wait_clock_ready = AsyncMock(return_value=(ClockReadiness.UNREPORTED, 0))
    stream.warm_lead_ms = 0
    stream.flushed_head_unix_ms = 0
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

    pcm_format = MagicMock()
    pcm_format.pcm_sample_size = PCM_SAMPLE_SIZE
    pcm_format.sample_rate = 44100
    pcm_format.bit_depth = 16
    pcm_format.channels = 2

    leader = MagicMock()
    leader.player_id = "leader"
    leader.protocol = StreamingProtocol.RAOP
    # a joinable session has a reference member that is actually playing
    leader.playback_state = PlaybackState.PLAYING
    leader.stream = _stream_defaults(MagicMock())
    leader.stream.running = True
    leader.stream.connected = True
    leader.stream.wait_audio_present = AsyncMock(return_value=True)
    leader.stream.cumulative_shift_seconds = 0.0
    leader.config.get_value = MagicMock(return_value=0)

    session = AirPlayStreamSession(prov, [leader], pcm_format, MagicMock(elapsed_time=0))
    session.start_time = start_time
    session.seconds_streamed = seconds_streamed
    session.start_unix_ms = 1  # dummy

    return session


def _make_late_joiner() -> MagicMock:
    """Create a mock AirPlay player for late-join testing."""
    player = MagicMock()
    player.player_id = "late_joiner"
    player.protocol = StreamingProtocol.RAOP
    player.stream = None
    player.config = MagicMock()
    player.config.get_value = MagicMock(return_value=0)
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
    first_player.player_id = "first"
    first_player.protocol = first_protocol
    first_player.config.get_value = MagicMock(return_value=0)
    second_player = MagicMock(player_id="second")
    second_player.protocol = second_protocol
    second_player.config.get_value = MagicMock(return_value=0)
    session.sync_clients.append(second_player)
    session.media.elapsed_time = 12
    operations: list[str] = []

    async def start_client(player: MagicMock, _use_shared_ptp: bool) -> None:
        stream = _stream_defaults(MagicMock(running=True))

        async def wait_for_connection() -> None:
            operations.append(f"connected:{player.player_id}")

        async def start(start_unix_ms: int, position_ms: int) -> int:
            assert position_ms == 12_000
            operations.append(f"started:{player.player_id}:{start_unix_ms}")
            return start_unix_ms

        stream.wait_for_connection = AsyncMock(side_effect=wait_for_connection)
        stream.wait_audio_present = AsyncMock(return_value=True)
        stream.start = AsyncMock(side_effect=start)
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "_resolve_shared_ptp", new_callable=AsyncMock, return_value=False),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0),
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
    first.config.get_value = MagicMock(return_value=0)
    second = MagicMock(player_id="second", protocol=StreamingProtocol.AIRPLAY2)
    second.config.get_value = MagicMock(return_value=0)
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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
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
    player.config.get_value = MagicMock(return_value=0)

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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
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
    player.config.get_value = MagicMock(return_value=0)

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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        await session.start(MagicMock())

    assert player.stream.start.await_args.args[0] == int(now * 1000) + AIRPLAY_START_LEAD_MS


@pytest.mark.asyncio
async def test_group_start_that_never_converges_anchors_where_members_landed() -> None:
    """A group that keeps correcting is anchored at the members' own last instant."""
    session = _make_session(0, 0)
    first: Any = session.sync_clients[0]
    first.config.get_value = MagicMock(return_value=0)
    second = MagicMock(player_id="second")
    second.protocol = StreamingProtocol.RAOP
    second.config.get_value = MagicMock(return_value=0)
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
    player.config.get_value = MagicMock(return_value=0)
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
    second_player = MagicMock(player_id="silent", protocol=StreamingProtocol.RAOP)
    second_player.config.get_value = MagicMock(return_value=0)
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
    player.player_id = "solo"
    player.protocol = protocol
    player.config.get_value = MagicMock(return_value=0)
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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=200.0),
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
    second_player = MagicMock(player_id="second")
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
    second_player = MagicMock(player_id="second", display_name="Player B")
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
    second_player = MagicMock(player_id="second", display_name="Player B")
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
    player.player_id = "solo"
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
        player.player_id = f"player-{index}"
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
        player.player_id = f"player-{index}"
        player.protocol = protocol
        player.stream = _stream_defaults(MagicMock(running=True, connected=True))
        player.stream.send_cli_command = AsyncMock(return_value=True)
        players.append(player)
    session.sync_clients = players

    assert await session.standby()
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
        player.player_id = f"player-{index}"
        player.protocol = protocol
        player.config.get_value = MagicMock(return_value=0)
        player.stream = _stream_defaults(MagicMock(running=True, connected=True))
        player.stream.send_cli_command = AsyncMock(return_value=True)
        player.stream.wait_audio_present = AsyncMock(return_value=True)
        player.stream.flush = AsyncMock(return_value=True)
        players.append(player)
        original_streams[player.player_id] = player.stream
    session.sync_clients = players

    assert await session.standby()
    assert session.can_replace(players, session.pcm_format)
    media = MagicMock(elapsed_time=10)
    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0),
    ):
        assert await session.replace(MagicMock(), media)

    # start = now (100_000 ms) + solo/group start lead, position 10s
    expected_start = 100_000 + (
        AIRPLAY_START_LEAD_MS if len(protocols) == 1 else AIRPLAY_GROUP_START_LEAD_MS
    )
    for player in players:
        stream = original_streams[player.player_id]
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
        player = MagicMock(player_id=player_id, protocol=StreamingProtocol.AIRPLAY2)
        player.config.get_value = MagicMock(return_value=0)
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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0),
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
    player.config.get_value = MagicMock(return_value=0)
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
    session._player_ffmpeg[player.player_id] = old_ffmpeg

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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0),
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
        player = MagicMock(player_id=player_id, protocol=StreamingProtocol.RAOP)
        player.config.get_value = MagicMock(return_value=0)
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
    player.config.get_value = MagicMock(return_value=0)
    stream = player.stream
    stream.running = True
    stream.connected = True
    stream.flush = AsyncMock(return_value=True)
    stream.start = AsyncMock(side_effect=OSError("start failed"))

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0),
    ):
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=0)) is False


@pytest.mark.parametrize(
    ("member_specs", "expected_requirement_ms"),
    [
        # the largest member requirement wins, wherever that member sits
        (((4000, 0), (1500, 0)), 4000),
        (((1500, 0), (4000, 0)), 4000),
        # a negative sync_adjust widens that member's own requirement by exactly
        # the amount it moves that member's commanded instant earlier
        (((4000, 0), (3600, -600)), 4200),
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
    second = MagicMock(player_id="second", protocol=StreamingProtocol.AIRPLAY2)
    second.stream = _stream_defaults(MagicMock(running=True, connected=True))
    session.sync_clients.append(second)
    members.append(second)
    for player, (warm_lead_ms, adjust_ms) in zip(members, member_specs, strict=True):
        player.stream.warm_lead_ms = warm_lead_ms
        player.config.get_value = MagicMock(return_value=adjust_ms)

    with patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0):
        anchor = session._anchor_start_unix_ms(warm=True)

    assert anchor == 100_000 + expected_requirement_ms + AIRPLAY_SPLICE_LEAD_MARGIN_MS


def test_warm_anchor_clears_the_head_every_flushed_member_froze() -> None:
    """
    A warm re-anchor lands beyond the frozen head the flushed members reported.

    A member renders from its own commanded instant (the anchor plus its
    sync_adjust), so it is that instant, not the bare anchor, that has to clear
    the head the member froze at flush, with margin for the command round-trip.
    """
    session = _make_session(0, 0)
    first: Any = session.sync_clients[0]
    first.stream.flushed_head_unix_ms = 102_000
    first.config.get_value = MagicMock(return_value=0)
    second = MagicMock(player_id="second", protocol=StreamingProtocol.AIRPLAY2)
    second.stream = _stream_defaults(MagicMock(running=True, connected=True))
    second.stream.flushed_head_unix_ms = 105_000
    second.config.get_value = MagicMock(return_value=300)
    session.sync_clients.append(second)

    with patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0):
        anchor = session._anchor_start_unix_ms(warm=True)

    # the later head wins, and the offset its member adds comes back out of it
    assert anchor == 105_000 - 300 + AIRPLAY_SPLICE_LEAD_MARGIN_MS


@pytest.mark.asyncio
async def test_start_player_ffmpeg_wires_persistent_cli_stdin() -> None:
    """The per-seek ffmpeg is wired to the member's persistent cli stdin fd and tracked."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    old_ffmpeg = MagicMock()
    old_ffmpeg.close = AsyncMock()
    session._player_ffmpeg[player.player_id] = old_ffmpeg

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
        await session._start_player_ffmpeg(player, MagicMock())

    # the old ffmpeg is closed, never killing the shared cli stdin
    old_ffmpeg.close.assert_awaited_once()
    # the fresh ffmpeg writes into the cli process stdin fd (77)
    assert ffmpeg_factory.call_args.kwargs["audio_output"] == 77
    new_ffmpeg.start.assert_awaited_once()
    assert session._player_ffmpeg[player.player_id] is new_ffmpeg


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_state", ["missing", "stopped", "disconnected"])
async def test_standby_requires_every_member_running_and_connected(stream_state: str) -> None:
    """Standby is unavailable when any member lacks a reusable connected session."""
    session = _make_session(0, 0)
    ready_player = MagicMock(player_id="ready", protocol=StreamingProtocol.AIRPLAY2)
    ready_player.stream = _stream_defaults(MagicMock(running=True, connected=True))
    ready_player.stream.send_cli_command = AsyncMock()
    unavailable_player = MagicMock(player_id="unavailable", protocol=StreamingProtocol.RAOP)
    unavailable_player.stream = None
    if stream_state != "missing":
        unavailable_player.stream = MagicMock(
            running=stream_state != "stopped",
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
    delivered_player = MagicMock(player_id="delivered", protocol=StreamingProtocol.AIRPLAY2)
    delivered_player.stream = _stream_defaults(MagicMock(running=True, connected=True))
    delivered_player.stream.send_cli_command = AsyncMock(return_value=True)
    dropped_player = MagicMock(player_id="dropped", protocol=StreamingProtocol.RAOP)
    dropped_player.stream = _stream_defaults(MagicMock(running=True, connected=True))
    dropped_player.stream.send_cli_command = AsyncMock(return_value=False)
    pending_player = MagicMock(player_id="pending", protocol=StreamingProtocol.AIRPLAY2)
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
    player = MagicMock(player_id="failed", protocol=StreamingProtocol.AIRPLAY2)
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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    assert mock_start.called, "_start_client was never called"
    # the whole ring is primed, nothing is skipped, and the anchor maps to the
    # ring's first sample: start_at = start_time + (seconds_streamed - buffer)
    assert session._client_skip_bytes[player.player_id] == 0
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
    assert player.player_id not in session._client_skip_bytes
    player.stream.rebase_position.assert_not_called()
    stop_client.assert_awaited_once_with(player, reason="late joiner start/prime failed")


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
    leader.stream = _stream_defaults(MagicMock())
    leader.stream.running = False
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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
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
    assert session._client_skip_bytes[player.player_id] == 0
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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    # anchor is now + min_headroom and nothing is primed (position past the head)
    expected_headroom = AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000
    assert _captured_start_at(player) - now == pytest.approx(expected_headroom, abs=0.01)
    assert written_chunks == []
    skip_seconds = session._client_skip_bytes[player.player_id] / PCM_SAMPLE_SIZE
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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    expected_headroom = AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000
    assert _captured_start_at(player) - now == pytest.approx(expected_headroom, abs=0.01)
    assert session._client_skip_bytes[player.player_id] == 0
    assert written_chunks, "expected a prime write from the ring tail"
    primed_seconds = len(written_chunks[0]) / PCM_SAMPLE_SIZE
    assert primed_seconds == pytest.approx(1.699, abs=0.01)


@pytest.mark.asyncio
async def test_late_join_anchors_on_the_reported_clock_readiness() -> None:
    """A projected readiness instant anchors the join, just past the receiver's clock."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    session = _make_session(now - 5.0, 5.0)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 5)
    player = _make_late_joiner()
    # A cold receiver: its clock is projected usable 3.0s out, well past the floor.
    ready_at_unix_ms = int((now + 3.0) * 1000)

    def setup_with_projection(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()
        player.stream.wait_clock_ready = AsyncMock(
            return_value=(ClockReadiness.PROJECTED, ready_at_unix_ms)
        )

    with (
        patch.object(session, "_start_client", side_effect=setup_with_projection),
        patch.object(session, "_write_chunk_to_player", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        await session.add_client(player)

    expected_lead = AIRPLAY_CLOCK_READY_LEAD_MS / 1000
    assert _captured_start_at(player) - now == pytest.approx(3.0 + expected_lead, abs=0.01)
    player.stream.wait_clock_ready.assert_awaited_once_with(
        timeout=AIRPLAY_CLOCK_READY_TIMEOUT_MS / 1000
    )


@pytest.mark.asyncio
async def test_late_join_floor_wins_over_a_clock_that_is_already_ready() -> None:
    """A receiver whose clock is already locked still gets the join floor as its anchor."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    session = _make_session(now - 5.0, 5.0)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 5)
    player = _make_late_joiner()
    # A warm receiver reports a readiness instant that has already passed.
    ready_at_unix_ms = int((now - 1.0) * 1000)

    def setup_with_projection(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()
        player.stream.wait_clock_ready = AsyncMock(
            return_value=(ClockReadiness.PROJECTED, ready_at_unix_ms)
        )

    with (
        patch.object(session, "_start_client", side_effect=setup_with_projection),
        patch.object(session, "_write_chunk_to_player", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        await session.add_client(player)

    expected_headroom = AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000
    assert _captured_start_at(player) - now == pytest.approx(expected_headroom, abs=0.01)


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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        await session.add_client(player)

    assert player in session.sync_clients


@pytest.mark.asyncio
async def test_late_joiner_with_a_stalled_clock_is_not_added() -> None:
    """A receiver that never answered our clock renders silence, so keep it out of the group."""
    now = 1_000_000.0
    session = _make_session(now - 5.0, 5.0)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 5)
    player = _make_late_joiner()

    def setup_stalled(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()
        player.stream.wait_clock_ready = AsyncMock(return_value=(ClockReadiness.STALLED, 0))

    with (
        patch.object(session, "_start_client", side_effect=setup_stalled),
        patch.object(session, "_write_chunk_to_player", new_callable=AsyncMock),
        patch.object(session, "stop_client", new_callable=AsyncMock) as stop_client,
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        await session.add_client(player)

    player.stream.start.assert_not_awaited()
    assert player not in session.sync_clients
    stop_client.assert_awaited_once_with(player, reason="receiver clock stalled")


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
        writes.append((target.player_id, len(chunk)))

    with (
        patch.object(session, "_start_client", side_effect=setup_deferred_ack),
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
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
    skip_seconds = session._client_skip_bytes[player.player_id] / PCM_SAMPLE_SIZE
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
    adjust_ms = 200
    player.config.get_value = MagicMock(return_value=adjust_ms)
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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        await session.add_client(player)

    commanded_ms = int((now + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000) * 1000) + adjust_ms
    assert player.stream.start.await_args.args[0] == commanded_ms
    # The ack lands 1.5s later than commanded, so the due position is 9.0s of
    # feed instead of 7.5s: the joiner skips 4.0s of the live feed.
    assert written_chunks == []
    skip_seconds = session._client_skip_bytes[player.player_id] / PCM_SAMPLE_SIZE
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
    session._player_ffmpeg[player.player_id] = ffmpeg
    # skip 1.5s of a 1s-per-chunk feed
    session._client_skip_bytes[player.player_id] = PCM_SAMPLE_SIZE * 3 // 2

    chunk_one = b"\x01" * PCM_SAMPLE_SIZE
    chunk_two = b"\x02" * PCM_SAMPLE_SIZE
    chunk_three = b"\x03" * PCM_SAMPLE_SIZE
    for chunk in (chunk_one, chunk_two, chunk_three):
        await session._write_chunk_to_player(player, chunk)

    written = [call_args.args[0] for call_args in ffmpeg.write.await_args_list]
    # first chunk fully consumed, second chunk sliced in half, third whole
    assert written == [b"\x02" * (PCM_SAMPLE_SIZE // 2), chunk_three]
    assert session._client_skip_bytes[player.player_id] == 0


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
    session._client_skip_bytes[player.player_id] = 12_345

    await session.stop_client(player)

    assert player.player_id not in session._client_skip_bytes
    player.stream.reset_reanchor_shift.assert_called_once_with()
    player.stream.stop.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_replace_clears_skip_and_shift_state() -> None:
    """A warm replace re-anchors everyone, so skip counters and shifts reset."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.config.get_value = MagicMock(return_value=0)
    stream = player.stream
    stream.running = True
    stream.connected = True
    stream.flush = AsyncMock(return_value=True)
    stream.wait_audio_present = AsyncMock(return_value=True)
    session._client_skip_bytes[player.player_id] = 999

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0),
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
        written.append((target.player_id, chunk))

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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        await session.add_client(player)

    # The anchor is the one that was commanded: an acked instant is never moved.
    headroom = AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS / 1000
    assert _captured_start_at(player) == pytest.approx(now + headroom, abs=0.001)
    prime = next(chunk for pid, chunk in written if pid == player.player_id)
    # due is 102.5s of feed and the write head reached 112.0s, so 9.5s is owed
    # while only 8s survives in the ring: the missing 1.5s opens as silence.
    assert len(prime) / PCM_SAMPLE_SIZE == pytest.approx(9.5, abs=0.001)
    pad = int(1.5 * PCM_SAMPLE_SIZE)
    assert prime[:pad] == bytes(pad), "the missing head must be silence"
    assert set(prime[pad:]) == {1, 2}, "the rest must be the buffered feed"
    assert pad % session._pcm_frame_size == 0
    assert session._client_skip_bytes[player.player_id] == 0
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
        written.append((target.player_id, chunk))

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
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        await session.add_client(player)

    prime = next(chunk for pid, chunk in written if pid == player.player_id)
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

    with patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now):
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
    with patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now):
        session._observe_write_head_lead()
    assert session._peak_lead_seconds == 0.0
    assert session._pcm_buffer_max == int(AIRPLAY_LATE_JOIN_RING_MIN_SECONDS * PCM_SAMPLE_SIZE)

    unanchored = _make_session(0.0, 8.0)
    with patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now):
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
        written.append((target.player_id, chunk))

    def setup_with_stale_ack(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()
        # The binary reports an instant far behind the commanded one, mapping
        # the joiner back near the start of the session. 320s of head is owed.
        player.stream.start = AsyncMock(return_value=int((now - 300.0) * 1000))

    with (
        patch.object(session, "_start_client", side_effect=setup_with_stale_ack),
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        await session.add_client(player)

    prime = next(chunk for pid, chunk in written if pid == player.player_id)
    # Bounded at one ring of silence plus the ring itself - never the 320s owed.
    assert len(prime) / PCM_SAMPLE_SIZE == pytest.approx(4.0, abs=0.01)
    assert (session._pcm_total_fed - len(prime)) % session._pcm_frame_size == 0
    # The joiner cannot be placed exactly, so the log must not claim sync.
    logger.warning.assert_called_once()
    tail = logger.warning.call_args.args[-1]
    assert "ahead of the group" in tail
    assert "in sync" not in tail
