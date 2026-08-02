"""
Unit tests for the Sendspin -> AirPlay bridge timing.

Cover seven things, with the Sendspin clock mocked via ``ManualClock`` so the
tests are deterministic and independent of the host wall-clock:

* the clock-domain conversion turning a Sendspin audible instant (Sendspin's own
  monotonic clock) into the unix epoch ms used by the START command, and back;
* the startup lead reported to Sendspin, which decides how far ahead of the
  audible instant it schedules the first chunk;
* the start anchor: byte 0 is anchored to the first chunk Sendspin delivers, so a
  fresh track keeps position 0 and a late joiner lands at the group's live position;
* anchoring against the binary: the commanded instant honours the join headroom,
  the receiver's clock-ready projection and the content Sendspin already
  scheduled, and the content is then mapped onto the instant the binary acked;
* the timeline alignment that keeps every chunk at the byte offset its timestamp
  claims, so a discontinuity in the Sendspin timeline does not shift the device
  off the group's clock for the rest of the stream;
* the write pacing that keeps the device buffered a bounded amount ahead of real
  time so a late-join catch-up backlog is not dumped into the CLI;
* the warm handover: a running, connected stream is kept (not torn down) across
  a new Sendspin stream and rides the persistent-stdin flush-refill (FLUSH +
  re-anchoring START) instead of a cold reconnect -- with flush-timeout and
  superseded-task fallback, and the supersession handling that keeps a stale
  start from tearing down the stream a newer one owns.
"""

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosendspin.clock import ManualClock
from aiosendspin.server.roles import AudioChunk

from music_assistant.providers.airplay.constants import (
    AIRPLAY_CLOCK_READY_LEAD_MS,
    AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS,
    AIRPLAY_SPLICE_LEAD_MARGIN_MS,
    StreamingProtocol,
)
from music_assistant.providers.airplay.sendspin_bridge import (
    BRIDGE_COLD_START_LEAD_MS,
    BRIDGE_MIN_BUFFER_MS,
    BRIDGE_WARM_START_LEAD_MS,
    MAX_DEVICE_BUFFER_SECONDS,
    MAX_HELD_AUDIO_US,
    MAX_HELD_CHUNKS,
    SendspinAirPlayBridge,
    device_buffer_ahead_seconds,
    sendspin_audible_instant_to_unix_ms,
    unix_ms_to_sendspin_audible_instant,
)
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BYTES_PER_SAMPLE,
    BRIDGE_CHANNELS,
    BRIDGE_SAMPLE_RATE,
)

BRIDGE_BYTES_PER_SECOND = BRIDGE_SAMPLE_RATE * BRIDGE_CHANNELS * BRIDGE_BYTES_PER_SAMPLE
BRIDGE_BYTES_PER_FRAME = BRIDGE_CHANNELS * BRIDGE_BYTES_PER_SAMPLE

# A large, arbitrary Sendspin monotonic-clock epoch (microseconds). Real
# monotonic clocks start from an unspecified point (e.g. host boot), so the
# conversion must never depend on this value.
SENDSPIN_EPOCH_US = 5_000_000_000_000  # ~57.8 days of monotonic uptime
UNIX_NOW_S = 1_784_000_000.0  # fixed unix wall-clock reading for the tests
UNIX_NOW_MS = int(UNIX_NOW_S * 1000)
COLD_LEAD_MS = BRIDGE_COLD_START_LEAD_MS
WARM_LEAD_MS = BRIDGE_WARM_START_LEAD_MS


def _audible_instant_us(clock: ManualClock, lead_ms: int) -> int:
    """Return a sample Sendspin audible instant that far ahead of now (exercises the mapping)."""
    return clock.now_us() + lead_ms * 1_000


def _unix_at(sendspin_us: int) -> float:
    """Model a constant-offset, same-rate Sendspin<->unix relationship."""
    return UNIX_NOW_S + (sendspin_us - SENDSPIN_EPOCH_US) / 1_000_000


def test_maps_future_delta_to_unix_now_plus_lead() -> None:
    """An instant a lead ahead maps to unix_now + that lead (in ms)."""
    clock = ManualClock(now_us_value=SENDSPIN_EPOCH_US)
    drop_until = _audible_instant_us(clock, COLD_LEAD_MS)

    start_unix_ms = sendspin_audible_instant_to_unix_ms(drop_until, clock.now_us(), UNIX_NOW_S)

    assert start_unix_ms == int(UNIX_NOW_S * 1000) + COLD_LEAD_MS


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
        _audible_instant_us(clock_a, COLD_LEAD_MS), clock_a.now_us(), UNIX_NOW_S
    )
    result_b = sendspin_audible_instant_to_unix_ms(
        _audible_instant_us(clock_b, COLD_LEAD_MS), clock_b.now_us(), UNIX_NOW_S
    )

    assert result_a == result_b


