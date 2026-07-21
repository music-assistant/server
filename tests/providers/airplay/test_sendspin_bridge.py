"""
Unit tests for the Sendspin -> AirPlay bridge timing.

Cover three things, with the Sendspin clock mocked via ``ManualClock`` so the
tests are deterministic and independent of the host wall-clock:

* the clock-domain conversion turning a Sendspin audible instant (Sendspin's own
  monotonic clock) into the unix epoch ms the binary expects (``--start-unix-ms``);
* the start anchor: byte 0 is anchored to the first chunk Sendspin delivers, so a
  fresh track keeps position 0 and a late joiner lands at the group's live position;
* the write pacing that keeps the device buffered a bounded amount ahead of real
  time so a late-join catch-up backlog is not dumped into the CLI.
"""

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosendspin.clock import ManualClock
from aiosendspin.server.roles import AudioChunk

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


def _make_bridge(clock_now_us: int, wait_start_ms: int = AP2_LEAD_MS) -> SendspinAirPlayBridge:
    """Build a bridge with mocked provider/player/server and a ManualClock."""
    provider = MagicMock()
    provider.mass = MagicMock()
    airplay_player = MagicMock()
    airplay_player.player_id = "apc43875e9e53a"
    airplay_player.display_name = "Test Player"
    airplay_player.wait_start = wait_start_ms
    airplay_player.stream = None
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


@pytest.mark.asyncio
async def test_stop_streaming_awaits_writer_and_stream_teardown() -> None:
    """Bridge teardown does not return while its writer or AirPlay stream remains active."""
    bridge = _make_bridge(SENDSPIN_EPOCH_US)
    writer_cancelled = asyncio.Event()

    async def writer() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            writer_cancelled.set()

    async def stop_stream(*_args: Any, **_kwargs: Any) -> None:
        assert bridge.airplay_player.stream is stream

    writer_task = asyncio.create_task(writer())
    stream = MagicMock()
    stream.stop = AsyncMock(side_effect=stop_stream)
    bridge_state = cast("Any", bridge)
    bridge_state._writer_task = writer_task
    bridge_state._airplay_stream = stream
    bridge.airplay_player.stream = stream

    with patch.object(bridge.mass, "create_task", side_effect=asyncio.create_task):
        await bridge.stop_streaming()

    assert writer_cancelled.is_set()
    assert writer_task.done()
    stream.stop.assert_awaited_once_with(force=True)
    assert bridge_state._writer_task is None
    assert bridge_state._airplay_stream is None
    assert bridge.airplay_player.stream is None


@pytest.mark.asyncio
async def test_cleanup_does_not_cancel_replacement_writer() -> None:
    """A cleanup only tears down the bridge generation it captured."""
    bridge = _make_bridge(SENDSPIN_EPOCH_US)
    bridge_state = cast("Any", bridge)
    start_cancelled = asyncio.Event()
    release_start = asyncio.Event()

    async def old_start() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            start_cancelled.set()
            await release_start.wait()
            raise

    async def writer() -> None:
        await asyncio.Event().wait()

    old_start_task = asyncio.create_task(old_start())
    old_writer_task = asyncio.create_task(writer())
    old_stream = MagicMock()
    old_stream.stop = AsyncMock()
    bridge_state._airplay_stream_start_task = old_start_task
    bridge_state._writer_task = old_writer_task
    bridge_state._airplay_stream = old_stream
    bridge.airplay_player.stream = old_stream

    await asyncio.sleep(0)
    with patch.object(bridge.mass, "create_task", side_effect=asyncio.create_task):
        cleanup_task = bridge._schedule_cleanup()
        await start_cancelled.wait()

        replacement_writer = asyncio.create_task(writer())
        replacement_stream = MagicMock()
        replacement_stream.stop = AsyncMock()
        bridge_state._writer_task = replacement_writer
        bridge_state._airplay_stream = replacement_stream
        bridge.airplay_player.stream = replacement_stream
        release_start.set()
        await cleanup_task

    assert old_writer_task.cancelled()
    old_stream.stop.assert_awaited_once_with(force=True)
    assert not replacement_writer.done()
    replacement_stream.stop.assert_not_awaited()
    assert bridge_state._writer_task is replacement_writer
    assert bridge_state._airplay_stream is replacement_stream

    replacement_writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await replacement_writer


