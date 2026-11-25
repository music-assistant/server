"""
Unit tests for the Sendspin -> AirPlay bridge timing.

Cover ten things, with the Sendspin clock mocked via ``ManualClock`` so the
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
* the playout shift the binary reports after a PCM starvation, which moves the
  anchor so the device does not stay behind the group once it re-anchors itself;
* the write pacing that keeps the device buffered a bounded amount ahead of real
  time so a late-join catch-up backlog is not dumped into the CLI;
* the warm handover: a running, connected stream is kept (not torn down) across
  a new Sendspin stream and rides the persistent-stdin flush-refill (FLUSH +
  re-anchoring START) instead of a cold reconnect -- with flush-timeout and
  superseded-task fallback, and the supersession handling that keeps a stale
  start from spawning a process or touching the stream a newer one owns;
* the recovery from a transport lost mid-stream: the dead CLI is released and
  re-anchored on the group's live timeline, starting on the audio the bridge still
  holds from where a late joiner starts. Every give-up then takes the speaker
  out of the Sendspin session, so the player stops reporting playback nobody can
  hear, and a bounded re-join brings back one that was only briefly away.
"""

import asyncio
import time
from collections.abc import Coroutine
from contextlib import nullcontext
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosendspin.clock import ManualClock
from aiosendspin.server.roles import AudioChunk
from music_assistant_models.enums import IdentifierType, PlaybackState

from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.providers.airplay.constants import (
    AIRPLAY_CLOCK_READY_LEAD_MS,
    AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS,
    AIRPLAY_SILENT_MEMBER_REJOIN_DELAY,
    AIRPLAY_SILENT_MEMBER_RESET_WINDOW,
    AIRPLAY_SPLICE_LEAD_MARGIN_MS,
    ClockReadiness,
    StreamingProtocol,
)
from music_assistant.providers.airplay.player import AirPlayPlayer
from music_assistant.providers.airplay.sendspin_bridge import (
    BRIDGE_COLD_START_LEAD_MS,
    BRIDGE_MIN_BUFFER_MS,
    BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS,
    BRIDGE_WARM_START_LEAD_MS,
    MAX_DEVICE_BUFFER_SECONDS,
    MAX_HELD_AUDIO_US,
    MAX_HELD_CHUNKS,
    PAD_BLOCK_FRAMES,
    SILENCE_BLOCK,
    SendspinAirPlayBridge,
    SendspinBridgeManager,
    _BridgeTransport,
    device_buffer_ahead_seconds,
    get_bridge_client_id,
    sendspin_audible_instant_to_unix_ms,
    unix_ms_to_sendspin_audible_instant,
)
from music_assistant.providers.airplay.stream_session import AirPlayStreamSession
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BYTES_PER_SAMPLE,
    BRIDGE_CHANNELS,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)

if TYPE_CHECKING:
    from music_assistant.models.player import Player

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
# How far ahead of what the group plays Sendspin delivers audio: the buffer of its producer
# (_PRODUCER_BUFFER_LIMIT_US of the Sendspin provider).
SENDSPIN_BUFFER_US = 30_000_000
# Patched with zero delays so the re-join backoff runs instantly while the
# attempt-count and give-up logic around it stays real.
_NO_REJOIN_DELAYS = "music_assistant.providers.airplay.sendspin_bridge.BRIDGE_REJOIN_ATTEMPT_DELAYS"


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
    # Real values: the decision is handed to the CLI verbatim and the group's is
    # compared against it, both of which a MagicMock would answer truthily
    # whatever was resolved. None models a group with no live decision.
    provider.bridge_manager.resolve_shared_ptp = MagicMock(return_value=False)
    provider.bridge_manager.group_shared_ptp = MagicMock(return_value=None)
    # the player has no Sendspin output yet: its bridge registers one under the id of its device
    provider.bridge_manager.bridge_client_id = get_bridge_client_id
    airplay_player = MagicMock()
    airplay_player.protocol_id = "apc43875e9e53a"
    airplay_player.display_name = "Test Player"
    airplay_player.protocol = protocol
    # A real None: the start path stops whatever stream the player already holds
    # before it spawns a process, and a bare MagicMock reads as a live session.
    airplay_player.stream = None
    # A real lock: it is what keeps the bridge and native spawn paths off the
    # same receiver, and `async with` on a MagicMock does not even work.
    airplay_player.stream_spawn_lock = asyncio.Lock()
    # A real int, as the player derives it from its sync adjust setting: a positive
    # setting plays earlier.
    airplay_player.sync_offset_ms = -sync_adjust
    # the player plays on itself, not on the speakers of a set
    airplay_player.endpoints = [airplay_player]
    airplay_player.silent_member = None
    airplay_player.renders_audio = True
    sendspin_server = MagicMock()
    sendspin_server.clock = ManualClock(now_us_value=clock_now_us)
    bridge = SendspinAirPlayBridge(provider, airplay_player, sendspin_server)
    bridge._is_streaming = True
    return bridge


def _transport(bridge: SendspinAirPlayBridge) -> _BridgeTransport:
    """Return the transport of a bridge whose player plays on itself."""
    return bridge._transports[bridge.airplay_player.protocol_id]


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

    with patch.object(_transport(bridge), "_start_protocol_from_chunk", MagicMock()):
        bridge._on_audio_chunk(_pcm_chunk(first_chunk_ts))

    assert _transport(bridge)._drop_until_us == first_chunk_ts
    # Held while the anchor is negotiated, then queued -- never discarded.
    _settle_anchor(bridge)
    assert not _transport(bridge)._write_queue.empty()


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

    with patch.object(_transport(bridge), "_start_protocol_from_chunk", MagicMock()):
        bridge._on_audio_chunk(_pcm_chunk(catchup_target_ts))

    assert _transport(bridge)._drop_until_us == catchup_target_ts
    # The anchor tracks the advanced playhead, not a fresh now+lead-from-zero.
    assert _transport(bridge)._drop_until_us > SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000


# --- Timeline alignment: a discontinuity must not shift the device off the group clock ---


def _drain_queued_bytes(bridge: SendspinAirPlayBridge) -> int:
    """Return the number of audio bytes handed to the CLI writer, emptying the queue."""
    total = 0
    while not _transport(bridge)._write_queue.empty():
        data = _transport(bridge)._write_queue.get_nowait()
        if data is not None:
            total += len(data)
    return total


def _settle_anchor(bridge: SendspinAirPlayBridge) -> None:
    """Model the binary acking exactly the anchor asked for: replay what was held."""
    _transport(bridge)._anchor_settled = True
    held = list(_transport(bridge)._held_chunks)
    _transport(bridge)._held_chunks.clear()
    _transport(bridge)._held_us = 0
    for chunk in held:
        _transport(bridge)._align_chunk(chunk)


def _start_stream_at(bridge: SendspinAirPlayBridge, first_chunk_ts: int) -> None:
    """Feed the anchoring first chunk so the bridge is aligned and streaming."""
    with patch.object(_transport(bridge), "_start_protocol_from_chunk", MagicMock()):
        bridge._on_audio_chunk(_pcm_chunk(first_chunk_ts))
    # The mocked task reports done() truthy by default, which the chunk handler
    # reads as a failed protocol start; model a start still in flight instead.
    cast("MagicMock", _transport(bridge)._airplay_stream_start_task).done.return_value = False
    _settle_anchor(bridge)


def _expected_frames(bridge: SendspinAirPlayBridge, timeline_end_us: int) -> int:
    """Frames the CLI stream must hold for its cursor to sit at a timeline instant."""
    return round(
        (timeline_end_us - _transport(bridge)._drop_until_us) * BRIDGE_SAMPLE_RATE / 1_000_000
    )


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
    assert _transport(bridge)._queued_frames == expected
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

    assert _transport(bridge)._queued_frames == _expected_frames(bridge, next_ts + 100_000)


def test_chunk_entirely_behind_the_cursor_is_dropped() -> None:
    """Audio already written is not queued a second time."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    first_ts = SENDSPIN_EPOCH_US + 250_000
    _start_stream_at(bridge, first_ts)
    _drain_queued_bytes(bridge)
    cursor_frames = _transport(bridge)._queued_frames

    bridge._on_audio_chunk(_pcm_chunk(first_ts + 10_000, duration_us=50_000))

    assert _transport(bridge)._queued_frames == cursor_frames
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
    _transport(bridge)._held_chunks.append(_pcm_chunk(SENDSPIN_EPOCH_US + 250_000))
    assert _transport(bridge)._queued_frames > 0

    if server_side:
        bridge._on_stream_start(MagicMock())
    else:
        bridge._on_bridge_stream_start()

    assert _transport(bridge)._queued_frames == 0
    assert _transport(bridge)._drop_until_us == 0
    assert _transport(bridge)._anchor_settled is False
    assert not _transport(bridge)._held_chunks


def test_contiguous_chunks_are_written_untouched() -> None:
    """Normal playback queues exactly its own audio -- no padding, no trimming."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    first_ts = SENDSPIN_EPOCH_US + 250_000
    _start_stream_at(bridge, first_ts)
    _drain_queued_bytes(bridge)

    for index in range(1, 20):
        bridge._on_audio_chunk(_pcm_chunk(first_ts + index * 100_000))

    assert _transport(bridge)._queued_frames == _expected_frames(bridge, first_ts + 20 * 100_000)
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
    _transport(bridge)._airplay_stream = stream
    _transport(bridge)._airplay_stream_ready.set()
    _transport(bridge)._start_unix_ms = int(UNIX_NOW_S * 1000)
    _transport(bridge)._write_queue.put_nowait(b"first")
    _transport(bridge)._write_queue.put_nowait(b"second")
    _transport(bridge)._write_queue.put_nowait(None)

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.device_buffer_ahead_seconds",
        return_value=0.0,
    ) as buffer_ahead:
        await _transport(bridge)._cli_writer()

    assert [call.args[1] for call in buffer_ahead.call_args_list] == [0, 0]
    assert stream.write_audio.await_count == 2


# --- Commanded cold start and warm handover ------------------------------------


def _make_anchor_stream(
    *,
    ready_at_unix_ms: int | None = None,
    ack: int | None = None,
    warm_lead_ms: int = 0,
    flushed_head_unix_ms: int = 0,
    audio_pending_ms: int = 0,
) -> MagicMock:
    """
    Build an AirPlayStream mock the anchor math can run against.

    The bridge reads these off the stream and does arithmetic on them, so they
    must be real numbers: the anchor compares ``warm_lead_ms`` /
    ``flushed_head_unix_ms`` / ``audio_pending_ms`` with ``> 0`` and the shift
    fold subtracts ``cumulative_shift_seconds``, none of which a bare MagicMock
    can answer (every one of them is truthy).

    :param ack: Instant the binary acks the START at. None acks the commanded
        instant, as a feasible one is.
    """

    async def _ack_start(start_unix_ms: int = 0, **_kwargs: object) -> int:
        return start_unix_ms if ack is None else ack

    stream = MagicMock()
    stream.cumulative_shift_seconds = 0.0
    stream.connect = AsyncMock()
    stream.wait_for_connection = AsyncMock()
    stream.stop = AsyncMock()
    stream.flush = AsyncMock(return_value=True)
    stream.wait_clock_ready = AsyncMock(return_value=(ClockReadiness.PROJECTED, ready_at_unix_ms))
    stream.start = AsyncMock(side_effect=_ack_start)
    stream.warm_lead_ms = warm_lead_ms
    stream.flushed_head_unix_ms = flushed_head_unix_ms
    stream.audio_pending_ms = audio_pending_ms
    return stream


async def test_cold_start_connects_then_anchors_first_start() -> None:
    """A fresh bridge stream anchors its first START only after the CLI connects."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    commanded = UNIX_NOW_MS + COLD_LEAD_MS
    stream = _make_anchor_stream(ack=commanded)

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
            return_value=UNIX_NOW_S,
        ),
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    stream.connect.assert_awaited_once_with(False)
    stream.wait_for_connection.assert_awaited_once_with()
    stream.start.assert_awaited_once_with(commanded, join=True)
    assert _transport(bridge)._airplay_stream is stream
    assert bridge.airplay_player.stream is stream
    assert _transport(bridge)._started is True
    assert _transport(bridge)._airplay_stream_ready.is_set()


async def test_a_fresh_process_releases_a_foreign_mute_latch_before_it_connects() -> None:
    """
    A cold start releases a foreign mute latch before it connects.

    Connecting is what carries that state to the device, so releasing the latch
    after it would not be heard until the next command.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    stream = _make_anchor_stream()
    order: list[str] = []
    cast("MagicMock", bridge.airplay_player).release_foreign_mute_latch = MagicMock(
        side_effect=lambda: order.append("release_mute")
    )
    stream.connect = AsyncMock(side_effect=lambda *_a, **_kw: order.append("connect"))

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
            return_value=UNIX_NOW_S,
        ),
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    assert order == ["release_mute", "connect"]


async def test_a_kept_process_keeps_a_mute_latch_owned_by_another_control() -> None:
    """
    A warm handover does not release a foreign mute latch.

    Only a connect re-sends VOLUME=, so releasing the latch over a kept process
    would clear it while the device is still muted on its own end, leaving the
    stream silent with nothing to say so.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = _make_anchor_stream()
    _transport(bridge)._airplay_stream = kept_stream
    bridge.airplay_player.stream = kept_stream
    _transport(bridge)._started = True

    # the whole warm restart, from the Sendspin stream-start callback to the handover
    bridge._on_bridge_stream_start()
    assert _transport(bridge)._airplay_stream is kept_stream
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
        return_value=UNIX_NOW_S,
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    kept_stream.flush.assert_awaited_once_with()
    cast("MagicMock", bridge.airplay_player).release_foreign_mute_latch.assert_not_called()


async def test_a_failed_warm_handover_releases_the_latch_before_its_cold_retry() -> None:
    """
    The cold retry after a failed warm handover still releases a foreign mute latch.

    That retry spawns a fresh process, which is sent whatever volume and mute it
    finds on connect, so a mute latch the parent no longer owns would start it silent.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    kept_stream = _make_anchor_stream()
    kept_stream.flush = AsyncMock(return_value=False)
    _transport(bridge)._airplay_stream = kept_stream
    cold_stream = _make_anchor_stream()

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=cold_stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
            return_value=UNIX_NOW_S,
        ),
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    cold_stream.connect.assert_awaited_once_with(False)
    cast("MagicMock", bridge.airplay_player).release_foreign_mute_latch.assert_called_once_with()


async def test_a_superseded_cold_start_never_reaches_the_receiver() -> None:
    """
    A cold start that already lost the race bails out before it spawns anything.

    Connecting first would pay a full process spawn and session setup only to
    kill it again, put a second session on a receiver the newer start is about
    to claim, and overwrite the shared-clock decision of the process that start
    is really running.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    # the decision the newer start recorded for the process it is spawning
    _transport(bridge)._use_shared_ptp = True
    # a different task owns the bridge: this cold start is stale
    _transport(bridge)._airplay_stream_start_task = MagicMock()
    stream = _make_anchor_stream()

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
            return_value=UNIX_NOW_S,
        ),
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    stream.connect.assert_not_awaited()
    stream.stop.assert_not_awaited()
    assert _transport(bridge)._use_shared_ptp is True


async def test_a_superseded_start_leaves_the_kept_stream_untouched() -> None:
    """
    A start that lost the race never flushes the stream the newer one kept.

    Arming the bridge keeps a warm-eligible stream alive, so the stale and the
    newer start find the same instance; flushing it here would cut into the
    audio the newer start is anchoring on it.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = _make_anchor_stream()
    _transport(bridge)._airplay_stream = kept_stream
    # a different task owns the bridge: this start is stale
    _transport(bridge)._airplay_stream_start_task = MagicMock()

    await _transport(bridge)._start_protocol_from_chunk()

    kept_stream.flush.assert_not_awaited()
    kept_stream.stop.assert_not_awaited()
    assert _transport(bridge)._airplay_stream is kept_stream


async def test_a_start_superseded_during_the_warm_fallback_spawns_nothing() -> None:
    """
    Losing the race while releasing the kept stream still stops short of the receiver.

    A failed warm handover tears the kept stream down before it falls back to a
    cold start, and that teardown is long enough for a newer start to claim the
    bridge in the meantime.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    kept_stream = _make_anchor_stream()
    kept_stream.flush = AsyncMock(return_value=False)
    _transport(bridge)._airplay_stream = kept_stream
    cold_stream = _make_anchor_stream()

    async def stop(**_kwargs: object) -> None:
        # a newer stream start claimed the bridge while the kept stream went down
        _transport(bridge)._airplay_stream_start_task = MagicMock()

    kept_stream.stop = AsyncMock(side_effect=stop)

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
        return_value=cold_stream,
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    cold_stream.connect.assert_not_awaited()
    cold_stream.stop.assert_not_awaited()


async def test_cold_start_superseded_while_connecting_stops_its_transport() -> None:
    """A cold stream superseded while its process comes up is torn down again."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    stream = _make_anchor_stream()

    async def wait_for_connection() -> None:
        # a newer stream start claimed the bridge while the process came up
        _transport(bridge)._airplay_stream_start_task = MagicMock()

    stream.wait_for_connection = AsyncMock(side_effect=wait_for_connection)

    assert await _transport(bridge)._start_cold_stream(stream) is False

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
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    stream = _make_anchor_stream()

    async def start(start_unix_ms: int, **_kwargs: object) -> int:
        # A newer stream start claimed the bridge while the binary held its ack.
        _transport(bridge)._airplay_stream_start_task = MagicMock()
        return start_unix_ms

    stream.start = AsyncMock(side_effect=start)

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
        return_value=UNIX_NOW_S,
    ):
        assert await _transport(bridge)._start_cold_stream(stream) is False

    stream.stop.assert_awaited_once_with(force=True)
    assert _transport(bridge)._airplay_stream is None
    assert bridge.airplay_player.stream is None


async def test_superseded_cold_stream_teardown_spares_the_newer_owner() -> None:
    """A newer start's published stream survives the stale cold stream's teardown."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    stream = _make_anchor_stream()
    newer_stream = _make_anchor_stream()

    async def start(start_unix_ms: int, **_kwargs: object) -> int:
        _transport(bridge)._airplay_stream_start_task = MagicMock()
        _transport(bridge)._airplay_stream = newer_stream
        bridge.airplay_player.stream = newer_stream
        return start_unix_ms

    stream.start = AsyncMock(side_effect=start)

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
        return_value=UNIX_NOW_S,
    ):
        assert await _transport(bridge)._start_cold_stream(stream) is False

    stream.stop.assert_awaited_once_with(force=True)
    newer_stream.stop.assert_not_awaited()
    assert _transport(bridge)._airplay_stream is newer_stream
    assert bridge.airplay_player.stream is newer_stream


# --- Displacing a native session the bridge does not own ---


def _make_native_stream(session: MagicMock | None = None) -> MagicMock:
    """Build a live native AirPlayStream mock the bridge has to displace."""
    stream = MagicMock()
    stream.stop = AsyncMock()
    stream.session = session
    return stream


async def test_cold_start_stops_a_native_session_before_it_spawns_a_process() -> None:
    """
    A native session on the same player is released before the bridge connects.

    Two cliairplay processes on one receiver reset each other's control
    channel, so the displaced transport has to be gone before the new
    pair-setup; the session bookkeeping follows it.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    session = MagicMock()
    order: list[str] = []
    session.remove_client = AsyncMock(side_effect=lambda *_a, **_kw: order.append("remove_client"))
    native_stream = _make_native_stream(session)
    native_stream.stop = AsyncMock(side_effect=lambda **_kw: order.append("stop"))
    bridge.airplay_player.stream = native_stream
    stream = _make_anchor_stream()
    stream.connect = AsyncMock(side_effect=lambda *_a, **_kw: order.append("connect"))

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
            return_value=UNIX_NOW_S,
        ),
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    assert order == ["stop", "remove_client", "connect"]
    assert session.remove_client.await_args.args[0] is bridge.airplay_player
    assert bridge.airplay_player.stream is stream