def test_derived_start_equals_sendspin_audible_instant_in_unix() -> None:
    """
    The derived start lands on the unix time that coincides with the Sendspin instant.

    Models the two clocks as running at the same rate with a constant offset
    (unix = anchor + (sendspin_us - epoch)/1e6). The bridge only ever reads the
    two clocks together, so the result must land exactly on the unix time that
    coincides with the Sendspin audible instant, for any offset and any lead.
    """
    for lead_ms in (WARM_LEAD_MS, COLD_LEAD_MS):
        clock = ManualClock(now_us_value=SENDSPIN_EPOCH_US)
        drop_until = _audible_instant_us(clock, lead_ms)
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
    drop_until = _audible_instant_us(clock, COLD_LEAD_MS)

    immediate = sendspin_audible_instant_to_unix_ms(drop_until, clock.now_us(), UNIX_NOW_S)

    gap_s = 0.4
    clock.advance_us(int(gap_s * 1_000_000))
    delayed = sendspin_audible_instant_to_unix_ms(drop_until, clock.now_us(), UNIX_NOW_S + gap_s)

    assert immediate == delayed
    # And the remaining lead really did shrink by the gap.
    remaining_lead_ms = delayed - int((UNIX_NOW_S + gap_s) * 1000)
    assert remaining_lead_ms == COLD_LEAD_MS - int(gap_s * 1000)


def test_anchor_already_in_the_past_maps_to_a_past_unix_instant() -> None:
    """
    An audible instant behind 'now' yields a unix ms before the unix reading.

    This is the setup-outran-the-lead edge case: the value stays a faithful
    projection (negative lead) rather than being clamped here, so the anchor
    math can see it and raise the start to the join floor itself.
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
    protocol: StreamingProtocol = StreamingProtocol.AIRPLAY2,
    sync_adjust: int = 0,
) -> SendspinAirPlayBridge:
    """Build a bridge with mocked provider/player/server and a ManualClock."""
    provider = MagicMock()
    provider.mass = MagicMock()
    airplay_player = MagicMock()
    airplay_player.player_id = "apc43875e9e53a"
    airplay_player.display_name = "Test Player"
    airplay_player.protocol = protocol
    # A real int: the anchor math guards sync_adjust with isinstance(..., int), so
    # a MagicMock would silently read as 0 and pass the test for the wrong reason.
    airplay_player.config.get_value = MagicMock(return_value=sync_adjust)
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
    A fresh track's opening is kept: byte 0 anchors to the first chunk, not now+lead.

    Models the clip scenario where the first delivered chunk (file position 0)
    is scheduled earlier than ``clock.now() + the bridge lead``. Anchoring to
    ``now + lead`` would drop everything before it -- the intro. The chunk
    timestamp must win, and its audio must reach the CLI, not be dropped.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    now_plus_lead = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    first_chunk_ts = SENDSPIN_EPOCH_US + 250_000  # position 0, only 250 ms ahead of now
    assert first_chunk_ts < now_plus_lead

    with patch.object(bridge, "_start_protocol_from_chunk", MagicMock()):
        bridge._on_audio_chunk(_pcm_chunk(first_chunk_ts))

    assert bridge._drop_until_us == first_chunk_ts
    # Held while the anchor is negotiated, then queued -- never discarded.
    _settle_anchor(bridge)
    assert not bridge._write_queue.empty()


def test_late_join_anchors_to_catchup_target_live_position() -> None:
    """
    A late joiner lands at the group's current position, not at track zero.

    After minutes of playback the first delivered chunk is the catch-up target
    (playhead + the bridge lead), far from the track start. The anchor must follow that
    chunk so the joiner maps onto the live timeline instead of restarting at 0.
    """
    playhead_us = SENDSPIN_EPOCH_US + 600_000_000  # 600 s into the session
    bridge = _make_bridge(clock_now_us=playhead_us)
    catchup_target_ts = playhead_us + COLD_LEAD_MS * 1_000

    with patch.object(bridge, "_start_protocol_from_chunk", MagicMock()):
        bridge._on_audio_chunk(_pcm_chunk(catchup_target_ts))

    assert bridge._drop_until_us == catchup_target_ts
    # The anchor tracks the advanced playhead, not a fresh now+lead-from-zero.
    assert bridge._drop_until_us > SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000


# --- Timeline alignment: a discontinuity must not shift the device off the group clock ---


def _drain_queued_bytes(bridge: SendspinAirPlayBridge) -> int:
    """Return the number of audio bytes handed to the CLI writer, emptying the queue."""
    total = 0
    while not bridge._write_queue.empty():
        data = bridge._write_queue.get_nowait()
        if data is not None:
            total += len(data)
    return total


def _settle_anchor(bridge: SendspinAirPlayBridge) -> None:
    """Model the binary acking exactly the anchor asked for: replay what was held."""
    bridge._anchor_settled = True
    held = list(bridge._held_chunks)
    bridge._held_chunks.clear()
    bridge._held_us = 0
    for chunk in held:
        bridge._align_chunk(chunk)


def _start_stream_at(bridge: SendspinAirPlayBridge, first_chunk_ts: int) -> None:
    """Feed the anchoring first chunk so the bridge is aligned and streaming."""
    with patch.object(bridge, "_start_protocol_from_chunk", MagicMock()):
        bridge._on_audio_chunk(_pcm_chunk(first_chunk_ts))
    # The mocked task reports done() truthy by default, which the chunk handler
    # reads as a failed protocol start; model a start still in flight instead.
    cast("MagicMock", bridge._airplay_stream_start_task).done.return_value = False
    _settle_anchor(bridge)


def _expected_frames(bridge: SendspinAirPlayBridge, timeline_end_us: int) -> int:
    """Frames the CLI stream must hold for its cursor to sit at a timeline instant."""
    return round((timeline_end_us - bridge._drop_until_us) * BRIDGE_SAMPLE_RATE / 1_000_000)


def test_timeline_gap_is_padded_with_silence() -> None:
    """
    A hole in the Sendspin timeline is filled so the device stays on the group clock.

    Sendspin rebases the shared timeline forward when audio production stalls,
    delivering no audio for the skipped span. The CLI plays its byte stream at a
    fixed rate from an anchor that is never revised, so writing the next chunk
    straight after the previous one would leave this device permanently ahead of
    the group by the size of the hole.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    first_ts = SENDSPIN_EPOCH_US + 250_000
    _start_stream_at(bridge, first_ts)
    _drain_queued_bytes(bridge)

    gap_us = 415_711
    next_ts = first_ts + 100_000 + gap_us
    bridge._on_audio_chunk(_pcm_chunk(next_ts))

    expected = _expected_frames(bridge, next_ts + 100_000)
    assert bridge._queued_frames == expected
    assert (
        _drain_queued_bytes(bridge)
        == (expected - _expected_frames(bridge, first_ts + 100_000))
        * BRIDGE_CHANNELS
        * BRIDGE_BYTES_PER_SAMPLE
    )


