"""
Unit tests for the Sendspin -> AirPlay bridge timing math.

These pin the clock-domain conversion the bridge uses to turn a Sendspin
audible instant (on Sendspin's own monotonic clock) into the unix epoch
millisecond the cliairplay binary expects for ``--start-unix-ms``. The
Sendspin clock is mocked with ``ManualClock`` so the tests are deterministic
and independent of the host wall-clock.
"""

from aiosendspin.clock import ManualClock

from music_assistant.providers.airplay.sendspin_bridge import (
    sendspin_audible_instant_to_unix_ms,
)

# A large, arbitrary Sendspin monotonic-clock epoch (microseconds). Real
# monotonic clocks start from an unspecified point (e.g. host boot), so the
# conversion must never depend on this value.
SENDSPIN_EPOCH_US = 5_000_000_000_000  # ~57.8 days of monotonic uptime
UNIX_NOW_S = 1_784_000_000.0  # fixed unix wall-clock reading for the tests
AP2_LEAD_MS = 2500
RAOP_LEAD_MS = 1500


def _drop_until_us(clock: ManualClock, wait_start_ms: int) -> int:
    """Replicate the bridge's first-chunk anchor: now + wait_start on the Sendspin clock."""
    return clock.now_us() + wait_start_ms * 1_000


def _unix_at(sendspin_us: int) -> float:
    """Model a constant-offset, same-rate Sendspin<->unix relationship."""
    return UNIX_NOW_S + (sendspin_us - SENDSPIN_EPOCH_US) / 1_000_000


def test_maps_future_delta_to_unix_now_plus_lead() -> None:
    """An instant wait_start ahead maps to unix_now + wait_start (in ms)."""
    clock = ManualClock(now_us_value=SENDSPIN_EPOCH_US)
    drop_until = _drop_until_us(clock, AP2_LEAD_MS)

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
        _drop_until_us(clock_a, AP2_LEAD_MS), clock_a.now_us(), UNIX_NOW_S
    )
    result_b = sendspin_audible_instant_to_unix_ms(
        _drop_until_us(clock_b, AP2_LEAD_MS), clock_b.now_us(), UNIX_NOW_S
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
        drop_until = _drop_until_us(clock, wait_start_ms)
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
    drop_until = _drop_until_us(clock, AP2_LEAD_MS)

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