class _ReceiverProcesses:
    """
    Counts the cli processes live on one receiver.

    Two of them reset each other's RTSP channel and both sessions die, so
    ``peak`` is the value the collision tests assert on.
    """

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0

    def spawn(self, *_args: object, **_kwargs: object) -> None:
        """Record a cli process pairing with the receiver."""
        self.live += 1
        self.peak = max(self.peak, self.live)

    def kill(self, *_args: object, **_kwargs: object) -> None:
        """Record a cli process letting go of the receiver."""
        self.live = max(0, self.live - 1)


async def _run_native_start(player: AirPlayPlayer, stream: MagicMock) -> None:
    """Run the real native start path for a player against a throwaway session."""
    pcm_format = MagicMock(sample_rate=44100, bit_depth=16, channels=2)
    session = AirPlayStreamSession(MagicMock(), [player], pcm_format, MagicMock(elapsed_time=0))
    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch(
            "music_assistant.providers.airplay.stream_session.AirPlayStream",
            return_value=stream,
        ),
    ):
        await session._start_client(player, False)


async def _bridge_start_racing_a_native_start(
    interleave_at: str,
) -> tuple[SendspinAirPlayBridge, _ReceiverProcesses, MagicMock, asyncio.Task[None]]:
    """
    Drive a bridge start and let a native start in at one of its awaits.

    :param interleave_at: "displacement" starts the native path while the bridge
        releases the session it is displacing, "connect" starts it while the
        bridge's own cold connect is in flight.
    :return: The bridge, its receiver process counter, the stream the bridge
        started and the still-pending native start task.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    player = bridge.airplay_player
    processes = _ReceiverProcesses()
    native_task: asyncio.Task[None] | None = None

    native_stream = _make_native_stream()
    native_stream.running = False
    native_stream.connect = AsyncMock(side_effect=processes.spawn)
    native_stream.stop = AsyncMock(side_effect=processes.kill)

    async def _start_native() -> None:
        """Fire the native start and let it run until it can get no further."""
        nonlocal native_task
        native_task = asyncio.create_task(_run_native_start(player, native_stream))
        # Three passes through the loop: enough for the native start to reach
        # the spawn lock and, without one, to spawn its process outright.
        for _ in range(3):
            await asyncio.sleep(0)

    displaced_stream = _make_native_stream(MagicMock(remove_client=AsyncMock()))
    displaced_stream.running = True
    processes.spawn()

    async def _stop_displaced(**_kwargs: object) -> None:
        displaced_stream.running = False
        processes.kill()
        if interleave_at == "displacement":
            await _start_native()

    displaced_stream.stop = AsyncMock(side_effect=_stop_displaced)
    player.stream = displaced_stream

    bridge_stream = _make_anchor_stream()
    # A real bool: the native start behind the lock only stops what it reads as
    # live, and this stream is what it finds published once the bridge is done.
    bridge_stream.running = True
    bridge_stream.stop = AsyncMock(side_effect=processes.kill)

    async def _connect(*_args: object, **_kwargs: object) -> None:
        processes.spawn()
        if interleave_at == "connect":
            await _start_native()

    bridge_stream.connect = AsyncMock(side_effect=_connect)

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=bridge_stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
            return_value=UNIX_NOW_S,
        ),
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    assert native_task is not None
    return bridge, processes, bridge_stream, native_task


@pytest.mark.parametrize("interleave_at", ["displacement", "connect"])
async def test_a_native_start_cannot_slip_into_a_bridge_start(interleave_at: str) -> None:
    """
    A native start racing a bridge start waits for it instead of joining it.

    Both paths spawn a cli process for the same receiver, and neither the
    displacement nor the cold connect is instant. Without the shared spawn lock
    the native start publishes its own stream inside one of those awaits and the
    bridge then connects a second process over it - two sessions on one receiver,
    with the native one orphaned by the bridge's later publication.
    """
    bridge, processes, bridge_stream, native_task = await _bridge_start_racing_a_native_start(
        interleave_at
    )

    # the bridge start finished on its own process, with the native one still
    # queued behind the lock rather than paired with the same receiver
    assert processes.peak == 1
    assert not native_task.done()
    assert bridge.airplay_player.stream is bridge_stream

    await native_task

    # the native start then displaces the bridge in turn, still one at a time
    assert processes.peak == 1
    assert processes.live == 1
    bridge_stream.stop.assert_awaited()


async def test_a_bridge_start_cannot_slip_into_a_native_start() -> None:
    """
    A bridge start racing a native start waits for it instead of joining it.

    The mirror of the case above: the native path's own displacement stop is a
    real await, and a bridge start landing inside it would connect its own
    process and publish over a native start still in flight.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    player = bridge.airplay_player
    processes = _ReceiverProcesses()
    bridge_task: asyncio.Task[None] | None = None

    bridge_stream = _make_anchor_stream()
    bridge_stream.running = True
    bridge_stream.connect = AsyncMock(side_effect=processes.spawn)
    bridge_stream.stop = AsyncMock(side_effect=processes.kill)

    displaced_stream = _make_native_stream()
    displaced_stream.running = True
    processes.spawn()

    async def _stop_displaced(**_kwargs: object) -> None:
        """Fire the bridge start from inside the native path's displacement stop."""
        nonlocal bridge_task
        displaced_stream.running = False
        processes.kill()
        if bridge_task is not None:
            # Only the first call is the interleaving under test. Without the
            # spawn lock the bridge start finds this same stream still published
            # and re-enters here; starting another bridge task would supersede
            # the one under test and the race would go unnoticed.
            return
        bridge_task = asyncio.create_task(_transport(bridge)._start_protocol_from_chunk())
        # published before the task runs: the start path compares itself against
        # this handle to tell whether it still owns the bridge
        _transport(bridge)._airplay_stream_start_task = bridge_task
        for _ in range(3):
            await asyncio.sleep(0)

    displaced_stream.stop = AsyncMock(side_effect=_stop_displaced)
    player.stream = displaced_stream

    native_stream = _make_native_stream()
    native_stream.running = True
    native_stream.connect = AsyncMock(side_effect=processes.spawn)
    native_stream.stop = AsyncMock(side_effect=processes.kill)

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=bridge_stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
            return_value=UNIX_NOW_S,
        ),
    ):
        await _run_native_start(player, native_stream)

        # the native start finished on its own process, with the bridge still
        # queued behind the lock
        assert processes.peak == 1
        assert bridge_task is not None
        assert not bridge_task.done()
        assert cast("MagicMock", player).stream is native_stream

        await bridge_task

    # the bridge then displaces the native session in turn, still one at a time
    assert processes.peak == 1
    assert cast("MagicMock", player).stream is bridge_stream


def _run_bridge_tasks_for_real(bridge: SendspinAirPlayBridge) -> list[asyncio.Task[None]]:
    """
    Let the bridge schedule real tasks instead of MagicMock ones.

    :return: The list the scheduled tasks are appended to, to await at the end
        of the test.
    """
    loop = asyncio.get_running_loop()
    started: list[asyncio.Task[None]] = []

    def create_task(
        coro: Coroutine[None, None, None], *, eager_start: bool = True, **_kwargs: object
    ) -> asyncio.Task[None]:
        # mirrors mass.create_task, whose default eager start would run the
        # coroutine to its first await before this returns
        task = asyncio.Task(coro, loop=loop, eager_start=eager_start)
        started.append(task)
        return task

    cast("MagicMock", bridge.mass).create_task = create_task
    return started


async def test_a_native_start_waits_for_the_bridge_to_let_go_of_the_receiver() -> None:
    """
    A native start after a stop waits for the bridge process to actually be gone.

    A stop (stop_streaming, or a give-up through _abandon_streaming) hands the
    transport to the cleanup path, which only kills the process a few awaits
    later. A play arriving in between would otherwise pair its own process with
    a receiver the old one is still holding.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    player = bridge.airplay_player
    processes = _ReceiverProcesses()
    scheduled = _run_bridge_tasks_for_real(bridge)

    bridge_stream = _make_anchor_stream()

    async def _slow_stop(**_kwargs: object) -> None:
        # a real teardown writes ACTION=STOP, drops the command pipe and kills
        # the process, none of which is instant
        for _ in range(4):
            await asyncio.sleep(0)
        processes.kill()

    bridge_stream.stop = AsyncMock(side_effect=_slow_stop)
    _transport(bridge)._airplay_stream = bridge_stream
    player.stream = bridge_stream
    processes.spawn()

    native_stream = _make_native_stream()
    native_stream.running = False
    native_stream.connect = AsyncMock(side_effect=processes.spawn)
    native_stream.stop = AsyncMock(side_effect=processes.kill)

    bridge._release_streaming()
    # the reference is already gone while the process is not
    assert cast("MagicMock", player).stream is None
    assert processes.live == 1

    await _run_native_start(player, native_stream)

    assert processes.peak == 1
    assert cast("MagicMock", player).stream is native_stream
    await asyncio.gather(*scheduled, return_exceptions=True)
    assert processes.live == 1


async def test_a_teardown_does_not_lose_the_receiver_to_a_queued_native_start() -> None:
    """
    A native start already queued for the lock still finds the process to displace.

    The lock is handed out in arrival order, so a teardown that only queues for
    it after cancelling the start holding it lands behind a native start that
    was already waiting. That start has to see the stream the teardown has not
    released yet, or it reads the speaker as free and pairs a second process
    with it.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    player = bridge.airplay_player
    processes = _ReceiverProcesses()
    scheduled = _run_bridge_tasks_for_real(bridge)

    kept_stream = _make_kept_stream()
    flush_gate = asyncio.Event()

    async def _blocking_flush(**_kwargs: object) -> bool:
        await flush_gate.wait()
        return True

    kept_stream.flush = AsyncMock(side_effect=_blocking_flush)
    kept_stream.stop = AsyncMock(side_effect=lambda **_kw: processes.kill())
    _transport(bridge)._airplay_stream = kept_stream
    # a legacy RAOP process carries no timing decision, so it stays warm eligible
    _transport(bridge)._use_shared_ptp = None
    player.stream = kept_stream
    processes.spawn()

    warm_task = asyncio.create_task(_transport(bridge)._start_protocol_from_chunk())
    _transport(bridge)._airplay_stream_start_task = warm_task
    for _ in range(5):
        await asyncio.sleep(0)
    assert player.stream_spawn_lock.locked()

    native_stream = _make_native_stream()
    native_stream.running = False
    native_stream.connect = AsyncMock(side_effect=processes.spawn)
    native_stream.stop = AsyncMock(side_effect=processes.kill)
    native_task = asyncio.create_task(_run_native_start(player, native_stream))
    for _ in range(3):
        await asyncio.sleep(0)
    assert not native_task.done()

    # the speaker is stopped: the teardown cancels the warm start that holds the
    # lock, and can only queue for it behind the native start already waiting
    bridge._release_streaming()

    await asyncio.gather(warm_task, native_task, *scheduled, return_exceptions=True)

    assert processes.peak == 1
    assert cast("MagicMock", player).stream is native_stream


async def test_a_native_start_joins_a_teardown_already_under_way() -> None:
    """
    A start waits out a stop that is already running on the stream it displaces.

    A stream stops reporting itself as running the moment its own stop() begins,
    which is before the STOP write and the process kill. Reading that as "gone"
    would let the start pair a process with a receiver the old one has not let
    go of yet.
    """
    player = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US).airplay_player
    processes = _ReceiverProcesses()

    # mid-teardown: no longer reporting itself as running, process still alive
    stopping_stream = _make_native_stream()
    stopping_stream.running = False
    processes.spawn()

    async def _finish_teardown(**_kwargs: object) -> None:
        for _ in range(4):
            await asyncio.sleep(0)
        processes.kill()

    stopping_stream.stop = AsyncMock(side_effect=_finish_teardown)
    player.stream = stopping_stream

    native_stream = _make_native_stream()
    native_stream.running = False
    native_stream.connect = AsyncMock(side_effect=processes.spawn)
    native_stream.stop = AsyncMock(side_effect=processes.kill)

    await _run_native_start(player, native_stream)

    stopping_stream.stop.assert_awaited_once()
    assert processes.peak == 1
    assert cast("MagicMock", player).stream is native_stream


async def test_a_displaced_stream_that_cannot_be_stopped_blocks_the_start() -> None:
    """
    A transport the bridge cannot release stops it from spawning a second process.

    Carrying on would put the new cli process on a receiver that is still
    serving the old one, which is the collision this guard exists to prevent.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    native_stream = _make_native_stream()
    native_stream.stop = AsyncMock(side_effect=RuntimeError("device unreachable"))
    bridge.airplay_player.stream = native_stream
    stream = _make_anchor_stream()

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
            return_value=UNIX_NOW_S,
        ),
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    stream.connect.assert_not_awaited()
    # the session we could not stop stays published, so it is not lost track of
    assert bridge.airplay_player.stream is native_stream


async def test_failed_session_bookkeeping_still_lets_the_start_proceed() -> None:
    """The transport is what matters: a failed client removal does not block the start."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    session = MagicMock()
    session.remove_client = AsyncMock(side_effect=RuntimeError("session already gone"))
    bridge.airplay_player.stream = _make_native_stream(session)
    stream = _make_anchor_stream()

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
            return_value=UNIX_NOW_S,
        ),
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    stream.connect.assert_awaited_once()
    assert bridge.airplay_player.stream is stream


async def test_deferred_teardown_spares_a_native_stream_that_replaced_the_bridge() -> None:
    """
    A teardown that fires after the native path took the speaker leaves it alone.

    The grace window between a Sendspin stream ending and its cleanup is long
    enough for native playback to start, and unpublishing its stream here would
    strand the process behind it.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge_stream = MagicMock()
    bridge_stream.stop = AsyncMock()
    _transport(bridge)._airplay_stream = bridge_stream
    native_stream = _make_native_stream()
    bridge.airplay_player.stream = native_stream

    await bridge._stop_streaming()

    assert bridge.airplay_player.stream is native_stream
    native_stream.stop.assert_not_awaited()
    cast("MagicMock", bridge.airplay_player).set_state_from_stream.assert_not_called()


async def test_cold_start_stops_a_sessionless_native_stream_directly() -> None:
    """A displaced stream with no session behind it is stopped on its own."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    native_stream = _make_native_stream()
    bridge.airplay_player.stream = native_stream
    stream = _make_anchor_stream()

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
            return_value=UNIX_NOW_S,
        ),
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    native_stream.stop.assert_awaited_once_with(force=True)
    assert bridge.airplay_player.stream is stream


async def test_warm_reuse_never_displaces_the_stream_it_reuses() -> None:
    """The stream a warm handover rides is the bridge's own, so it is not stopped."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + WARM_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    kept_stream = _make_anchor_stream(ack=UNIX_NOW_MS + WARM_LEAD_MS)
    _transport(bridge)._airplay_stream = kept_stream
    bridge.airplay_player.stream = kept_stream

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
        return_value=UNIX_NOW_S,
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    kept_stream.stop.assert_not_awaited()
    kept_stream.flush.assert_awaited_once()
    assert bridge.airplay_player.stream is kept_stream


@pytest.mark.parametrize("server_side", [False, True])
def test_stream_start_callbacks_leave_a_native_session_published(server_side: bool) -> None:
    """
    Both Sendspin stream-start callbacks leave a native stream on the player.

    Dropping the reference there would strand its cli process: the start task
    stops what the player still points at, and only that.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    native_stream = _make_native_stream()
    bridge.airplay_player.stream = native_stream

    if server_side:
        bridge._on_stream_start(MagicMock())
    else:
        bridge._on_bridge_stream_start()

    assert _transport(bridge)._airplay_stream is None
    assert bridge.airplay_player.stream is native_stream


# --- Teardown player state reset ---


async def test_bridge_teardown_resets_the_players_stream_state() -> None:
    """A torn-down bridge stream leaves the AirPlay player IDLE at position 0."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = MagicMock()
    stream.stop = AsyncMock()
    _transport(bridge)._airplay_stream = stream
    bridge.airplay_player.stream = stream

    await bridge._stop_streaming()

    stream.stop.assert_awaited_once_with(force=True)
    cast("MagicMock", bridge.airplay_player).set_state_from_stream.assert_called_once_with(
        state=PlaybackState.IDLE, elapsed_time=0
    )


async def test_bridge_teardown_spares_a_newer_streams_state() -> None:
    """Cleanup of a superseded stream never resets state a newer stream owns."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    old_stream = MagicMock()
    old_stream.stop = AsyncMock()
    bridge.airplay_player.stream = MagicMock()

    await _transport(bridge)._cleanup_old_stream(old_stream, None, None)

    cast("MagicMock", bridge.airplay_player).set_state_from_stream.assert_not_called()


# --- Anchoring: command an instant the device can hit, then honour the ack ---


def _prepare_anchor(
    bridge: SendspinAirPlayBridge, stream: MagicMock, first_chunk_lead_ms: int
) -> None:
    """Wire a bridge so ``_anchor_stream`` can be awaited directly on ``stream``."""
    _transport(bridge)._drop_until_us = (
        bridge.sendspin_server.clock.now_us() + first_chunk_lead_ms * 1_000
    )
    _transport(bridge)._airplay_stream = stream
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()


async def _anchor(bridge: SendspinAirPlayBridge, stream: MagicMock, *, warm: bool = False) -> bool:
    """Run ``_anchor_stream`` with the unix clock pinned to UNIX_NOW_S."""
    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
        return_value=UNIX_NOW_S,
    ):
        return await _transport(bridge)._anchor_stream(stream, warm=warm)


def _commanded_instant(stream: MagicMock) -> int:
    """Return the instant the START command carried."""
    return int(stream.start.await_args.args[0])


async def test_anchor_floors_at_the_join_headroom() -> None:
    """
    A Sendspin lead shorter than the join floor is raised to it.

    AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS is the least the anchor may sit ahead of
    now, so a shorter lead is floored rather than honoured.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = _make_anchor_stream()
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)

    assert await _anchor(bridge, stream) is True

    assert _commanded_instant(stream) == UNIX_NOW_MS + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS


async def test_anchor_reports_leftover_audio_pending_on_stdin() -> None:
    """
    Audio still pending when the anchor is commanded is named as the offset it causes.

    The writer is gated until the anchor settles, so anything pending was left
    behind by an earlier stream and the START anchors it as this one's first
    sample. The cursor only counts what the bridge queued itself, so no later
    realignment can see the resulting offset, which leaves this warning as the
    one place it surfaces.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = _make_anchor_stream(audio_pending_ms=92)
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=WARM_LEAD_MS)

    with patch.object(bridge.logger, "warning") as warning:
        assert await _anchor(bridge, stream, warm=True) is True

    pending = [call for call in warning.call_args_list if "pending" in call.args[0]]
    assert len(pending) == 1
    assert pending[0].args[2] == 92


async def test_anchor_stays_quiet_when_stdin_was_left_empty() -> None:
    """An anchor commanded against empty stdin reports nothing."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = _make_anchor_stream()
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=WARM_LEAD_MS)

    with patch.object(bridge.logger, "warning") as warning:
        assert await _anchor(bridge, stream, warm=True) is True

    assert not [call for call in warning.call_args_list if "pending" in call.args[0]]


async def test_anchor_follows_the_clock_ready_projection() -> None:
    """A receiver that projects a later readiness pushes the anchor out past it."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    ready_at = UNIX_NOW_MS + 3200
    stream = _make_anchor_stream(ready_at_unix_ms=ready_at)
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)

    assert await _anchor(bridge, stream) is True

    assert _commanded_instant(stream) == ready_at + AIRPLAY_CLOCK_READY_LEAD_MS