def test_overlapping_chunk_head_is_trimmed() -> None:
    """A chunk reaching back behind the write cursor keeps only its unwritten tail."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    first_ts = SENDSPIN_EPOCH_US + 250_000
    _start_stream_at(bridge, first_ts)
    _drain_queued_bytes(bridge)

    overlap_us = 40_000
    next_ts = first_ts + 100_000 - overlap_us
    bridge._on_audio_chunk(_pcm_chunk(next_ts))

    assert bridge._queued_frames == _expected_frames(bridge, next_ts + 100_000)


def test_chunk_entirely_behind_the_cursor_is_dropped() -> None:
    """Audio already written is not queued a second time."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    first_ts = SENDSPIN_EPOCH_US + 250_000
    _start_stream_at(bridge, first_ts)
    _drain_queued_bytes(bridge)
    cursor_frames = bridge._queued_frames

    bridge._on_audio_chunk(_pcm_chunk(first_ts + 10_000, duration_us=50_000))

    assert bridge._queued_frames == cursor_frames
    assert _drain_queued_bytes(bridge) == 0


@pytest.mark.parametrize("server_side", [False, True])
def test_stream_start_resets_the_write_cursor(server_side: bool) -> None:
    """
    Both stream-start entry points rewind the cursor so the next chunk re-anchors byte 0.

    A cursor carried over from the previous stream would place the first chunk of
    the new one far behind the write position and get it trimmed away as already
    written, and a settled-anchor flag carried over would let the new stream's
    chunks be placed against the previous stream's anchor.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _start_stream_at(bridge, SENDSPIN_EPOCH_US + 250_000)
    bridge._held_chunks.append(_pcm_chunk(SENDSPIN_EPOCH_US + 250_000))
    assert bridge._queued_frames > 0

    if server_side:
        bridge._on_stream_start(MagicMock())
    else:
        bridge._on_bridge_stream_start()

    assert bridge._queued_frames == 0
    assert bridge._drop_until_us == 0
    assert bridge._anchor_settled is False
    assert not bridge._held_chunks


def test_contiguous_chunks_are_written_untouched() -> None:
    """Normal playback queues exactly its own audio -- no padding, no trimming."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    first_ts = SENDSPIN_EPOCH_US + 250_000
    _start_stream_at(bridge, first_ts)
    _drain_queued_bytes(bridge)

    for index in range(1, 20):
        bridge._on_audio_chunk(_pcm_chunk(first_ts + index * 100_000))

    assert bridge._queued_frames == _expected_frames(bridge, first_ts + 20 * 100_000)
    assert _drain_queued_bytes(bridge) == 19 * 100_000 * BRIDGE_BYTES_PER_SECOND // 1_000_000


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


def _make_anchor_stream(
    *,
    ready_at_unix_ms: int | None = None,
    ack: int | None = None,
    warm_lead_ms: int = 0,
    flushed_head_unix_ms: int = 0,
) -> MagicMock:
    """
    Build an AirPlayStream mock the anchor math can run against.

    ``warm_lead_ms`` / ``flushed_head_unix_ms`` must be real ints: the anchor
    compares them with ``> 0``, which a bare MagicMock cannot answer.
    """
    stream = MagicMock()
    stream.connect = AsyncMock()
    stream.wait_for_connection = AsyncMock()
    stream.stop = AsyncMock()
    stream.flush = AsyncMock(return_value=True)
    stream.wait_clock_ready = AsyncMock(return_value=ready_at_unix_ms)
    stream.start = AsyncMock(return_value=ack)
    stream.warm_lead_ms = warm_lead_ms
    stream.flushed_head_unix_ms = flushed_head_unix_ms
    return stream


