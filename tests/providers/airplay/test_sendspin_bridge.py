"""
Unit tests for the Sendspin -> AirPlay bridge timing.

Cover four things, with the Sendspin clock mocked via ``ManualClock`` so the
tests are deterministic and independent of the host wall-clock:

* the clock-domain conversion turning a Sendspin audible instant (Sendspin's own
  monotonic clock) into the unix epoch ms used by the generation START command;
* the start anchor: byte 0 is anchored to the first chunk Sendspin delivers, so a
  fresh track keeps position 0 and a late joiner lands at the group's live position;
* the write pacing that keeps the device buffered a bounded amount ahead of real
  time so a late-join catch-up backlog is not dumped into the CLI;
* the warm handover: a running, connected stream is kept (not torn down) across
  a new Sendspin stream, and stages/commits a new generation on itself
  instead of a cold reconnect -- with prime-timeout and superseded-task fallback.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from aiosendspin.clock import ManualClock
from aiosendspin.server.roles import AudioChunk

from music_assistant.providers.airplay.constants import StreamingProtocol
from music_assistant.providers.airplay.sendspin_bridge import (
    MAX_DEVICE_BUFFER_SECONDS,
    SendspinAirPlayBridge,
    device_buffer_ahead_seconds,
    sendspin_audible_instant_to_unix_ms,
)
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BYTES_PER_SAMPLE,
    BRIDGE_CHANNELS,
    BRIDGE_SAMPLE_RATE,
)

BRIDGE_BYTES_PER_SECOND = BRIDGE_SAMPLE_RATE * BRIDGE_CHANNELS * BRIDGE_BYTES_PER_SAMPLE

# A large, arbitrary Sendspin monotonic-clock epoch (microseconds). Real
# monotonic clocks start from an unspecified point (e.g. host boot), so the
# conversion must never depend on this value.
SENDSPIN_EPOCH_US = 5_000_000_000_000  # ~57.8 days of monotonic uptime
UNIX_NOW_S = 1_784_000_000.0  # fixed unix wall-clock reading for the tests
AP2_LEAD_MS = 2500
RAOP_LEAD_MS = 1500


def _audible_instant_us(clock: ManualClock, wait_start_ms: int) -> int:
    """Return a sample Sendspin audible instant wait_start ahead of now (exercises the mapping)."""
    return clock.now_us() + wait_start_ms * 1_000


def _unix_at(sendspin_us: int) -> float:
    """Model a constant-offset, same-rate Sendspin<->unix relationship."""
    return UNIX_NOW_S + (sendspin_us - SENDSPIN_EPOCH_US) / 1_000_000


def test_maps_future_delta_to_unix_now_plus_lead() -> None:
    """An instant wait_start ahead maps to unix_now + wait_start (in ms)."""
    clock = ManualClock(now_us_value=SENDSPIN_EPOCH_US)
    drop_until = _audible_instant_us(clock, AP2_LEAD_MS)

    start_unix_ms = sendspin_audible_instant_to_unix_ms(drop_until, clock.now_us(), UNIX_NOW_S)

    assert start_unix_ms == int(UNIX_NOW_S * 1000) + AP2_LEAD_MS


def test_standing_clock_offset_cancels_out() -> None:
    """
    The absolute Sendspin epoch must not affect the result.

    Two wildly different monotonic epochs, with the same future delta and the
    same unix reading, must yield the exact same start instant. This is what
    makes the naive ``now/now`` subtraction correct: only the delta transfers
    between the clocks, so any standing offset cancels.
    """
    clock_a = ManualClock(now_us_value=SENDSPIN_EPOCH_US)
    clock_b = ManualClock(now_us_value=SENDSPIN_EPOCH_US + 987_654_321_000)

    result_a = sendspin_audible_instant_to_unix_ms(
        _audible_instant_us(clock_a, AP2_LEAD_MS), clock_a.now_us(), UNIX_NOW_S
    )
    result_b = sendspin_audible_instant_to_unix_ms(
        _audible_instant_us(clock_b, AP2_LEAD_MS), clock_b.now_us(), UNIX_NOW_S
    )

    assert result_a == result_b


def test_derived_start_equals_sendspin_audible_instant_in_unix() -> None:
    """
    The derived start lands on the unix time that coincides with the Sendspin instant.

    Models the two clocks as running at the same rate with a constant offset
    (unix = anchor + (sendspin_us - epoch)/1e6). The bridge only ever reads the
    two clocks together, so the result must land exactly on the unix time that
    coincides with the Sendspin audible instant, for any offset and any wait_start.
    """
    for wait_start_ms in (RAOP_LEAD_MS, AP2_LEAD_MS):
        clock = ManualClock(now_us_value=SENDSPIN_EPOCH_US)
        drop_until = _audible_instant_us(clock, wait_start_ms)
        # Some real time passes between setting the anchor and starting the CLI.
        clock.advance_us(40_000)  # 40 ms of setup churn (cleanup, task hop)
        sendspin_now = clock.now_us()
        unix_now = _unix_at(sendspin_now)

        start_unix_ms = sendspin_audible_instant_to_unix_ms(drop_until, sendspin_now, unix_now)

        assert start_unix_ms == int(_unix_at(drop_until) * 1000)


def test_scheduling_gap_between_reads_shrinks_lead_not_target() -> None:
    """
    A gap before CLI start shrinks the remaining lead but keeps the audible instant fixed.

    Computing the anchor immediately vs after a 400 ms gap must resolve to the
    same unix instant, because the future delta is recomputed against the same
    (advanced) Sendspin clock and unix reading.
    """
    clock = ManualClock(now_us_value=SENDSPIN_EPOCH_US)
    drop_until = _audible_instant_us(clock, AP2_LEAD_MS)

    immediate = sendspin_audible_instant_to_unix_ms(drop_until, clock.now_us(), UNIX_NOW_S)

    gap_s = 0.4
    clock.advance_us(int(gap_s * 1_000_000))
    delayed = sendspin_audible_instant_to_unix_ms(drop_until, clock.now_us(), UNIX_NOW_S + gap_s)

    assert immediate == delayed
    # And the remaining lead really did shrink by the gap.
    remaining_lead_ms = delayed - int((UNIX_NOW_S + gap_s) * 1000)
    assert remaining_lead_ms == AP2_LEAD_MS - int(gap_s * 1000)


def test_anchor_already_in_the_past_maps_to_a_past_unix_instant() -> None:
    """
    An audible instant behind 'now' yields a unix ms before the unix reading.

    This is the setup-outran-the-lead edge case: the value is still a faithful
    projection (negative lead), which the caller detects to warn about a late
    start rather than silently mis-anchoring.
    """
    clock = ManualClock(now_us_value=SENDSPIN_EPOCH_US)
    audible_in_the_past = clock.now_us() - 300_000  # 300 ms ago

    start_unix_ms = sendspin_audible_instant_to_unix_ms(
        audible_in_the_past, clock.now_us(), UNIX_NOW_S
    )

    assert start_unix_ms == int(UNIX_NOW_S * 1000) - 300
    assert start_unix_ms < int(UNIX_NOW_S * 1000)


# --- Start anchor: fresh keeps position 0, late join lands at live position ---


def _make_bridge(
    clock_now_us: int,
    wait_start_ms: int = AP2_LEAD_MS,
    protocol: StreamingProtocol = StreamingProtocol.AIRPLAY2,
) -> SendspinAirPlayBridge:
    """Build a bridge with mocked provider/player/server and a ManualClock."""
    provider = MagicMock()
    provider.mass = MagicMock()
    airplay_player = MagicMock()
    airplay_player.player_id = "apc43875e9e53a"
    airplay_player.display_name = "Test Player"
    airplay_player.wait_start = wait_start_ms
    airplay_player.protocol = protocol
    sendspin_server = MagicMock()
    sendspin_server.clock = ManualClock(now_us_value=clock_now_us)
    bridge = SendspinAirPlayBridge(provider, airplay_player, sendspin_server)
    bridge._is_streaming = True
    return bridge


def _pcm_chunk(timestamp_us: int, duration_us: int = 100_000) -> AudioChunk:
    """Build a silent PCM AudioChunk at a Sendspin timestamp."""
    frames = int(duration_us * BRIDGE_SAMPLE_RATE / 1_000_000)
    data = b"\x00" * (frames * BRIDGE_CHANNELS * BRIDGE_BYTES_PER_SAMPLE)
    return AudioChunk(
        data=data, timestamp_us=timestamp_us, duration_us=duration_us, byte_count=len(data)
    )


def test_fresh_start_anchors_to_first_chunk_and_keeps_intro() -> None:
    """
    A fresh track's opening is kept: byte 0 anchors to the first chunk, not now+wait_start.

    Models the clip scenario where the first delivered chunk (file position 0)
    is scheduled earlier than ``clock.now() + wait_start``. Anchoring to
    ``now + wait_start`` (the old behaviour) would drop everything before it -- the
    intro. The chunk timestamp must win, and its audio must be queued, not dropped.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    now_plus_wait_start = SENDSPIN_EPOCH_US + AP2_LEAD_MS * 1_000
    first_chunk_ts = SENDSPIN_EPOCH_US + 250_000  # position 0, only 250 ms ahead of now
    assert first_chunk_ts < now_plus_wait_start

    with patch.object(bridge, "_start_protocol_from_chunk", MagicMock()):
        bridge._on_audio_chunk(_pcm_chunk(first_chunk_ts))

    assert bridge._drop_until_us == first_chunk_ts
    assert bridge._start_aligned is True
    assert not bridge._write_queue.empty()  # opening audio queued, not discarded