async def test_anchor_still_starts_a_receiver_whose_clock_stalled() -> None:
    """
    A stalled receiver is anchored anyway, unlike a late joiner.

    A joiner is dropped because the session plays on without it, while here
    dropping would stop the speaker, and the binary's stall report is a
    diagnosis a receiver can still come good from.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = _make_anchor_stream()
    stream.wait_clock_ready = AsyncMock(return_value=(ClockReadiness.STALLED, 0))
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)

    assert await _anchor(bridge, stream) is True

    assert _commanded_instant(stream) == UNIX_NOW_MS + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS
    assert _transport(bridge)._started is True


async def test_warm_reanchors_leave_a_latched_stall_to_the_streams_own_warning() -> None:
    """
    Track changes on a kept stream do not repeat the stall warning.

    The stall stays latched on the stream across warm re-anchors, and the stream
    already warned about it once when the binary reported it.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = _make_anchor_stream()
    stream.wait_clock_ready = AsyncMock(return_value=(ClockReadiness.STALLED, 0))

    with patch.object(bridge.logger, "warning") as warning:
        for _ in range(3):
            _prepare_anchor(bridge, stream, first_chunk_lead_ms=WARM_LEAD_MS)
            assert await _anchor(bridge, stream, warm=True) is True

    assert stream.start.await_count == 3
    assert not [call for call in warning.call_args_list if "PTP clock" in call.args[0]]


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
    _transport(bridge)._held_chunks.append(_pcm_chunk(first_chunk_us))

    assert await _anchor(bridge, stream) is True

    assert _commanded_instant(stream) == UNIX_NOW_MS + 6000
    # Nothing skipped, and the held opening reached the writer intact.
    assert _transport(bridge)._drop_until_us == first_chunk_us
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
    _transport(bridge)._airplay_stream_start_task = cast("asyncio.Task[None]", anchor_task)
    await start_called.wait()

    assert not _transport(bridge)._airplay_stream_ready.is_set()
    ack_gate.set()
    assert await anchor_task is True
    assert _transport(bridge)._airplay_stream_ready.is_set()


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
    _transport(bridge)._airplay_stream_start_task = cast("asyncio.Task[None]", anchor_task)
    await start_called.wait()

    for index in range(4):
        bridge._on_audio_chunk(_pcm_chunk(first_chunk_us + index * 100_000))
    assert len(_transport(bridge)._held_chunks) == 4
    assert _transport(bridge)._write_queue.empty()

    ack_gate.set()
    assert await anchor_task is True

    # Four contiguous 100 ms chunks, replayed without padding or trimming.
    assert _transport(bridge)._queued_frames == _expected_frames(bridge, first_chunk_us + 400_000)
    assert _drain_queued_bytes(bridge) == 4 * 100_000 * BRIDGE_BYTES_PER_SECOND // 1_000_000


def test_held_backlog_is_capped_and_drops_the_oldest() -> None:
    """An anchor that never settles cannot grow the hold without bound."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    chunk_us = 100_000
    over_cap = MAX_HELD_AUDIO_US // chunk_us + 50

    for index in range(over_cap):
        _transport(bridge)._hold_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + index * chunk_us))

    assert sum(chunk.duration_us for chunk in _transport(bridge)._held_chunks) <= MAX_HELD_AUDIO_US
    # The running total the cap is measured against tracks the deque exactly.
    assert _transport(bridge)._held_us == sum(
        chunk.duration_us for chunk in _transport(bridge)._held_chunks
    )
    # The oldest went, the newest stayed.
    assert _transport(bridge)._held_chunks[0].timestamp_us > SENDSPIN_EPOCH_US
    assert (
        _transport(bridge)._held_chunks[-1].timestamp_us
        == SENDSPIN_EPOCH_US + (over_cap - 1) * chunk_us
    )


def test_held_backlog_is_capped_by_chunk_count() -> None:
    """
    A run of zero-duration chunks is bounded by the count cap.

    Such chunks carry no duration at all, so the µs cap can never trip on them
    however many arrive.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)

    for index in range(MAX_HELD_CHUNKS + 50):
        _transport(bridge)._hold_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + index, duration_us=0))

    assert len(_transport(bridge)._held_chunks) == MAX_HELD_CHUNKS
    assert _transport(bridge)._held_us == 0
    assert (
        _transport(bridge)._held_chunks[-1].timestamp_us == SENDSPIN_EPOCH_US + MAX_HELD_CHUNKS + 49
    )


async def test_corrected_ack_rebases_the_content_onto_the_acked_instant() -> None:
    """An instant the binary moved forward re-bases the anchor, cursor and pacing base."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    correction_ms = 3000
    acked = UNIX_NOW_MS + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS + correction_ms
    stream = _make_anchor_stream(ack=acked)
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)
    _transport(bridge)._queued_frames = 12_345

    assert await _anchor(bridge, stream) is True

    assert _commanded_instant(stream) == UNIX_NOW_MS + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS
    assert _transport(bridge)._drop_until_us == SENDSPIN_EPOCH_US + (acked - UNIX_NOW_MS) * 1_000
    assert _transport(bridge)._queued_frames == 0
    assert _transport(bridge)._start_unix_ms == acked


async def test_content_before_the_acked_instant_is_dropped_and_trimmed() -> None:
    """Held audio the acked anchor moved past is dropped, the straddling chunk trimmed."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    acked = UNIX_NOW_MS + 2600
    stream = _make_anchor_stream(ack=acked)
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)
    anchor_us = SENDSPIN_EPOCH_US + 2_600_000
    for offset in (-200_000, -50_000, 50_000):
        _transport(bridge)._held_chunks.append(_pcm_chunk(anchor_us + offset))

    assert await _anchor(bridge, stream) is True

    # Fully-behind chunk gone, the straddling one keeps its 50 ms tail, and the
    # last chunk continues contiguously: 150 ms of audio in total.
    assert _transport(bridge)._queued_frames == _expected_frames(bridge, anchor_us + 150_000)
    assert _drain_queued_bytes(bridge) == _transport(bridge)._queued_frames * BRIDGE_BYTES_PER_FRAME


async def test_sync_adjust_shifts_the_command_but_not_the_content_mapping() -> None:
    """The device's own offset rides on the command; the group timeline stays untouched."""
    sync_adjust = 300
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US, sync_adjust=sync_adjust)
    anchor_ms = UNIX_NOW_MS + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS
    # a positive sync adjust plays earlier
    stream = _make_anchor_stream(ack=anchor_ms - sync_adjust)
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)

    assert await _anchor(bridge, stream) is True

    assert _commanded_instant(stream) == anchor_ms - sync_adjust
    # Pacing tracks the real wall-clock instant of byte 0 (adjust included)...
    assert _transport(bridge)._start_unix_ms == anchor_ms - sync_adjust
    # ...while the content is placed on the group timeline, without it.
    assert (
        _transport(bridge)._drop_until_us
        == SENDSPIN_EPOCH_US + AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS * 1_000
    )


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
    assert _transport(bridge)._start_unix_ms == acked
    assert _transport(bridge)._drop_until_us == SENDSPIN_EPOCH_US + (acked - UNIX_NOW_MS) * 1_000


def test_first_chunk_after_an_anchor_is_not_reported_as_drift() -> None:
    """The gap between a fresh anchor and its first chunk is placement, not drift."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US
    _transport(bridge)._queued_frames = 0

    _transport(bridge)._align_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 900_000))

    cast("MagicMock", bridge.logger).warning.assert_not_called()

    # A cursor that already advanced can drift, and that is still reported.
    _transport(bridge)._align_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 2_000_000))

    cast("MagicMock", bridge.logger).warning.assert_called_once()


def test_a_long_timeline_gap_is_padded_from_one_shared_block() -> None:
    """
    A long hole is queued as repeats of one silence block, not as a single buffer.

    Sendspin rebases the shared timeline forward when audio production stalls,
    which can open a hole of tens of seconds. Building that as one bytes object
    puts megabytes on the event loop in a single synchronous allocation.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    first_ts = SENDSPIN_EPOCH_US + 250_000
    _start_stream_at(bridge, first_ts)
    _drain_queued_bytes(bridge)

    gap_us = 60_000_000
    next_ts = first_ts + 100_000 + gap_us
    bridge._on_audio_chunk(_pcm_chunk(next_ts))

    queued: list[bytes] = []
    while not _transport(bridge)._write_queue.empty():
        block = _transport(bridge)._write_queue.get_nowait()
        assert block is not None
        queued.append(block)

    pad, data = queued[:-1], queued[-1]
    gap_frames = round(gap_us * BRIDGE_SAMPLE_RATE / 1_000_000)
    assert len(pad) == gap_frames // PAD_BLOCK_FRAMES
    # Identity, not equality: the whole hole costs one allocation.
    assert all(block is SILENCE_BLOCK for block in pad)
    # The hole is still filled exactly, so the device stays on the group's clock.
    assert sum(len(block) for block in pad) == gap_frames * BRIDGE_BYTES_PER_FRAME
    assert len(data) == 100_000 * BRIDGE_BYTES_PER_SECOND // 1_000_000


# --- Playout shift: the binary's own mid-stream re-anchors move the mapping ---


def _shifted_bridge(shift_seconds: float) -> tuple[SendspinAirPlayBridge, MagicMock, int]:
    """
    Start a streaming bridge whose CLI reports a mid-stream playout shift.

    :param shift_seconds: Cumulative shift the binary reports since its START.
    :return: The bridge, its stream mock and the first chunk's timestamp.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    first_ts = SENDSPIN_EPOCH_US + 250_000
    _start_stream_at(bridge, first_ts)
    _transport(bridge)._start_unix_ms = UNIX_NOW_MS
    _drain_queued_bytes(bridge)
    stream = MagicMock()
    # A real float: the fold subtracts it from the applied baseline, which a bare
    # MagicMock cannot answer.
    stream.cumulative_shift_seconds = shift_seconds
    _transport(bridge)._airplay_stream = stream
    return bridge, stream, first_ts


def test_reported_reanchor_moves_the_anchor_and_the_pacing_start() -> None:
    """
    A starvation re-anchor makes every byte audible later, so the anchor follows it.

    cliairplay shifts its playout forward when it runs out of PCM on stdin.
    Leaving the bridge's mapping where it was would place all following content
    ahead of where the device actually plays it, for the rest of the stream.
    """
    shift_s = 1.5
    bridge, _, first_ts = _shifted_bridge(shift_s)
    anchor_before = _transport(bridge)._drop_until_us

    bridge._on_audio_chunk(_pcm_chunk(first_ts + 100_000))

    assert _transport(bridge)._drop_until_us == anchor_before + round(shift_s * 1_000_000)
    # The write pacing measures from the same instant, so it moves with it.
    assert _transport(bridge)._start_unix_ms == UNIX_NOW_MS + round(shift_s * 1000)


def test_reported_reanchor_skips_content_until_the_device_catches_up() -> None:
    """The device plays the shift late, so exactly that much content is dropped."""
    shift_s = 0.5
    bridge, _, first_ts = _shifted_bridge(shift_s)

    fed_us = 1_000_000
    for index in range(fed_us // 100_000):
        bridge._on_audio_chunk(_pcm_chunk(first_ts + 100_000 * (index + 1)))

    assert _transport(bridge)._queued_frames == _expected_frames(
        bridge, first_ts + 100_000 + fed_us
    )
    assert _drain_queued_bytes(bridge) == round(
        (fed_us / 1_000_000 - shift_s) * BRIDGE_BYTES_PER_SECOND
    )


def test_absorbing_a_reanchor_is_not_reported_as_timeline_drift() -> None:
    """The trim that works a folded shift off is a correction, not Sendspin drift."""
    bridge, _, first_ts = _shifted_bridge(0.5)
    logger = cast("MagicMock", bridge.logger)

    # Straddles the shifted anchor, so it is trimmed rather than dropped outright.
    bridge._on_audio_chunk(_pcm_chunk(first_ts + 550_000))

    assert _transport(bridge)._queued_frames == _expected_frames(bridge, first_ts + 650_000)
    # Only the re-anchor itself is reported; the trim it caused stays quiet.
    logger.warning.assert_called_once()
    logger.warning.reset_mock()

    # Back on the timeline: the shift is worked off and nothing is realigned.
    bridge._on_audio_chunk(_pcm_chunk(first_ts + 650_000))
    assert not _transport(bridge)._absorbing_shift
    logger.warning.assert_not_called()

    # A real discontinuity is reported again.
    bridge._on_audio_chunk(_pcm_chunk(first_ts + 1_500_000))
    logger.warning.assert_called_once()


def test_a_cursor_off_the_frame_grid_still_absorbs_quietly() -> None:
    """
    The trim stays quiet however the cursor happens to sit when the shift lands.

    ``_align_chunk`` re-targets each chunk against the anchor independently, so
    the cursor routinely rests a frame either side of the timeline. The trim a
    fold asks for is a correction at any of those offsets, never Sendspin drift.
    """
    bridge, stream, first_ts = _shifted_bridge(0.0)
    logger = cast("MagicMock", bridge.logger)

    # Play on a while first, so the cursor sits well past the anchor the way it
    # does mid-stream when a starvation hits.
    for index in range(1, 11):
        bridge._on_audio_chunk(_pcm_chunk(first_ts + 100_000 * index))
    logger.warning.assert_not_called()

    # A frame past the timeline: the trim the fold asks for then runs one frame
    # deeper than the shift itself.
    _transport(bridge)._queued_frames += 1
    stream.cumulative_shift_seconds = 0.5
    bridge._on_audio_chunk(_pcm_chunk(first_ts + 1_100_000))

    # Only the re-anchor itself is reported.
    logger.warning.assert_called_once()


def test_an_absorption_spanning_chunks_reports_only_the_reanchor() -> None:
    """
    A shift takes several chunks to trim off, and stays one report throughout.

    The binary keeps reporting the same running total while the trim works, so
    every chunk until the cursor is back on the timeline realigns by design.
    """
    bridge, stream, first_ts = _shifted_bridge(0.0)
    logger = cast("MagicMock", bridge.logger)
    for index in range(1, 11):
        bridge._on_audio_chunk(_pcm_chunk(first_ts + 100_000 * index))
    logger.warning.assert_not_called()

    stream.cumulative_shift_seconds = 0.5
    # Five chunks of content are trimmed away before the cursor catches up.
    for index in range(11, 17):
        bridge._on_audio_chunk(_pcm_chunk(first_ts + 100_000 * index))

    logger.warning.assert_called_once()
    assert not _transport(bridge)._absorbing_shift


def test_an_unchanged_reanchor_total_is_folded_only_once() -> None:
    """The binary reports a running total, so only what is new moves the anchor."""
    bridge, _, first_ts = _shifted_bridge(0.5)
    bridge._on_audio_chunk(_pcm_chunk(first_ts + 600_000))
    anchor_after_fold = _transport(bridge)._drop_until_us
    assert anchor_after_fold == first_ts + 500_000

    bridge._on_audio_chunk(_pcm_chunk(first_ts + 700_000))

    assert _transport(bridge)._drop_until_us == anchor_after_fold


def test_a_reset_reanchor_total_rebaselines_without_moving_the_anchor() -> None:
    """
    A total that went backwards means a START already replaced the mapping.

    The binary zeroes its running total on every START, so the drop is not the
    device un-shifting: the bridge takes the new baseline and leaves the anchor
    to the START that set it.
    """
    bridge, stream, first_ts = _shifted_bridge(0.5)
    bridge._on_audio_chunk(_pcm_chunk(first_ts + 600_000))
    anchor_after_fold = _transport(bridge)._drop_until_us
    assert anchor_after_fold == first_ts + 500_000

    stream.cumulative_shift_seconds = 0.0
    bridge._on_audio_chunk(_pcm_chunk(first_ts + 700_000))

    assert _transport(bridge)._drop_until_us == anchor_after_fold
    assert _transport(bridge)._applied_shift_seconds == 0.0


def test_a_reset_reanchor_total_ends_the_absorption() -> None:
    """
    A total that went backwards leaves no correction outstanding to stay quiet for.

    The START that zeroed the total replaced the mapping the trim was working
    against, so a realignment after it is Sendspin timeline drift again and
    worth reporting.
    """
    bridge, stream, first_ts = _shifted_bridge(0.5)
    logger = cast("MagicMock", bridge.logger)
    # Mid-absorption: the trim has not caught the cursor up to the timeline yet.
    bridge._on_audio_chunk(_pcm_chunk(first_ts + 550_000))
    logger.warning.reset_mock()

    stream.cumulative_shift_seconds = 0.0
    bridge._on_audio_chunk(_pcm_chunk(first_ts + 600_000))

    assert not _transport(bridge)._absorbing_shift
    logger.warning.assert_called_once()


async def test_anchoring_clears_the_folded_shift_baseline() -> None:
    """A START re-anchors the binary from scratch, so the fold starts over with it."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = _make_anchor_stream(ack=UNIX_NOW_MS + COLD_LEAD_MS)
    _prepare_anchor(bridge, stream, COLD_LEAD_MS)
    _transport(bridge)._applied_shift_seconds = 1.5
    _transport(bridge)._absorbing_shift = True

    assert await _anchor(bridge, stream)

    assert _transport(bridge)._applied_shift_seconds == 0.0
    assert not _transport(bridge)._absorbing_shift


@pytest.mark.parametrize(
    ("warm_lead_ms", "flushed_head_offset_ms", "sync_adjust", "expected_anchor_offset_ms"),
    [
        (4000, 0, 0, 4000 + AIRPLAY_SPLICE_LEAD_MARGIN_MS),
        (4000, 0, 600, 4600 + AIRPLAY_SPLICE_LEAD_MARGIN_MS),
        (0, 5000, 0, 5000 + AIRPLAY_SPLICE_LEAD_MARGIN_MS),
    ],
)
async def test_warm_anchor_clears_the_receivers_queued_audio(
    warm_lead_ms: int,
    flushed_head_offset_ms: int,
    sync_adjust: int,
    expected_anchor_offset_ms: int,
) -> None:
    """
    A warm re-anchor lands beyond the audio the receiver still has queued.

    A positive sync_adjust moves the commanded instant earlier and eats into that
    lead, so it is added back to the requirement.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US, sync_adjust=sync_adjust)
    stream = _make_anchor_stream(
        warm_lead_ms=warm_lead_ms,
        flushed_head_unix_ms=UNIX_NOW_MS + flushed_head_offset_ms if flushed_head_offset_ms else 0,
    )
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)

    assert await _anchor(bridge, stream, warm=True) is True

    assert _commanded_instant(stream) == UNIX_NOW_MS + expected_anchor_offset_ms - sync_adjust


async def test_superseded_during_the_ack_mutates_nothing() -> None:
    """A newer stream start taking over while the ack is outstanding wins untouched."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = _make_anchor_stream()
    _prepare_anchor(bridge, stream, first_chunk_lead_ms=250)
    original_drop_until = _transport(bridge)._drop_until_us
    _transport(bridge)._held_chunks.append(_pcm_chunk(original_drop_until))

    async def start(start_unix_ms: int, **_kwargs: object) -> int:
        # A newer stream start claimed the bridge while the binary held its ack.
        _transport(bridge)._airplay_stream_start_task = MagicMock()
        return start_unix_ms

    stream.start = AsyncMock(side_effect=start)

    assert await _anchor(bridge, stream) is False

    assert _transport(bridge)._drop_until_us == original_drop_until
    assert _transport(bridge)._start_unix_ms == 0
    assert _transport(bridge)._started is False
    assert _transport(bridge)._anchor_settled is False
    assert len(_transport(bridge)._held_chunks) == 1
    assert not _transport(bridge)._airplay_stream_ready.is_set()


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