async def test_cold_start_connects_then_anchors_first_start() -> None:
    """A fresh bridge stream anchors its first START only after the CLI connects."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    bridge._airplay_stream_start_task = asyncio.current_task()
    commanded = UNIX_NOW_MS + COLD_LEAD_MS
    stream = _make_anchor_stream(ack=commanded)

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

    stream.connect.assert_awaited_once_with()
    stream.wait_for_connection.assert_awaited_once_with()
    stream.start.assert_awaited_once_with(commanded, join=True)
    assert bridge._airplay_stream is stream
    assert bridge.airplay_player.stream is stream
    assert bridge._started is True
    assert bridge._airplay_stream_ready.is_set()


async def test_cold_start_superseded_before_start_stops_transport() -> None:
    """A cold bridge start that is superseded after connect stops its transport."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    # a different task owns the bridge: this cold start is stale
    bridge._airplay_stream_start_task = MagicMock()
    stream = _make_anchor_stream()

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

    stream.start.assert_not_awaited()
    stream.stop.assert_awaited_once_with(force=True)


async def test_cold_start_superseded_during_the_anchor_stops_its_transport() -> None:
    """
    A cold stream superseded while the binary holds its ack is torn down.

    The anchor publishes the stream before commanding START, so a supersession
    inside it would otherwise leave a live cliairplay attached to the receiver
    with nobody owning it.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    bridge._airplay_stream_start_task = asyncio.current_task()
    stream = _make_anchor_stream()

    async def start(start_unix_ms: int, **_kwargs: object) -> int:
        # A newer stream start claimed the bridge while the binary held its ack.
        bridge._airplay_stream_start_task = MagicMock()
        return start_unix_ms

    stream.start = AsyncMock(side_effect=start)

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.time.time",
        return_value=UNIX_NOW_S,
    ):
        assert await bridge._start_cold_stream(stream) is False

    stream.stop.assert_awaited_once_with(force=True)
    assert bridge._airplay_stream is None
    assert bridge.airplay_player.stream is None


async def test_superseded_cold_stream_teardown_spares_the_newer_owner() -> None:
    """A newer start's published stream survives the stale cold stream's teardown."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    bridge._airplay_stream_start_task = asyncio.current_task()
    stream = _make_anchor_stream()
    newer_stream = _make_anchor_stream()

    async def start(start_unix_ms: int, **_kwargs: object) -> int:
        bridge._airplay_stream_start_task = MagicMock()
        bridge._airplay_stream = newer_stream
        bridge.airplay_player.stream = newer_stream
        return start_unix_ms

    stream.start = AsyncMock(side_effect=start)

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.time.time",
        return_value=UNIX_NOW_S,
    ):
        assert await bridge._start_cold_stream(stream) is False

    stream.stop.assert_awaited_once_with(force=True)
    newer_stream.stop.assert_not_awaited()
    assert bridge._airplay_stream is newer_stream
    assert bridge.airplay_player.stream is newer_stream


# --- Anchoring: command an instant the device can hit, then honour the ack ---


def _prepare_anchor(
    bridge: SendspinAirPlayBridge, stream: MagicMock, first_chunk_lead_ms: int
) -> None:
    """Wire a bridge so ``_anchor_stream`` can be awaited directly on ``stream``."""
    bridge._drop_until_us = bridge.sendspin_server.clock.now_us() + first_chunk_lead_ms * 1_000
    bridge._airplay_stream = stream
    bridge._airplay_stream_start_task = asyncio.current_task()


async def _anchor(bridge: SendspinAirPlayBridge, stream: MagicMock, *, warm: bool = False) -> bool:
    """Run ``_anchor_stream`` with the unix clock pinned to UNIX_NOW_S."""
    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.time.time",
        return_value=UNIX_NOW_S,
    ):
        return await bridge._anchor_stream(stream, warm=warm)


def _commanded_instant(stream: MagicMock) -> int:
    """Return the instant the START command carried."""
    return int(stream.start.await_args.args[0])


async def test_anchor_floors_at_the_join_headroom() -> None:
    """
    A Sendspin lead shorter than the binary needs is raised to the join floor.

    The binary verifies the receiver's clock before it will seat an anchor and
    gives up on that verification shortly before the commanded instant, so an
    anchor inside AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS leaves the device seating on
    an unverified clock and landing audibly behind the group.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = _make_anchor_stream()
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)

    assert await _anchor(bridge, stream) is True

    assert _commanded_instant(stream) == UNIX_NOW_MS + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS


async def test_anchor_follows_the_clock_ready_projection() -> None:
    """A receiver that projects a later readiness pushes the anchor out past it."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    ready_at = UNIX_NOW_MS + 3200
    stream = _make_anchor_stream(ready_at_unix_ms=ready_at)
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)

    assert await _anchor(bridge, stream) is True

    assert _commanded_instant(stream) == ready_at + AIRPLAY_CLOCK_READY_LEAD_MS