def test_late_join_anchors_to_catchup_target_live_position() -> None:
    """
    A late joiner lands at the group's current position, not at track zero.

    After minutes of playback the first delivered chunk is the catch-up target
    (playhead + wait_start), far from the track start. The anchor must follow that
    chunk so the joiner maps onto the live timeline instead of restarting at 0.
    """
    playhead_us = SENDSPIN_EPOCH_US + 600_000_000  # 600 s into the session
    bridge = _make_bridge(clock_now_us=playhead_us)
    catchup_target_ts = playhead_us + AP2_LEAD_MS * 1_000

    with patch.object(bridge, "_start_protocol_from_chunk", MagicMock()):
        bridge._on_audio_chunk(_pcm_chunk(catchup_target_ts))

    assert bridge._drop_until_us == catchup_target_ts
    # The anchor tracks the advanced playhead, not a fresh now+wait_start-from-zero.
    assert bridge._drop_until_us > SENDSPIN_EPOCH_US + AP2_LEAD_MS * 1_000


# --- Write pacing: bound the device buffer so a catch-up backlog is not dumped ---


def test_device_buffer_ahead_seconds_tracks_write_cursor() -> None:
    """The buffered-ahead measure follows byte 0 = start anchor, +1 s per second written."""
    start_unix_ms = 1_784_000_000_000
    now = start_unix_ms / 1000

    assert device_buffer_ahead_seconds(start_unix_ms, 0, BRIDGE_BYTES_PER_SECOND, now) == 0.0
    one_second = BRIDGE_BYTES_PER_SECOND
    ahead = device_buffer_ahead_seconds(start_unix_ms, one_second, BRIDGE_BYTES_PER_SECOND, now)
    assert abs(ahead - 1.0) < 1e-9