def _make_kept_stream(
    *,
    running: bool = True,
    connected: bool = True,
    ended_cleanly: bool = False,
    receiver_closed: bool = False,
) -> MagicMock:
    """
    Build a mock AirPlayStream reporting the given running/connected state.

    :param running: Whether the cli process behind the stream is still alive.
    :param connected: Whether the device connection has been established.
    :param ended_cleanly: Whether the binary reported the end of the stream
        itself. A real bool: a bare MagicMock reads as a clean end, which the
        loss check treats as no loss at all.
    :param receiver_closed: Whether the receiver ended the session itself. A
        real bool: a bare MagicMock reads as a takeover by another sender.
    """
    stream = MagicMock()
    stream.running = running
    stream.connected = connected
    stream.ended_cleanly = ended_cleanly
    stream.receiver_closed = receiver_closed
    # A real float: the shift fold subtracts it from the applied baseline, which
    # a bare MagicMock cannot answer.
    stream.cumulative_shift_seconds = 0.0
    return stream


def test_on_bridge_stream_start_keeps_warm_eligible_stream() -> None:
    """A running, connected AirPlay 2 stream survives a new Sendspin stream start."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = _make_kept_stream()
    _transport(bridge)._airplay_stream = kept_stream
    bridge.airplay_player.stream = kept_stream
    _transport(bridge)._started = True

    bridge._on_bridge_stream_start()

    assert _transport(bridge)._airplay_stream is kept_stream
    assert bridge.airplay_player.stream is kept_stream
    assert _transport(bridge)._stream_is_warm_eligible()


def test_on_bridge_stream_start_keeps_raop_stream() -> None:
    """A started legacy RAOP stream is eligible for warm Sendspin flush-refill."""
    bridge = _make_bridge(
        clock_now_us=SENDSPIN_EPOCH_US,
        protocol=StreamingProtocol.RAOP,
    )
    old_stream = _make_kept_stream()
    _transport(bridge)._airplay_stream = old_stream
    bridge.airplay_player.stream = old_stream
    _transport(bridge)._started = True

    bridge._on_bridge_stream_start()

    assert _transport(bridge)._airplay_stream is old_stream
    assert bridge.airplay_player.stream is old_stream


def test_sendspin_callbacks_keep_raop_stream_until_warm_handover() -> None:
    """Both Sendspin start callbacks preserve a reusable legacy RAOP session."""
    bridge = _make_bridge(
        clock_now_us=SENDSPIN_EPOCH_US,
        protocol=StreamingProtocol.RAOP,
    )
    kept_stream = _make_kept_stream()
    _transport(bridge)._airplay_stream = kept_stream
    bridge.airplay_player.stream = kept_stream
    _transport(bridge)._started = True

    bridge._on_stream_start(MagicMock())
    bridge._on_bridge_stream_start()

    assert _transport(bridge)._airplay_stream is kept_stream
    assert bridge.airplay_player.stream is kept_stream


def test_on_bridge_stream_start_replaces_uncommitted_stream() -> None:
    """A connected stream cannot be retained before its first START."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    old_stream = _make_kept_stream()
    _transport(bridge)._airplay_stream = old_stream
    bridge.airplay_player.stream = old_stream

    bridge._on_bridge_stream_start()

    assert _transport(bridge)._airplay_stream is None
    # the player keeps the reference until the scheduled teardown has the process
    # off the receiver, so nothing reads the speaker as free while it is not
    assert bridge.airplay_player.stream is old_stream


def test_on_stream_start_keeps_warm_eligible_stream() -> None:
    """The Sendspin-server-side stream-start callback also keeps a warm-eligible stream."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = _make_kept_stream()
    _transport(bridge)._airplay_stream = kept_stream
    bridge.airplay_player.stream = kept_stream
    _transport(bridge)._started = True

    bridge._on_stream_start(MagicMock())

    assert _transport(bridge)._airplay_stream is kept_stream
    assert bridge.airplay_player.stream is kept_stream


async def test_warm_stream_flushes_and_reanchors_on_kept_instance() -> None:
    """A warm handover flushes and re-anchors START on the same stream instance."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + WARM_LEAD_MS * 1_000
    commanded = UNIX_NOW_MS + WARM_LEAD_MS
    kept_stream = _make_anchor_stream(ack=commanded)
    _transport(bridge)._airplay_stream = kept_stream
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
        return_value=UNIX_NOW_S,
    ):
        committed = await _transport(bridge)._start_warm_stream(kept_stream)

    assert committed is True
    assert _transport(bridge)._airplay_stream is kept_stream  # no new instance was built
    kept_stream.flush.assert_awaited_once_with()
    kept_stream.start.assert_awaited_once_with(commanded, join=True)
    assert _transport(bridge)._started is True
    assert _transport(bridge)._airplay_stream_ready.is_set()


async def test_warm_stream_flush_timeout_falls_back_to_cold() -> None:
    """A flush that is never acknowledged never re-anchors and falls back to cold."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = _make_anchor_stream()
    kept_stream.flush = AsyncMock(return_value=False)
    _transport(bridge)._airplay_stream = kept_stream
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()

    committed = await _transport(bridge)._start_warm_stream(kept_stream)

    assert committed is False
    kept_stream.start.assert_not_awaited()
    assert _transport(bridge)._started is False


async def test_warm_stream_superseded_before_start_does_not_anchor() -> None:
    """If a newer stream start already owns the bridge, the stale flush never re-anchors."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = _make_anchor_stream()
    _transport(bridge)._airplay_stream = kept_stream
    # Simulate a newer stream start having already replaced the tracked task.
    _transport(bridge)._airplay_stream_start_task = MagicMock()

    committed = await _transport(bridge)._start_warm_stream(kept_stream)

    assert committed is False
    kept_stream.start.assert_not_awaited()


async def test_warm_stream_cancellation_propagates() -> None:
    """Cancellation while flushing propagates without re-anchoring."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    kept_stream = _make_anchor_stream()
    flush_waiting = asyncio.Event()

    async def flush(*_args: object, **_kwargs: object) -> bool:
        _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
        flush_waiting.set()
        await asyncio.Event().wait()
        return True

    kept_stream.flush = AsyncMock(side_effect=flush)
    _transport(bridge)._airplay_stream = kept_stream

    warm_task = asyncio.create_task(_transport(bridge)._start_warm_stream(kept_stream))
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
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + WARM_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    kept_stream = _make_anchor_stream()
    _transport(bridge)._airplay_stream = kept_stream
    bridge.airplay_player.stream = kept_stream
    cold_stream = _make_anchor_stream()

    async def start(start_unix_ms: int, **_kwargs: object) -> int:
        # A newer stream start claimed the bridge while the binary held its ack.
        _transport(bridge)._airplay_stream_start_task = MagicMock()
        return start_unix_ms

    kept_stream.start = AsyncMock(side_effect=start)

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=cold_stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
            return_value=UNIX_NOW_S,
        ),
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    kept_stream.stop.assert_not_awaited()
    cold_stream.connect.assert_not_awaited()
    assert _transport(bridge)._airplay_stream is kept_stream
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
    _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    _transport(bridge)._hold_chunk(_pcm_chunk(SENDSPIN_EPOCH_US))
    newer_stream = _make_anchor_stream()
    stale_stream = _make_anchor_stream()

    async def connect(_use_shared_ptp: bool | None) -> None:
        # The newer start won the receiver, so this one cannot have it.
        _transport(bridge)._airplay_stream_start_task = MagicMock()
        _transport(bridge)._airplay_stream = newer_stream
        bridge.airplay_player.stream = newer_stream
        raise OSError("device busy")

    stale_stream.connect = AsyncMock(side_effect=connect)

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
        return_value=stale_stream,
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    assert bridge._is_streaming is True
    assert len(_transport(bridge)._held_chunks) == 1
    assert not _transport(bridge)._airplay_stream_ready.is_set()
    assert _transport(bridge)._airplay_stream is newer_stream
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
    _transport(bridge)._airplay_stream = _make_kept_stream()
    _transport(bridge)._started = True

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
    _transport(bridge)._airplay_stream = kept_stream
    bridge.airplay_player.stream = kept_stream
    _transport(bridge)._started = True
    observed: list[tuple[bool, object]] = []
    refresh = bridge._refresh_bridge_timing

    def record() -> None:
        observed.append((_transport(bridge)._started, _transport(bridge)._airplay_stream))
        refresh()

    with patch.object(bridge, "_refresh_bridge_timing", record):
        bridge._on_stream_start(MagicMock())

    assert observed == [(True, kept_stream)]
    role.set_timing.assert_called_once_with(
        required_lead_time_ms=BRIDGE_WARM_START_LEAD_MS, min_buffer_ms=BRIDGE_MIN_BUFFER_MS
    )


# --- Mid-stream transport loss: re-anchoring, and giving up when it keeps dropping ---


def _make_completed_start_task(*, failed: bool = False) -> MagicMock:
    """
    Build a start-task mock the chunk handler reads as a finished protocol start.

    Every predicate must answer a real bool: a bare MagicMock reports itself as
    cancelled, which the handler reads as a failed start.
    """
    task = MagicMock()
    task.done.return_value = True
    task.cancelled.return_value = failed
    task.exception.return_value = None
    return task


def _make_anchored_bridge(
    *, running: bool, ended_cleanly: bool = False, receiver_closed: bool = False
) -> tuple[SendspinAirPlayBridge, MagicMock]:
    """
    Return a bridge anchored on a transport in the given running state, plus that transport.

    :param running: Whether the cli process behind the transport is still alive.
    :param ended_cleanly: Whether the binary reported the end of the stream
        itself, which stops the transport without losing it.
    :param receiver_closed: Whether the receiver ended the session itself.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    stream = _make_kept_stream(
        running=running, ended_cleanly=ended_cleanly, receiver_closed=receiver_closed
    )
    _play_anchored(_transport(bridge), stream)
    return bridge, stream


def _play_anchored(transport: _BridgeTransport, stream: MagicMock) -> None:
    """
    Model a transport whose start finished and that plays a stream anchored at the epoch.

    :param transport: The transport to model.
    :param stream: The stream it plays.
    """
    transport._airplay_stream = stream
    transport.endpoint.stream = stream
    transport._airplay_stream_start_task = _make_completed_start_task()
    transport._started = True
    transport._anchor_settled = True
    transport._drop_until_us = SENDSPIN_EPOCH_US


def _deliver_chunks(bridge: SendspinAirPlayBridge, start_us: int, end_us: int) -> list[AudioChunk]:
    """
    Deliver contiguous chunks from one instant of the Sendspin timeline on; return them.

    :param bridge: The bridge to deliver them to.
    :param start_us: The instant the first chunk plays at.
    :param end_us: The instant before which the last chunk starts.
    """
    chunks = [_pcm_chunk(timestamp_us) for timestamp_us in range(start_us, end_us, 100_000)]
    for chunk in chunks:
        bridge._on_audio_chunk(chunk)
    return chunks


def test_a_lost_transport_starts_again_on_the_audio_the_bridge_holds() -> None:
    """
    A transport that died mid-stream starts again where a late joiner starts.

    Sendspin delivers up to its whole buffer ahead of what the group plays, so started
    on the chunk that exposes the loss the speaker would stay silent that long. The
    chunks before that one were delivered already, and the bridge still holds them.
    """
    bridge, stream = _make_anchored_bridge(running=True)
    # on a grid that straddles the instant a late joiner starts at
    delivered = _deliver_chunks(
        bridge, SENDSPIN_EPOCH_US + 50_000, SENDSPIN_EPOCH_US + SENDSPIN_BUFFER_US
    )
    stream.running = False
    exposing = _pcm_chunk(delivered[-1].timestamp_us + 100_000)

    with patch.object(_transport(bridge), "_start_protocol_from_chunk", MagicMock()) as start:
        bridge._on_audio_chunk(exposing)

    start.assert_called_once_with()
    join_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    kept = [chunk for chunk in delivered if chunk.timestamp_us + chunk.duration_us > join_us]
    # provisionally anchored on the first chunk that still plays at the join instant
    assert kept[0].timestamp_us < join_us
    assert _transport(bridge)._drop_until_us == kept[0].timestamp_us
    # held from there through the chunk that exposed the loss, each once and in order
    assert list(_transport(bridge)._held_chunks) == [*kept, exposing]


def test_lost_transport_rearms_a_cold_start_on_the_current_chunk() -> None:
    """
    A transport that died mid-stream is released and re-anchored on the live timeline.

    The CLI accepts and discards writes once its process is gone, so the loss is
    only visible on the stream itself. A live source keeps the bridge supplied only
    about a second ahead, short of where a late joiner starts, so the chunk that
    exposes the loss is the one the fresh transport anchors to.
    """
    bridge, dead_stream = _make_anchored_bridge(running=True)
    _deliver_chunks(bridge, SENDSPIN_EPOCH_US, SENDSPIN_EPOCH_US + 1_000_000)
    dead_stream.running = False
    chunk_ts = SENDSPIN_EPOCH_US + 1_000_000

    bridge._on_audio_chunk(_pcm_chunk(chunk_ts))

    assert _transport(bridge)._airplay_stream is None
    # released from the bridge, but still published until the teardown that owns
    # it has the dead process cleared
    assert bridge.airplay_player.stream is dead_stream
    assert dead_stream.superseded is True
    assert _transport(bridge)._started is False
    assert _transport(bridge)._anchor_settled is False
    # a fresh start is armed and anchored on the chunk that exposed the loss
    assert _transport(bridge)._drop_until_us == chunk_ts
    # the chunk is held until the new anchor is acked, not placed against the dead one
    assert len(_transport(bridge)._held_chunks) == 1


def test_the_bridge_keeps_the_audio_its_group_has_yet_to_play() -> None:
    """A delivered chunk is kept until the group played it out, then dropped as the next comes."""
    bridge, _ = _make_anchored_bridge(running=True)
    delivered = _deliver_chunks(bridge, SENDSPIN_EPOCH_US, SENDSPIN_EPOCH_US + 1_000_000)
    assert list(bridge._unplayed) == delivered
    # the group played the first three chunks out, the third one just now
    cast("ManualClock", bridge.sendspin_server.clock).advance_us(300_000)
    later = _pcm_chunk(SENDSPIN_EPOCH_US + 1_000_000)

    bridge._on_audio_chunk(later)

    assert list(bridge._unplayed) == [*delivered[3:], later]


def test_a_new_sendspin_stream_starts_on_its_own_first_chunk() -> None:
    """
    A new Sendspin stream keeps none of the audio of the stream before it.

    A seek or a next track starts a new stream, scheduled on the instants the stream
    before it was scheduled on as well.
    """
    bridge, _ = _make_anchored_bridge(running=True)
    _deliver_chunks(bridge, SENDSPIN_EPOCH_US, SENDSPIN_EPOCH_US + SENDSPIN_BUFFER_US)

    bridge._on_bridge_stream_start()

    assert not bridge._unplayed
    first = _pcm_chunk(SENDSPIN_EPOCH_US + 250_000)
    with patch.object(_transport(bridge), "_start_protocol_from_chunk", MagicMock()):
        bridge._on_audio_chunk(first)
    assert _transport(bridge)._drop_until_us == first.timestamp_us
    assert list(bridge._unplayed) == [first]


@pytest.mark.parametrize("stop", ["stream_end", "release", "stop"])
async def test_a_bridge_that_stops_streaming_keeps_no_audio(stop: str) -> None:
    """A bridge lets go of the audio it kept for its stream once it stops streaming."""
    bridge, _ = _make_anchored_bridge(running=True)
    _deliver_chunks(bridge, SENDSPIN_EPOCH_US, SENDSPIN_EPOCH_US + 1_000_000)
    assert bridge._unplayed

    if stop == "stream_end":
        bridge._on_bridge_stream_end()
    elif stop == "release":
        bridge._release_streaming()
    else:
        await bridge._stop_streaming()

    assert not bridge._unplayed


def test_lost_transport_and_its_writer_are_torn_down() -> None:
    """The dead transport and the writer feeding it are handed to the cleanup path."""
    bridge, dead_stream = _make_anchored_bridge(running=False)
    writer_task = MagicMock()
    _transport(bridge)._writer_task = writer_task
    start_task = _transport(bridge)._airplay_stream_start_task

    with patch.object(_transport(bridge), "_cleanup_old_stream", MagicMock()) as cleanup:
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 1_000_000))

    assert cleanup.call_args.args[:3] == (dead_stream, writer_task, start_task)


def test_live_transport_keeps_streaming_untouched() -> None:
    """A running transport is left alone: chunks keep flowing to the same stream."""
    bridge, stream = _make_anchored_bridge(running=True)
    start_task = _transport(bridge)._airplay_stream_start_task

    bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 1_000_000))

    assert _transport(bridge)._airplay_stream is stream
    assert _transport(bridge)._airplay_stream_start_task is start_task
    assert _transport(bridge)._started is True
    assert not _transport(bridge)._write_queue.empty()


def test_transport_is_not_judged_while_a_start_is_in_flight() -> None:
    """
    A start owns its transport, so a stream it is tearing down is not a loss.

    A warm handover that fails stops the kept stream before dropping it, leaving
    a window where the bridge still points at a stopped stream. Restarting from
    that window would fight the start already falling back to a cold reconnect.
    """
    bridge, stopped_stream = _make_anchored_bridge(running=False)
    cast("MagicMock", _transport(bridge)._airplay_stream_start_task).done.return_value = False

    bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 1_000_000))

    assert _transport(bridge)._airplay_stream is stopped_stream
    assert _transport(bridge)._started is True


def test_unanchored_transport_is_not_treated_as_a_loss() -> None:
    """
    A stream that never anchored is the start's to report, not a mid-stream loss.

    Recovery re-joins the group where it plays, which only means anything once
    an anchor existed. A start that finished without one has already taken the
    bridge out of streaming through its own failure path.
    """
    bridge, stopped_stream = _make_anchored_bridge(running=False)
    start_task = _transport(bridge)._airplay_stream_start_task
    _transport(bridge)._started = False

    bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 1_000_000))

    assert _transport(bridge)._airplay_stream is stopped_stream
    assert _transport(bridge)._airplay_stream_start_task is start_task


def test_a_stream_the_native_path_took_over_is_not_recovered() -> None:
    """
    A transport the bridge no longer owns is not the bridge's to restart.

    The native path stops (or replaces) the player's stream without telling the
    bridge, which reads its own stopped stream as a crash. Recovering would put
    a second cli process on the same receiver and let the cold start publish its
    stream over the native session's.
    """
    bridge, stopped_stream = _make_anchored_bridge(running=False)
    # the native path took the player over and left the bridge holding a stream
    # that is no longer the player's
    cast("MagicMock", bridge.airplay_player).stream = _make_kept_stream()

    with patch.object(_transport(bridge), "restart", MagicMock()) as restart:
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 1_000_000))

    restart.assert_not_called()
    assert _transport(bridge)._airplay_stream is stopped_stream


def test_a_receiver_taken_over_by_another_sender_is_released_not_recovered() -> None:
    """
    A receiver another sender took over is left to that sender.

    Recovering would take the receiver straight back from whoever took it over.
    """
    bridge, _ = _make_anchored_bridge(running=False, receiver_closed=True)

    with (
        patch.object(_transport(bridge), "restart", MagicMock()) as restart,
        patch.object(bridge, "_leave_group_or_session", MagicMock()) as leave,
    ):
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 1_000_000))

    restart.assert_not_called()
    leave.assert_called_once_with()
    assert bridge._is_streaming is False