@pytest.mark.asyncio
async def test_protocol_start_only_waits_for_preceding_cleanup() -> None:
    """A cleanup scheduled after protocol start cannot become its dependency."""
    bridge = _make_bridge(SENDSPIN_EPOCH_US)
    bridge_state = cast("Any", bridge)
    release_previous_cleanup = asyncio.Event()

    async def previous_cleanup() -> None:
        await release_previous_cleanup.wait()

    async def start_stream(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.Event().wait()

    previous_cleanup_task = asyncio.create_task(previous_cleanup())
    bridge_state._cleanup_task = previous_cleanup_task
    cast("Any", bridge.airplay_player).start_stream = AsyncMock(side_effect=start_stream)
    stream = MagicMock()
    stream.stop = AsyncMock()

    with (
        patch.object(bridge.mass, "create_task", side_effect=asyncio.create_task),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=stream,
        ),
    ):
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + AP2_LEAD_MS * 1_000))
        protocol_start_task = bridge._airplay_stream_start_task
        assert protocol_start_task is not None
        cleanup_task = bridge._schedule_cleanup()
        release_previous_cleanup.set()
        await asyncio.wait_for(cleanup_task, timeout=1)

    assert protocol_start_task.done()


@pytest.mark.asyncio
async def test_stale_start_failure_does_not_cleanup_replacement() -> None:
    """A failed superseded start cannot schedule teardown for its replacement."""
    bridge = _make_bridge(SENDSPIN_EPOCH_US)
    bridge_state = cast("Any", bridge)
    start_entered = asyncio.Event()
    release_start = asyncio.Event()

    async def fail_start(*_args: Any, **_kwargs: Any) -> None:
        start_entered.set()
        await release_start.wait()
        raise RuntimeError("start failed")

    async def replacement_start() -> None:
        await asyncio.Event().wait()

    old_stream = MagicMock()
    old_stream.stop = AsyncMock()
    cast("Any", bridge.airplay_player).start_stream = AsyncMock(side_effect=fail_start)

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=old_stream,
        ),
        patch.object(bridge, "_schedule_cleanup") as schedule_cleanup,
    ):
        old_start_task = asyncio.create_task(bridge._start_protocol_from_chunk())
        bridge_state._airplay_stream_start_task = old_start_task
        await start_entered.wait()
        replacement_task = asyncio.create_task(replacement_start())
        bridge_state._airplay_stream_start_task = replacement_task
        release_start.set()
        await old_start_task

    schedule_cleanup.assert_not_called()
    assert not replacement_task.done()
    replacement_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await replacement_task


@pytest.mark.asyncio
async def test_stop_streaming_rejects_replacement_writer_during_cleanup() -> None:
    """An explicit bridge stop cannot return with a writer started during cleanup."""
    bridge = _make_bridge(SENDSPIN_EPOCH_US)
    bridge_state = cast("Any", bridge)
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()

    async def stop_stream(*_args: Any, **_kwargs: Any) -> None:
        stop_started.set()
        await release_stop.wait()

    stream = MagicMock()
    stream.stop = AsyncMock(side_effect=stop_stream)
    bridge_state._airplay_stream = stream
    bridge_state._stream_generation = object()
    bridge.airplay_player.stream = stream

    with patch.object(bridge.mass, "create_task", side_effect=asyncio.create_task):
        stop_task = asyncio.create_task(bridge.stop_streaming())
        await stop_started.wait()
        bridge._on_bridge_stream_start()

        assert bridge_state._writer_task is None
        assert bridge._is_streaming is False
        release_stop.set()
        await stop_task

    bridge._on_bridge_stream_start()
    assert bridge_state._writer_task is None
    assert bridge._is_streaming is False


@pytest.mark.asyncio
async def test_cancelled_bridge_stop_still_unregisters_client() -> None:
    """Cancelling bridge stop waits through Sendspin client unregistration."""
    bridge = _make_bridge(SENDSPIN_EPOCH_US)
    bridge_state = cast("Any", bridge)
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()

    async def stop_stream(*_args: Any, **_kwargs: Any) -> None:
        stop_started.set()
        await release_stop.wait()

    stream = MagicMock()
    stream.stop = AsyncMock(side_effect=stop_stream)
    bridge_state._airplay_stream = stream
    bridge_state._sendspin_client = MagicMock()
    bridge_state._bridge_client_id = "bridge-client"
    bridge.airplay_player.stream = stream
    sendspin_server = cast("Any", bridge.sendspin_server)
    sendspin_server.remove_client = AsyncMock()

    with patch.object(bridge.mass, "create_task", side_effect=asyncio.create_task):
        stop_task = asyncio.create_task(bridge.stop())
        await stop_started.wait()
        stop_task.cancel()
        await asyncio.sleep(0)
        release_stop.set()
        with pytest.raises(asyncio.CancelledError):
            await stop_task

    sendspin_server.remove_client.assert_awaited_once_with("bridge-client")
    assert bridge_state._sendspin_client is None
    assert bridge._stop_requests == 0