def test_late_join_backlog_trips_pacing_bound_but_steady_feed_does_not() -> None:
    """A ~27 s catch-up backlog exceeds the bound; a few seconds of steady audio stays under it."""
    start_unix_ms = 1_784_000_000_000
    now = start_unix_ms / 1000

    backlog_ahead = device_buffer_ahead_seconds(
        start_unix_ms, 27 * BRIDGE_BYTES_PER_SECOND, BRIDGE_BYTES_PER_SECOND, now
    )
    assert backlog_ahead > MAX_DEVICE_BUFFER_SECONDS

    steady_ahead = device_buffer_ahead_seconds(
        start_unix_ms, 3 * BRIDGE_BYTES_PER_SECOND, BRIDGE_BYTES_PER_SECOND, now
    )
    assert steady_ahead < MAX_DEVICE_BUFFER_SECONDS


async def test_failed_cli_write_does_not_advance_pacing_cursor() -> None:
    """A dropped write cannot move the pacing cursor past audio the CLI never received."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = MagicMock()
    stream.write_audio = AsyncMock(side_effect=[OSError("write failed"), None])
    stream.write_audio_eof = AsyncMock()
    bridge._airplay_stream = stream
    bridge._airplay_stream_ready.set()
    bridge._start_unix_ms = int(UNIX_NOW_S * 1000)
    bridge._write_queue.put_nowait(b"first")
    bridge._write_queue.put_nowait(b"second")
    bridge._write_queue.put_nowait(None)

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.device_buffer_ahead_seconds",
        return_value=0.0,
    ) as buffer_ahead:
        await bridge._cli_writer()

    assert [call.args[1] for call in buffer_ahead.call_args_list] == [0, 0]
    assert stream.write_audio.await_count == 2


# --- Commanded cold start and warm handover ------------------------------------


async def test_cold_start_connects_then_prepares_and_starts_generation_zero() -> None:
    """A fresh bridge stream commands generation 0 only after the CLI connects."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._drop_until_us = SENDSPIN_EPOCH_US + AP2_LEAD_MS * 1_000
    bridge._airplay_stream_start_task = asyncio.current_task()
    stream = MagicMock()
    stream.start = AsyncMock()
    stream.wait_for_connection = AsyncMock()
    stream.prepare_generation = AsyncMock()
    stream.wait_generation_primed = AsyncMock(return_value=True)
    stream.start_generation = AsyncMock()

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.time.time",
            return_value=UNIX_NOW_S,
        ),
    ):
        await bridge._start_protocol_from_chunk()

    stream.start.assert_awaited_once_with()
    stream.wait_for_connection.assert_awaited_once_with()
    stream.prepare_generation.assert_awaited_once_with(0, "-", 0)
    stream.wait_generation_primed.assert_awaited_once_with(0)
    stream.start_generation.assert_awaited_once_with(0, 0, int(UNIX_NOW_S * 1000) + AP2_LEAD_MS)
    assert bridge._airplay_stream is stream
    assert bridge.airplay_player.stream is stream
    assert bridge._generation_started is True