async def test_anchor_never_precedes_content_already_scheduled() -> None:
    """
    A buffered source keeps its intro: the anchor lands on the first chunk we hold.

    Sendspin can schedule the first sample much further out than the device
    needs. Anchoring on the floor instead would place byte 0 in the middle of
    the audio already delivered and throw away everything before it.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    first_chunk_us = SENDSPIN_EPOCH_US + 6_000_000
    stream = _make_anchor_stream(ack=UNIX_NOW_MS + 6000)
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=6000)
    bridge._held_chunks.append(_pcm_chunk(first_chunk_us))

    assert await _anchor(bridge, stream) is True

    assert _commanded_instant(stream) == UNIX_NOW_MS + 6000
    # Nothing skipped, and the held opening reached the writer intact.
    assert bridge._drop_until_us == first_chunk_us
    assert _drain_queued_bytes(bridge) == 100_000 * BRIDGE_BYTES_PER_SECOND // 1_000_000


async def test_writer_stays_blocked_until_the_start_is_acked() -> None:
    """
    The writer is released only once the content is mapped onto the acked instant.

    Feeding the CLI before the ack would place bytes against an anchor the
    binary has not confirmed and may still correct forward.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = _make_anchor_stream()
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)
    ack_gate = asyncio.Event()
    start_called = asyncio.Event()

    async def start(start_unix_ms: int, **_kwargs: object) -> int:
        start_called.set()
        await ack_gate.wait()
        return start_unix_ms

    stream.start = AsyncMock(side_effect=start)
    anchor_task = asyncio.create_task(_anchor(bridge, stream))
    bridge._airplay_stream_start_task = cast("asyncio.Task[None]", anchor_task)
    await start_called.wait()

    assert not bridge._airplay_stream_ready.is_set()
    ack_gate.set()
    assert await anchor_task is True
    assert bridge._airplay_stream_ready.is_set()


async def test_chunks_arriving_before_the_ack_are_held_and_replayed_in_order() -> None:
    """Audio delivered while the anchor is outstanding is queued once, in order."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = _make_anchor_stream()
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=2500)
    first_chunk_us = SENDSPIN_EPOCH_US + 2_500_000
    ack_gate = asyncio.Event()
    start_called = asyncio.Event()

    async def start(start_unix_ms: int, **_kwargs: object) -> int:
        start_called.set()
        await ack_gate.wait()
        return start_unix_ms

    stream.start = AsyncMock(side_effect=start)
    anchor_task = asyncio.create_task(_anchor(bridge, stream))
    bridge._airplay_stream_start_task = cast("asyncio.Task[None]", anchor_task)
    await start_called.wait()

    for index in range(4):
        bridge._on_audio_chunk(_pcm_chunk(first_chunk_us + index * 100_000))
    assert len(bridge._held_chunks) == 4
    assert bridge._write_queue.empty()

    ack_gate.set()
    assert await anchor_task is True

    # Four contiguous 100 ms chunks, replayed without padding or trimming.
    assert bridge._queued_frames == _expected_frames(bridge, first_chunk_us + 400_000)
    assert _drain_queued_bytes(bridge) == 4 * 100_000 * BRIDGE_BYTES_PER_SECOND // 1_000_000


def test_held_backlog_is_capped_and_drops_the_oldest() -> None:
    """An anchor that never settles cannot grow the hold without bound."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    chunk_us = 100_000
    over_cap = MAX_HELD_AUDIO_US // chunk_us + 50

    for index in range(over_cap):
        bridge._hold_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + index * chunk_us))

    assert sum(chunk.duration_us for chunk in bridge._held_chunks) <= MAX_HELD_AUDIO_US
    # The running total the cap is measured against tracks the deque exactly.
    assert bridge._held_us == sum(chunk.duration_us for chunk in bridge._held_chunks)
    # The oldest went, the newest stayed.
    assert bridge._held_chunks[0].timestamp_us > SENDSPIN_EPOCH_US
    assert bridge._held_chunks[-1].timestamp_us == SENDSPIN_EPOCH_US + (over_cap - 1) * chunk_us


def test_held_backlog_is_capped_by_chunk_count() -> None:
    """
    A run of zero-duration chunks is bounded by the count cap.

    Such chunks carry no duration at all, so the µs cap can never trip on them
    however many arrive.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)

    for index in range(MAX_HELD_CHUNKS + 50):
        bridge._hold_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + index, duration_us=0))

    assert len(bridge._held_chunks) == MAX_HELD_CHUNKS
    assert bridge._held_us == 0
    assert bridge._held_chunks[-1].timestamp_us == SENDSPIN_EPOCH_US + MAX_HELD_CHUNKS + 49


async def test_corrected_ack_rebases_the_content_onto_the_acked_instant() -> None:
    """An instant the binary moved forward re-bases the anchor, cursor and pacing base."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    correction_ms = 3000
    acked = UNIX_NOW_MS + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS + correction_ms
    stream = _make_anchor_stream(ack=acked)
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)
    bridge._queued_frames = 12_345

    assert await _anchor(bridge, stream) is True

    assert _commanded_instant(stream) == UNIX_NOW_MS + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS
    assert bridge._drop_until_us == SENDSPIN_EPOCH_US + (acked - UNIX_NOW_MS) * 1_000
    assert bridge._queued_frames == 0
    assert bridge._start_unix_ms == acked