def _make_taken_over_bridge(
    *, synced_to: str | None = None, group_members: list[str] | None = None
) -> tuple[SendspinAirPlayBridge, MagicMock]:
    """
    Return a bridge whose Sendspin client is in the given group state, plus the players mock.

    :param synced_to: The Sendspin protocol id of the leader the client follows.
    :param group_members: The members of the group the client leads.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._bridge_client_id = "ss_bridge_client"
    cast("MagicMock", bridge.airplay_player).player.player_id = "own_player"
    cast("MagicMock", bridge.airplay_player).player.active_group = None
    bridge_client = MagicMock(synced_to=synced_to, group_members=group_members or [])
    players = cast("MagicMock", bridge.mass).players
    players.get_protocol = MagicMock(return_value=bridge_client)
    players.get_player_by_protocol = MagicMock(return_value=MagicMock(player_id="leader_player"))
    players.get_player_lock = MagicMock(return_value=nullcontext())
    players.get_group_and_player_lock = MagicMock(return_value=nullcontext())
    players._handle_set_members = AsyncMock()
    return bridge, players


async def test_a_taken_over_group_member_leaves_through_the_controller() -> None:
    """A member is taken out of its leader's group, so the rest of the group plays on."""
    bridge, players = _make_taken_over_bridge(synced_to="ss_leader_client")

    with patch.object(bridge, "_leave_sendspin_session", AsyncMock()) as leave:
        await bridge._leave_group_or_session()

    players.get_player_by_protocol.assert_called_once_with("ss_leader_client")
    players.get_player_lock.assert_called_once_with("leader_player", PlayerLockPurpose.PLAYBACK)
    players._handle_set_members.assert_awaited_once_with(
        players.get_player_by_protocol.return_value, player_ids_to_remove=["own_player"]
    )
    leave.assert_not_called()


async def test_a_taken_over_group_leader_leaves_its_own_group_through_the_controller() -> None:
    """A leader leaves through the controller, which hands the group to a remaining member."""
    bridge, players = _make_taken_over_bridge(
        group_members=["ss_bridge_client", "ss_member_client"]
    )

    with patch.object(bridge, "_leave_sendspin_session", AsyncMock()) as leave:
        await bridge._leave_group_or_session()

    own_player = bridge.airplay_player.player
    players.get_player_lock.assert_called_once_with("own_player", PlayerLockPurpose.PLAYBACK)
    players._handle_set_members.assert_awaited_once_with(
        own_player, player_ids_to_remove=["own_player"]
    )
    leave.assert_not_called()


@pytest.mark.parametrize("group_members", [[], ["ss_bridge_client", "ss_member_client"]])
async def test_a_taken_over_member_of_a_group_player_is_taken_out_of_it(
    group_members: list[str],
) -> None:
    """The group player lets the speaker go, whether it leads the Sendspin group or not."""
    bridge, players = _make_taken_over_bridge(
        synced_to=None if group_members else "ss_leader_client", group_members=group_members
    )
    cast("MagicMock", bridge.airplay_player).player.active_group = "group_player"
    players.cmd_ungroup = AsyncMock()
    bridge._is_streaming = False

    await bridge._leave_group_or_session()

    players.get_group_and_player_lock.assert_called_once_with("own_player")
    players.cmd_ungroup.assert_awaited_once_with("own_player")
    players._handle_set_members.assert_not_called()


async def test_a_taken_over_member_that_plays_again_stays_in_its_group_player() -> None:
    """A takeover that is handled once the speaker plays in its group again changes nothing."""
    bridge, players = _make_taken_over_bridge(synced_to="ss_leader_client")
    cast("MagicMock", bridge.airplay_player).player.active_group = "group_player"
    players.cmd_ungroup = AsyncMock()
    bridge._is_streaming = True

    await bridge._leave_group_or_session()

    players.cmd_ungroup.assert_not_awaited()
    players._handle_set_members.assert_not_called()


async def test_a_taken_over_solo_speaker_ends_its_own_session() -> None:
    """A speaker playing alone leaves its Sendspin session, which is what stops it."""
    # a Sendspin client that plays alone lists itself as its only group member
    bridge, players = _make_taken_over_bridge(group_members=["ss_bridge_client"])

    with patch.object(bridge, "_leave_sendspin_session", AsyncMock()) as leave:
        await bridge._leave_group_or_session()

    leave.assert_awaited_once_with(rejoin=False)
    players._handle_set_members.assert_not_called()


def test_a_stream_the_binary_ended_itself_is_not_a_loss() -> None:
    """
    A cli process that reported the end of the stream did not lose its transport.

    The stderr loop also ends on a clean [STATUS] eof or the binary's idle cap,
    which stops the stream exactly like a crash does. Restarting one of those
    spawns a process for audio that is already over, and two such restarts
    inside the guard window take the speaker out of the group for good. Its
    counterpart is test_lost_transport_rearms_a_cold_start_on_the_current_chunk,
    where the same stopped stream ended without saying so.
    """
    bridge, ended_stream = _make_anchored_bridge(running=False, ended_cleanly=True)

    with patch.object(_transport(bridge), "restart", MagicMock()) as restart:
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 1_000_000))

    restart.assert_not_called()
    assert _transport(bridge)._airplay_stream is ended_stream
    assert _transport(bridge)._started is True


def test_restarting_the_transport_drops_a_deferred_teardown() -> None:
    """
    A teardown deferred by an earlier stream end must not fire into the new transport.

    The restart arms a transport that pending timer knows nothing about, so it is
    cancelled along with the stream it was scheduled for.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)

    bridge._restart_transport()

    cast("MagicMock", bridge.mass).cancel_timer.assert_called_once_with(bridge._teardown_timer_id)


def test_a_grace_timer_that_already_fired_spares_the_restarted_stream() -> None:
    """
    A teardown whose timer fired before the restart cancelled it leaves the new stream alone.

    cancel_timer cannot recall a handle that already fired, so a stream arriving
    at the very end of the grace window still gets the call. Reading the live
    fields there would cancel that stream's writer and drain its queue, leaving
    the speaker silent for the whole track -- and the warm restart keeps the same
    stream object, so telling the two apart by the stream alone cannot work.
    """
    bridge, stream = _make_anchored_bridge(running=True)
    _transport(bridge)._writer_task = MagicMock()

    bridge._on_bridge_stream_end()
    # the next stream arrives and rides the kept process, cancelling a timer that
    # has already fired
    bridge._restart_transport()
    new_writer_task = _transport(bridge)._writer_task
    _transport(bridge)._write_queue.put_nowait(b"\x00" * BRIDGE_BYTES_PER_FRAME)

    bridge._deferred_cleanup()

    assert _transport(bridge)._airplay_stream is stream
    assert _transport(bridge)._writer_task is new_writer_task
    assert not _transport(bridge)._write_queue.empty()


async def test_the_cleanup_a_start_waits_on_cannot_cancel_it() -> None:
    """
    A start waiting for the pending teardown is not among the handles it cancels.

    _start_protocol_from_chunk and _cli_writer both await _cleanup_task before
    touching the transport. A teardown reading the live fields when it finally
    ran would find the waiting start there and cancel it, killing the stream it
    was clearing the way for.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._is_streaming = False
    stream = _make_kept_stream()
    stream.stop = AsyncMock()
    _transport(bridge)._airplay_stream = stream
    _transport(bridge)._airplay_stream_start_task = _make_completed_start_task()

    bridge._schedule_cleanup()
    teardown = cast("MagicMock", bridge.mass).create_task.call_args.args[0]
    # the start that arrives next publishes itself and then awaits the teardown
    start = cast("asyncio.Task[None]", asyncio.current_task())
    _transport(bridge)._airplay_stream_start_task = start

    await teardown

    # the teardown ran against what the bridge held when it was scheduled
    stream.stop.assert_awaited_once_with(force=True)
    assert start.cancelling() == 0


def test_a_new_sendspin_stream_restores_the_recovery_budget() -> None:
    """
    Every Sendspin stream starts with a full recovery budget.

    A loss on the previous stream says nothing about the device's health on this
    one; carrying the stamp over would abandon a speaker on its very first loss.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._last_transport_recovery = 100.0

    bridge._on_stream_start(MagicMock())

    assert _transport(bridge)._last_transport_recovery is None


def test_a_stream_start_on_an_unavailable_player_still_restores_the_budget() -> None:
    """
    The recovery budget is settled before any early return can skip it.

    The stream-start callback bails out when the player is unavailable, but the
    role-side entry point has no such gate; leaving the verdict of the previous
    stream in place would abandon the speaker on the next stream's first loss.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    cast("MagicMock", bridge.airplay_player).available = False
    _transport(bridge)._last_transport_recovery = 100.0

    bridge._on_stream_start(MagicMock())

    assert _transport(bridge)._last_transport_recovery is None


def test_second_transport_loss_within_the_guard_window_gives_up() -> None:
    """A device dropping its transport again right away is abandoned, not re-anchored."""
    bridge, _ = _make_anchored_bridge(running=False)

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.time.monotonic",
            side_effect=[100.0, 100.0 + BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS - 1],
        ),
        patch.object(_transport(bridge), "restart", MagicMock()) as restart,
        patch.object(bridge, "_abandon_streaming", MagicMock()) as abandon,
    ):
        assert _transport(bridge)._recover_transport() is True
        assert _transport(bridge)._recover_transport() is False

    restart.assert_called_once_with()
    abandon.assert_called_once_with()


def test_transport_loss_after_the_guard_window_recovers_again() -> None:
    """A single blip hours apart is a new incident, not a flapping device."""
    bridge, _ = _make_anchored_bridge(running=False)

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.time.monotonic",
            side_effect=[100.0, 100.0 + BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS + 1],
        ),
        patch.object(_transport(bridge), "restart", MagicMock()) as restart,
        patch.object(bridge, "_abandon_streaming", MagicMock()) as abandon,
    ):
        assert _transport(bridge)._recover_transport() is True
        assert _transport(bridge)._recover_transport() is True

    assert restart.call_count == 2
    abandon.assert_not_called()


def test_giving_up_does_not_queue_the_chunk_that_exposed_the_loss() -> None:
    """
    The chunk that trips the give-up is dropped, not written into the dead stream.

    Giving up leaves the anchor and the stream reference untouched, so a chunk
    that carried on through the handler would still be placed and queued.
    """
    bridge, _ = _make_anchored_bridge(running=False)
    _transport(bridge)._last_transport_recovery = 100.0

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.time.monotonic",
            return_value=100.0 + BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS - 1,
        ),
        patch.object(_transport(bridge), "restart", MagicMock()) as restart,
    ):
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 1_000_000))

    restart.assert_not_called()
    assert _transport(bridge)._write_queue.empty()


async def test_a_failed_protocol_start_leaves_the_session() -> None:
    """
    A cold start that raised takes the speaker out of the group it cannot play in.

    Whether the start was the stream's first or a replacement for a transport
    that died, the outcome is the same silence; leaving is what stops the player
    reporting playback nobody can hear.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
    stream = _make_anchor_stream()
    stream.connect = AsyncMock(side_effect=OSError("no route to device"))

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=stream,
        ),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        await _transport(bridge)._start_protocol_from_chunk()

    assert bridge._is_streaming is False
    leave.assert_called_once_with()


async def test_losing_a_speaker_for_good_runs_the_whole_chain() -> None:
    """
    End to end: a transport dies, the reconnect is refused, the speaker leaves the group.

    Every step here is the real one -- detection, the recovery decision, the
    re-arm and the cold start -- so a give-up swallowed anywhere along that
    chain shows up as a speaker that stays silently "playing" instead of
    dropping out.
    """
    bridge, _ = _make_anchored_bridge(running=False)
    stream = _make_anchor_stream()
    stream.connect = AsyncMock(side_effect=OSError("device gone"))

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=stream,
        ),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        # the chunk that exposes the loss re-arms and anchors a replacement
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 1_000_000))
        assert _transport(bridge)._airplay_stream_start_task is not None
        leave.assert_not_called()
        # run the replacement start the chunk handler scheduled
        start = cast("MagicMock", bridge.mass).create_task.call_args.args[0]
        _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
        await start

    assert bridge._is_streaming is False
    leave.assert_called_once_with()


def test_abandoning_streaming_stops_the_feed_and_leaves_the_session() -> None:
    """Giving up stops accepting chunks, unblocks the writer and leaves the session."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._held_chunks.append(_pcm_chunk(SENDSPIN_EPOCH_US))
    _transport(bridge)._held_us = 100_000

    with patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave:
        bridge._abandon_streaming()

    assert bridge._is_streaming is False
    assert not _transport(bridge)._held_chunks
    assert _transport(bridge)._held_us == 0
    assert _transport(bridge)._airplay_stream_ready.is_set()
    # scheduled, not merely constructed: an unscheduled coroutine never leaves
    leave.assert_called_once_with()
    scheduled = [call.args[0] for call in cast("MagicMock", bridge.mass).create_task.call_args_list]
    assert leave.return_value in scheduled


def test_a_flapping_device_is_taken_out_of_the_sendspin_session() -> None:
    """
    Only a device that cannot hold a transport is dropped from the group.

    Its silence is real and permanent, so the visible player must stop reporting
    playback; the rest of the group keeps going without it.
    """
    bridge, _ = _make_anchored_bridge(running=False)

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.time.monotonic",
            side_effect=[100.0, 100.0 + BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS - 1],
        ),
        patch.object(_transport(bridge), "restart", MagicMock()),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        assert _transport(bridge)._recover_transport() is True
        # the first loss is recoverable, so the speaker keeps its place
        leave.assert_not_called()
        assert _transport(bridge)._recover_transport() is False

    # scheduled, not merely constructed: an unscheduled coroutine never leaves
    leave.assert_called_once_with()
    scheduled = [call.args[0] for call in cast("MagicMock", bridge.mass).create_task.call_args_list]
    assert leave.return_value in scheduled


def test_failed_start_task_gives_up_on_the_stream() -> None:
    """A protocol start that failed stops the feed and drops out of the group."""
    bridge, _ = _make_anchored_bridge(running=True)
    _transport(bridge)._airplay_stream_start_task = _make_completed_start_task(failed=True)

    with patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave:
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 1_000_000))

    assert bridge._is_streaming is False
    leave.assert_called_once_with()


async def test_writer_readiness_timeout_gives_up_on_the_stream() -> None:
    """
    A protocol that never becomes ready stops the feed and drops out of the group.

    A transport that hangs instead of failing renders the same silence as one
    that refused the connection, so it is given up on the same way.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._writer_task = asyncio.current_task()
    _transport(bridge)._airplay_stream_ready = MagicMock(wait=AsyncMock(side_effect=TimeoutError))

    with patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave:
        await _transport(bridge)._cli_writer()

    assert bridge._is_streaming is False
    leave.assert_called_once_with()


async def test_a_stale_writer_cannot_give_up_on_a_newer_stream() -> None:
    """
    Only the writer still feeding the bridge may abandon it.

    A writer left behind by a slow teardown speaks for a stream that is already
    gone; letting it give up would stop, and un-group, its successor.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    # a newer stream owns the bridge; this writer is the previous stream's
    _transport(bridge)._writer_task = MagicMock()
    _transport(bridge)._airplay_stream_ready = MagicMock(wait=AsyncMock(side_effect=TimeoutError))

    with patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave:
        await _transport(bridge)._cli_writer()

    assert bridge._is_streaming is True
    leave.assert_not_called()


def _make_grouped_client(*, group_members: int = 2, has_active_stream: bool = False) -> MagicMock:
    """
    Build a bridge client mock that reads as having left a shared group.

    Quiescing moves the client on to a solo group, exactly as the real one does,
    so a caller reading ``client.group`` after leaving no longer sees the group
    that was left.

    :param group_members: Members in the group the client lands in after
        leaving; more than one means it was grouped again meanwhile.
    :param has_active_stream: Whether that group is playing something of its own.
    """
    client = MagicMock()
    client.group.clients = [MagicMock() for _ in range(group_members)]
    client.group.has_active_stream = has_active_stream

    async def _quiesce() -> str:
        client.group = MagicMock(clients=[client], has_active_stream=False)
        return "group-1"

    # a real group id: leaving a shared group is what earns a re-join, and None
    # (a solo group, which leaving simply stops) must stay distinguishable
    client.quiesce_to_solo_stopped = AsyncMock(side_effect=_quiesce)
    return client


async def test_leaving_a_shared_group_lines_up_a_rejoin() -> None:
    """
    A bridge taken out of a shared group is given an attempt to come back.

    The group it left is captured before quiescing, because that is what moves
    the client into a solo group of its own.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    client = _make_grouped_client()
    left_group = client.group
    bridge._sendspin_client = client

    with patch.object(bridge, "_rejoin_attempts", MagicMock()) as rejoin:
        await bridge._leave_sendspin_session()

    rejoin.assert_called_once_with(left_group)


async def test_leaving_a_solo_group_has_nothing_to_rejoin() -> None:
    """
    A solo bridge is stopped by leaving, so there is no group to return to.

    Quiescing reports that by returning no previous group; scheduling a re-join
    against the group it is already alone in would put it back on PLAYING.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    client = _make_grouped_client()
    client.quiesce_to_solo_stopped = AsyncMock(return_value=None)
    bridge._sendspin_client = client

    with patch.object(bridge, "_rejoin_attempts", MagicMock()) as rejoin:
        await bridge._leave_sendspin_session()

    rejoin.assert_not_called()


async def test_a_speaker_that_fails_again_right_after_a_rejoin_stays_out() -> None:
    """
    A speaker that keeps dropping out cannot cycle in and out of its group.

    Re-joining re-runs the stream start that just failed, and a device that
    accepts a START before dying would otherwise earn a fresh attempt every
    time round, churning CLI processes and group membership indefinitely.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._sendspin_client = _make_grouped_client()
    bridge._last_rejoin = 100.0

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.time.monotonic",
            return_value=100.0 + BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS - 1,
        ),
        patch.object(bridge, "_rejoin_attempts", MagicMock()) as rejoin,
    ):
        await bridge._leave_sendspin_session()

    rejoin.assert_not_called()


async def test_a_speaker_that_held_its_place_earns_another_rejoin() -> None:
    """A device that played on for a while before failing is worth bringing back again."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._sendspin_client = _make_grouped_client()
    bridge._last_rejoin = 100.0

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.time.monotonic",
            return_value=100.0 + BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS + 1,
        ),
        patch.object(bridge, "_rejoin_attempts", MagicMock()) as rejoin,
    ):
        await bridge._leave_sendspin_session()

    rejoin.assert_called_once()


async def test_the_rejoin_window_is_measured_from_the_actual_rejoin() -> None:
    """
    The guard is stamped where the speaker rejoins, not where the attempt was scheduled.

    Stamping at schedule time would tie the guard to the backoff: longer delays
    would put the stamp far enough in the past for the window to have expired by
    the time the re-joined speaker fails, letting the cycle run again.
    """
    bridge, _, group = _make_rejoin_bridge()

    with (
        patch(_NO_REJOIN_DELAYS, (0,)),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.time.monotonic",
            return_value=1234.0,
        ),
    ):
        await bridge._rejoin_attempts(group)

    group.add_client.assert_awaited_once()
    assert bridge._last_rejoin == 1234.0


async def test_a_failed_rejoin_never_stamps_the_window() -> None:
    """A speaker that never made it back has not held a place to be judged on."""
    bridge, _, group = _make_rejoin_bridge()
    group.add_client = AsyncMock(side_effect=OSError("group is gone"))

    with patch(_NO_REJOIN_DELAYS, (0,)):
        await bridge._rejoin_attempts(group)

    assert bridge._last_rejoin is None


