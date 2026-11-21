"""Tests for the timeline clock, its rate estimator and the keeper that applies the rate."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.constants import CONF_TIMELINE_RATE_PPB
from music_assistant.helpers.timeline_clock import (
    MAX_RATE_PPB,
    ClockRateEstimator,
    TimelineClock,
    TimelineRateKeeper,
    rate_term_ns,
)

NS = 1_000_000_000
TEN_YEARS_NS = 10 * 365 * 86400 * NS
# raw clock readings taken well after boot, past the settling time of NTP
HOUR_NS = 3600 * NS
SAMPLE_NS = 10 * NS
_RAW_CLOCK = "music_assistant.helpers.timeline_clock.raw_clock_ns"


@contextmanager
def _raw_at(raw_ns: int) -> Iterator[None]:
    """Hold the raw clock of the host at the given reading."""
    with patch(_RAW_CLOCK, return_value=raw_ns):
        yield


def _c_div(dividend: int, divisor: int) -> int:
    """Divide like C, truncating toward zero."""
    quotient = abs(dividend) // abs(divisor)
    return quotient if (dividend < 0) == (divisor < 0) else -quotient


def _c_rate_term(raw_ns: int, rate_ppb: int) -> int:
    """Return host_clock_rate_term_ns() of cliairplay, with C's integer division and modulo."""
    whole_s = _c_div(raw_ns, NS)
    return whole_s * rate_ppb + _c_div((raw_ns - whole_s * NS) * rate_ppb, NS)


# --- the rate term, as cliairplay computes it ---------------------------------------------