async def test_content_before_the_acked_instant_is_dropped_and_trimmed() -> None:
    """Held audio the acked anchor moved past is dropped, the straddling chunk trimmed."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    acked = UNIX_NOW_MS + 2600
    stream = _make_anchor_stream(ack=acked)
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)
    anchor_us = SENDSPIN_EPOCH_US + 2_600_000
    for offset in (-200_000, -50_000, 50_000):
        bridge._held_chunks.append(_pcm_chunk(anchor_us + offset))

    assert await _anchor(bridge, stream) is True

    # Fully-behind chunk gone, the straddling one keeps its 50 ms tail, and the
    # last chunk continues contiguously: 150 ms of audio in total.
    assert bridge._queued_frames == _expected_frames(bridge, anchor_us + 150_000)
    assert _drain_queued_bytes(bridge) == bridge._queued_frames * BRIDGE_BYTES_PER_FRAME


async def test_sync_adjust_shifts_the_command_but_not_the_content_mapping() -> None:
    """The device's own offset rides on the command; the group timeline stays untouched."""
    adjust_ms = 300
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US, sync_adjust=adjust_ms)
    anchor_ms = UNIX_NOW_MS + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS
    stream = _make_anchor_stream(ack=anchor_ms + adjust_ms)
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)

    assert await _anchor(bridge, stream) is True

    assert _commanded_instant(stream) == anchor_ms + adjust_ms
    # Pacing tracks the real wall-clock instant of byte 0 (adjust included)...
    assert bridge._start_unix_ms == anchor_ms + adjust_ms
    # ...while the content is placed on the group timeline, without it.
    assert bridge._drop_until_us == SENDSPIN_EPOCH_US + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS * 1_000


async def test_ack_earlier_than_commanded_is_trusted_verbatim() -> None:
    """
    An acked instant before the commanded one is used as-is, never clamped up.

    Clamping to the commanded instant would map the content onto a moment the
    binary is not rendering at and put the device ahead of the rest of the group.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    acked = UNIX_NOW_MS + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS - 400
    stream = _make_anchor_stream(ack=acked)
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)

    assert await _anchor(bridge, stream) is True

    assert _commanded_instant(stream) > acked
    assert bridge._start_unix_ms == acked
    assert bridge._drop_until_us == SENDSPIN_EPOCH_US + (acked - UNIX_NOW_MS) * 1_000


def test_first_chunk_after_an_anchor_is_not_reported_as_drift() -> None:
    """The gap between a fresh anchor and its first chunk is placement, not drift."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._drop_until_us = SENDSPIN_EPOCH_US
    bridge._queued_frames = 0

    bridge._align_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 900_000))

    cast("MagicMock", bridge.logger).warning.assert_not_called()

    # A cursor that already advanced can drift, and that is still reported.
    bridge._align_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 2_000_000))

    cast("MagicMock", bridge.logger).warning.assert_called_once()


@pytest.mark.parametrize(
    ("warm_lead_ms", "flushed_head_offset_ms", "adjust_ms", "expected_anchor_offset_ms"),
    [
        (4000, 0, 0, 4000 + AIRPLAY_SPLICE_LEAD_MARGIN_MS),
        (4000, 0, -600, 4600 + AIRPLAY_SPLICE_LEAD_MARGIN_MS),
        (0, 5000, 0, 5000 + AIRPLAY_SPLICE_LEAD_MARGIN_MS),
    ],
)
async def test_warm_anchor_clears_the_receivers_queued_audio(
    warm_lead_ms: int,
    flushed_head_offset_ms: int,
    adjust_ms: int,
    expected_anchor_offset_ms: int,
) -> None:
    """
    A warm re-anchor lands beyond the audio the receiver still has queued.

    A negative sync_adjust moves the commanded instant earlier and eats into that
    lead, so it is added back to the requirement.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US, sync_adjust=adjust_ms)
    stream = _make_anchor_stream(
        warm_lead_ms=warm_lead_ms,
        flushed_head_unix_ms=UNIX_NOW_MS + flushed_head_offset_ms if flushed_head_offset_ms else 0,
    )
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)

    assert await _anchor(bridge, stream, warm=True) is True

    assert _commanded_instant(stream) == UNIX_NOW_MS + expected_anchor_offset_ms + adjust_ms


async def test_missing_ack_falls_back_to_the_commanded_instant() -> None:
    """An older binary that never acks must not wedge the writer."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = _make_anchor_stream(ack=None)
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)

    assert await _anchor(bridge, stream) is True

    commanded = UNIX_NOW_MS + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS
    assert bridge._start_unix_ms == commanded
    assert bridge._drop_until_us == SENDSPIN_EPOCH_US + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS * 1_000
    assert bridge._anchor_settled is True
    assert bridge._airplay_stream_ready.is_set()


