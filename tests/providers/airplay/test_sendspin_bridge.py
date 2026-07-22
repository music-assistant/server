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

from unittest.mock import AsyncMock, MagicMock, patch

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