# --- Warm handover: a kept stream survives a new stream start and absorbs a generation ---


def _make_kept_stream(*, running: bool = True, connected: bool = True) -> MagicMock:
    """Build a mock AirPlayStream reporting the given running/connected state."""
    stream = MagicMock()
    stream.running = running
    stream.connected = connected
    return stream


def test_on_bridge_stream_start_keeps_warm_eligible_stream() -> None:
    """A running, connected AirPlay 2 stream survives a new Sendspin stream start."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = _make_kept_stream()
    bridge._airplay_stream = kept_stream
    bridge.airplay_player.stream = kept_stream
    bridge._generation_started = True

    bridge._on_bridge_stream_start()

    assert bridge._airplay_stream is kept_stream
    assert bridge.airplay_player.stream is kept_stream
    assert bridge._stream_is_warm_eligible()


def test_on_bridge_stream_start_keeps_raop_stream() -> None:
    """A committed legacy RAOP stream is eligible for warm Sendspin generations."""
    bridge = _make_bridge(
        clock_now_us=SENDSPIN_EPOCH_US,
        wait_start_ms=RAOP_LEAD_MS,
        protocol=StreamingProtocol.RAOP,
    )
    old_stream = _make_kept_stream()
    bridge._airplay_stream = old_stream
    bridge.airplay_player.stream = old_stream
    bridge._generation_started = True

    bridge._on_bridge_stream_start()

    assert bridge._airplay_stream is old_stream
    assert bridge.airplay_player.stream is old_stream


def test_sendspin_callbacks_keep_raop_stream_until_warm_handover() -> None:
    """Both Sendspin start callbacks preserve a reusable legacy RAOP session."""
    bridge = _make_bridge(
        clock_now_us=SENDSPIN_EPOCH_US,
        wait_start_ms=RAOP_LEAD_MS,
        protocol=StreamingProtocol.RAOP,
    )
    kept_stream = _make_kept_stream()
    bridge._airplay_stream = kept_stream
    bridge.airplay_player.stream = kept_stream
    bridge._generation_started = True

    bridge._on_stream_start(MagicMock())
    bridge._on_bridge_stream_start()

    assert bridge._airplay_stream is kept_stream
    assert bridge.airplay_player.stream is kept_stream


def test_on_bridge_stream_start_replaces_uncommitted_stream() -> None:
    """A connected stream cannot be retained before generation 0 has started."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    old_stream = _make_kept_stream()
    bridge._airplay_stream = old_stream
    bridge.airplay_player.stream = old_stream

    bridge._on_bridge_stream_start()

    assert bridge._airplay_stream is None
    assert bridge.airplay_player.stream is None  # type: ignore[unreachable]