async def test_superseded_during_the_ack_mutates_nothing() -> None:
    """A newer stream start taking over while the ack is outstanding wins untouched."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = _make_anchor_stream()
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)
    original_drop_until = bridge._drop_until_us
    bridge._held_chunks.append(_pcm_chunk(original_drop_until))

    async def start(start_unix_ms: int, **_kwargs: object) -> int:
        # A newer stream start claimed the bridge while the binary held its ack.
        bridge._airplay_stream_start_task = MagicMock()
        return start_unix_ms

    stream.start = AsyncMock(side_effect=start)

    assert await _anchor(bridge, stream) is False

    assert bridge._drop_until_us == original_drop_until
    assert bridge._start_unix_ms == 0
    assert bridge._started is False
    assert bridge._anchor_settled is False
    assert len(bridge._held_chunks) == 1
    assert not bridge._airplay_stream_ready.is_set()


def test_unix_to_sendspin_instant_round_trips() -> None:
    """The two clock-domain helpers are exact inverses of each other."""
    clock = ManualClock(now_us_value=SENDSPIN_EPOCH_US)
    for lead_ms in (-300, 0, WARM_LEAD_MS, COLD_LEAD_MS):
        audible_us = _audible_instant_us(clock, lead_ms)
        unix_ms = sendspin_audible_instant_to_unix_ms(audible_us, clock.now_us(), UNIX_NOW_S)

        assert unix_ms == UNIX_NOW_MS + lead_ms
        assert (
            unix_ms_to_sendspin_audible_instant(unix_ms, clock.now_us(), UNIX_NOW_S) == audible_us
        )


# --- Warm handover: a kept stream survives a new stream start and rides flush-refill ---


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
    bridge._started = True

    bridge._on_bridge_stream_start()

    assert bridge._airplay_stream is kept_stream
    assert bridge.airplay_player.stream is kept_stream
    assert bridge._stream_is_warm_eligible()


def test_on_bridge_stream_start_keeps_raop_stream() -> None:
    """A started legacy RAOP stream is eligible for warm Sendspin flush-refill."""
    bridge = _make_bridge(
        clock_now_us=SENDSPIN_EPOCH_US,
        protocol=StreamingProtocol.RAOP,
    )
    old_stream = _make_kept_stream()
    bridge._airplay_stream = old_stream
    bridge.airplay_player.stream = old_stream
    bridge._started = True

    bridge._on_bridge_stream_start()

    assert bridge._airplay_stream is old_stream
    assert bridge.airplay_player.stream is old_stream


def test_sendspin_callbacks_keep_raop_stream_until_warm_handover() -> None:
    """Both Sendspin start callbacks preserve a reusable legacy RAOP session."""
    bridge = _make_bridge(
        clock_now_us=SENDSPIN_EPOCH_US,
        protocol=StreamingProtocol.RAOP,
    )
    kept_stream = _make_kept_stream()
    bridge._airplay_stream = kept_stream
    bridge.airplay_player.stream = kept_stream
    bridge._started = True

    bridge._on_stream_start(MagicMock())
    bridge._on_bridge_stream_start()

    assert bridge._airplay_stream is kept_stream
    assert bridge.airplay_player.stream is kept_stream


def test_on_bridge_stream_start_replaces_uncommitted_stream() -> None:
    """A connected stream cannot be retained before its first START."""
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
    bridge._started = True

    bridge._on_stream_start(MagicMock())

    assert bridge._airplay_stream is kept_stream
    assert bridge.airplay_player.stream is kept_stream


async def test_warm_stream_flushes_and_reanchors_on_kept_instance() -> None:
    """A warm handover flushes and re-anchors START on the same stream instance."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._drop_until_us = SENDSPIN_EPOCH_US + WARM_LEAD_MS * 1_000
    commanded = UNIX_NOW_MS + WARM_LEAD_MS
    kept_stream = _make_anchor_stream(ack=commanded)
    bridge._airplay_stream = kept_stream
    bridge._airplay_stream_start_task = asyncio.current_task()

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.time.time",
        return_value=UNIX_NOW_S,
    ):
        committed = await bridge._start_warm_stream(kept_stream)

    assert committed is True
    assert bridge._airplay_stream is kept_stream  # no new instance was built
    kept_stream.flush.assert_awaited_once_with()
    kept_stream.start.assert_awaited_once_with(commanded, join=True)
    assert bridge._started is True
    assert bridge._airplay_stream_ready.is_set()


async def test_warm_stream_flush_timeout_falls_back_to_cold() -> None:
    """A flush that is never acknowledged never re-anchors and falls back to cold."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = _make_anchor_stream()
    kept_stream.flush = AsyncMock(return_value=False)
    bridge._airplay_stream = kept_stream
    bridge._airplay_stream_start_task = asyncio.current_task()

    committed = await bridge._start_warm_stream(kept_stream)

    assert committed is False
    kept_stream.start.assert_not_awaited()
    assert bridge._started is False


async def test_warm_stream_superseded_before_start_does_not_anchor() -> None:
    """If a newer stream start already owns the bridge, the stale flush never re-anchors."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = _make_anchor_stream()
    bridge._airplay_stream = kept_stream
    # Simulate a newer stream start having already replaced the tracked task.
    bridge._airplay_stream_start_task = MagicMock()

    committed = await bridge._start_warm_stream(kept_stream)

    assert committed is False
    kept_stream.start.assert_not_awaited()