async def test_a_give_up_inside_the_window_drops_a_pending_rejoin() -> None:
    """
    Leaving the speaker out means dropping the attempt that would put it back.

    A schedule left running would contradict the decision this give-up just
    made, and re-add a speaker that was meant to stay out.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._sendspin_client = _make_grouped_client()
    bridge._last_rejoin = 100.0
    pending = MagicMock()
    pending.done.return_value = False
    bridge._rejoin_task = pending

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.time.monotonic",
        return_value=100.0 + BRIDGE_TRANSPORT_RECOVERY_GUARD_SECONDS - 1,
    ):
        await bridge._leave_sendspin_session()

    assert bridge._rejoin_task is None
    pending.cancel.assert_called_once_with()  # type: ignore[unreachable]


def _make_rejoin_bridge(
    *, group_members: int = 1, has_active_stream: bool = False
) -> tuple[SendspinAirPlayBridge, MagicMock, MagicMock]:
    """
    Build a bridge in the state a give-up leaves behind, with its client and lost group.

    The AirPlay stream is cleared explicitly: a give-up tears it down, and a
    bridge still pointing at one reads as a speaker streaming outside the
    bridge, which is itself a reason not to re-join.

    :param group_members: Members of the group the client sits in now.
    :param has_active_stream: Whether that group is playing something of its own.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    cast("MagicMock", bridge.airplay_player).stream = None
    # A real False: a MagicMock reads as a speaker switched off at its power control.
    cast("MagicMock", bridge.airplay_player).player.offline_by_power_control = False
    client = _make_grouped_client(group_members=group_members, has_active_stream=has_active_stream)
    bridge._sendspin_client = client
    group = MagicMock()
    group.clients = [MagicMock()]
    group.add_client = AsyncMock()
    return bridge, client, group


async def test_a_rejoin_puts_the_bridge_back_into_the_group_it_left() -> None:
    """The bridge re-joins through the ordinary group-add, which re-runs the stream start."""
    bridge, client, group = _make_rejoin_bridge()

    with patch(_NO_REJOIN_DELAYS, (0,)):
        await bridge._rejoin_attempts(group)

    group.add_client.assert_awaited_once_with(client)


async def test_a_rejoin_leaves_a_regrouped_speaker_alone() -> None:
    """
    A speaker grouped again meanwhile is never pulled out of where it was put.

    The re-join answers a failure, not the user; landing anywhere other than the
    solo group the give-up left means someone else has since decided otherwise.
    """
    bridge, _, group = _make_rejoin_bridge(group_members=2)

    with patch(_NO_REJOIN_DELAYS, (0,)):
        await bridge._rejoin_attempts(group)

    group.add_client.assert_not_awaited()


async def test_a_rejoin_leaves_a_speaker_playing_on_its_own_alone() -> None:
    """
    A speaker started on its own meanwhile keeps that playback.

    Its solo group has one member, so membership alone cannot tell it apart from
    the group the give-up left it in -- but adding a client to another group
    stops the group it came from, which here is the user's own playback.
    """
    bridge, _, group = _make_rejoin_bridge(has_active_stream=True)

    with patch(_NO_REJOIN_DELAYS, (0,)):
        await bridge._rejoin_attempts(group)

    group.add_client.assert_not_awaited()


async def test_a_rejoin_leaves_a_natively_streaming_speaker_alone() -> None:
    """
    A speaker taken over by native AirPlay is not dragged back into Sendspin.

    Re-joining restarts the bridge transport, which would tear down a session
    the bridge does not own.
    """
    bridge, _, group = _make_rejoin_bridge()
    cast("MagicMock", bridge.airplay_player).stream = MagicMock()

    with patch(_NO_REJOIN_DELAYS, (0,)):
        await bridge._rejoin_attempts(group)

    group.add_client.assert_not_awaited()


async def test_an_offline_speaker_is_looked_for_again_before_giving_up() -> None:
    """
    A speaker missing from discovery is never re-joined, but is looked for again.

    A rebooting device is absent from discovery for a while after it starts
    answering, so abandoning on the first look would spend the whole re-join
    budget inside the window where such a device is always missing. Running out
    of attempts, rather than returning on the first one, is what shows the later
    look happened.
    """
    bridge, _, group = _make_rejoin_bridge()
    cast("MagicMock", bridge.airplay_player).available = False
    logger = MagicMock()
    bridge.logger = logger

    with patch(_NO_REJOIN_DELAYS, (0, 0)):
        await bridge._rejoin_attempts(group)

    group.add_client.assert_not_awaited()
    assert logger.debug.call_count == 2
    # the give-up is only reached once the attempts run out
    logger.warning.assert_called_once()


async def test_a_switched_off_speaker_is_not_rejoined() -> None:
    """
    A speaker switched off at its power control is left out of the group for good.

    Its protocol still reads available: a device without power says no goodbye.
    Returning on the first look, rather than running out of attempts, is what
    shows the bridge gave up instead of looking for it again.
    """
    bridge, _, group = _make_rejoin_bridge()
    cast("MagicMock", bridge.airplay_player).player.offline_by_power_control = True
    logger = MagicMock()
    bridge.logger = logger

    with patch(_NO_REJOIN_DELAYS, (0, 0)):
        await bridge._rejoin_attempts(group)

    group.add_client.assert_not_awaited()
    logger.debug.assert_called_once()
    logger.warning.assert_not_called()


async def test_a_rejoin_is_abandoned_when_the_group_is_gone() -> None:
    """
    A group everyone else has left is not a group to return to.

    Its object outlives the members holding it, so adding the bridge back would
    strand it alone in a group nothing streams to.
    """
    bridge, _, group = _make_rejoin_bridge()
    group.clients = []

    with patch(_NO_REJOIN_DELAYS, (0,)):
        await bridge._rejoin_attempts(group)

    group.add_client.assert_not_awaited()


async def test_a_rejoin_that_keeps_failing_gives_up() -> None:
    """Every attempt is tried, and a speaker that never returns leaves the player idle."""
    bridge, _, group = _make_rejoin_bridge()
    group.add_client = AsyncMock(side_effect=OSError("group is gone"))

    with patch(_NO_REJOIN_DELAYS, (0, 0)):
        await bridge._rejoin_attempts(group)

    assert group.add_client.await_count == 2


async def test_a_new_stream_supersedes_a_pending_rejoin() -> None:
    """Joining a session by any means makes the pending re-join stale."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    pending = MagicMock()
    pending.done.return_value = False
    bridge._rejoin_task = pending

    bridge._on_stream_start(MagicMock())

    assert bridge._rejoin_task is None
    pending.cancel.assert_called_once_with()  # type: ignore[unreachable]


async def test_a_rejoin_never_cancels_itself() -> None:
    """
    The re-join survives the stream start it causes.

    Adding the bridge back to the group runs the stream-start path that clears
    stale schedules, and that path cannot be allowed to kill the attempt making
    the call.
    """
    bridge, client, group = _make_rejoin_bridge()

    async def _add_client(_client: MagicMock) -> None:
        bridge._rejoin_task = asyncio.current_task()
        bridge._on_stream_start(MagicMock())

    group.add_client = AsyncMock(side_effect=_add_client)

    with patch(_NO_REJOIN_DELAYS, (0,)):
        await bridge._rejoin_attempts(group)

    group.add_client.assert_awaited_once_with(client)


async def test_stopping_the_bridge_drops_a_pending_rejoin() -> None:
    """An unloaded bridge has no group to return to."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    pending = MagicMock()
    pending.done.return_value = False
    bridge._rejoin_task = pending

    await bridge.stop()

    assert bridge._rejoin_task is None
    pending.cancel.assert_called_once_with()  # type: ignore[unreachable]


async def test_leaving_the_session_quiesces_the_bridge_client() -> None:
    """The bridge leaves a shared group (or stops a solo one) but stays registered."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    client = _make_grouped_client()
    bridge._sendspin_client = client

    await bridge._leave_sendspin_session()

    client.quiesce_to_solo_stopped.assert_awaited_once_with()
    # staying registered is what keeps the player around for the next stream
    cast("MagicMock", bridge.sendspin_server).remove_client.assert_not_called()


async def test_leaving_the_session_without_a_client_is_a_noop() -> None:
    """
    Giving up before registration completed has no session to leave.

    The call has to return without touching anything: swallowing an error from
    an absent client would look identical from the outside, so the absence of a
    complaint is what distinguishes the two.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    logger = MagicMock()
    bridge.logger = logger
    assert bridge._sendspin_client is None

    await bridge._leave_sendspin_session()

    logger.warning.assert_not_called()


# --- An explicit stop: end playback now, without lining up a return ------------


def _bridge_manager_for(bridge: SendspinAirPlayBridge) -> SendspinBridgeManager:
    """Return a bridge manager holding the given bridge under its player id."""
    manager = SendspinBridgeManager(cast("MagicMock", bridge.provider))
    manager._bridges[bridge.airplay_player.protocol_id] = bridge
    return manager


async def test_an_explicit_stop_tears_the_transport_down_at_once() -> None:
    """
    A stop the user asked for stops the speaker now, not after the grace window.

    A Sendspin stream ending defers the teardown so the next track can ride the
    warm binary; nothing follows a stop, and the device holds seconds of audio,
    so deferring there just plays out what the user asked to end.
    """
    bridge, stream = _make_anchored_bridge(running=True)
    writer_task = MagicMock()
    _transport(bridge)._writer_task = writer_task
    start_task = _transport(bridge)._airplay_stream_start_task
    manager = _bridge_manager_for(bridge)

    with (
        patch.object(_transport(bridge), "_cleanup_old_stream", MagicMock()) as cleanup,
        patch.object(bridge, "_leave_sendspin_session", MagicMock()),
    ):
        assert manager.stop_streaming(bridge.airplay_player.protocol_id) is True

    assert cleanup.call_args.args[:3] == (stream, writer_task, start_task)
    assert bridge._is_streaming is False
    assert _transport(bridge)._airplay_stream is None
    # no grace window is armed: that is what the teardown would have waited out
    cast("MagicMock", bridge.mass).call_later.assert_not_called()


async def test_an_explicit_stop_leaves_the_session_without_a_return() -> None:
    """
    Stopping takes the speaker out of the session, and it stays out.

    Sendspin reports playback from the group's state, so a stopped bridge that
    stayed in would hold the visible player on PLAYING. The re-join exists to
    recover a speaker that dropped out by itself; a user who stopped one has not
    asked for it back.
    """
    bridge, _ = _make_anchored_bridge(running=True)
    manager = _bridge_manager_for(bridge)

    with patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave:
        manager.stop_streaming(bridge.airplay_player.protocol_id)

    leave.assert_called_once_with(rejoin=False)
    # scheduled, not merely constructed: an unscheduled coroutine never leaves
    scheduled = [call.args[0] for call in cast("MagicMock", bridge.mass).create_task.call_args_list]
    assert leave.return_value in scheduled


async def test_a_stop_of_an_idle_bridge_keeps_its_place_in_the_group() -> None:
    """
    A bridge with nothing playing has no session to leave.

    Its group is not reporting playback through this speaker, so quiescing it out
    would only cost a grouped-but-idle player its membership on a stop command.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._is_streaming = False
    manager = _bridge_manager_for(bridge)

    with patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave:
        assert manager.stop_streaming(bridge.airplay_player.protocol_id) is True

    leave.assert_not_called()


async def test_a_stop_never_reaches_a_player_without_a_bridge() -> None:
    """An unbridged player is left to the caller's own stop path."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)

    assert _bridge_manager_for(bridge).stop_streaming("apother") is False


# --- A stop on the Sendspin side skips the warm grace (support#6195) -----------


def test_a_sendspin_stop_skips_the_grace_and_tears_the_transport_down() -> None:
    """
    A stop reaching the bridge through its role kills the CLI at once.

    The stream end that precedes it arms the warm grace window, during which
    the device plays out the seconds it holds buffered. The explicit-stop
    signal cancels that window and hands the transport straight to the
    cleanup path.
    """
    bridge, stream = _make_anchored_bridge(running=True)
    writer_task = MagicMock()
    _transport(bridge)._writer_task = writer_task
    start_task = _transport(bridge)._airplay_stream_start_task

    with patch.object(_transport(bridge), "_cleanup_old_stream", MagicMock()) as cleanup:
        bridge._on_bridge_stream_end()
        bridge._on_bridge_explicit_stop()

    cast("MagicMock", bridge.mass).cancel_timer.assert_called_with(bridge._teardown_timer_id)
    assert cleanup.call_args.args[:3] == (stream, writer_task, start_task)
    assert _transport(bridge)._airplay_stream is None


def test_a_sendspin_stop_keeps_the_bridges_seat_in_the_group() -> None:
    """
    A group-wide stop leaves the membership alone.

    The group's own STOPPED state is what the visible player reports, and the
    next play on the group must include this speaker -- unlike a stop aimed at
    the AirPlay player itself, there is no session to leave here.
    """
    bridge, _ = _make_anchored_bridge(running=True)

    with (
        patch.object(_transport(bridge), "_cleanup_old_stream", MagicMock()),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        bridge._on_bridge_stream_end()
        bridge._on_bridge_explicit_stop()

    leave.assert_not_called()


def test_an_explicit_stop_spares_a_stream_that_already_took_over() -> None:
    """A play racing the stop owns the transport; the stop must not kill it."""
    bridge, stream = _make_anchored_bridge(running=True)
    bridge._is_streaming = True

    with patch.object(bridge, "_schedule_cleanup", MagicMock()) as schedule:
        bridge._on_bridge_explicit_stop()

    schedule.assert_not_called()
    assert _transport(bridge)._airplay_stream is stream


def test_an_explicit_stop_with_nothing_held_is_a_no_op() -> None:
    """The ungroup that follows a stop finds the transport already gone."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    bridge._is_streaming = False

    with patch.object(bridge, "_schedule_cleanup", MagicMock()) as schedule:
        bridge._on_bridge_explicit_stop()

    schedule.assert_not_called()


def test_a_stream_end_with_no_transport_arms_no_grace_timer() -> None:
    """
    A stream end that left nothing behind has nothing to keep warm or defer.

    Removing the member from its Sendspin group ends the stream for its roles
    a second time after the stop already tore the transport down; re-arming
    the timer there would only reschedule an empty teardown.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)

    bridge._on_bridge_stream_end()

    cast("MagicMock", bridge.mass).call_later.assert_not_called()
    assert bridge._is_streaming is False


# --- One shared-PTP decision per Sendspin group --------------------------------


def _group_bridges(*bridges: SendspinAirPlayBridge, daemon_ready: bool) -> SendspinBridgeManager:
    """
    Put the given bridges in one Sendspin group behind a shared bridge manager.

    :param bridges: Bridges to place in the group.
    :param daemon_ready: What the shared PTP daemon answers a fresh resolve.
    """
    provider = MagicMock()
    provider.ptp_daemon_ready = daemon_ready
    manager = SendspinBridgeManager(provider)
    provider.bridge_manager = manager
    group = MagicMock()
    for index, bridge in enumerate(bridges):
        bridge.provider = provider
        bridge._sendspin_client = MagicMock()
        bridge._sendspin_client.group = group
        manager._bridges[f"player{index}"] = bridge
    return manager


def test_the_first_group_member_asks_the_daemon() -> None:
    """With no live decision in the group, the daemon's readiness decides."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _group_bridges(bridge, daemon_ready=True)

    assert bridge._resolve_shared_ptp() is True


@pytest.mark.parametrize(("live_decision", "daemon_ready"), [(True, False), (False, True)])
def test_a_later_member_adopts_the_groups_live_decision(
    live_decision: bool, daemon_ready: bool
) -> None:
    """
    A member starting later joins on the clock the group is already running.

    Bridges in one group can start minutes apart, so what the daemon answers at
    the second start says nothing about the source the first member's process
    was spawned against. Parametrised both ways so the decision is proven to
    follow the sibling rather than the daemon.
    """
    playing = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    joiner = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _group_bridges(playing, joiner, daemon_ready=daemon_ready)
    _transport(playing)._use_shared_ptp = live_decision

    assert joiner._resolve_shared_ptp() is live_decision


def test_a_warm_member_still_speaks_for_the_group() -> None:
    """
    A process kept for a warm reuse keeps deciding for its group.

    Its Sendspin stream ended, but the next one rides that same cli process with
    the flag it was spawned with, so a sibling cold-starting alongside it has to
    match that flag rather than resolve against the daemon.
    """
    warm = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    joiner = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _group_bridges(warm, joiner, daemon_ready=False)
    _transport(warm)._use_shared_ptp = True
    _transport(warm)._airplay_stream = _make_kept_stream()
    _transport(warm)._started = True
    warm._is_streaming = False

    assert warm.active_shared_ptp is True
    assert joiner._resolve_shared_ptp() is True


def test_an_idle_member_does_not_decide() -> None:
    """A bridge with no cli process left leaves the group to resolve fresh."""
    idle = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    starter = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _group_bridges(idle, starter, daemon_ready=True)
    _transport(idle)._use_shared_ptp = False
    idle._is_streaming = False

    assert idle.active_shared_ptp is None
    assert starter._resolve_shared_ptp() is True


def test_another_groups_decision_is_not_adopted() -> None:
    """Only members of the same Sendspin group share one timing source."""
    stranger = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    starter = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _group_bridges(stranger, starter, daemon_ready=False)
    _transport(stranger)._use_shared_ptp = True
    # the stranger moved on to a group of its own
    stranger_client = MagicMock()
    stranger_client.group = MagicMock()
    stranger._sendspin_client = stranger_client

    assert starter._resolve_shared_ptp() is False


def test_a_raop_member_carries_no_decision() -> None:
    """A legacy RAOP process has no shared-clock flag to hand its group."""
    raop = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US, protocol=StreamingProtocol.RAOP)
    _group_bridges(raop, daemon_ready=True)

    assert _transport(raop)._resolve_shared_ptp() is None


async def test_a_cold_start_spawns_the_cli_with_the_groups_decision() -> None:
    """The adopted decision reaches the cli process and is recorded on the bridge."""
    playing = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    joiner = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _group_bridges(playing, joiner, daemon_ready=False)
    _transport(playing)._use_shared_ptp = True
    _transport(joiner)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    _transport(joiner)._airplay_stream_start_task = asyncio.current_task()
    stream = _make_anchor_stream(ack=UNIX_NOW_MS + COLD_LEAD_MS)

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
        return_value=UNIX_NOW_S,
    ):
        assert await _transport(joiner)._start_cold_stream(stream) is True

    stream.connect.assert_awaited_once_with(True)
    assert joiner.active_shared_ptp is True


async def test_a_daemon_lost_mid_start_cannot_split_the_group() -> None:
    """
    Members starting together agree even when the daemon goes away between them.

    The first member records its decision before it awaits its connect, so the
    second one finds it however the daemon answers by the time it resolves.
    """
    first = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    second = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    manager = _group_bridges(first, second, daemon_ready=True)
    first_stream = _make_anchor_stream(ack=UNIX_NOW_MS + COLD_LEAD_MS)
    second_stream = _make_anchor_stream(ack=UNIX_NOW_MS + COLD_LEAD_MS)

    async def connect(_use_shared_ptp: bool | None) -> None:
        # the daemon dies while the first member is still connecting
        cast("MagicMock", manager.provider).ptp_daemon_ready = False
        await asyncio.sleep(0)

    first_stream.connect = AsyncMock(side_effect=connect)

    async def cold_start(bridge: SendspinAirPlayBridge, stream: MagicMock) -> None:
        _transport(bridge)._drop_until_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
        _transport(bridge)._airplay_stream_start_task = asyncio.current_task()
        await _transport(bridge)._start_cold_stream(stream)

    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
        return_value=UNIX_NOW_S,
    ):
        await asyncio.gather(cold_start(first, first_stream), cold_start(second, second_stream))

    assert first.active_shared_ptp is True
    assert second.active_shared_ptp is True
    second_stream.connect.assert_awaited_once_with(True)


async def test_a_torn_down_bridge_stops_deciding() -> None:
    """
    The decision dies with the cli process it was spawned for.

    A new Sendspin stream arms the bridge before it resolves, so a decision left
    behind by the torn-down process would be handed to the group on its behalf.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _transport(bridge)._use_shared_ptp = True

    await bridge._stop_streaming()
    bridge._on_stream_start(MagicMock())

    assert bridge._is_streaming is True
    assert bridge.active_shared_ptp is None