def test_on_stream_start_keeps_warm_eligible_stream() -> None:
    """The Sendspin-server-side stream-start callback also keeps a warm-eligible stream."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = _make_kept_stream()
    bridge._airplay_stream = kept_stream
    bridge.airplay_player.stream = kept_stream
    bridge._generation_started = True

    bridge._on_stream_start(MagicMock())

    assert bridge._airplay_stream is kept_stream
    assert bridge.airplay_player.stream is kept_stream


async def test_warm_generation_commits_on_kept_stream_instance() -> None:
    """A warm handover stages and commits a new generation on the same stream instance."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = MagicMock()
    kept_stream.next_generation = MagicMock(return_value=3)
    kept_stream.prepare_generation = AsyncMock()
    kept_stream.wait_generation_primed = AsyncMock(return_value=True)
    kept_stream.start_generation = AsyncMock()
    bridge._airplay_stream = kept_stream
    bridge._airplay_stream_start_task = asyncio.current_task()
    expected_fifo = f"/tmp/ma-bridge-{bridge.airplay_player.player_id}-3.pcm"  # noqa: S108

    with (
        patch("music_assistant.providers.airplay.sendspin_bridge.os.unlink"),
        patch("music_assistant.providers.airplay.sendspin_bridge.os.mkfifo"),
        patch("music_assistant.providers.airplay.sendspin_bridge.os.open", return_value=99),
    ):
        committed = await bridge._start_warm_generation(kept_stream, 1_784_000_000_000)

    assert committed is True
    assert bridge._airplay_stream is kept_stream  # no new instance was built
    kept_stream.prepare_generation.assert_awaited_once_with(3, expected_fifo, 0)
    kept_stream.wait_generation_primed.assert_awaited_once_with(3)
    kept_stream.start_generation.assert_awaited_once_with(3, 0, 1_784_000_000_000)
    assert bridge._sink_fd == 99
    assert bridge._airplay_stream_ready.is_set()


async def test_warm_generation_prime_timeout_falls_back_to_cold_restart() -> None:
    """A prime timeout never commits and reverts the writer to the stdin sink."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = MagicMock()
    kept_stream.next_generation = MagicMock(return_value=1)
    kept_stream.prepare_generation = AsyncMock()
    kept_stream.wait_generation_primed = AsyncMock(return_value=False)
    kept_stream.start_generation = AsyncMock()
    bridge._airplay_stream = kept_stream
    bridge._airplay_stream_start_task = asyncio.current_task()

    with (
        patch("music_assistant.providers.airplay.sendspin_bridge.os.unlink"),
        patch("music_assistant.providers.airplay.sendspin_bridge.os.mkfifo"),
        patch("music_assistant.providers.airplay.sendspin_bridge.os.open", return_value=55),
        patch("music_assistant.providers.airplay.sendspin_bridge.os.close") as mock_close,
    ):
        committed = await bridge._start_warm_generation(kept_stream, 1_784_000_000_000)

    assert committed is False
    kept_stream.start_generation.assert_not_awaited()
    assert bridge._sink_fd is None  # reverted so the cold-path writer uses stdin again
    mock_close.assert_any_call(55)


async def test_warm_generation_superseded_before_commit_does_not_start() -> None:
    """If a newer stream start already owns the bridge, the stale attempt never commits."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = MagicMock()
    kept_stream.next_generation = MagicMock(return_value=2)
    kept_stream.prepare_generation = AsyncMock()
    kept_stream.wait_generation_primed = AsyncMock(return_value=True)
    kept_stream.start_generation = AsyncMock()
    bridge._airplay_stream = kept_stream
    # Simulate a newer stream start having already replaced the tracked task.
    bridge._airplay_stream_start_task = MagicMock()

    with (
        patch("music_assistant.providers.airplay.sendspin_bridge.os.unlink"),
        patch("music_assistant.providers.airplay.sendspin_bridge.os.mkfifo"),
        patch("music_assistant.providers.airplay.sendspin_bridge.os.open", return_value=77),
        patch("music_assistant.providers.airplay.sendspin_bridge.os.close") as mock_close,
    ):
        committed = await bridge._start_warm_generation(kept_stream, 1_784_000_000_000)

    assert committed is False
    kept_stream.start_generation.assert_not_awaited()
    # The stale attempt must NOT close the fd: it was published as self._sink_fd,
    # so closing it here would break the writer (EBADF) or double-close a fd a
    # newer task already replaced. Its lifecycle is owned by _set_sink_fd/teardown.
    assert all(call.args[0] != 77 for call in mock_close.call_args_list)
    assert bridge._sink_fd == 77