async def test_warm_stream_cancellation_propagates() -> None:
    """Cancellation while flushing propagates without re-anchoring."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = _make_anchor_stream()
    flush_waiting = asyncio.Event()

    async def flush(*_args: object, **_kwargs: object) -> bool:
        bridge._airplay_stream_start_task = asyncio.current_task()
        flush_waiting.set()
        await asyncio.Event().wait()
        return True

    kept_stream.flush = AsyncMock(side_effect=flush)
    bridge._airplay_stream = kept_stream

    warm_task = asyncio.create_task(bridge._start_warm_stream(kept_stream))
    await flush_waiting.wait()
    warm_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await warm_task

    kept_stream.start.assert_not_awaited()


async def test_warm_handover_superseded_during_the_anchor_keeps_the_stream() -> None:
    """
    A superseded warm handover leaves the kept stream to the newer start.

    The anchor reports the same False for "superseded" as for a genuine failure,
    so without an ownership re-check the stale task would stop the very transport
    the newer start decided to keep and put a second cliairplay on the receiver.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._drop_until_us = SENDSPIN_EPOCH_US + WARM_LEAD_MS * 1_000
    bridge._airplay_stream_start_task = asyncio.current_task()
    kept_stream = _make_anchor_stream()
    bridge._airplay_stream = kept_stream
    bridge.airplay_player.stream = kept_stream
    cold_stream = _make_anchor_stream()

    async def start(start_unix_ms: int, **_kwargs: object) -> int:
        # A newer stream start claimed the bridge while the binary held its ack.
        bridge._airplay_stream_start_task = MagicMock()
        return start_unix_ms

    kept_stream.start = AsyncMock(side_effect=start)

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=cold_stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.time.time",
            return_value=UNIX_NOW_S,
        ),
    ):
        await bridge._start_protocol_from_chunk()

    kept_stream.stop.assert_not_awaited()
    cold_stream.connect.assert_not_awaited()
    assert bridge._airplay_stream is kept_stream
    assert bridge.airplay_player.stream is kept_stream


async def test_superseded_start_failure_leaves_the_newer_stream_alone() -> None:
    """
    A stale start that fails must not take the newer stream's state with it.

    The receiver is busy precisely because the newer start just claimed it, so a
    superseded cold connect failing is the ordinary outcome. Running the recovery
    would drop the newer stream's held backlog, release its writer before its own
    anchor is settled and schedule its teardown.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    bridge._airplay_stream_start_task = asyncio.current_task()
    bridge._hold_chunk(_pcm_chunk(SENDSPIN_EPOCH_US))
    newer_stream = _make_anchor_stream()
    stale_stream = _make_anchor_stream()

    async def connect() -> None:
        # The newer start won the receiver, so this one cannot have it.
        bridge._airplay_stream_start_task = MagicMock()
        bridge._airplay_stream = newer_stream
        bridge.airplay_player.stream = newer_stream
        raise OSError("device busy")

    stale_stream.connect = AsyncMock(side_effect=connect)

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
        return_value=stale_stream,
    ):
        await bridge._start_protocol_from_chunk()

    assert bridge._is_streaming is True
    assert len(bridge._held_chunks) == 1
    assert not bridge._airplay_stream_ready.is_set()
    assert bridge._airplay_stream is newer_stream
    newer_stream.stop.assert_not_awaited()
    # No teardown was scheduled for the newer stream's resources.
    cast("MagicMock", bridge.mass).create_task.assert_not_called()


# --- Startup lead: how far ahead Sendspin schedules the first chunk ---


def _make_timed_bridge() -> tuple[SendspinAirPlayBridge, MagicMock]:
    """Return a bridge with a mocked bridge role attached, plus that role."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    role = MagicMock()
    bridge._bridge_role = role
    return bridge, role


def test_bridge_timing_reports_the_cold_lead_without_a_warm_stream() -> None:
    """Without a reusable stream the lead has to cover a full process spawn and connect."""
    bridge, role = _make_timed_bridge()

    bridge._refresh_bridge_timing()

    role.set_timing.assert_called_once_with(
        required_lead_time_ms=BRIDGE_COLD_START_LEAD_MS, min_buffer_ms=BRIDGE_MIN_BUFFER_MS
    )


def test_bridge_timing_reports_the_warm_lead_for_a_reusable_stream() -> None:
    """A kept, connected, already-anchored stream pays no connect, so it needs less lead."""
    bridge, role = _make_timed_bridge()
    bridge._airplay_stream = _make_kept_stream()
    bridge._started = True

    bridge._refresh_bridge_timing()

    role.set_timing.assert_called_once_with(
        required_lead_time_ms=BRIDGE_WARM_START_LEAD_MS, min_buffer_ms=BRIDGE_MIN_BUFFER_MS
    )


def test_bridge_timing_is_a_noop_without_a_bridge_role() -> None:
    """Timing can be refreshed before registration completes, with nothing to push it to."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    assert bridge._bridge_role is None

    bridge._refresh_bridge_timing()


def test_stream_start_reads_the_lead_before_rewinding_the_stream_state() -> None:
    """
    The warm/cold decision is taken while the previous stream's state is intact.

    ``_on_stream_start`` rewinds the per-stream state; reading the timing after
    that rewind would report the cold lead for every start, including the warm
    handovers that need none of that budget.
    """
    bridge, role = _make_timed_bridge()
    kept_stream = _make_kept_stream()
    bridge._airplay_stream = kept_stream
    bridge.airplay_player.stream = kept_stream
    bridge._started = True
    observed: list[tuple[bool, object]] = []
    refresh = bridge._refresh_bridge_timing

    def record() -> None:
        observed.append((bridge._started, bridge._airplay_stream))
        refresh()

    with patch.object(bridge, "_refresh_bridge_timing", record):
        bridge._on_stream_start(MagicMock())

    assert observed == [(True, kept_stream)]
    role.set_timing.assert_called_once_with(
        required_lead_time_ms=BRIDGE_WARM_START_LEAD_MS, min_buffer_ms=BRIDGE_MIN_BUFFER_MS
    )