def _make_warm_bridge(
    *,
    use_shared_ptp: bool | None,
    protocol: StreamingProtocol = StreamingProtocol.AIRPLAY2,
) -> SendspinAirPlayBridge:
    """
    Build a bridge holding a connected, anchored cli process on the given flag.

    :param use_shared_ptp: The shared-PTP flag its process was spawned with,
        None for a process that carries no such decision.
    :param protocol: The streaming protocol the bridged player speaks.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US, protocol=protocol)
    _transport(bridge)._airplay_stream = _make_kept_stream()
    _transport(bridge)._started = True
    _transport(bridge)._use_shared_ptp = use_shared_ptp
    return bridge


def test_a_regrouped_warm_process_is_not_reused() -> None:
    """
    A process whose flag no longer matches its group has to be respawned.

    The flag is baked in at spawn, so reusing the process would keep the bridge
    on the clock its old group ran on. Its start lead has to report the cold
    figure too, or the respawn lands past the audio Sendspin already scheduled.
    """
    regrouped = _make_warm_bridge(use_shared_ptp=False)
    playing = _make_warm_bridge(use_shared_ptp=True)
    _group_bridges(regrouped, playing, daemon_ready=True)
    regrouped._bridge_role = MagicMock()

    assert _transport(regrouped)._stream_is_warm_eligible() is True
    assert regrouped._can_reuse_stream_warm() is False

    regrouped._refresh_bridge_timing()

    regrouped._bridge_role.set_timing.assert_called_once_with(
        required_lead_time_ms=BRIDGE_COLD_START_LEAD_MS, min_buffer_ms=BRIDGE_MIN_BUFFER_MS
    )


def test_a_warm_process_matching_its_group_is_reused() -> None:
    """A group already on the process's flag costs it no respawn."""
    warm = _make_warm_bridge(use_shared_ptp=True)
    playing = _make_warm_bridge(use_shared_ptp=True)
    _group_bridges(warm, playing, daemon_ready=False)

    assert warm._can_reuse_stream_warm() is True


def test_a_group_without_a_live_decision_reuses_the_warm_process() -> None:
    """
    A bridge whose group has no other live decision keeps its process.

    Its own process is the group's decision, so a daemon that changed state
    since must not churn the transport on every track change.
    """
    solo = _make_warm_bridge(use_shared_ptp=True)
    _group_bridges(solo, daemon_ready=False)

    assert solo._can_reuse_stream_warm() is True


def test_a_raop_member_keeps_its_warm_process_beside_an_ap2_member() -> None:
    """
    A RAOP process is never respawned over a group's shared-clock decision.

    It carries no such decision of its own, and no respawn could give it one, so
    comparing it against an AirPlay 2 sibling's would cost the group a cold
    reconnect (and its longer start lead) on every track change for nothing.
    """
    raop = _make_warm_bridge(use_shared_ptp=None, protocol=StreamingProtocol.RAOP)
    ap2 = _make_warm_bridge(use_shared_ptp=True)
    _group_bridges(raop, ap2, daemon_ready=True)
    raop._bridge_role = MagicMock()

    assert raop._can_reuse_stream_warm() is True

    raop._refresh_bridge_timing()

    raop._bridge_role.set_timing.assert_called_once_with(
        required_lead_time_ms=BRIDGE_WARM_START_LEAD_MS, min_buffer_ms=BRIDGE_MIN_BUFFER_MS
    )


async def test_the_real_chunk_path_records_the_decision_it_spawns_with() -> None:
    """
    Driving the bridge the way Sendspin does still records what the CLI got.

    The start path tells whether it still owns the bridge by comparing itself
    against the task handle the chunk handler publishes, so the start task must
    not run before that handle is set. Started eagerly it would read None on its
    very first check and give up as if a newer start had claimed the bridge.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    _group_bridges(bridge, daemon_ready=True)
    stream = _make_anchor_stream(ack=UNIX_NOW_MS + COLD_LEAD_MS)
    started: list[asyncio.Task[None]] = []

    async def connect(_use_shared_ptp: bool | None) -> None:
        # a real connect does I/O, so the task suspends here
        await asyncio.sleep(0)

    stream.connect = AsyncMock(side_effect=connect)

    loop = asyncio.get_running_loop()

    def create_task(
        coro: Coroutine[None, None, None], *, eager_start: bool = True, **_kwargs: object
    ) -> asyncio.Task[None]:
        # mirrors mass.create_task, whose default eager start would run the
        # coroutine to its first await before this returns
        task = asyncio.Task(coro, loop=loop, eager_start=eager_start)
        started.append(task)
        return task

    cast("MagicMock", bridge.mass).create_task = create_task

    with (
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.AirPlayStream",
            return_value=stream,
        ),
        patch(
            "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
            return_value=UNIX_NOW_S,
        ),
    ):
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000))
        await asyncio.gather(*started)

    stream.connect.assert_awaited_once_with(True)
    assert bridge.active_shared_ptp is True


@pytest.mark.parametrize("arm", ["sendspin_stream_start", "transport_restart"])
def test_a_released_process_stops_deciding_for_its_group(arm: str) -> None:
    """
    A process the bridge is about to tear down no longer speaks for its group.

    Arming the bridge for its next stream happens well before that stream
    resolves, so a decision left over from the released process would be handed
    to a sibling resolving in between - and after a regroup it is the wrong one.
    """
    regrouped = _make_warm_bridge(use_shared_ptp=False)
    playing = _make_warm_bridge(use_shared_ptp=True)
    _group_bridges(regrouped, playing, daemon_ready=True)

    if arm == "sendspin_stream_start":
        regrouped._on_stream_start(MagicMock())
    else:
        regrouped._on_bridge_stream_start()

    assert regrouped._is_streaming is True
    assert regrouped.active_shared_ptp is None
    # the sibling still holding a live process keeps deciding for the group
    assert playing.active_shared_ptp is True


def test_an_abandoned_process_stops_deciding_for_its_group() -> None:
    """Giving up on a transport takes its decision out of the group with it."""
    abandoned = _make_warm_bridge(use_shared_ptp=True)
    _group_bridges(abandoned, daemon_ready=True)

    abandoned._abandon_streaming()

    assert _transport(abandoned)._use_shared_ptp is None
    assert abandoned.active_shared_ptp is None


# --- Volume/mute moved on the AirPlay side, fed back into the bridge role ------


def _make_bridge_with_role(
    volume: int | None = 40, muted: bool = False
) -> tuple[SendspinAirPlayBridge, BridgePlayerRole]:
    """Build a bridge whose (real) role is wired to its mocked AirPlay player."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    role = BridgePlayerRole(client=MagicMock())
    role.set_callbacks(
        on_audio_chunk=bridge._on_audio_chunk,
        on_volume_change=bridge._on_volume_change,
        on_mute_change=bridge._on_mute_change,
        on_stream_start=bridge._on_bridge_stream_start,
        on_stream_end=bridge._on_bridge_stream_end,
        initial_volume=volume or 25,
        initial_muted=muted,
    )
    bridge._bridge_role = role
    player = cast("MagicMock", bridge.airplay_player)
    player.volume_level = volume
    player.volume_muted = muted
    return bridge, role


async def test_registration_seeds_the_role_with_the_state_the_speaker_is_in() -> None:
    """
    A bridge (re)registering adopts the volume and mute the speaker is already at.

    A bridge is torn down and rebuilt on a config change, so starting from a
    fixed unmuted default would re-create the divergence on every rebuild.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    player = cast("MagicMock", bridge.airplay_player)
    player.volume_level = 35
    player.volume_muted = True
    role = MagicMock()
    server = cast("MagicMock", bridge.sendspin_server)
    server.register_external_player.return_value.roles_by_family.return_value = [role]

    await bridge.start()

    assert role.set_callbacks.call_args.kwargs["initial_volume"] == 35
    assert role.set_callbacks.call_args.kwargs["initial_muted"] is True


async def test_the_bridge_client_rides_on_the_airplay_protocol() -> None:
    """The Sendspin protocol of the bridge belongs to the player of the AirPlay protocol."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    sendspin = cast("MagicMock", bridge.mass).get_provider.return_value

    await bridge.start()

    sendspin.register_bridge_underlying_protocol.assert_called_once_with(
        "spb_c43875e9e53a", "apc43875e9e53a"
    )
    sendspin.register_bridge_identifiers.assert_called_once_with(
        "spb_c43875e9e53a", {IdentifierType.AIRPLAY_ID: "apc43875e9e53a"}
    )


def test_device_volume_feedback_reaches_the_visible_player() -> None:
    """
    A volume the device reports itself is adopted by the role, not sent back to it.

    The role is what the player's volume resolves to while the bridge streams, so
    it has to follow the speaker; forwarding it would hand the device back the
    value it just reported.
    """
    bridge, role = _make_bridge_with_role(volume=40)
    player = cast("MagicMock", bridge.airplay_player)
    player.volume_level = 55

    bridge.sync_role_volume_state()

    assert role.get_player_volume() == 55
    player.volume_set.assert_not_called()


def test_a_mute_latched_on_the_airplay_side_shows_through_the_bridge() -> None:
    """
    A mute applied on the AirPlay side is visible on the Sendspin player.

    While it is latched the AirPlay player swallows every volume command, so a
    bridge still reporting the speaker as unmuted leaves nothing to explain why
    it is silent - or to unmute it with.
    """
    bridge, role = _make_bridge_with_role(muted=False)
    player = cast("MagicMock", bridge.airplay_player)
    player.volume_muted = True

    bridge.sync_role_volume_state()

    assert role.get_player_muted() is True
    player.volume_mute.assert_not_called()


def _player_of(*protocols: Any) -> Player:
    """Return a player stand-in with the given protocols attached, and an id of its own."""
    return cast(
        "Player",
        SimpleNamespace(
            player_id="pl0123456789ab",
            attached_protocols={protocol.protocol_id: protocol for protocol in protocols},
        ),
    )


def test_an_airplay_volume_change_is_routed_to_that_player_s_bridge() -> None:
    """A state update carrying a volume or mute change lands on the bridge of its protocol."""
    bridge, role = _make_bridge_with_role(volume=40)
    manager = _bridge_manager_for(bridge)
    player = cast("MagicMock", bridge.airplay_player)
    player.volume_level = 55
    player.volume_muted = True

    manager._on_player_state_updated(
        _player_of(player), {"volume_level": (40, 55), "volume_muted": (False, True)}
    )

    assert role.get_player_volume() == 55
    assert role.get_player_muted() is True


def test_state_updates_without_a_volume_change_leave_the_role_alone() -> None:
    """
    Every player's state update passes through, so unrelated ones do no work.

    The callback runs for the whole player graph on every tick, including the
    position updates of a playing queue.
    """
    bridge, role = _make_bridge_with_role(volume=40)
    manager = _bridge_manager_for(bridge)
    player = cast("MagicMock", bridge.airplay_player)
    player.volume_level = 55

    manager._on_player_state_updated(_player_of(player), {"playback_state": ("idle", "playing")})

    assert role.get_player_volume() == 40


def test_a_player_without_a_bridge_is_ignored() -> None:
    """A volume change on a player this manager knows nothing about leaves bridges alone."""
    bridge, role = _make_bridge_with_role(volume=40)
    manager = _bridge_manager_for(bridge)
    other_protocol = MagicMock()
    other_protocol.protocol_id = "ap0011223344ff"
    other_protocol.volume_level = 55

    manager._on_player_state_updated(_player_of(other_protocol), {"volume_level": (40, 55)})

    assert role.get_player_volume() == 40


def test_the_manager_listens_for_the_state_updates_it_routes() -> None:
    """
    The players of the bridged AirPlay protocols are watched for the whole life of the manager.

    The controller's internal state-update subscription is how a volume change of such
    a player is seen.
    """
    manager = SendspinBridgeManager(MagicMock())

    cast("MagicMock", manager.mass).players.subscribe_player_state_update.assert_called_once_with(
        manager._on_player_state_updated
    )


def test_a_volume_the_airplay_protocol_reports_reaches_its_bridge() -> None:
    """
    The AirPlay protocol hands its own volume feedback to its bridge.

    While the player plays over Sendspin, the bridge role is its volume control, so a
    level the speaker reports does not move the player's state by itself.
    """
    bridge, role = _make_bridge_with_role(volume=40)
    manager = _bridge_manager_for(bridge)
    player = cast("MagicMock", bridge.airplay_player)
    player.volume_level = 55

    manager.sync_volume("ap0011223344ff")
    assert role.get_player_volume() == 40

    manager.sync_volume(player.protocol_id)
    assert role.get_player_volume() == 55


def _bridge_client(
    manager: SendspinBridgeManager, bridge: SendspinAirPlayBridge, synced_to: str | None
) -> None:
    """
    Let the bridge own the AirPlay stream, with its Sendspin protocol attached to a player.

    :param manager: The manager holding the bridge.
    :param bridge: The bridge.
    :param synced_to: The Sendspin protocol the bridge client follows, if any.
    """
    bridge._bridge_client_id = "bridge_c43875e9e53a"
    stream = MagicMock()
    _transport(bridge)._airplay_stream = stream
    cast("MagicMock", bridge.airplay_player).stream = stream
    client = MagicMock(synced_to=synced_to, player=SimpleNamespace(player_id="pl_kitchen"))
    leader_player = SimpleNamespace(player_id="pl_living")
    players = cast("MagicMock", manager.mass).players
    players.get_protocol.side_effect = {"bridge_c43875e9e53a": client}.get
    players.get_player_by_protocol.side_effect = {"sendspin_living": leader_player}.get


