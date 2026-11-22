"""The clock every audio timeline of the server runs on, kept at the true rate."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections import deque
from statistics import linear_regression
from typing import TYPE_CHECKING, Final

from music_assistant.constants import CONF_TIMELINE_RATE_PPB, MASS_LOGGER_NAME

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.timeline_clock")

# The largest rate correction cliairplay accepts, far beyond the error of any real clock.
MAX_RATE_PPB: Final[int] = 2_000_000

_NS_PER_S: Final[int] = 1_000_000_000
# NTP can still slew the clock hard this long after boot, which would skew the estimate.
_BOOT_SETTLE_NS: Final[int] = 15 * 60 * _NS_PER_S
# The samples have to span this long before the estimate is used, and are kept this long.
_MIN_SPAN_NS: Final[int] = 30 * 60 * _NS_PER_S
_MAX_SPAN_NS: Final[int] = 3 * 3600 * _NS_PER_S
# A sample this far off the trend means one of the clocks jumped (macOS sleep moves
# CLOCK_MONOTONIC but not the raw clock), so the window starts over.
_DISCONTINUITY_NS: Final[int] = 50_000_000
_SAMPLE_INTERVAL: Final[float] = 10.0
_APPLY_THRESHOLD_PPB: Final[int] = 5_000
_FAR_OFF_RATE_PPB: Final[int] = 200_000

if sys.platform == "linux":

    def raw_clock_ns() -> int:
        """Return the raw clock of the host, which NTP never adjusts, in ns."""
        return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)

else:

    def raw_clock_ns() -> int:
        """Return the raw clock of the host, which NTP never adjusts, in ns."""
        # mach_absolute_time on macOS: the CLOCK_UPTIME_RAW cliairplay reads there
        return time.monotonic_ns()


def rate_term_ns(raw_ns: int, rate_ppb: int) -> int:
    """
    Return what a rate correction adds to a reading of the raw clock, as cliairplay does.

    :param raw_ns: The reading of the raw clock, in ns.
    :param rate_ppb: The rate correction, in parts per billion.
    """
    # The exact product, truncated toward zero like cliairplay's C integer division.
    product = raw_ns * rate_ppb
    return product // _NS_PER_S if product >= 0 else -(-product // _NS_PER_S)


class TimelineClock:
    """
    Monotonic clock every audio timeline of the server runs on.

    It is the raw clock of the host with a rate correction that makes it run at the true
    rate, so NTP slews and steps never move it, and it stays continuous when the rate
    changes. It implements the ``Clock`` protocol of aiosendspin, and a process that is
    handed its offset and rate (cliairplay) computes the very same timeline. An instance
    equals only itself, so everything that compares clocks has to be given this one.
    """

    def __init__(self) -> None:
        """Initialize the clock without a rate correction."""
        self._rate_ppb = 0
        self._offset_ns = 0
        self._listeners: list[tuple[Callable[[], None] | None, Callable[[], bool] | None]] = []
        self._unix_offset_ns = time.time_ns() - self.now_ns()

    @property
    def rate_ppb(self) -> int:
        """Return the rate correction, in parts per billion (positive runs the clock faster)."""
        return self._rate_ppb

    @property
    def offset_ns(self) -> int:
        """Return what the timeline adds to the raw clock besides the rate correction, in ns."""
        return self._offset_ns

    @property
    def unix_offset_ns(self) -> int:
        """Return the unix time minus the timeline when the clock was created, in ns."""
        return self._unix_offset_ns

    @property
    def rate_pinned(self) -> bool:
        """Return whether a user of the timeline runs something that cannot follow a new rate."""
        return any(pinned() for _, pinned in self._listeners if pinned is not None)

    def now_ns(self) -> int:
        """Return the current time on the timeline, in ns."""
        raw_ns = raw_clock_ns()
        return raw_ns + rate_term_ns(raw_ns, self._rate_ppb) + self._offset_ns

    def now_us(self) -> int:
        """Return the current time on the timeline, in µs."""
        return self.now_ns() // 1000

    def set_rate(self, rate_ppb: int) -> None:
        """
        Change the rate correction, keeping the timeline continuous at this instant.

        Call it only while :attr:`rate_pinned` is False.

        :param rate_ppb: The new rate correction, in parts per billion.
        """
        raw_ns = raw_clock_ns()
        self._offset_ns += rate_term_ns(raw_ns, self._rate_ppb) - rate_term_ns(raw_ns, rate_ppb)
        self._rate_ppb = rate_ppb
        for listener, _ in list(self._listeners):
            if listener is not None:
                listener()

    def add_rate_listener(
        self,
        listener: Callable[[], None] | None = None,
        pinned: Callable[[], bool] | None = None,
    ) -> Callable[[], None]:
        """
        Register a user of the timeline that acts on a change of its rate, or holds one back.

        :param listener: Called right after the rate changed.
        :param pinned: Returns True while the user runs something that cannot follow a
            change of the rate, which then has to wait (see :attr:`rate_pinned`).
        :return: A callable that removes the registration again.
        """
        entry = (listener, pinned)
        self._listeners.append(entry)

        def remove_listener() -> None:
            if entry in self._listeners:
                self._listeners.remove(entry)

        return remove_listener


class ClockRateEstimator:
    """
    Estimate the rate correction that brings the raw clock of the host to the true rate.

    It compares the raw clock with CLOCK_MONOTONIC, which NTP keeps at the true rate
    without ever stepping it, over a sliding window of a few hours.
    """

    def __init__(self) -> None:
        """Initialize the estimator without samples."""
        # (raw clock, CLOCK_MONOTONIC minus raw clock) of each sample, in ns
        self._samples: deque[tuple[int, int]] = deque()

    @property
    def rate_ppb(self) -> int | None:
        """Return the estimated rate correction in ppb, or None while the samples span too short."""
        if not self._samples or self._samples[-1][0] - self._samples[0][0] < _MIN_SPAN_NS:
            return None
        return round(self._slope() * _NS_PER_S)

    def add_sample(self, raw_ns: int, monotonic_ns: int) -> None:
        """
        Add a reading of both clocks, taken together.

        :param raw_ns: The raw clock of the host, in ns.
        :param monotonic_ns: CLOCK_MONOTONIC of the host, in ns.
        """
        if raw_ns < _BOOT_SETTLE_NS:
            return
        diff_ns = monotonic_ns - raw_ns
        if self._samples:
            last_raw_ns, last_diff_ns = self._samples[-1]
            expected_ns = last_diff_ns + (raw_ns - last_raw_ns) * self._slope()
            if abs(diff_ns - expected_ns) > _DISCONTINUITY_NS:
                self._samples.clear()
        self._samples.append((raw_ns, diff_ns))
        while raw_ns - self._samples[0][0] > _MAX_SPAN_NS:
            self._samples.popleft()

    def _slope(self) -> float:
        """Return the least-squares slope of the clock difference, 0 with fewer than 2 samples."""
        if len(self._samples) < 2:
            return 0.0
        first_raw_ns, first_diff_ns = self._samples[0]
        return linear_regression(
            [raw_ns - first_raw_ns for raw_ns, _ in self._samples],
            [diff_ns - first_diff_ns for _, diff_ns in self._samples],
        ).slope


class TimelineRateKeeper:
    """
    Keep the timeline clock of the server at the true rate.

    It measures how far the raw clock of the host runs off, stores that for the next start
    of the server, and corrects the timeline for it at a moment nothing pins its rate.
    """

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the keeper.

        :param mass: The Music Assistant server, whose timeline clock it keeps.
        """
        self.mass = mass
        self._estimator = ClockRateEstimator()
        self._stored_ppb = 0
        self._beyond_bound_warned = False

    def start(self) -> None:
        """Put the timeline on the stored rate and start measuring."""
        stored_ppb = self.mass.config.get(CONF_TIMELINE_RATE_PPB, 0)
        if isinstance(stored_ppb, int) and stored_ppb and abs(stored_ppb) <= MAX_RATE_PPB:
            self._stored_ppb = stored_ppb
            self.mass.timeline_clock.set_rate(stored_ppb)
            _log_rate(stored_ppb)
        self.mass.create_task(self._measure(), task_name="timeline_rate_keeper")

    async def _measure(self) -> None:
        """Sample the clocks and apply a new rate when one is due, for as long as the server runs."""
        while True:
            await asyncio.sleep(_SAMPLE_INTERVAL)
            self._estimator.add_sample(raw_clock_ns(), time.clock_gettime_ns(time.CLOCK_MONOTONIC))
            self._update_rate()

    def _update_rate(self) -> None:
        """Store and apply the estimated rate when it differs enough from the current one."""
        if (rate_ppb := self._estimator.rate_ppb) is None:
            return
        if abs(rate_ppb) > MAX_RATE_PPB:
            if not self._beyond_bound_warned:
                self._beyond_bound_warned = True
                LOGGER.warning(
                    "The clock of this host seems to run %.1f ppm %s, more than any clock "
                    "does; the audio timing is not corrected for it",
                    abs(rate_ppb) / 1000,
                    _direction(rate_ppb),
                )
            return
        self._beyond_bound_warned = False
        # stored before it can be applied: a next start runs on it, also when the rate
        # stays pinned until then
        if abs(rate_ppb - self._stored_ppb) >= _APPLY_THRESHOLD_PPB:
            self._stored_ppb = rate_ppb
            self.mass.config.set(CONF_TIMELINE_RATE_PPB, rate_ppb)
        clock = self.mass.timeline_clock
        if abs(rate_ppb - clock.rate_ppb) < _APPLY_THRESHOLD_PPB or clock.rate_pinned:
            return
        clock.set_rate(rate_ppb)
        _log_rate(rate_ppb)


def _log_rate(rate_ppb: int) -> None:
    """Log the rate correction the timeline runs on, which support looks for."""
    if abs(rate_ppb) > _FAR_OFF_RATE_PPB:
        LOGGER.warning(
            "The clock of this host runs %.1f ppm %s, far off for a hardware clock; "
            "the audio timing is corrected for it",
            abs(rate_ppb) / 1000,
            _direction(rate_ppb),
        )
    else:
        LOGGER.info(
            "The clock of this host runs %.1f ppm %s; the audio timing is corrected for it",
            abs(rate_ppb) / 1000,
            _direction(rate_ppb),
        )


def _direction(rate_ppb: int) -> str:
    """Return how the raw clock runs that needs the given rate correction."""
    return "slow" if rate_ppb > 0 else "fast"