@pytest.mark.parametrize(
    ("raw_ns", "rate_ppb", "expected"),
    [
        # the values the test suite of cliairplay asserts for host_clock_rate_term_ns()
        (0, 0, 0),
        (123456789012345, 0, 0),
        (TEN_YEARS_NS, 0, 0),
        (NS, 857000, 857000),
        (NS, -857000, -857000),
        (100 * NS, 1000, 100000),
        (100 * NS, -1000, -100000),
        (NS // 2, 1000000, 500000),
        (NS // 2, -1000000, -500000),
        (999999999, 1000, 999),
        (999999999, -1000, -999),
        (1500000000, 1000, 1500),
        (TEN_YEARS_NS, MAX_RATE_PPB, 630720000000000),
        (TEN_YEARS_NS, -MAX_RATE_PPB, -630720000000000),
        (TEN_YEARS_NS + 999999999, MAX_RATE_PPB, 630720000000000 + 1999999),
        (TEN_YEARS_NS + 999999999, -MAX_RATE_PPB, -630720000000000 - 1999999),
    ],
)
def test_rate_term_matches_cliairplay(raw_ns: int, rate_ppb: int, expected: int) -> None:
    """The rate term equals what cliairplay adds, truncated toward zero for either sign."""
    assert rate_term_ns(raw_ns, rate_ppb) == expected


def test_rate_term_matches_cliairplay_split_arithmetic() -> None:
    """For any reading and rate, the rate term equals the split arithmetic of cliairplay."""
    rng = random.Random(6958)  # noqa: S311 - seeded for determinism
    for _ in range(20_000):
        raw_ns = rng.randrange(-TEN_YEARS_NS, 10 * TEN_YEARS_NS)
        rate_ppb = rng.randint(-MAX_RATE_PPB, MAX_RATE_PPB)
        assert rate_term_ns(raw_ns, rate_ppb) == _c_rate_term(raw_ns, rate_ppb)


# --- the clock --------------------------------------------------------------------------


def test_clock_without_a_rate_is_the_raw_clock() -> None:
    """Without a rate correction the timeline reads the raw clock of the host."""
    clock = TimelineClock()
    with _raw_at(5 * HOUR_NS + 123_456_789):
        assert clock.now_ns() == 5 * HOUR_NS + 123_456_789
        assert clock.now_us() == (5 * HOUR_NS + 123_456_789) // 1000


def test_clock_starts_at_unix_time() -> None:
    """The timeline plus its unix offset reads as unix time when the clock is created."""
    clock = TimelineClock()
    assert abs(clock.now_ns() + clock.unix_offset_ns - time.time_ns()) < 100_000_000


@pytest.mark.parametrize("rate_ppb", [857_000, -857_000, 18_257, MAX_RATE_PPB, -MAX_RATE_PPB])
def test_clock_runs_at_the_corrected_rate(rate_ppb: int) -> None:
    """With a rate correction, each second of the raw clock lasts that much longer or shorter."""
    clock = TimelineClock()
    with _raw_at(2 * HOUR_NS):
        clock.set_rate(rate_ppb)
        start_ns = clock.now_ns()
    with _raw_at(2 * HOUR_NS + 1000 * NS):
        assert clock.now_ns() - start_ns == 1000 * NS + 1000 * rate_ppb


@pytest.mark.parametrize("raw_ns", [16 * 60 * NS, 5 * 86400 * NS + 999_999_999, TEN_YEARS_NS + 1])
def test_clock_stays_continuous_across_rate_changes(raw_ns: int) -> None:
    """A rate change leaves the timeline where it was at that instant, however long the uptime."""
    clock = TimelineClock()
    for rate_ppb in (857_000, -857_000, 1, 0, MAX_RATE_PPB, -18_257):
        with _raw_at(raw_ns):
            before_ns = clock.now_ns()
            clock.set_rate(rate_ppb)
            assert clock.now_ns() == before_ns
        assert clock.rate_ppb == rate_ppb
        raw_ns += 3 * HOUR_NS + 7


def test_clock_offset_carries_the_timeline_across_a_rate_change() -> None:
    """The timeline is the raw clock plus the rate term plus the offset, as cliairplay computes it."""
    clock = TimelineClock()
    with _raw_at(3 * HOUR_NS):
        clock.set_rate(857_000)
    raw_ns = 7 * HOUR_NS + 42
    with _raw_at(raw_ns):
        assert clock.now_ns() == raw_ns + rate_term_ns(raw_ns, 857_000) + clock.offset_ns
    assert clock.offset_ns == -rate_term_ns(3 * HOUR_NS, 857_000)


def test_rate_listeners_follow_a_rate_change() -> None:
    """A listener runs once the new rate is in place, until it is removed."""
    clock = TimelineClock()
    seen: list[int] = []
    remove = clock.add_rate_listener(lambda: seen.append(clock.rate_ppb))

    clock.set_rate(857_000)
    remove()
    clock.set_rate(0)
    remove()

    assert seen == [857_000]


def test_rate_is_pinned_while_a_user_cannot_follow_a_change() -> None:
    """The rate is pinned while any registered user says so."""
    clock = TimelineClock()
    running = {"a": False, "b": False}
    listener = MagicMock()
    clock.add_rate_listener(listener)
    remove_a = clock.add_rate_listener(MagicMock(), pinned=lambda: running["a"])
    # a user that only holds the rate back
    clock.add_rate_listener(pinned=lambda: running["b"])

    pinned = [clock.rate_pinned]
    running["a"] = True
    pinned.append(clock.rate_pinned)
    remove_a()
    pinned.append(clock.rate_pinned)
    running["b"] = True
    pinned.append(clock.rate_pinned)
    clock.set_rate(857_000)

    assert pinned == [False, True, False, True]
    listener.assert_called_once_with()


# --- the estimator ----------------------------------------------------------------------


def _feed(
    estimator: ClockRateEstimator,
    start_raw_ns: int,
    seconds: int,
    diff_ns: Any,
) -> list[int | None]:
    """
    Feed a sample every 10 s and return the estimate after each one.

    :param estimator: The estimator to feed.
    :param start_raw_ns: The raw clock at the first sample.
    :param seconds: How long to sample.
    :param diff_ns: CLOCK_MONOTONIC minus the raw clock, in ns, for the seconds since the start.
    """
    estimates: list[int | None] = []
    for elapsed_s in range(0, seconds + 1, 10):
        raw_ns = start_raw_ns + elapsed_s * NS
        estimator.add_sample(raw_ns, raw_ns + diff_ns(elapsed_s))
        estimates.append(estimator.rate_ppb)
    return estimates


@pytest.mark.parametrize("rate_ppb", [857_000, -857_000, 18_257, 0])
def test_estimator_finds_a_steady_rate_after_half_an_hour(rate_ppb: int) -> None:
    """The estimate is the rate of the samples, once they span half an hour."""
    estimator = ClockRateEstimator()
    estimates = _feed(estimator, HOUR_NS, 3600, lambda s: 4_000_000_000 + s * rate_ppb)

    assert set(estimates[:180]) == {None}  # the first 29:50 of samples
    assert estimates[180] == rate_ppb
    assert estimates[-1] == rate_ppb


def test_estimator_sees_through_ntp_phase_slews() -> None:
    """A sawtooth of ±20 ms from NTP phase slews barely moves the estimate."""
    estimator = ClockRateEstimator()

    def diff_ns(seconds: int) -> int:
        # the phase error ramps over each 256 s poll interval and is slewed back
        sawtooth = ((seconds % 256) / 256 * 2 - 1) * 20_000_000
        return round(seconds * 857_000 + sawtooth)

    estimates = _feed(estimator, HOUR_NS, 5 * 3600, diff_ns)

    measured = [estimate for estimate in estimates if estimate is not None]
    assert measured
    assert all(abs(estimate - 857_000) < 10_000 for estimate in measured)
    # a window of hours brings it to well within a ppm
    assert all(
        estimate is not None and abs(estimate - 857_000) < 1_000
        for estimate in estimates[3 * 360 :]
    )


def test_estimator_starts_over_after_a_clock_jump() -> None:
    """A jump of one clock against the other (macOS sleep) restarts the window."""
    estimator = ClockRateEstimator()
    _feed(estimator, HOUR_NS, 3600, lambda s: s * 50_000)
    assert estimator.rate_ppb == 50_000

    # slept for a minute: CLOCK_MONOTONIC moved on, the raw clock did not
    after_ns = HOUR_NS + 3610 * NS
    estimates = _feed(estimator, after_ns, 1800, lambda s: 60 * NS + 3600 * 50_000 + s * 857_000)

    assert set(estimates[:-1]) == {None}
    assert estimates[-1] == 857_000


def test_estimator_keeps_its_window_through_a_small_step() -> None:
    """A step well under the jump threshold is no reason to start over."""
    estimator = ClockRateEstimator()
    _feed(estimator, HOUR_NS, 3600, lambda s: s * 50_000 + (30_000_000 if s > 1800 else 0))

    assert estimator.rate_ppb is not None


def test_estimator_waits_for_ntp_to_settle_after_boot() -> None:
    """Samples from the first quarter hour after boot are left out of the estimate."""
    estimator = ClockRateEstimator()

    def diff_ns(seconds: int) -> int:
        # NTP slews hard right after boot
        if seconds < 15 * 60:
            return seconds * 500_000
        return 15 * 60 * 500_000 + (seconds - 15 * 60) * 857_000

    estimates = _feed(estimator, 0, 45 * 60, diff_ns)

    assert set(estimates[:-1]) == {None}
    assert estimates[-1] == 857_000


def test_estimator_forgets_samples_older_than_its_window() -> None:
    """The estimate follows the rate of the last hours only."""
    estimator = ClockRateEstimator()

    def diff_ns(seconds: int) -> int:
        if seconds < 3600:
            return seconds * 100_000
        return 3600 * 100_000 + (seconds - 3600) * 200_000

    _feed(estimator, HOUR_NS, 5 * 3600, diff_ns)

    assert estimator.rate_ppb == 200_000


# --- the keeper -------------------------------------------------------------------------


def _keeper(
    *, estimate: int | None = 857_000, settings: dict[str, Any] | None = None
) -> tuple[TimelineRateKeeper, MagicMock, MagicMock]:
    """
    Build a keeper with a set estimate, on a server that keeps its settings in a dict.

    :param estimate: The rate the estimator gives, in ppb.
    :param settings: The stored settings of the server, which a next start reads again.
    :return: The keeper, the server and a rate listener registered on its clock.
    """
    stored = {} if settings is None else settings
    mass = MagicMock()
    mass.timeline_clock = TimelineClock()
    mass.config.get.side_effect = stored.get
    mass.config.set.side_effect = stored.__setitem__
    mass.create_task.side_effect = lambda coro, **_kwargs: coro.close()
    keeper = TimelineRateKeeper(mass)
    keeper._estimator = MagicMock(rate_ppb=estimate)
    listener = MagicMock()
    mass.timeline_clock.add_rate_listener(listener)
    return keeper, mass, listener


def test_keeper_applies_and_stores_a_new_rate() -> None:
    """With nothing pinning the rate the estimate is applied, followed by its users, and stored."""
    keeper, mass, listener = _keeper()

    keeper._update_rate()

    assert mass.timeline_clock.rate_ppb == 857_000
    listener.assert_called_once_with()
    mass.config.set.assert_called_once_with(CONF_TIMELINE_RATE_PPB, 857_000)


def test_keeper_ignores_players_off_the_timeline() -> None:
    """A playing or paused player that does not run on the timeline holds nothing back."""
    keeper, mass, _ = _keeper()
    mass.players = [
        MagicMock(playback_state=PlaybackState.PLAYING),
        MagicMock(playback_state=PlaybackState.PAUSED),
    ]

    keeper._update_rate()

    assert mass.timeline_clock.rate_ppb == 857_000


def test_keeper_never_changes_a_pinned_rate() -> None:
    """A user that runs something on the current rate keeps it, but the estimate is stored."""
    keeper, mass, listener = _keeper()
    mass.timeline_clock.add_rate_listener(pinned=lambda: True)

    keeper._update_rate()

    assert mass.timeline_clock.rate_ppb == 0
    listener.assert_not_called()
    mass.config.set.assert_called_once_with(CONF_TIMELINE_RATE_PPB, 857_000)


def test_keeper_applies_the_rate_once_nothing_pins_it() -> None:
    """A new rate waits for its users, and is applied once they let go."""
    keeper, mass, _ = _keeper()
    pinned = {"stream": True}
    mass.timeline_clock.add_rate_listener(pinned=lambda: pinned["stream"])
    keeper._update_rate()
    assert mass.timeline_clock.rate_ppb == 0

    pinned["stream"] = False
    keeper._update_rate()

    assert mass.timeline_clock.rate_ppb == 857_000
    mass.config.set.assert_called_once_with(CONF_TIMELINE_RATE_PPB, 857_000)


async def test_keeper_stores_a_pinned_rate_for_the_next_start() -> None:
    """A rate that stays pinned while the server runs is in place from its next start."""
    settings: dict[str, Any] = {}
    keeper, mass, _ = _keeper(settings=settings)
    mass.timeline_clock.add_rate_listener(pinned=lambda: True)
    keeper._update_rate()
    assert mass.timeline_clock.rate_ppb == 0

    next_keeper, next_mass, _ = _keeper(estimate=None, settings=settings)
    next_keeper.start()

    assert next_mass.timeline_clock.rate_ppb == 857_000


def test_keeper_stores_only_an_estimate_that_moved() -> None:
    """The stored rate is only written again for an estimate 5 ppm or more off it."""
    keeper, mass, _ = _keeper(estimate=857_000)
    estimator = cast("MagicMock", keeper._estimator)
    mass.timeline_clock.add_rate_listener(pinned=lambda: True)
    keeper._update_rate()
    for estimate in (858_000, 861_999, 856_000):
        estimator.rate_ppb = estimate
        keeper._update_rate()
    estimator.rate_ppb = 862_000
    keeper._update_rate()

    assert [call.args for call in mass.config.set.call_args_list] == [
        (CONF_TIMELINE_RATE_PPB, 857_000),
        (CONF_TIMELINE_RATE_PPB, 862_000),
    ]


async def test_keeper_does_not_store_the_rate_it_started_on_again() -> None:
    """An estimate close to the stored rate leaves the stored rate as it is."""
    keeper, mass, _ = _keeper(estimate=858_000, settings={CONF_TIMELINE_RATE_PPB: 857_000})
    keeper.start()

    keeper._update_rate()

    mass.config.set.assert_not_called()
    assert mass.timeline_clock.rate_ppb == 857_000


@pytest.mark.parametrize(("estimate", "applied"), [(861_999, False), (862_000, True)])
def test_keeper_ignores_a_change_under_five_ppm(estimate: int, applied: bool) -> None:
    """Only an estimate 5 ppm or more off the current rate is applied."""
    keeper, mass, _ = _keeper(estimate=estimate)
    mass.timeline_clock.set_rate(857_000)

    keeper._update_rate()

    assert mass.timeline_clock.rate_ppb == (estimate if applied else 857_000)


def test_keeper_waits_for_an_estimate() -> None:
    """Without an estimate yet, nothing changes."""
    keeper, mass, _ = _keeper(estimate=None)

    keeper._update_rate()

    assert mass.timeline_clock.rate_ppb == 0
    mass.config.set.assert_not_called()


def test_keeper_refuses_a_rate_beyond_any_clock(caplog: pytest.LogCaptureFixture) -> None:
    """An estimate past 2000 ppm is never applied or stored, and is reported once."""
    keeper, mass, _ = _keeper(estimate=MAX_RATE_PPB + 1)

    with caplog.at_level(logging.WARNING):
        keeper._update_rate()
        keeper._update_rate()

    assert mass.timeline_clock.rate_ppb == 0
    mass.config.set.assert_not_called()
    assert len(caplog.records) == 1
    assert "not corrected" in caplog.records[0].getMessage()


@pytest.mark.parametrize(
    ("rate_ppb", "level", "text"),
    [
        (18_257, logging.INFO, "runs 18.3 ppm slow"),
        (-45_000, logging.INFO, "runs 45.0 ppm fast"),
        (857_000, logging.WARNING, "runs 857.0 ppm slow, far off"),
    ],
)
def test_keeper_reports_an_applied_rate(
    caplog: pytest.LogCaptureFixture, rate_ppb: int, level: int, text: str
) -> None:
    """The applied rate is logged with its size and direction, as a warning when far off."""
    keeper, _, _ = _keeper(estimate=rate_ppb)

    with caplog.at_level(logging.INFO):
        keeper._update_rate()

    (record,) = caplog.records
    assert record.levelno == level
    assert text in record.getMessage()


async def test_keeper_starts_on_the_stored_rate(caplog: pytest.LogCaptureFixture) -> None:
    """The rate measured before is in place before anything schedules on the timeline."""
    keeper, mass, listener = _keeper(settings={CONF_TIMELINE_RATE_PPB: 857_000})

    with caplog.at_level(logging.INFO):
        keeper.start()

    assert mass.timeline_clock.rate_ppb == 857_000
    listener.assert_called_once_with()
    mass.config.set.assert_not_called()
    assert "runs 857.0 ppm slow" in caplog.records[0].getMessage()
    mass.create_task.assert_called_once()


@pytest.mark.parametrize("stored", [0, None, "857000", MAX_RATE_PPB + 1, 1.5])
async def test_keeper_ignores_a_stored_rate_it_cannot_use(stored: Any) -> None:
    """Without a usable stored rate the timeline starts without a correction."""
    keeper, mass, _ = _keeper(settings={CONF_TIMELINE_RATE_PPB: stored})

    keeper.start()

    assert mass.timeline_clock.rate_ppb == 0


async def test_keeper_samples_both_clocks_every_ten_seconds() -> None:
    """Each round reads the raw clock and CLOCK_MONOTONIC and then looks at the estimate."""
    keeper, _, _ = _keeper()
    sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError])

    with (
        patch("music_assistant.helpers.timeline_clock.asyncio.sleep", sleep),
        patch.object(keeper, "_update_rate") as update_rate,
        pytest.raises(asyncio.CancelledError),
    ):
        await keeper._measure()

    assert sleep.await_args_list[0].args == (10.0,)
    estimator = keeper._estimator
    assert isinstance(estimator, MagicMock)
    assert estimator.add_sample.call_count == 2
    raw_ns, monotonic_ns = estimator.add_sample.call_args.args
    assert isinstance(raw_ns, int)
    assert isinstance(monotonic_ns, int)
    assert update_rate.call_count == 2