@pytest.mark.parametrize(
    ("synced_to", "target"), [(None, "pl_kitchen"), ("sendspin_living", "pl_living")]
)
def test_a_remote_command_goes_to_the_player_of_the_sendspin_session(
    synced_to: str | None, target: str
) -> None:
    """A command from the receiver goes to the player whose Sendspin session plays on it."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    manager = _bridge_manager_for(bridge)
    _bridge_client(manager, bridge, synced_to)

    assert manager.get_transport_command_target(bridge.airplay_player.protocol_id) == target


def test_no_command_target_while_the_bridge_does_not_play() -> None:
    """A receiver Sendspin does not play on keeps its commands to itself."""
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    manager = _bridge_manager_for(bridge)
    _bridge_client(manager, bridge, None)
    cast("MagicMock", bridge.airplay_player).stream = MagicMock()

    assert manager.get_transport_command_target(bridge.airplay_player.protocol_id) is None
    assert manager.get_transport_command_target("ap0011223344ff") is None


def test_a_volume_set_through_the_bridge_settles_in_one_pass() -> None:
    """
    A volume coming down from Sendspin is not announced again on its way back.

    The player ends up holding what the role handed it, so reading that state back
    has to compare equal - otherwise every command would bounce between the two.
    """
    bridge, role = _make_bridge_with_role(volume=40)
    player = cast("MagicMock", bridge.airplay_player)
    client = cast("MagicMock", role._client)

    role.set_player_volume(70)
    player.volume_set.assert_called_once_with(70)
    # the AirPlay player records the level it was handed
    player.volume_level = 70
    client.reset_mock()

    bridge.sync_role_volume_state()

    assert role.get_player_volume() == 70
    client._signal_event.assert_not_called()


def test_a_volume_from_the_role_is_not_resolved_a_second_time() -> None:
    """
    A volume the role delivers reaches the speaker as-is, not via the controller.

    The level arrives on the device's own scale, already resolved and scaled where
    it came from Music Assistant, so sending it back through the controller would
    resolve it a second time and return over the same route.
    """
    bridge, role = _make_bridge_with_role(volume=40)
    player = cast("MagicMock", bridge.airplay_player)
    players_ctrl = cast("MagicMock", bridge.mass).players

    role.set_player_volume(60)
    role.set_player_mute(True)

    player.volume_set.assert_called_once_with(60)
    player.volume_mute.assert_called_once_with(True)
    players_ctrl.cmd_volume_set.assert_not_called()
    players_ctrl.cmd_volume_mute.assert_not_called()


# --- The bridge of a home theater set: its speakers, and its Apple TV after them ---


def _make_room_bridge(
    speakers: int = 1, *, homepods_only: bool = False
) -> tuple[SendspinAirPlayBridge, list[MagicMock], MagicMock]:
    """
    Build the bridge of the Apple TV of a set, which plays on the HomePods of the set.

    :param speakers: How many HomePods the set has.
    :param homepods_only: Whether the set plays on its HomePods only.
    :return: The bridge, its HomePods and its Apple TV.
    """
    bridge = _make_bridge(clock_now_us=SENDSPIN_EPOCH_US)
    apple_tv = cast("MagicMock", bridge.airplay_player)
    apple_tv.renders_audio = False
    homepods = []
    for index in range(speakers):
        homepod = MagicMock(
            protocol_id=f"aphomepod{index}",
            display_name=f"HomePod {index}",
            protocol=StreamingProtocol.AIRPLAY2,
            stream=None,
            sync_offset_ms=0,
            renders_audio=True,
        )
        homepod.stream_spawn_lock = asyncio.Lock()
        homepods.append(homepod)
    apple_tv.endpoints = homepods
    apple_tv.silent_member = None if homepods_only else apple_tv
    bridge._sync_transports()
    return bridge, homepods, apple_tv


def _anchor_started(*transports: _BridgeTransport) -> None:
    """Model the starts of transports acked on the anchor they asked for."""
    for transport in transports:
        cast("MagicMock", transport._airplay_stream_start_task).done.return_value = False
        transport._anchor_settled = True


def _starting(transport: _BridgeTransport) -> bool:
    """Return whether a start was set off for a transport, read anew at every call."""
    return transport.starting


def test_a_room_starts_its_speakers_first_and_its_apple_tv_once_they_play() -> None:
    """
    The speakers start on the first chunk; the Apple TV joins at a chunk after they play.

    An Apple TV whose SETUP goes out before the one of a speaker resets its session.
    """
    bridge, homepods, apple_tv = _make_room_bridge(speakers=2)
    speakers = [bridge._transports[homepod.protocol_id] for homepod in homepods]
    silent = bridge._transports[apple_tv.protocol_id]
    join_ts = SENDSPIN_EPOCH_US + 600_000

    with patch.object(_BridgeTransport, "_start_protocol_from_chunk", MagicMock()):
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 500_000))
        assert [transport.starting for transport in speakers] == [True, True]
        assert _starting(silent) is False
        _anchor_started(speakers[0])
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 550_000))
        # one speaker plays, the other one is still starting
        assert _starting(silent) is False
        _anchor_started(speakers[1])
        bridge._on_audio_chunk(_pcm_chunk(join_ts))

    assert _starting(silent) is True
    # anchored where the speakers play now, not at the chunk Sendspin sent ahead
    assert silent._drop_until_us == SENDSPIN_EPOCH_US


def test_the_apple_tv_of_a_room_is_fed_silence() -> None:
    """The speakers get the audio of a chunk, the Apple TV the same length of silence."""
    bridge, (homepod,), apple_tv = _make_room_bridge()
    chunk = _pcm_chunk(SENDSPIN_EPOCH_US)
    chunk = AudioChunk(
        data=b"\x01" * len(chunk.data),
        timestamp_us=chunk.timestamp_us,
        duration_us=chunk.duration_us,
        byte_count=len(chunk.data),
    )

    for transport in (
        bridge._transports[homepod.protocol_id],
        bridge._transports[apple_tv.protocol_id],
    ):
        transport._anchor_settled = True
        transport._drop_until_us = SENDSPIN_EPOCH_US
        transport._align_chunk(chunk)

    assert bridge._transports[homepod.protocol_id]._write_queue.get_nowait() == chunk.data
    assert bridge._transports[apple_tv.protocol_id]._write_queue.get_nowait() == bytes(
        len(chunk.data)
    )


async def test_only_a_speaker_of_a_room_reports_dropped_audio() -> None:
    """An anchor past the first chunk drops audio of a speaker, but nothing of the Apple TV."""
    bridge, (homepod,), apple_tv = _make_room_bridge()

    for transport in (
        bridge._transports[homepod.protocol_id],
        bridge._transports[apple_tv.protocol_id],
    ):
        stream = _make_anchor_stream(ack=UNIX_NOW_MS + 3_000)
        transport._drop_until_us = bridge.sendspin_server.clock.now_us()
        transport._airplay_stream = stream
        transport._airplay_stream_start_task = asyncio.current_task()
        with patch(
            "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
            return_value=UNIX_NOW_S,
        ):
            assert await transport._anchor_stream(stream, warm=False) is True

    warnings = cast("MagicMock", bridge.logger).warning.call_args_list
    assert [call.args[1] for call in warnings if "dropped" in call.args[0]] == ["HomePod 0"]


def test_a_speaker_of_a_room_that_fails_to_start_ends_the_stream_of_the_room() -> None:
    """A set plays on all of its speakers or not at all."""
    bridge, homepods, _ = _make_room_bridge(speakers=2)
    failed = bridge._transports[homepods[1].protocol_id]
    failed._airplay_stream_start_task = _make_completed_start_task(failed=True)

    with patch.object(bridge, "_abandon_streaming", MagicMock()) as abandon:
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 500_000))

    abandon.assert_called_once_with()


def test_the_apple_tv_of_a_room_that_fails_is_added_once_more_then_left_out() -> None:
    """
    The Apple TV never stops its speakers: it is added once more after a wait, then left out.

    A session to an Apple TV that reset stays up when it is opened some seconds later.
    """
    bridge, (homepod,), apple_tv = _make_room_bridge()
    speaker = bridge._transports[homepod.protocol_id]
    silent = bridge._transports[apple_tv.protocol_id]
    silent._airplay_stream_start_task = _make_completed_start_task(failed=True)
    speaker._airplay_stream_start_task = MagicMock()
    speaker._drop_until_us = SENDSPIN_EPOCH_US
    _anchor_started(speaker)
    monotonic = "music_assistant.providers.airplay.sendspin_bridge.time.monotonic"

    with (
        patch.object(bridge, "_abandon_streaming", MagicMock()) as abandon,
        patch.object(_BridgeTransport, "_start_protocol_from_chunk", MagicMock()),
    ):
        with patch(monotonic, return_value=100.0):
            bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 500_000))
            assert silent.retried is True
            assert _starting(silent) is False
            # not before the wait is over
            bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 600_000))
            assert _starting(silent) is False
        with patch(monotonic, return_value=100.0 + AIRPLAY_SILENT_MEMBER_REJOIN_DELAY):
            bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 700_000))
        assert _starting(silent) is True
        silent._airplay_stream_start_task = _make_completed_start_task(failed=True)
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 800_000))

    abandon.assert_not_called()
    assert list(bridge._transports) == [homepod.protocol_id]
    # nothing in this stream brings it back
    bridge.update_endpoints()
    assert list(bridge._transports) == [homepod.protocol_id]


def test_a_speaker_of_a_room_that_sets_up_again_gives_the_apple_tv_its_retry_back() -> None:
    """A HomePod that sets up again resets the session of its Apple TV, which may then retry."""
    bridge, (homepod,), apple_tv = _make_room_bridge()
    silent = bridge._transports[apple_tv.protocol_id]
    silent.retried = True
    speaker = bridge._transports[homepod.protocol_id]
    speaker._airplay_stream = _make_kept_stream(running=False)
    homepod.stream = speaker._airplay_stream
    speaker._airplay_stream_start_task = _make_completed_start_task()
    speaker._started = True

    assert speaker._recover_transport() is True

    assert silent.retried is False


def test_a_homepod_that_lost_its_transport_starts_again_while_the_other_one_plays_on() -> None:
    """A HomePod whose transport died starts again on its own: the other one of its room plays on."""
    bridge, homepods, _ = _make_room_bridge(speakers=2, homepods_only=True)
    # the HomePod that loses its transport takes each chunk first
    lost, playing = (bridge._transports[homepod.protocol_id] for homepod in homepods)
    lost_stream, playing_stream = _make_kept_stream(), _make_kept_stream()
    _play_anchored(lost, lost_stream)
    _play_anchored(playing, playing_stream)
    playing_start = playing._airplay_stream_start_task
    _deliver_chunks(bridge, SENDSPIN_EPOCH_US, SENDSPIN_EPOCH_US + SENDSPIN_BUFFER_US)
    lost_stream.running = False
    exposing = _pcm_chunk(SENDSPIN_EPOCH_US + SENDSPIN_BUFFER_US)

    with patch.object(_BridgeTransport, "_start_protocol_from_chunk", MagicMock()):
        bridge._on_audio_chunk(exposing)

    assert lost._airplay_stream is None
    assert lost.starting is True
    assert playing._airplay_stream is playing_stream
    assert playing._airplay_stream_start_task is playing_start
    assert playing.anchored is True
    assert playing._drop_until_us == SENDSPIN_EPOCH_US
    # it took the chunk that exposed the loss as well
    played_us = exposing.timestamp_us + exposing.duration_us - SENDSPIN_EPOCH_US
    assert playing._queued_frames == round(played_us * BRIDGE_SAMPLE_RATE / 1_000_000)


@pytest.mark.parametrize(
    ("speaker_set_up_ago", "added_once_more"),
    [(None, False), (1.0, True), (AIRPLAY_SILENT_MEMBER_RESET_WINDOW + 1, False)],
)
def test_an_apple_tv_that_ended_its_session_itself_is_not_taken_back(
    speaker_set_up_ago: float | None, added_once_more: bool
) -> None:
    """
    An Apple TV taken over by another sender, or put to sleep, is left out of the stream.

    Right after a speaker of its room set up again, that end is the reset the speaker caused, and
    the Apple TV is added once more.
    """
    bridge, _, apple_tv = _make_room_bridge()
    silent = bridge._transports[apple_tv.protocol_id]
    stream = _make_kept_stream(running=False, receiver_closed=True)
    silent._airplay_stream = stream
    apple_tv.stream = stream
    silent._airplay_stream_start_task = _make_completed_start_task()
    silent._started = True
    if speaker_set_up_ago is not None:
        bridge._renew_silent_retry()
        bridge._speaker_set_up_at = time.monotonic() - speaker_set_up_ago

    with (
        patch.object(bridge, "_abandon_streaming", MagicMock()) as abandon,
        patch.object(_BridgeTransport, "_start_protocol_from_chunk", MagicMock()),
    ):
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 1_000_000))

    abandon.assert_not_called()
    assert (apple_tv.protocol_id in bridge._transports) is added_once_more
    bridge.update_endpoints()
    assert (apple_tv.protocol_id in bridge._transports) is added_once_more


def test_a_room_that_plays_on_its_homepods_only_has_no_apple_tv_leg() -> None:
    """With the option on, the bridge feeds the HomePods of the room only."""
    bridge, homepods, _ = _make_room_bridge(speakers=2, homepods_only=True)

    assert list(bridge._transports) == [homepod.protocol_id for homepod in homepods]


def test_a_room_follows_its_option_and_its_speakers_while_it_plays() -> None:
    """The Apple TV leaves and joins a stream that plays, and so does a speaker of the set."""
    bridge, homepods, apple_tv = _make_room_bridge(speakers=2)
    bridge._is_streaming = True
    left_out = bridge._transports[apple_tv.protocol_id]

    apple_tv.silent_member = None
    apple_tv.endpoints = [homepods[0]]
    bridge.update_endpoints()
    assert list(bridge._transports) == [homepods[0].protocol_id]
    assert left_out.is_idle

    apple_tv.silent_member = apple_tv
    apple_tv.endpoints = homepods
    bridge.update_endpoints()
    added = [bridge._transports[homepods[1].protocol_id], bridge._transports[apple_tv.protocol_id]]
    assert left_out not in added
    # armed for the next chunk, where each starts as a join
    assert all(transport._writer_task is not None for transport in added)


def _homepod_back(bridge: SendspinAirPlayBridge, homepods: list[MagicMock]) -> _BridgeTransport:
    """
    Let the second HomePod of a room that plays leave it and come back; return its new transport.

    :param bridge: The bridge of the room, which streams.
    :param homepods: The two HomePods of the room.
    """
    apple_tv = cast("MagicMock", bridge.airplay_player)
    apple_tv.endpoints = [homepods[0]]
    bridge.update_endpoints()
    apple_tv.endpoints = homepods
    bridge.update_endpoints()
    return bridge._transports[homepods[1].protocol_id]


def test_a_homepod_back_while_its_room_plays_starts_where_the_room_plays() -> None:
    """
    A HomePod that comes back while its room plays starts on the audio the bridge holds.

    The next chunk Sendspin delivers sits up to its whole buffer ahead of what the room
    plays, so started on it the HomePod would stay silent that long.
    """
    bridge, homepods, _ = _make_room_bridge(speakers=2, homepods_only=True)
    for homepod in homepods:
        _play_anchored(bridge._transports[homepod.protocol_id], _make_kept_stream())
    # on a grid that straddles the instant a late joiner starts at
    delivered = _deliver_chunks(
        bridge, SENDSPIN_EPOCH_US + 50_000, SENDSPIN_EPOCH_US + SENDSPIN_BUFFER_US
    )

    with patch.object(_BridgeTransport, "_start_protocol_from_chunk", MagicMock()):
        back = _homepod_back(bridge, homepods)
        # set off at once, without waiting for a chunk
        assert back.starting is True
        cast("MagicMock", back._airplay_stream_start_task).done.return_value = False
        following = _pcm_chunk(delivered[-1].timestamp_us + 100_000)
        bridge._on_audio_chunk(following)

    join_us = SENDSPIN_EPOCH_US + COLD_LEAD_MS * 1_000
    kept = [chunk for chunk in delivered if chunk.timestamp_us + chunk.duration_us > join_us]
    assert back.joining_late is True
    assert back._drop_until_us == kept[0].timestamp_us
    # the chunk Sendspin delivers next follows them, and none is held twice
    assert list(back._held_chunks) == [*kept, following]


def test_a_homepod_back_after_sendspin_cleared_the_stream_starts_on_what_follows() -> None:
    """
    A HomePod that comes back after Sendspin cleared the stream starts on none of the cleared audio.

    A seek or a track change can clear the audio delivered so far while the stream goes on, and
    the room will not play what the bridge held of it.
    """
    bridge, homepods, _ = _make_room_bridge(speakers=2, homepods_only=True)
    for homepod in homepods:
        _play_anchored(bridge._transports[homepod.protocol_id], _make_kept_stream())
    _deliver_chunks(bridge, SENDSPIN_EPOCH_US, SENDSPIN_EPOCH_US + SENDSPIN_BUFFER_US)

    bridge._on_bridge_stream_clear()

    assert not bridge._unplayed
    with patch.object(_BridgeTransport, "_start_protocol_from_chunk", MagicMock()):
        back = _homepod_back(bridge, homepods)
    # armed for the next chunk Sendspin delivers, as at the start of a stream
    assert back.starting is False
    assert not back._held_chunks


def test_an_apple_tv_back_while_its_room_plays_starts_where_its_speakers_play() -> None:
    """
    The Apple TV added back to a room that plays starts on none of the audio the bridge holds.

    As at the start of a stream it starts once its speakers play, where they play then: its
    session resets when its SETUP precedes the one of a speaker.
    """
    bridge, (homepod,), apple_tv = _make_room_bridge()
    _play_anchored(bridge._transports[homepod.protocol_id], _make_kept_stream())
    # the room plays on its HomePod only for a while
    apple_tv.silent_member = None
    bridge.update_endpoints()
    _deliver_chunks(bridge, SENDSPIN_EPOCH_US, SENDSPIN_EPOCH_US + SENDSPIN_BUFFER_US)
    apple_tv.silent_member = apple_tv

    with patch.object(_BridgeTransport, "_start_protocol_from_chunk", MagicMock()):
        bridge.update_endpoints()
        silent = bridge._transports[apple_tv.protocol_id]
        assert _starting(silent) is False
        assert not silent._held_chunks
        following = _pcm_chunk(SENDSPIN_EPOCH_US + SENDSPIN_BUFFER_US)
        bridge._on_audio_chunk(following)

    assert _starting(silent) is True
    # anchored where its speaker plays now
    assert silent._drop_until_us == SENDSPIN_EPOCH_US
    assert list(silent._held_chunks) == [following]


def test_a_homepod_back_while_its_room_plays_that_cannot_join_is_left_out() -> None:
    """
    A HomePod that comes back while its room plays and fails before it joins is left out.

    The room plays on what is there until the HomePod comes back again. A speaker the stream
    started with still ends it when it fails.
    """
    bridge, homepods, _ = _make_room_bridge(speakers=2)
    bridge._is_streaming = True
    back = _homepod_back(bridge, homepods)
    back._airplay_stream_start_task = _make_completed_start_task(failed=True)

    with (
        patch.object(bridge, "_abandon_streaming", MagicMock()) as abandon,
        patch.object(_BridgeTransport, "_start_protocol_from_chunk", MagicMock()),
    ):
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 500_000))
        abandon.assert_not_called()
        assert homepods[1].protocol_id not in bridge._transports
        bridge.update_endpoints()
        assert homepods[1].protocol_id not in bridge._transports
        # it gets a new chance when it comes back again
        _homepod_back(bridge, homepods)
        assert homepods[1].protocol_id in bridge._transports
        first = bridge._transports[homepods[0].protocol_id]
        first._airplay_stream_start_task = _make_completed_start_task(failed=True)
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 600_000))

    abandon.assert_called_once_with()


def test_a_room_whose_other_homepod_leaves_after_one_could_not_join_stops() -> None:
    """A room has nothing heard to play on once its HomePod leaves and the other is left out."""
    bridge, homepods, apple_tv = _make_room_bridge(speakers=2)
    bridge._is_streaming = True
    back = _homepod_back(bridge, homepods)

    with patch.object(bridge, "_abandon_streaming", MagicMock()) as abandon:
        bridge._on_transport_failed(back)
        abandon.assert_not_called()
        apple_tv.endpoints = [homepods[1]]
        bridge.update_endpoints()

    abandon.assert_called_once_with()


def test_a_room_whose_only_homepod_left_cannot_join_stops() -> None:
    """A room has nothing heard to play on once the only HomePod it has left cannot join."""
    bridge, homepods, apple_tv = _make_room_bridge(speakers=2)
    bridge._is_streaming = True
    back = _homepod_back(bridge, homepods)

    with patch.object(bridge, "_abandon_streaming", MagicMock()) as abandon:
        apple_tv.endpoints = [homepods[1]]
        bridge.update_endpoints()
        abandon.assert_not_called()
        bridge._on_transport_failed(back)

    abandon.assert_called_once_with()


async def test_a_homepod_back_in_its_room_that_joined_ends_the_stream_when_it_fails() -> None:
    """A HomePod that came back and joined the music of its room is one of its speakers again."""
    bridge, homepods, _ = _make_room_bridge(speakers=2)
    bridge._is_streaming = True
    back = _homepod_back(bridge, homepods)
    stream = _make_anchor_stream()
    back._drop_until_us = bridge.sendspin_server.clock.now_us()
    back._airplay_stream = stream
    back._airplay_stream_start_task = asyncio.current_task()
    with patch(
        "music_assistant.providers.airplay.sendspin_bridge.cli_clock_time",
        return_value=UNIX_NOW_S,
    ):
        assert await back._anchor_stream(stream, warm=False) is True

    with patch.object(bridge, "_abandon_streaming", MagicMock()) as abandon:
        bridge._on_transport_failed(back)

    abandon.assert_called_once_with()


async def test_a_new_stream_of_a_room_starts_on_all_of_its_homepods() -> None:
    """A new stream starts on every HomePod of a room again, and fails when one of them does."""
    bridge, homepods, _ = _make_room_bridge(speakers=3)
    bridge._is_streaming = True
    back = _homepod_back(bridge, homepods)
    with patch.object(bridge, "_abandon_streaming", MagicMock()) as abandon:
        bridge._on_transport_failed(bridge._transports[homepods[2].protocol_id])
        abandon.assert_not_called()

        bridge._on_stream_start(MagicMock())
        assert homepods[2].protocol_id in bridge._transports
        bridge._on_transport_failed(back)

    abandon.assert_called_once_with()


def test_a_homepod_of_a_room_taken_over_by_another_sender_takes_the_room_along() -> None:
    """
    A HomePod another sender took over is left to that sender, and so is its room.

    A set plays on all of its speakers or not at all: the room leaves its Sendspin group without
    a re-join, and none of its other devices is fed any more.
    """
    bridge, homepods, _ = _make_room_bridge(speakers=2)
    taken = bridge._transports[homepods[0].protocol_id]
    stream = _make_kept_stream(running=False, receiver_closed=True)
    taken._airplay_stream = stream
    homepods[0].stream = stream
    taken._airplay_stream_start_task = _make_completed_start_task()
    taken._started = True

    with (
        patch.object(_BridgeTransport, "restart", MagicMock()) as restart,
        patch.object(_BridgeTransport, "_start_protocol_from_chunk", MagicMock()),
        patch.object(bridge, "_leave_group_or_session", MagicMock()) as leave,
    ):
        bridge._on_audio_chunk(_pcm_chunk(SENDSPIN_EPOCH_US + 1_000_000))

    restart.assert_not_called()
    leave.assert_called_once_with()
    assert bridge._is_streaming is False
    assert all(transport.is_idle for transport in bridge._transports.values())


@pytest.mark.parametrize("speakers_left", [0, 1])
def test_a_room_whose_last_speaker_leaves_while_it_plays_leaves_its_sendspin_session(
    speakers_left: int,
) -> None:
    """
    A room left without a speaker while it plays has nothing to play on, as at its start.

    One that keeps a speaker plays on it.
    """
    bridge, homepods, apple_tv = _make_room_bridge(speakers=2)
    bridge._is_streaming = True
    apple_tv.endpoints = homepods[:speakers_left]

    with patch.object(bridge, "_abandon_streaming", MagicMock()) as abandon:
        bridge.update_endpoints()

    assert abandon.call_count == (0 if speakers_left else 1)


def test_a_room_without_a_speaker_on_the_network_leaves_its_sendspin_session() -> None:
    """A set none of whose speakers is there has nothing to play on, so it plays nothing."""
    bridge, _, apple_tv = _make_room_bridge()
    apple_tv.endpoints = []

    with patch.object(bridge, "_abandon_streaming", MagicMock()) as abandon:
        bridge._restart_transport()

    abandon.assert_called_once_with()


def test_the_devices_of_a_room_share_one_timing_decision() -> None:
    """A device that starts later takes the shared-PTP decision the others run with."""
    bridge, homepods, _ = _make_room_bridge(speakers=2)
    bridge._transports[homepods[0].protocol_id]._use_shared_ptp = True
    cast("MagicMock", bridge.provider).bridge_manager.resolve_shared_ptp.return_value = False

    assert bridge._transports[homepods[1].protocol_id]._resolve_shared_ptp() is True


def test_a_room_plays_warm_without_its_apple_tv() -> None:
    """The next stream rides the speakers warm, whatever the Apple TV, which joins after them."""
    bridge, (homepod,), _ = _make_room_bridge()
    speaker = bridge._transports[homepod.protocol_id]
    speaker._airplay_stream = _make_kept_stream()
    speaker._started = True

    assert bridge._can_reuse_stream_warm() is True


def test_a_room_that_plays_on_its_homepods_only_owns_their_streams() -> None:
    """Commands of a HomePod reach the Sendspin session that feeds it, without an Apple TV leg."""
    bridge, (homepod,), apple_tv = _make_room_bridge(homepods_only=True)
    stream = _make_kept_stream()
    bridge._transports[homepod.protocol_id]._airplay_stream = stream
    homepod.stream = stream

    assert apple_tv.stream is None
    assert bridge.owns_airplay_stream is True
