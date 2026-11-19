"""Tests for the clock mapping and the sync loop of the slimproto bridge."""

from __future__ import annotations

import bisect
import gzip
import json
import math
import random
import struct
from dataclasses import dataclass, field
from itertools import accumulate, pairwise
from pathlib import Path

import pytest

from music_assistant.providers.squeezelite.bridge_sync import (
    GAIN_PPM_PER_MS,
    MAX_RATE_PPM,
    MAX_START_JUMP_MS,
    PlayerClock,
    SyncLoop,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"

RATE = 48000
FRAME_SIZE = 4
START_US = 50_000_000
JIFFIES_WRAP = 1 << 32


# ---------------------------------------------------------------- helpers


class _Player:
    """
    A simulated player.

    Its clock and its DAC each run at their own rate against the server clock, and it
    reports its clock and what it played in whole milliseconds, like a real player.
    """

    def __init__(
        self, *, clock_ppm: float = 0.0, dac_ppm: float = 0.0, clock_at_zero_ms: float = 7_000_000
    ) -> None:
        """Initialize a player that is not playing yet."""
        self.clock_rate = 1 + clock_ppm * 1e-6
        self.dac_rate = 1 + dac_ppm * 1e-6
        self.clock_at_zero_ms = clock_at_zero_ms
        self.clock_step_ms = 0.0
        self.playing_since_us: float | None = None

    def clock_ms(self, server_us: float) -> float:
        """Return the player's clock at a server time, neither truncated nor wrapped."""
        return self.clock_at_zero_ms + self.clock_step_ms + server_us / 1000 * self.clock_rate

    def jiffies(self, server_us: float) -> int:
        """Return the jiffies the player reports at a server time."""
        return int(self.clock_ms(server_us)) % JIFFIES_WRAP

    def start_at(self, jiffies: int, now_us: float, output_delay_ms: float = 0.0) -> None:
        """Start playing when the player's clock reads ``jiffies``, plus an output delay."""
        now_ms = self.clock_ms(now_us)
        ahead_ms = (jiffies - now_ms) % JIFFIES_WRAP
        self.playing_since_us = now_us + (ahead_ms / self.clock_rate + output_delay_ms) * 1000

    def position(self, server_us: float) -> float:
        """Return the number of stream frames played at a server time."""
        if self.playing_since_us is None or server_us <= self.playing_since_us:
            return 0.0
        return (server_us - self.playing_since_us) * RATE * self.dac_rate / 1_000_000

    def elapsed_ms(self, server_us: float) -> int:
        """Return the elapsed milliseconds the player reports at a server time."""
        return int(self.position(server_us) * 1000 / RATE)


def _sample(
    clock: PlayerClock,
    player: _Player,
    sent_us: int,
    *,
    to_player_us: int = 300,
    to_server_us: int = 300,
) -> None:
    """Send a timed request to the player and give its reply to the clock."""
    jiffies = player.jiffies(sent_us + to_player_us)
    clock.add_sample(jiffies, sent_us, sent_us + to_player_us + to_server_us)


def _learn(clock: PlayerClock, player: _Player, from_us: int, seconds: float) -> int:
    """Poll the player four times a second; return the server time after the last poll."""
    polls = round(seconds * 4)
    for poll in range(polls):
        _sample(clock, player, from_us + poll * 250_000)
    return from_us + polls * 250_000


def _mapped(clock: PlayerClock, jiffies: int) -> int:
    """Return the server time of a jiffies reading, which the clock has to know."""
    server_us = clock.to_server_us(jiffies)
    assert server_us is not None
    return server_us


def _source(first: int, count: int) -> bytes:
    """Return PCM in which every frame holds its source frame number plus one (0 is silence)."""
    return struct.pack(f"<{count}I", *range(first + 1, first + count + 1))


def _frames(pcm: bytes) -> list[int]:
    """Return the source frame number of every frame of PCM, -1 for silence."""
    return [value - 1 for (value,) in struct.iter_unpack("<I", pcm)]


def _report(loop: SyncLoop, elapsed_ms: int, late_ms: float, generation: int = 0) -> float | None:
    """Report ``elapsed_ms`` played by a player that is ``late_ms`` late, corrections aside."""
    server_us = START_US + elapsed_ms * 1000 + 500 + round(late_ms * 1000)
    return loop.on_report(server_us, elapsed_ms, generation)


def _reports(
    loop: SyncLoop, late_ms: float, *, first_ms: int = 2000, count: int = 5, generation: int = 0
) -> int:
    """Report a player that stays ``late_ms`` late four times a second; return the next elapsed."""
    for elapsed_ms in range(first_ms, first_ms + count * 250, 250):
        assert _report(loop, elapsed_ms, late_ms, generation) is not None
    return first_ms + count * 250


def _offset(loop: SyncLoop) -> int:
    """Return how many more source frames were taken than stream frames were sent."""
    return loop.frames_consumed - loop.frames_sent


# ---------------------------------------------------------------- PlayerClock


def test_clock_knows_nothing_before_three_samples() -> None:
    """The mapping is unknown until three samples arrived, and known right after."""
    clock = PlayerClock()
    player = _Player()
    assert clock.to_server_us(1234) is None
    assert clock.to_jiffies(1_000_000) is None
    _sample(clock, player, 1_000_000)
    _sample(clock, player, 1_250_000, to_server_us=500)
    assert clock.to_server_us(player.jiffies(1_250_300)) is None
    assert clock.to_jiffies(1_250_300) is None
    _sample(clock, player, 1_500_000, to_server_us=400)
    # every reading was taken 300 us after its request was sent, and the replies took
    # 300, 500 and 400 us: their midpoints are 0, 100 and 50 us late
    assert _mapped(clock, player.jiffies(1_500_300)) == 1_500_350
    assert clock.to_jiffies(1_500_350) == player.jiffies(1_500_300)
    assert clock.drift_ppm == 0.0
    assert clock.generation == 0


def test_clock_maps_from_replies_that_differ_by_a_few_milliseconds() -> None:
    """Three replies whose round trips are within a few milliseconds map the clock at once."""
    clock = PlayerClock()
    player = _Player()
    for poll, rtt_us in enumerate((2100, 6800, 3500)):
        assert clock.to_jiffies(1_000_000) is None
        sent_us, half_us = 1_000_000 + poll * 50_000, rtt_us // 2
        _sample(clock, player, sent_us, to_player_us=half_us, to_server_us=half_us)
    now_us = 1_200_000
    assert abs(_mapped(clock, player.jiffies(now_us)) - now_us) <= 1000


def test_clock_is_not_mapped_from_replies_the_player_held_up() -> None:
    """
    Replies the player answers all at once after a while do not map its clock.

    Their round trips fall by the spacing of the requests and each of them is off by half
    of its round trip: the prompt replies after them are the ones the mapping is made from.
    """
    clock = PlayerClock()
    player = _Player()
    from_us = 1_000_000
    released_us = from_us + 830_000
    for sent_us in range(from_us, released_us, 50_000):
        _sample(clock, player, sent_us, to_player_us=released_us - sent_us)
    for poll in range(3):
        assert clock.to_jiffies(released_us) is None
        _sample(clock, player, released_us + 20_000 + poll * 50_000)
    now_us = released_us + 200_000
    assert abs(_mapped(clock, player.jiffies(now_us)) - now_us) <= 1000


def test_clock_is_mapped_by_steady_replies_after_a_quick_one() -> None:
    """One reply that is much quicker than the rest does not hold up the first mapping."""
    clock = PlayerClock()
    player = _Player()
    # one quick reply, then replies that take 62 to 66 ms on their way back
    for poll in range(4):
        assert clock.to_jiffies(1_000_000) is None
        delay_us = 300 if poll == 0 else 60_000 + 2000 * poll
        _sample(clock, player, 1_000_000 + poll * 50_000, to_server_us=delay_us)
    # the mapping is made from the quick one, which has the lowest round trip
    assert abs(_mapped(clock, player.jiffies(1_300_000)) - 1_300_000) <= 1000


def test_clock_is_mapped_when_its_round_trips_never_agree() -> None:
    """Without three replies that agree on the round trip, the first bucket maps the clock."""
    clock = PlayerClock()
    player = _Player()
    # replies 50 ms apart, each 30 ms longer on its way back than the one before
    for poll in range(41):
        assert clock.to_jiffies(1_000_000) is None
        _sample(clock, player, 1_000_000 + poll * 50_000, to_server_us=300 + poll * 30_000)
    # once two seconds of them are in, the one with the lowest round trip maps the clock
    assert abs(_mapped(clock, player.jiffies(3_000_000)) - 3_000_000) <= 1000


def test_clock_maps_both_ways() -> None:
    """A jiffies reading maps to its server time and back, within the millisecond it covers."""
    clock = PlayerClock()
    player = _Player(clock_ppm=40.0)
    now_us = _learn(clock, player, 3_000_000, 60)
    for server_us in range(now_us - 10_000_000, now_us + 10_000_000, 777_777):
        jiffies = player.jiffies(server_us)
        assert abs(_mapped(clock, jiffies) - server_us) <= 1000
        assert clock.to_jiffies(_mapped(clock, jiffies)) == jiffies
        back = clock.to_jiffies(server_us)
        assert back is not None
        assert abs(_mapped(clock, back) - server_us) <= 501


@pytest.mark.parametrize("clock_ppm", [200.0, -200.0])
def test_clock_measures_drift_after_ten_seconds(clock_ppm: float) -> None:
    """The drift is only estimated once the samples span more than ten seconds."""
    clock = PlayerClock()
    player = _Player(clock_ppm=clock_ppm)
    now_us = _learn(clock, player, 3_000_000, 9)
    assert clock.drift_ppm == 0.0
    now_us = _learn(clock, player, now_us, 51)
    # a clock that runs fast loses less server time per tick: negative drift
    assert clock.drift_ppm == pytest.approx(-clock_ppm, abs=5.0)
    # and the mapping extrapolates with it
    later_us = now_us + 30_000_000
    assert abs(_mapped(clock, player.jiffies(later_us)) - later_us) <= 1000


def test_clock_follows_the_jiffies_wrap() -> None:
    """The mapping continues when the 32-bit jiffies wrap around."""
    clock = PlayerClock()
    from_us = 3_000_000
    # the player's clock wraps five seconds into the samples
    player = _Player(clock_ppm=25.0, clock_at_zero_ms=JIFFIES_WRAP - 5000 - from_us / 1000)
    now_us = _learn(clock, player, from_us, 30)
    before_us, after_us = from_us + 1_000_000, now_us - 1_000_000
    assert player.jiffies(before_us) > JIFFIES_WRAP - 5000
    assert player.jiffies(after_us) < 30_000
    assert abs(_mapped(clock, player.jiffies(before_us)) - before_us) <= 1000
    assert abs(_mapped(clock, player.jiffies(after_us)) - after_us) <= 1000
    assert clock.to_jiffies(after_us) == player.jiffies(after_us)
    assert clock.generation == 0
    assert clock.drift_ppm == pytest.approx(-25.0, abs=10.0)
    # a timed start just past the next wrap gets a wrapped value too
    wrap_us = now_us + JIFFIES_WRAP * 1000
    jiffies = clock.to_jiffies(wrap_us)
    assert jiffies is not None
    assert 0 <= jiffies < JIFFIES_WRAP


def test_clock_ignores_a_delayed_reply() -> None:
    """A reply with a long round trip does not move the mapping."""
    clean, delayed = PlayerClock(), PlayerClock()
    player = _Player(clock_ppm=-30.0)
    for poll in range(240):
        sent_us = 3_000_000 + poll * 250_000
        _sample(clean, player, sent_us)
        _sample(delayed, player, sent_us)
        if poll % 9 == 5:
            # an extra reply that took 4 ms longer on its way back: its midpoint is 2 ms off
            _sample(delayed, player, sent_us + 100_000, to_server_us=4300)
    jiffies = player.jiffies(70_000_000)
    assert _mapped(delayed, jiffies) == _mapped(clean, jiffies)
    assert delayed.drift_ppm == clean.drift_ppm


def test_clock_keeps_the_best_replies_of_a_bucket() -> None:
    """Of every two seconds of replies, those within 0.3 ms of the lowest round trip count."""
    plain, passing, failing = PlayerClock(), PlayerClock(), PlayerClock()
    player = _Player()
    # the first three samples and two full buckets of nine
    for poll in range(21):
        sent_us = 3_000_000 + poll * 250_000
        _sample(plain, player, sent_us)
        # one reply in the middle of a bucket takes 0.2 ms longer, or 0.4 ms longer
        _sample(passing, player, sent_us, to_server_us=500 if poll == 7 else 300)
        _sample(failing, player, sent_us, to_server_us=700 if poll == 7 else 300)
    jiffies = player.jiffies(9_000_300)
    # the midpoint of the reply that passes is 100 us late: one of the 21 samples
    assert _mapped(passing, jiffies) - _mapped(plain, jiffies) == round(100 / 21)
    assert _mapped(failing, jiffies) == _mapped(plain, jiffies)


def test_clock_single_outlier_is_not_a_jump() -> None:
    """One or two samples far off the mapping are left out, without starting over."""
    clean, spiked = PlayerClock(), PlayerClock()
    player = _Player(clock_ppm=15.0)
    for poll in range(240):
        sent_us = 3_000_000 + poll * 250_000
        _sample(clean, player, sent_us)
        if poll in (100, 150, 151):
            # the player's clock is 20 ms off for this one reading
            player.clock_step_ms = 20.0
            _sample(spiked, player, sent_us)
            player.clock_step_ms = 0.0
        else:
            _sample(spiked, player, sent_us)
    assert spiked.generation == 0
    jiffies = player.jiffies(70_000_000)
    # the mapping only misses the three samples that were left out
    assert abs(_mapped(spiked, jiffies) - _mapped(clean, jiffies)) <= 20
    assert spiked.drift_ppm == pytest.approx(clean.drift_ppm, abs=0.5)


def test_clock_slow_replies_are_not_a_jump() -> None:
    """Replies that are far off because the network is slow neither move nor restart the mapping."""
    clean, slowed = PlayerClock(), PlayerClock()
    player = _Player(clock_ppm=15.0)
    for poll in range(480):
        sent_us = 3_000_000 + poll * 250_000
        _sample(clean, player, sent_us)
        # for seven seconds every reply takes 30 ms longer on its way back: 15 ms off
        slow = 240 <= poll < 268
        _sample(slowed, player, sent_us, to_server_us=30_300 if slow else 300)
        if poll == 267:
            jiffies = player.jiffies(sent_us)
            # the mapping coasts on what it knew
            assert abs(_mapped(slowed, jiffies) - _mapped(clean, jiffies)) <= 100
    assert slowed.generation == 0
    jiffies = player.jiffies(123_000_000)
    assert abs(_mapped(slowed, jiffies) - _mapped(clean, jiffies)) <= 100


def test_clock_step_shows_through_slow_replies() -> None:
    """A clock step is still a jump when the replies are slow at the same time."""
    clock = PlayerClock()
    player = _Player()
    now_us = _learn(clock, player, 3_000_000, 60)
    player.clock_step_ms = 50.0
    for poll in range(24):
        _sample(clock, player, now_us + poll * 250_000, to_server_us=30_300)
    assert clock.generation == 1


def test_clock_restart_forgets_the_old_round_trips() -> None:
    """After a restart on a slow path, the round trips from before it explain nothing."""
    clock = PlayerClock()
    player = _Player()
    now_us = _learn(clock, player, 3_000_000, 60)
    player.clock_step_ms = 50.0
    for poll in range(32):
        _sample(clock, player, now_us + poll * 250_000, to_server_us=30_300)
    assert clock.generation == 1
    # a second, smaller step on the same slow path
    now_us += 8_000_000
    player.clock_step_ms = 60.0
    for poll in range(32):
        _sample(clock, player, now_us + poll * 250_000, to_server_us=30_300)
    assert clock.generation == 2


def test_clock_follows_a_network_path_that_stays_slow() -> None:
    """When the slow path is the only one left in the window, the mapping moves onto it."""
    clock = PlayerClock()
    player = _Player()
    now_us = _learn(clock, player, 3_000_000, 60)
    for poll in range(4 * 150):
        _sample(clock, player, now_us + poll * 250_000, to_server_us=30_300)
        if poll == 4 * 100:
            assert clock.generation == 0
    assert clock.generation == 1
    now_us += 150_000_000
    assert _mapped(clock, player.jiffies(now_us)) - now_us == pytest.approx(15_000, abs=1000)


@pytest.mark.parametrize("step_ms", [50.0, -50.0, 3_600_000.0, -3_600_000.0])
def test_clock_starts_over_after_a_step(step_ms: float) -> None:
    """A step of the player's clock restarts the mapping and bumps the generation."""
    clock = PlayerClock()
    player = _Player(clock_ppm=15.0)
    now_us = _learn(clock, player, 3_000_000, 60)
    assert clock.generation == 0
    player.clock_step_ms = step_ms
    # the old mapping is off by the step for readings taken after it
    assert abs(_mapped(clock, player.jiffies(now_us)) - now_us) > 40_000
    now_us = _learn(clock, player, now_us, 6)
    assert clock.generation == 1
    assert abs(_mapped(clock, player.jiffies(now_us)) - now_us) <= 1000
    assert clock.to_jiffies(now_us) == player.jiffies(now_us)
    # and it settles again, drift included
    now_us = _learn(clock, player, now_us, 60)
    assert clock.generation == 1
    assert abs(_mapped(clock, player.jiffies(now_us)) - now_us) <= 1000
    assert clock.drift_ppm == pytest.approx(-15.0, abs=5.0)


def test_clock_survives_degenerate_samples() -> None:
    """Identical samples, a zero round trip and a long silence do not break the mapping."""
    clock = PlayerClock()
    for _ in range(40):
        clock.add_sample(5000, 9_000_000, 9_000_000)
    assert clock.to_server_us(5000) == 9_000_000
    assert clock.to_jiffies(9_000_000) == 5000
    assert clock.drift_ppm == 0.0

    clock = PlayerClock()
    player = _Player()
    now_us = _learn(clock, player, 3_000_000, 60)
    # an hour without samples
    now_us = _learn(clock, player, now_us + 3_600_000_000, 30)
    assert clock.generation == 0
    assert abs(_mapped(clock, player.jiffies(now_us)) - now_us) <= 1000


# ---------------------------------------------------------------- SyncLoop: reports


def test_report_error_is_measured_at_the_middle_of_the_millisecond() -> None:
    """Elapsed is truncated by the player, so a report stands for half a millisecond more."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    assert loop.error_ms is None
    assert loop.start_error_ms is None
    # 2000 ms after the start the player says it played 1990 ms: 1990.5 on average
    assert loop.on_report(START_US + 2_000_000, 1990) == pytest.approx(9.5)
    assert loop.error_ms == pytest.approx(9.5)
    # an early player has a negative error
    assert loop.on_report(START_US + 2_250_000, 2260) == pytest.approx(-10.5)
    assert loop.error_ms == pytest.approx(-0.5)


def test_reports_before_settling_are_ignored() -> None:
    """Reports of the first second after the start do not count for the error."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    assert loop.on_report(START_US - 500_000, 10) is None
    assert loop.on_report(START_US + 999_999, 989) is None
    assert loop.error_ms is None
    assert loop.on_report(START_US + 1_000_000, 990) == pytest.approx(9.5)

    slow_start = SyncLoop(RATE, FRAME_SIZE, START_US, settle_s=3.0)
    assert slow_start.on_report(START_US + 2_999_000, 2990) is None
    assert slow_start.on_report(START_US + 3_000_000, 2990) == pytest.approx(9.5)


def test_reports_without_progress_or_news_are_ignored() -> None:
    """A report with nothing played, or that repeats the previous one, does not count."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    assert loop.on_report(START_US + 2_000_000, 0) is None
    assert loop.error_ms is None
    assert loop.on_report(START_US + 2_000_000, 1990) == pytest.approx(9.5)
    assert loop.on_report(START_US + 2_000_000, 1990) is None
    # the same reading again after the clock mapping moved a little
    assert loop.on_report(START_US + 2_000_040, 1990) is None
    assert loop.on_report(START_US + 1_999_960, 1990) is None
    assert loop.error_ms == pytest.approx(9.5)
    # a later reading with the same elapsed time is news: the player did not move on
    assert loop.on_report(START_US + 2_001_000, 1990) == pytest.approx(10.5)
    assert loop.error_ms == pytest.approx(10.0)


def test_elapsed_carries_on_when_the_frame_counter_of_the_player_wraps() -> None:
    """A player counts frames in 32 bits: its elapsed time starts over, the error does not."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    wrap_ms = (1 << 32) * 1000 / RATE
    last_ms = int(wrap_ms) - 400
    for elapsed_ms in range(last_ms - 1000, last_ms + 250, 250):
        assert _report(loop, elapsed_ms, 3.0) == pytest.approx(3.0)
    # 800 ms later the counter is 400 ms past its wrap: the player reports what is left
    played_ms = last_ms + 800
    server_us = START_US + played_ms * 1000 + 500 + 3000
    assert loop.on_report(server_us, int(played_ms - wrap_ms)) == pytest.approx(3.0, abs=1.0)
    assert loop.error_ms == pytest.approx(3.0, abs=0.5)
    # and again at the next wrap
    for played_ms in (int(2 * wrap_ms) - 400, int(2 * wrap_ms) + 400):
        server_us = START_US + played_ms * 1000 + 500 + 3000
        reported_ms = int(played_ms - wrap_ms * (played_ms // wrap_ms))
        assert loop.on_report(server_us, reported_ms) == pytest.approx(3.0, abs=1.0)


def test_elapsed_that_steps_back_a_little_is_no_wrap() -> None:
    """A report that is behind the one before it is an error of that size, not a wrap."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    assert _report(loop, 2000, 0.0) == pytest.approx(0.0)
    # the same instant as a report of 2250 ms, with 400 ms less played
    assert loop.on_report(START_US + 2_250_500, 1850) == pytest.approx(400.0)


def test_loop_acts_from_the_fifth_report() -> None:
    """The rate stays zero until five reports are in, then follows the error."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    _reports(loop, 3.0, count=4)
    assert loop.rate_ppm == 0.0
    assert loop.error_ms == pytest.approx(3.0)
    _report(loop, 3000, 3.0)
    assert loop.rate_ppm == pytest.approx(3.0 * GAIN_PPM_PER_MS)
    assert loop.start_error_ms == pytest.approx(3.0)
    assert loop.drift_ppm == 0.0

    early = SyncLoop(RATE, FRAME_SIZE, START_US)
    _reports(early, -3.0)
    assert early.rate_ppm == pytest.approx(-3.0 * GAIN_PPM_PER_MS)


@pytest.mark.parametrize(("late_ms", "rate_ppm"), [(8.0, MAX_RATE_PPM), (-8.0, -MAX_RATE_PPM)])
def test_rate_is_limited(late_ms: float, rate_ppm: float) -> None:
    """The rate never goes beyond the maximum, in either direction."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    _reports(loop, late_ms)
    assert loop.rate_ppm == rate_ppm

    custom = SyncLoop(RATE, FRAME_SIZE, START_US, gain_ppm_per_ms=10.0, max_rate_ppm=50.0)
    _reports(custom, late_ms / 4)
    assert custom.rate_ppm == pytest.approx(late_ms / 4 * 10.0)
    _reports(custom, late_ms, first_ms=4000)
    assert custom.rate_ppm == rate_ppm / 10


def test_drift_is_fed_forward_after_ten_seconds() -> None:
    """Once the reports span ten seconds their slope is added to the rate."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    # the player loses 0.1 ms per second: 100 ppm slow
    for elapsed_ms in range(2000, 11_750, 250):
        _report(loop, elapsed_ms, elapsed_ms / 10_000)
    assert loop.drift_ppm == 0.0
    for elapsed_ms in range(11_750, 14_250, 250):
        _report(loop, elapsed_ms, elapsed_ms / 10_000)
    assert loop.drift_ppm == pytest.approx(100.0, rel=1e-3)
    assert loop.error_ms == pytest.approx(1.4, abs=1e-3)
    assert loop.rate_ppm == pytest.approx(100.0 + 1.4 * GAIN_PPM_PER_MS, rel=1e-3)


def test_drift_counts_for_the_time_until_a_correction_is_heard() -> None:
    """A correction is heard once what was sent before it has played: the player drifts on until then."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    # the stream is 6 seconds ahead of the last report of a player that is 100 ppm slow
    loop.process(bytes(20 * RATE * FRAME_SIZE))
    for elapsed_ms in range(2000, 14_250, 250):
        _report(loop, elapsed_ms, elapsed_ms / 10_000)

    assert loop.drift_ppm == pytest.approx(100.0, rel=1e-3)
    assert loop.error_ms == pytest.approx(1.4, abs=1e-3)
    assert loop.rate_ppm == pytest.approx(100.0 + (1.4 + 0.6) * GAIN_PPM_PER_MS, rel=1e-3)


@pytest.mark.parametrize(("late_ms", "on_timeline"), [(0.9, True), (-0.9, True), (1.4, False)])
def test_player_is_on_its_timeline_after_four_reports_with_little_to_correct(
    late_ms: float, on_timeline: bool
) -> None:
    """A second of reports within a millisecond of the timeline brings the long lead."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    # the loop acts from the fifth report on
    next_ms = _reports(loop, late_ms, count=7)
    assert not loop.on_timeline
    _reports(loop, late_ms, first_ms=next_ms, count=20)
    assert loop.on_timeline is on_timeline


def test_player_stays_on_its_timeline_whatever_it_reports_later() -> None:
    """An error or a clock step after it takes nothing back: the lead does not flap."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US, window_s=2.0)
    next_ms = _reports(loop, 0.5, count=8)
    assert loop.on_timeline

    for late_ms in (0.9, 1.1, 8.0, 0.9, 1.1):
        next_ms = _reports(loop, late_ms, first_ms=next_ms, count=10)
        assert loop.on_timeline
    _reports(loop, 50.0, first_ms=next_ms, count=10, generation=1)
    assert loop.on_timeline


def test_a_report_with_more_to_correct_starts_the_count_over() -> None:
    """Three reports with little to correct, one with more and three with little again are no four in a row."""
    # the error is the mean of the last five reports
    loop = SyncLoop(RATE, FRAME_SIZE, START_US, window_s=1.1)
    # which is 0.5 ms from the fifth report on, 1.5 ms at the eighth and 0.5 ms after it
    reports = [0.5] * 7 + [5.5, -4.5, 0.5, 0.5]
    errors = []
    for number, late_ms in enumerate(reports):
        _report(loop, 2000 + number * 250, late_ms)
        errors.append(loop.error_ms)
        assert not loop.on_timeline
    assert errors[4:] == pytest.approx([0.5, 0.5, 0.5, 1.5, 0.5, 0.5, 0.5])

    _report(loop, 2000 + len(reports) * 250, 0.5)
    assert loop.on_timeline


def test_reports_before_a_clock_step_do_not_count_for_the_timeline() -> None:
    """The four reports have to follow each other on one mapping of the clock of the player."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    next_ms = _reports(loop, 0.5, count=7)
    next_ms = _reports(loop, 0.5, first_ms=next_ms, count=7, generation=1)
    assert not loop.on_timeline
    _reports(loop, 0.5, first_ms=next_ms, count=1, generation=1)
    assert loop.on_timeline


def test_filter_window_slides() -> None:
    """Reports older than the window no longer count."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US, window_s=5.0, min_span_s=math.inf)
    _reports(loop, 2.0, first_ms=2000, count=20)
    assert loop.error_ms == pytest.approx(2.0)
    _reports(loop, 4.0, first_ms=7000, count=10)
    assert loop.error_ms == pytest.approx(3.0)
    _reports(loop, 4.0, first_ms=9500, count=11)
    assert loop.error_ms == pytest.approx(4.0)


def test_filter_leaves_outliers_out() -> None:
    """With twenty reports or more, one that is far off the line is left out."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    next_ms = _reports(loop, 2.0, count=24)
    _report(loop, next_ms, 8.0)
    _reports(loop, 2.0, first_ms=next_ms + 250, count=5)
    assert loop.error_ms == pytest.approx(2.0)

    # with fewer reports there is too little to tell an outlier by
    few = SyncLoop(RATE, FRAME_SIZE, START_US)
    next_ms = _reports(few, 2.0, count=13)
    _report(few, next_ms, 8.0)
    _reports(few, 2.0, first_ms=next_ms + 250, count=5)
    assert few.error_ms == pytest.approx(2.0 + 6.0 / 19)


def test_loop_survives_degenerate_reports() -> None:
    """Reports at one instant, a single report and a huge gap do not break the loop."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US, min_span_s=0.0)
    for elapsed_ms in range(1900, 1930):
        assert loop.on_report(START_US + 2_000_000, elapsed_ms) is not None
    assert loop.drift_ppm == 0.0
    assert loop.error_ms == pytest.approx(85.0)

    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    _reports(loop, 3.0)
    rate_ppm = loop.rate_ppm
    # the next report comes ten hours later: it is the only one left in the window
    assert _report(loop, 36_000_000, 1.0) == pytest.approx(1.0)
    assert loop.error_ms == pytest.approx(1.0)
    assert loop.drift_ppm == 0.0
    assert loop.rate_ppm == rate_ppm


# ---------------------------------------------------------------- SyncLoop: process


def test_process_passes_audio_through_while_there_is_nothing_to_correct() -> None:
    """Without a correction due the block comes back as the very same object."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    block = _source(0, 480)
    assert loop.process(block) is block
    assert loop.process(b"") == b""
    assert (loop.frames_sent, loop.frames_consumed) == (480, 480)
    _reports(loop, 3.0)
    # 300 ppm of 480 frames is 0.144 frame: nothing due for the first six blocks
    for index in range(1, 7):
        block = _source(index * 480, 480)
        assert loop.process(block) is block
    assert (loop.frames_sent, loop.frames_consumed) == (3360, 3360)
    assert len(loop.process(_source(3360, 480))) == 479 * FRAME_SIZE


def test_process_drops_one_frame_in_the_middle() -> None:
    """A late player gets a block with one frame less, and the remainder carries over."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    _reports(loop, 3.0)
    sent: list[int] = []
    lengths = []
    for index in range(8):
        out = loop.process(_source(index * 1000, 1000))
        lengths.append(len(out) // FRAME_SIZE)
        sent += _frames(out)
    # 0.3 frame per block: due in the fourth block and again in the seventh
    assert lengths == [1000, 1000, 1000, 999, 1000, 1000, 999, 1000]
    assert sent == [frame for frame in range(8000) if frame not in (3500, 6500)]
    assert (loop.frames_sent, loop.frames_consumed) == (7998, 8000)


def test_process_repeats_one_frame_in_the_middle() -> None:
    """An early player gets a block with one frame twice."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    _reports(loop, -3.0)
    sent: list[int] = []
    for index in range(8):
        sent += _frames(loop.process(_source(index * 1000, 1000)))
    expected = list(range(8000))
    expected.insert(6500, 6500)
    expected.insert(3500, 3500)
    assert sent == expected
    assert (loop.frames_sent, loop.frames_consumed) == (8002, 8000)


@pytest.mark.parametrize("late_ms", [8.0, -8.0])
def test_process_spreads_several_corrections_over_a_block(late_ms: float) -> None:
    """A big block gets its corrections evenly spread, never two next to each other."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    _reports(loop, late_ms)
    # 500 ppm of 9500 frames is 4.75 frames: four now, five in the next block
    first = _frames(loop.process(_source(0, 9500)))
    second = _frames(loop.process(_source(9500, 9500)))
    middles = [1187, 3562, 5937, 8312, 9500 + 950, 9500 + 2850, 9500 + 4750, 9500 + 6650, 18_050]
    expected = list(range(19_000))
    for middle in reversed(middles):
        if late_ms > 0:
            del expected[middle]
        else:
            expected.insert(middle, middle)
    assert first + second == expected
    assert len(first) == 9500 - math.copysign(4, late_ms)
    assert len(second) == 9500 - math.copysign(5, late_ms)
    assert _offset(loop) == math.copysign(9, late_ms)
    assert min(later - earlier for earlier, later in pairwise(middles)) > 1


@pytest.mark.parametrize("late_ms", [5.0, -5.0])
def test_process_handles_blocks_of_any_size(late_ms: float) -> None:
    """Odd and tiny blocks give the same stream: single frames dropped or repeated."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    _reports(loop, late_ms)
    rate = loop.rate_ppm * 1e-6
    rng = random.Random(7)  # noqa: S311
    sent: list[int] = []
    consumed = 0
    while consumed < 400_000:
        size = rng.choice([0, 1, 2, 3, 7, 97, 441, 480, 1001, 4096])
        out = loop.process(_source(consumed, size))
        consumed += size
        sent += _frames(out)
        assert len(out) % FRAME_SIZE == 0
        assert (loop.frames_sent, loop.frames_consumed) == (len(sent), consumed)
    steps = [later - earlier for earlier, later in pairwise(sent)]
    odd_step = 2 if late_ms > 0 else 0
    assert set(steps) == {1, odd_step}
    corrections = [index for index, step in enumerate(steps) if step == odd_step]
    # as many as the rate asks for, and about as far apart as it asks for
    assert len(corrections) == int(abs(rate) * consumed)
    gaps = [later - earlier for earlier, later in pairwise(corrections)]
    assert min(gaps) > 0.5 / abs(rate)
    assert max(gaps) < 2 / abs(rate)
    assert sent[0] == 0
    assert sent[-1] == consumed - 1


def test_report_error_follows_the_frames_actually_sent() -> None:
    """
    The error of a report counts the corrections that were played, wherever the ledger is cut.

    The stream that was sent tells which source frame the player is at, so the error can
    be worked out from the PCM itself. The ledger only has to keep what is not played yet.
    """
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    sent: list[int] = []

    def send(frames: int) -> None:
        for _ in range(frames // 480):
            sent.extend(_frames(loop.process(_source(loop.frames_consumed, 480))))

    send(3 * RATE)
    assert sent == list(range(3 * RATE))
    late_ms = 4.0
    _reports(loop, late_ms)
    assert loop.rate_ppm == pytest.approx(400.0)
    longest = played = 0
    for elapsed_ms in range(3250, 30_000, 250):
        # stay 1.5 s ahead of the player, like a real stream
        send((elapsed_ms + 1500) * RATE // 1000 - loop.frames_sent)
        position = int((elapsed_ms + 0.5) * RATE / 1000)
        # the corrections the player has played, read from the stream itself
        played = sent[position] - position
        error_ms = _report(loop, elapsed_ms, late_ms)
        assert error_ms == pytest.approx(late_ms - played * 1000 / RATE, abs=1e-9)
        longest = max(longest, len(loop._ledger))
    # the error goes down by a tenth per second: most of the 192 frames are gone by now
    assert 170 < _offset(loop) < 192
    # the filtered error counts what is played, which is a little less than what is sent
    assert loop.error_ms == pytest.approx(late_ms - played * 1000 / RATE)
    assert played < _offset(loop)
    assert {later - earlier for earlier, later in pairwise(sent)} == {1, 2}
    # only the corrections of the 1.5 s that were sent ahead are kept
    assert longest <= 2 + 1.5 * RATE * MAX_RATE_PPM * 1e-6


@pytest.mark.parametrize("late_ms", [8.0, -8.0])
def test_error_counts_a_correction_from_its_own_frame(late_ms: float) -> None:
    """
    A correction counts in the error from the very frame at which it is played.

    At 1 kHz a frame takes a millisecond, so a report can be at any frame.
    """
    loop = SyncLoop(1000, FRAME_SIZE, START_US)
    sent = _frames(loop.process(_source(0, 3000)))
    _reports(loop, late_ms)
    assert loop.rate_ppm == math.copysign(MAX_RATE_PPM, late_ms)
    # 500 ppm: two corrections in the first block, then none, one, none and two
    for size in (5000, 3, 1100, 7, 4900):
        sent += _frames(loop.process(_source(loop.frames_consumed, size)))
    # the corrections the player has played at every position, read from the stream itself
    played = [frame - position for position, frame in enumerate(sent)]
    corrections = [at for at in range(1, len(sent)) if played[at] != played[at - 1]]
    assert len(corrections) == 5
    for correction in corrections:
        for position in range(correction - 2, correction + 2):
            error_ms = _report(loop, position, late_ms)
            assert error_ms == pytest.approx(late_ms - played[position], abs=1e-9)


def test_error_counts_corrections_once_they_are_played() -> None:
    """Corrections on their way to the player lower the rate, but not the error yet."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    for _ in range(40):
        loop.process(bytes(4800 * FRAME_SIZE))
    _reports(loop, 4.0)
    assert loop.rate_ppm == pytest.approx(400.0)
    # one more second of audio: 400 ppm of it is 19 frames, at stream position 192000 and up
    for _ in range(10):
        loop.process(bytes(4800 * FRAME_SIZE))
    assert _offset(loop) == 19
    sent_ms = 19 * 1000 / RATE
    # the player is at 3.25 s, well before those frames
    assert _report(loop, 3250, 4.0) == pytest.approx(4.0)
    assert loop.error_ms == pytest.approx(4.0)
    assert loop.rate_ppm == pytest.approx((4.0 - sent_ms) * GAIN_PPM_PER_MS)
    # and once it is past them
    assert _report(loop, 5500, 4.0) == pytest.approx(4.0 - sent_ms)


# ---------------------------------------------------------------- SyncLoop: start error


def test_start_error_is_the_mean_of_the_first_three_reports() -> None:
    """The start is measured as soon as the player shows it plays, before it settled."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    # a player that is 280 ms late, reporting every 50 ms from 200 ms into its stream
    assert _report(loop, 200, 281.0) is None
    assert _report(loop, 250, 279.5) is None
    assert loop.start_error_ms is None
    assert _report(loop, 300, 280.0) is None
    assert loop.start_error_ms == pytest.approx(840.5 / 3)
    # which is all within a second of the start: nothing of it counts for the error yet
    assert loop.error_ms is None
    assert loop.rate_ppm == 0.0
    # and it is measured once
    _report(loop, 350, 250.0)
    assert loop.start_error_ms == pytest.approx(840.5 / 3)


def test_start_error_leaves_the_first_reports_of_a_stream_out() -> None:
    """A player reports tens of milliseconds off in the first 200 ms that it plays."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    # as recorded: 24 ms early, then 11 ms and 6 ms late, against the 2 ms it settles on
    for elapsed_ms, late_ms in ((31, -22.0), (131, 13.0), (146, 8.0), (199, 9.0)):
        assert _report(loop, elapsed_ms, late_ms) is None
    assert loop.start_error_ms is None
    # nothing played at all is no report either
    assert loop.on_report(START_US + 5_000_000, 0) is None
    for elapsed_ms in (200, 250):
        _report(loop, elapsed_ms, 2.0)
    assert loop.start_error_ms is None
    _report(loop, 300, 2.0)
    assert loop.start_error_ms == pytest.approx(2.0)


def test_start_error_is_measured_whenever_the_player_first_reports() -> None:
    """A player that only shows where it is long after its start is measured the same way."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    for elapsed_ms in (0, 0, 0):
        assert loop.on_report(START_US + 1_000_000, elapsed_ms) is None
    _reports(loop, 40.0, first_ms=6000, count=2)
    assert loop.start_error_ms is None
    _report(loop, 6500, 40.0)
    assert loop.start_error_ms == pytest.approx(40.0)


def _collected(loop: SyncLoop) -> list[float]:
    """Return the errors of the reports a loop takes its start error from so far."""
    return list(loop.start_reports)


def _at(loop: SyncLoop, elapsed_ms: int, at_ms: int) -> float | None:
    """Report ``elapsed_ms`` played, ``at_ms`` after the start instant."""
    return loop.on_report(START_US + at_ms * 1000, elapsed_ms)


def test_start_error_is_taken_from_where_a_player_settles() -> None:
    """As logged on a player: it counts from 126 ms too early at first, then moves its position."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US, start_jump_ms=1.0, told_us=START_US - 180_000)
    for elapsed_ms, at_ms in ((0, -120), (0, -69), (100, -15), (100, -15), (148, 33)):
        _at(loop, elapsed_ms, at_ms)
    assert _collected(loop) == []
    # one report of the first 200 ms that still counts from the early origin
    _at(loop, 200, 85)
    assert _collected(loop) == [-115.5]
    # its elapsed time goes back by 77 ms: from here on it is 11 ms late
    for elapsed_ms, at_ms in ((123, 134), (176, 187)):
        _at(loop, elapsed_ms, at_ms)
    assert _collected(loop) == [-115.5]
    _at(loop, 234, 245)
    assert _collected(loop) == [10.5]
    _at(loop, 283, 295)
    assert loop.start_error_ms is None
    _at(loop, 333, 345)

    assert loop.start_error_ms == pytest.approx(33.5 / 3)
    # a late player: frames are left out, and no silence is added
    assert loop.pending_jump == round(33.5 / 3 * 48)
    assert loop.process(b"") == b""


def test_start_error_starts_over_when_a_player_moves_its_position() -> None:
    """Reports from before a step say nothing about the start, however many there are."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    _report(loop, 200, -100.0)
    _report(loop, 250, -100.0)
    assert _collected(loop) == [-100.0, -100.0]
    # the report that shows the step is the first of the ones that count
    _report(loop, 300, 12.0)
    assert _collected(loop) == [12.0]
    _report(loop, 350, 12.0)
    assert loop.start_error_ms is None
    _report(loop, 400, 13.0)
    assert loop.start_error_ms == pytest.approx(37.0 / 3)

    # a player that keeps moving is not measured: there is no start to tell from it
    restless = SyncLoop(RATE, FRAME_SIZE, START_US)
    for index in range(12):
        _report(restless, 200 + 50 * index, 20.0 * (index // 2))
    assert restless.start_error_ms is None
    assert _collected(restless) == [100.0, 100.0]


def test_start_error_takes_reports_that_differ_by_a_millisecond_or_two() -> None:
    """Reports of one position scatter and drift a little: they are the same position."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    for elapsed_ms, late_ms in ((200, 10.0), (250, 11.0), (300, 12.5)):
        _report(loop, elapsed_ms, late_ms)
    assert loop.start_error_ms == pytest.approx(33.5 / 3)

    # it is the mean of the ones before that a report is held against, not the first of them
    drifting = SyncLoop(RATE, FRAME_SIZE, START_US)
    for elapsed_ms, late_ms in ((200, 10.0), (250, 14.0), (300, 16.5)):
        _report(drifting, elapsed_ms, late_ms)
    assert drifting.start_error_ms == pytest.approx(13.5)

    # up to 5 ms from that mean
    edge = SyncLoop(RATE, FRAME_SIZE, START_US)
    _report(edge, 200, 10.0)
    _report(edge, 250, 15.0)
    assert _collected(edge) == [10.0, 15.0]
    _report(edge, 300, 17.6)
    assert _collected(edge) == pytest.approx([17.6])


def test_start_error_leaves_out_reports_that_put_the_start_before_it_was_told() -> None:
    """A player does not start before it is told to: such a report is no position of this stream."""
    # a player with a start offset of 180 ms: it is told that much before its start instant
    loop = SyncLoop(RATE, FRAME_SIZE, START_US, told_us=START_US - 180_000)
    # as logged on a player: 5.6 s played, a quarter of a second after it started
    for elapsed_ms in (5568, 5618, 5668):
        assert _report(loop, elapsed_ms, -5368.0) is None
    assert _collected(loop) == []
    assert loop.start_error_ms is None
    assert loop.pending_jump == 0
    # the reports of what it plays, 20 ms late, are the start
    for elapsed_ms in (350, 400):
        _report(loop, elapsed_ms, 20.0)
    assert _collected(loop) == [20.0, 20.0]
    _report(loop, 450, 20.0)
    assert loop.start_error_ms == pytest.approx(20.0)
    assert loop.pending_jump == 960

    # a loop that is not told when the player was told to start takes every report
    unbound = SyncLoop(RATE, FRAME_SIZE, START_US)
    for elapsed_ms in (5568, 5618, 5668):
        _report(unbound, elapsed_ms, -5368.0)
    assert unbound.start_error_ms == pytest.approx(-5368.0)


def test_start_error_takes_every_report_once_its_bound_is_lifted() -> None:
    """A player that was not measured in time is measured from whatever it reports next."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US, told_us=START_US - 180_000)
    for elapsed_ms in (5568, 5618, 5668):
        assert _report(loop, elapsed_ms, -5368.0) is None
    assert _collected(loop) == []

    loop.lift_start_bound()

    for elapsed_ms in (5718, 5768, 5818):
        _report(loop, elapsed_ms, -5368.0)
    assert loop.start_error_ms == pytest.approx(-5368.0)


def test_start_error_of_a_player_that_starts_before_its_instant_counts() -> None:
    """A player that needs less than its start offset starts early, not before it was told."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US, told_us=START_US - 180_000)
    for elapsed_ms in (200, 250, 300):
        _report(loop, elapsed_ms, -150.0)
    assert loop.start_error_ms == pytest.approx(-150.0)

    # with 20 ms to spare for what a report can be off
    edge = SyncLoop(RATE, FRAME_SIZE, START_US, told_us=START_US - 180_000)
    _report(edge, 200, -200.1)
    assert _collected(edge) == []
    _report(edge, 250, -200.0)
    assert _collected(edge) == [-200.0]


def test_reports_that_put_the_start_before_it_was_told_do_not_count_for_the_error() -> None:
    """Until the start is measured such a report says nothing about the stream at all."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US, told_us=START_US)
    # settled, but no position of this stream
    assert _report(loop, 6400, -5368.0) is None
    assert loop.error_ms is None
    next_ms = _reports(loop, 3.0, first_ms=1100)
    assert loop.start_error_ms == pytest.approx(3.0)
    assert loop.error_ms == pytest.approx(3.0)

    # once the start is measured the bound is gone: a player that plays fast for long
    # enough is that early without its corrections
    assert _report(loop, next_ms, -300.0) == pytest.approx(-300.0)
    assert loop.error_ms == pytest.approx(3.0 - 303.0 / 6)


def test_start_error_in_silence_is_jumped_from_a_lower_threshold() -> None:
    """The threshold of the jump can be lowered for as long as the stream is silence."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US, start_jump_ms=1.0)
    for elapsed_ms in (200, 250, 300):
        _report(loop, elapsed_ms, 3.0)
    assert loop.start_error_ms == pytest.approx(3.0)
    assert loop.pending_jump == 144
    assert loop.process(bytes(4800 * FRAME_SIZE)) == bytes((4800 - 144) * FRAME_SIZE)
    assert loop.pending_jump == 0

    # and raised again before the start is measured, for a stream that carries audio by then
    audible = SyncLoop(RATE, FRAME_SIZE, START_US, start_jump_ms=1.0)
    for elapsed_ms in (200, 250):
        _report(audible, elapsed_ms, 3.0)
    audible.start_jump_ms = 10.0
    _report(audible, 300, 3.0)
    assert audible.start_error_ms == pytest.approx(3.0)
    assert audible.pending_jump == 0
    block = bytes(4800 * FRAME_SIZE)
    assert audible.process(block) is block

    # an error below the threshold is left to the rate
    exact = SyncLoop(RATE, FRAME_SIZE, START_US, start_jump_ms=1.0)
    for elapsed_ms in (200, 250, 300):
        _report(exact, elapsed_ms, 0.9)
    assert exact.start_error_ms == pytest.approx(0.9)
    assert exact.pending_jump == 0
    assert exact.process(block) is block


def test_start_error_of_an_early_player_in_silence_is_filled_at_once() -> None:
    """The silence of an early start goes out with nothing else to send."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US, start_jump_ms=1.0)
    for elapsed_ms in (200, 250, 300):
        _report(loop, elapsed_ms, -40.0)
    assert loop.pending_jump == -1920
    assert loop.process(b"") == bytes(1920 * FRAME_SIZE)
    assert loop.pending_jump == 0
    assert (loop.frames_sent, loop.frames_consumed) == (1920, 0)
    assert loop.process(b"") == b""


# ---------------------------------------------------------------- SyncLoop: start jump


def test_start_error_of_a_late_player_is_dropped_in_one_jump() -> None:
    """A player that starts far too late gets that much audio skipped, once."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    for index in range(40):
        loop.process(_source(index * 4800, 4800))
    _reports(loop, 420.0, count=2)
    assert loop.start_error_ms is None
    _report(loop, 2500, 420.0)
    assert loop.start_error_ms == pytest.approx(420.0)
    assert loop.pending_jump == 420 * RATE // 1000
    # the jump is as good as made: nothing is left for the rate to do
    _reports(loop, 420.0, first_ms=2750, count=2)
    assert abs(loop.rate_ppm) < 0.1
    jump = 420 * RATE // 1000
    sent: list[int] = []
    lengths = []
    for index in range(40, 48):
        out = loop.process(_source(index * 4800, 4800))
        lengths.append(len(out) // FRAME_SIZE)
        sent += _frames(out)
    # 20160 frames: four whole blocks and a part of the fifth
    assert lengths == [0, 0, 0, 0, 5 * 4800 - jump, 4800, 4800, 4800]
    assert sent == list(range(192_000 + jump, 48 * 4800))
    assert _offset(loop) == jump
    assert loop.pending_jump == 0
    # the player has not reached the jump yet: still late, and no second jump
    assert _report(loop, 3250, 420.0) == pytest.approx(420.0)
    assert loop.error_ms == pytest.approx(420.0)
    block = _source(48 * 4800, 4800)
    assert loop.process(block) is block
    # and when it has
    assert _report(loop, 4250, 420.0) == pytest.approx(0.0, abs=1e-9)
    assert loop.start_error_ms == pytest.approx(420.0)
    assert _offset(loop) == jump


def test_start_error_that_is_measured_late_is_jumped_for_what_the_rate_left_of_it() -> None:
    """A start the rate is correcting already is jumped for the rest, not for all of it again."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    # a player whose reports keep moving: no start error yet, but an error for the rate
    for index in range(8):
        _report(loop, 1000 + index * 250, 300.0 + (8.0 if index % 2 else -8.0))
        loop.process(_source(index * 12_000, 12_000))
    assert loop.start_error_ms is None
    assert loop.rate_ppm == MAX_RATE_PPM
    corrected = _offset(loop)
    assert corrected == 24

    for elapsed_ms in (3000, 3250, 3500):
        _report(loop, elapsed_ms, 300.0)
    assert loop.start_error_ms == pytest.approx(300.0)
    assert loop.pending_jump == 300 * RATE // 1000 - corrected
    # with the jump made the stream is corrected for the start error, once
    loop.process(_source(96_000, 24_000))
    assert _offset(loop) == 300 * RATE // 1000


def test_start_error_of_an_early_player_is_filled_with_silence() -> None:
    """A player that starts far too early gets silence first, once."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    for index in range(40):
        loop.process(_source(index * 4800, 4800))
    _reports(loop, -420.0)
    assert loop.start_error_ms == pytest.approx(-420.0)
    assert abs(loop.rate_ppm) < 0.1
    jump = 420 * RATE // 1000
    out = loop.process(_source(192_000, 4800))
    assert out[: jump * FRAME_SIZE] == bytes(jump * FRAME_SIZE)
    assert _frames(out[jump * FRAME_SIZE :]) == list(range(192_000, 196_800))
    assert (loop.frames_sent, loop.frames_consumed) == (196_800 + jump, 196_800)
    block = _source(196_800, 4800)
    assert loop.process(block) is block
    # the player is in the silence, 4.25 s into the stream: the rest will be on time
    assert _report(loop, 4250, -420.0) == pytest.approx(0.0, abs=1e-9)
    assert _offset(loop) == -jump


@pytest.mark.parametrize("late_ms", [8.0, -8.0])
def test_small_start_error_is_left_to_the_rate(late_ms: float) -> None:
    """Below the threshold there is no jump: the rate removes the error."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    _reports(loop, late_ms)
    assert loop.start_error_ms == pytest.approx(late_ms)
    assert loop.rate_ppm == math.copysign(MAX_RATE_PPM, late_ms)
    out = loop.process(bytes(4800 * FRAME_SIZE))
    assert len(out) // FRAME_SIZE == 4800 - math.copysign(2, late_ms)

    # the threshold can be moved
    jumpy = SyncLoop(RATE, FRAME_SIZE, START_US, start_jump_ms=5.0)
    _reports(jumpy, late_ms)
    out = jumpy.process(bytes(4800 * FRAME_SIZE))
    assert len(out) // FRAME_SIZE == 4800 - late_ms * RATE // 1000


@pytest.mark.parametrize("late_ms", [MAX_START_JUMP_MS + 1, -MAX_START_JUMP_MS - 1])
def test_start_error_beyond_the_maximum_is_not_jumped(late_ms: float) -> None:
    """A start that is off by more than the maximum is reported, but not corrected in a jump."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    _reports(loop, late_ms, first_ms=4000)
    assert loop.start_error_ms == pytest.approx(late_ms)
    out = loop.process(bytes(4800 * FRAME_SIZE))
    assert len(out) // FRAME_SIZE == 4800 - math.copysign(2, late_ms)


# ---------------------------------------------------------------- SyncLoop: clock jumps


def test_clock_jump_pauses_and_resumes_corrections() -> None:
    """A new clock generation empties the filter and holds the rate until it is filled again."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    next_ms = _reports(loop, 3.0, count=8)
    assert loop.rate_ppm == pytest.approx(300.0)
    for _ in range(10):
        loop.process(bytes(4800 * FRAME_SIZE))
    assert _offset(loop) == 14
    left_ms = 3.0 - 14 * 1000 / RATE
    # the first report after the clock started over
    assert _report(loop, next_ms, 3.0, generation=1) == pytest.approx(left_ms)
    assert loop.rate_ppm == 0.0
    assert loop.drift_ppm == 0.0
    assert loop.error_ms == pytest.approx(left_ms)
    for elapsed_ms in range(next_ms + 250, next_ms + 1000, 250):
        _report(loop, elapsed_ms, 3.0, generation=1)
        assert loop.rate_ppm == 0.0
        block = bytes(4800 * FRAME_SIZE)
        assert loop.process(block) is block
    # the fifth report of the new generation: back to work on what is left
    _report(loop, next_ms + 1000, 3.0, generation=1)
    assert loop.rate_ppm == pytest.approx(left_ms * GAIN_PPM_PER_MS)
    assert loop.start_error_ms == pytest.approx(3.0)
    assert _offset(loop) == 14


def test_clock_jump_does_not_repeat_the_start_jump() -> None:
    """The start error is learned once: a large error after a clock jump is slewed."""
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    next_ms = _reports(loop, 3.0)
    _reports(loop, 50.0, first_ms=next_ms, generation=1)
    assert loop.start_error_ms == pytest.approx(3.0)
    assert loop.rate_ppm == MAX_RATE_PPM
    assert len(loop.process(bytes(4800 * FRAME_SIZE))) == 4798 * FRAME_SIZE

    # before the start error is known, a clock jump only postpones it
    loop = SyncLoop(RATE, FRAME_SIZE, START_US)
    next_ms = _reports(loop, 50.0, count=2)
    next_ms = _reports(loop, 50.0, first_ms=next_ms, count=2, generation=1)
    assert loop.start_error_ms is None
    assert loop.rate_ppm == 0.0
    _report(loop, next_ms, 50.0, generation=1)
    assert loop.start_error_ms == pytest.approx(50.0)
    assert len(loop.process(bytes(4800 * FRAME_SIZE))) == (4800 - 2400) * FRAME_SIZE


# ---------------------------------------------------------------- replay of recorded runs

# Recorded runs of the measurement server on which the loop was validated, ten to fifteen
# minutes each: (fixture, status reports, reports used, corrections).
#   virtual_drift:   a virtual player on the same host with a DAC that runs 100 ppm fast, 44.1 kHz
#   squeezelite_lan: squeezelite on another machine on the LAN, 48 kHz
#   wiim_lan:        a WiiM on the LAN, started 420 ms early for its output delay, 48 kHz
# The fixtures hold numbers only, with the times rounded to whole microseconds. Where that
# rounding alone would change which replies pass the round trip filter of the clock, the
# round trip is 1 us off (8 of the 8418 replies).
RECORDINGS = [
    ("sync_run_virtual_drift", 4495, 3684, 4002),
    ("sync_run_squeezelite_lan", 3010, 2453, 1596),
    ("sync_run_wiim_lan", 3012, 2491, 631),
]

# The recordings hold 0.1 us and 0.01 ppm, but the module takes whole microseconds. That
# moves the arrival time and the round trip of a reply by up to 0.5 us each, which largely
# averages out over the samples of the clock mapping, and the mapped time of a report by
# up to 0.5 us more: 1.5 us at worst for a single report, less once filtered. A slope over
# ten seconds or more moves by under 0.1 ppm for it, and the rate by the slope plus the
# gain times the filtered error.
ERROR_TOLERANCE_US = 1.5
FILTERED_TOLERANCE_US = 1.0
DRIFT_TOLERANCE_PPM = 0.1
RATE_TOLERANCE_PPM = DRIFT_TOLERANCE_PPM + GAIN_PPM_PER_MS * FILTERED_TOLERANCE_US / 1000


@dataclass(frozen=True)
class _Status:
    """One status report of a recorded run, in the order of arrival."""

    received_us: int
    jiffies: int
    elapsed_ms: int
    # round trip of the timed request this is the reply to, -1 for an unsolicited report
    rtt_us: int
    # whether it is a timer status, the only kind the recording took reports from
    timer: int
    # drift of the recording's clock mapping at this point, in 0.01 ppm
    clock_drift: int
    # corrections the recording sent between the previous status and this one
    corrections: int


@dataclass(frozen=True)
class _Recording:
    """A recorded run: what the player reported and what the recording made of it."""

    sample_rate: int
    # server time at which source frame 0 had to be heard, in nanoseconds
    start_ns: int
    # jiffies the recording worked out for that time, when ``start_before`` statuses were in
    start_jiffies: int
    start_before: int
    status: list[_Status]
    # per used status: error, filtered and predicted error (0.1 us), drift and rate (0.01 ppm)
    reports: dict[int, tuple[int, int, int, int, int]]
    # stream position and the offset from there on
    corrections: list[tuple[int, int]] = field(repr=False)


def _load_recording(path: Path) -> _Recording:
    """Read a recorded run; the time and position columns are stored as differences."""
    data = json.loads(gzip.decompress(path.read_bytes()))
    status, reports, corrections = data["status"], data["reports"], data["corrections"]
    return _Recording(
        sample_rate=data["sample_rate"],
        start_ns=data["start_ns"],
        start_jiffies=data["start_jiffies"],
        start_before=data["start_before"],
        status=[
            _Status(*row)
            for row in zip(
                accumulate(status["received_us"]),
                accumulate(status["jiffies"]),
                accumulate(status["elapsed_ms"]),
                status["rtt_us"],
                status["timer"],
                status["clock_drift"],
                status["corrections"],
                strict=True,
            )
        ],
        reports=dict(
            zip(
                accumulate(reports["status"]),
                zip(
                    reports["error"],
                    reports["filtered"],
                    reports["predicted"],
                    reports["drift"],
                    reports["rate"],
                    strict=True,
                ),
                strict=True,
            )
        ),
        corrections=list(
            zip(accumulate(corrections["position"]), accumulate(corrections["offset"]), strict=True)
        ),
    )


@pytest.mark.parametrize(("name", "status_count", "report_count", "correction_count"), RECORDINGS)
def test_replay_reproduces_the_recorded_loop(
    name: str, status_count: int, report_count: int, correction_count: int
) -> None:
    """
    The module arrives at the numbers of the recorded run for every single report.

    The corrections of the recording are put into the ledger as they were sent, because
    the recording placed them on its own schedule. The recording had no start jump, so
    that is off, and it had no clock jump detection, which must stay quiet by itself.
    """
    recording = _load_recording(FIXTURE_DIR / f"{name}.json.gz")
    assert len(recording.status) == status_count
    assert len(recording.reports) == report_count
    assert len(recording.corrections) == correction_count
    start_us = round(recording.start_ns / 1000)
    # the loop starts on a whole microsecond, which makes its errors this much smaller
    start_shift_us = start_us - recording.start_ns / 1000
    clock = PlayerClock()
    loop = SyncLoop(recording.sample_rate, FRAME_SIZE, start_us, start_jump_ms=math.inf)
    corrections = iter(recording.corrections)
    used = []
    for index, status in enumerate(recording.status):
        if index == recording.start_before:
            assert clock.to_jiffies(start_us) == recording.start_jiffies
            assert abs(_mapped(clock, recording.start_jiffies) - start_us) <= 500
        for _ in range(status.corrections):
            # the test-only way in: as if process() had made this correction
            position, offset = next(corrections)
            loop._offset = offset
            loop._ledger.append((position, offset))
        if status.rtt_us >= 0:
            clock.add_sample(status.jiffies, status.received_us - status.rtt_us, status.received_us)
        clock_drift = clock.drift_ppm - status.clock_drift / 100
        assert abs(clock_drift) <= DRIFT_TOLERANCE_PPM, f"clock drift at status {index}"
        server_us = clock.to_server_us(status.jiffies)
        if not status.timer or server_us is None:
            continue
        error_ms = loop.on_report(server_us, status.elapsed_ms, clock.generation)
        if error_ms is None:
            continue
        used.append(index)
        assert index in recording.reports, f"status {index} is not a report of the recording"
        error, filtered, predicted, drift, rate = recording.reports[index]
        where = f"at status {index}, report {len(used)}"
        assert loop.error_ms is not None
        error_us = error_ms * 1000 + start_shift_us
        filtered_us = loop.error_ms * 1000 + start_shift_us
        assert abs(error_us - error / 10) <= ERROR_TOLERANCE_US, f"error {where}"
        assert abs(filtered_us - filtered / 10) <= FILTERED_TOLERANCE_US, f"filtered {where}"
        assert abs(loop.drift_ppm - drift / 100) <= DRIFT_TOLERANCE_PPM, f"drift {where}"
        assert abs(loop.rate_ppm - rate / 100) <= RATE_TOLERANCE_PPM, f"rate {where}"
        if len(used) >= 5:
            # what the rate is based on, from the report at which the loop starts to act
            assert loop._predicted_ms is not None
            predicted_us = loop._predicted_ms * 1000 + start_shift_us
            assert abs(predicted_us - predicted / 10) <= FILTERED_TOLERANCE_US, f"predicted {where}"
    assert used == list(recording.reports)
    assert next(corrections, None) is None
    assert clock.generation == 0


@pytest.mark.parametrize(("name", "status_count", "report_count", "correction_count"), RECORDINGS)
def test_replay_makes_the_recorded_corrections(
    name: str, status_count: int, report_count: int, correction_count: int
) -> None:
    """
    Left to make its own corrections, the loop makes the ones of the recorded run.

    The stream is sent in blocks of 10 ms, as in the recording, but no further than it is
    played: the recording did not allow for what a player drifts while it plays what it was
    sent ahead. The moment a block was built is not recorded, so a correction can land a
    block earlier or later: at every report the offset is within one frame of the recorded
    one.
    """
    recording = _load_recording(FIXTURE_DIR / f"{name}.json.gz")
    assert len(recording.status) == status_count
    rate = recording.sample_rate
    start_us = round(recording.start_ns / 1000)
    clock = PlayerClock()
    loop = SyncLoop(rate, FRAME_SIZE, start_us, start_jump_ms=math.inf)
    block = bytes(rate // 100 * FRAME_SIZE)
    # a frame of offset is worth this much rate
    frame_ppm = GAIN_PPM_PER_MS * 1000 / rate
    recorded = 0
    made = 0
    reports = 0
    for index, status in enumerate(recording.status):
        ahead: float = -_offset(loop)
        if index >= recording.start_before:
            ahead += (status.received_us - start_us) * rate / 1_000_000
        while loop.frames_sent < int(ahead):
            offset = _offset(loop)
            loop.process(block)
            made += abs(_offset(loop) - offset)
        recorded += status.corrections
        if status.rtt_us >= 0:
            clock.add_sample(status.jiffies, status.received_us - status.rtt_us, status.received_us)
        server_us = clock.to_server_us(status.jiffies)
        if not status.timer or server_us is None:
            continue
        if loop.on_report(server_us, status.elapsed_ms, clock.generation) is None:
            continue
        reports += 1
        assert index in recording.reports, f"status {index} is not a report of the recording"
        recorded_offset = recording.corrections[recorded - 1][1] if recorded else 0
        assert abs(_offset(loop) - recorded_offset) <= 1, f"offset at status {index}"
        recorded_rate = recording.reports[index][4] / 100
        assert abs(loop.rate_ppm - recorded_rate) <= frame_ppm + RATE_TOLERANCE_PPM
    assert reports == report_count
    assert recorded == correction_count
    assert abs(made - correction_count) <= 1


@pytest.mark.parametrize(
    ("name", "start_error_ms", "settled_ms"),
    [
        ("sync_run_virtual_drift", -0.33, -1.60),
        ("sync_run_squeezelite_lan", 31.14, 30.46),
        ("sync_run_wiim_lan", 9.56, 9.56),
    ],
)
def test_replay_measures_the_start_from_the_first_reports(
    name: str, start_error_ms: float, settled_ms: float
) -> None:
    """
    The start error of a recorded run is close to what the player settles on.

    The recordings report four times a second, so the three reports it is taken from
    span half a second, the last one 0.8 s into the stream. What a player settles on
    is the filtered error at the fifth report from a second after the start on.
    """
    recording = _load_recording(FIXTURE_DIR / f"{name}.json.gz")
    start_us = round(recording.start_ns / 1000)
    clock = PlayerClock()
    loop = SyncLoop(recording.sample_rate, FRAME_SIZE, start_us, start_jump_ms=math.inf)
    measured_at_ms = 0
    settled = 0
    for status in recording.status:
        if status.rtt_us >= 0:
            clock.add_sample(status.jiffies, status.received_us - status.rtt_us, status.received_us)
        server_us = clock.to_server_us(status.jiffies)
        if not status.timer or server_us is None:
            continue
        measured = loop.start_error_ms is not None
        settled += loop.on_report(server_us, status.elapsed_ms, clock.generation) is not None
        if not measured and loop.start_error_ms is not None:
            measured_at_ms = status.elapsed_ms
        if settled == 5:
            break
    assert 700 <= measured_at_ms < 850
    assert loop.start_error_ms == pytest.approx(start_error_ms, abs=0.005)
    assert loop.error_ms == pytest.approx(settled_ms, abs=0.005)
    assert abs(start_error_ms - settled_ms) <= 1.3


def test_replay_needs_a_complete_recording(tmp_path: Path) -> None:
    """A truncated recording or one with a missing or short column fails instead of passing."""
    raw = (FIXTURE_DIR / f"{RECORDINGS[0][0]}.json.gz").read_bytes()
    path = tmp_path / "recording.json.gz"
    path.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(EOFError):
        _load_recording(path)

    data = json.loads(gzip.decompress(raw))
    del data["status"]["rtt_us"]
    path.write_bytes(gzip.compress(json.dumps(data).encode()))
    with pytest.raises(KeyError, match="rtt_us"):
        _load_recording(path)

    data = json.loads(gzip.decompress(raw))
    del data["reports"]["rate"][-1]
    path.write_bytes(gzip.compress(json.dumps(data).encode()))
    with pytest.raises(ValueError, match="shorter"):
        _load_recording(path)


# ---------------------------------------------------------------- closed loop simulation

# what the stream is sent ahead: until the player is on its timeline, and from then on
SIM_START_LEAD_S = 1.5
SIM_LEAD_S = 6.0
SIM_LEADS = [SIM_START_LEAD_S, SIM_LEAD_S]
SIM_PREROLL_S = 3.0


@dataclass
class _Simulation:
    """The outcome of a simulated session."""

    clock: PlayerClock
    loop: SyncLoop
    # per poll: seconds since the start, error of what is heard (ms), rate, clock generation
    polls: list[tuple[float, float, float, int]] = field(default_factory=list)
    # every change of the offset: stream position of the block and the change in frames
    changes: list[tuple[int, int]] = field(default_factory=list)

    def errors(self, from_s: float, to_s: float = math.inf) -> list[float]:
        """Return the absolute errors heard in a part of the session, smallest first."""
        return sorted(abs(error) for at_s, error, _, _ in self.polls if from_s <= at_s < to_s)

    def p95(self, from_s: float, to_s: float = math.inf) -> float:
        """Return the 95th percentile of the absolute error heard in a part of the session."""
        errors = self.errors(from_s, to_s)
        return errors[int(len(errors) * 0.95)]


def _network_delay_us(rng: random.Random) -> int:
    """Return a one-way delay: a LAN with an occasional hiccup of tens of milliseconds."""
    delay = 200 + rng.expovariate(1 / 300)
    if rng.random() < 0.03:
        delay += rng.uniform(2000, 30_000)
    return round(delay)


def _simulate(
    seconds: float,
    *,
    seed: int,
    clock_ppm: float = 0.0,
    dac_ppm: float = 0.0,
    output_delay_ms: float = 0.0,
    clock_step: tuple[float, float] | None = None,
    wrap_after_s: float | None = None,
    lead_s: float = SIM_LEAD_S,
) -> _Simulation:
    """
    Run a session against a simulated player and return what was heard.

    The player is polled four times a second over a jittery network, starts on a timed
    start and plays what ``process`` returned about 1.5 s earlier, and the lead earlier
    once the loop has it on its timeline.

    :param seconds: Length of the session.
    :param seed: Seed of the network jitter.
    :param clock_ppm: How much fast the player's clock runs.
    :param dac_ppm: How much fast the player's DAC runs.
    :param output_delay_ms: Delay between the timed start and the first frame heard.
    :param clock_step: Seconds into the session at which the player's clock steps, and by how
        many milliseconds.
    :param wrap_after_s: Seconds into the session at which the player's jiffies wrap.
    :param lead_s: Seconds the stream is sent ahead once the player is on its timeline.
    """
    rng = random.Random(seed)  # noqa: S311
    begin_us = 1_000_000_000
    start_us = begin_us + round((SIM_PREROLL_S + SIM_START_LEAD_S) * 1_000_000)
    clock_at_zero_ms = 7_000_000.0
    if wrap_after_s is not None:
        clock_at_zero_ms = JIFFIES_WRAP - (start_us / 1000 + wrap_after_s * 1000)
    player = _Player(clock_ppm=clock_ppm, dac_ppm=dac_ppm, clock_at_zero_ms=clock_at_zero_ms)
    clock = PlayerClock()
    loop = SyncLoop(RATE, FRAME_SIZE, start_us)
    result = _Simulation(clock, loop)
    block = bytes(RATE // 100 * FRAME_SIZE)
    # the offset per stream position as it was really sent, to work out what is heard
    sent_positions, sent_offsets = [0], [0]
    started = False
    for tick in range(round((SIM_PREROLL_S + SIM_START_LEAD_S + seconds) * 100)):
        now_us = begin_us + tick * 10_000
        since_start_s = (now_us - start_us) / 1_000_000
        if not started and since_start_s >= -SIM_START_LEAD_S:
            started = True
            start_jiffies = clock.to_jiffies(start_us)
            assert start_jiffies is not None
            player.start_at(start_jiffies, now_us, output_delay_ms)
        if clock_step is not None and since_start_s >= clock_step[0]:
            player.clock_step_ms = clock_step[1]
        if started:
            while loop.frames_sent < player.position(now_us) + RATE * (
                lead_s if loop.on_timeline else SIM_START_LEAD_S
            ):
                sent, offset = loop.frames_sent, _offset(loop)
                loop.process(block)
                if _offset(loop) == offset:
                    continue
                sent_positions.append(sent)
                sent_offsets.append(_offset(loop))
                if result.changes and result.changes[-1][0] == sent:
                    # a jump that takes more than one block
                    offset -= result.changes.pop()[1]
                result.changes.append((sent, _offset(loop) - offset))
        if tick % 25:
            continue
        # a timed request, and every second one the player also reports by itself first
        sent_us = now_us + rng.randrange(10_000)
        read_us = sent_us + _network_delay_us(rng)
        received_us = read_us + _network_delay_us(rng)
        jiffies, elapsed_ms = player.jiffies(read_us), player.elapsed_ms(read_us)
        if tick % 50 == 0 and (server_us := clock.to_server_us(jiffies)) is not None:
            loop.on_report(server_us, elapsed_ms, clock.generation)
        clock.add_sample(jiffies, sent_us, received_us)
        if (server_us := clock.to_server_us(jiffies)) is not None:
            loop.on_report(server_us, elapsed_ms, clock.generation)
        position = player.position(now_us)
        if position > 0:
            offset = sent_offsets[bisect.bisect_right(sent_positions, int(position)) - 1]
            heard_ms = (now_us - start_us) / 1000 - (position + offset) * 1000 / RATE
            result.polls.append((since_start_s, heard_ms, loop.rate_ppm, clock.generation))
    return result


def _assert_rate_limited(simulation: _Simulation) -> None:
    """Check that the loop never asked for, nor made, more corrections than the maximum."""
    assert max(abs(rate) for _, _, rate, _ in simulation.polls) <= MAX_RATE_PPM
    singles = [position for position, change in simulation.changes if abs(change) == 1]
    limit = math.ceil(MAX_RATE_PPM * 1e-6 * RATE)
    # any run of one more than the limit takes over a second of stream
    for index in range(len(singles) - limit):
        assert singles[index + limit] - singles[index] >= RATE - RATE // 100


@pytest.mark.parametrize("lead_s", SIM_LEADS)
@pytest.mark.parametrize(
    ("dac_ppm", "clock_ppm", "wrap_after_s"),
    [(100.0, -30.0, None), (-100.0, 20.0, 60.0)],
)
def test_simulation_follows_a_drifting_player(
    dac_ppm: float, clock_ppm: float, wrap_after_s: float | None, lead_s: float
) -> None:
    """A player whose DAC runs 100 ppm off is kept within half a millisecond, whatever its lead."""
    simulation = _simulate(
        180,
        seed=1,
        dac_ppm=dac_ppm,
        clock_ppm=clock_ppm,
        wrap_after_s=wrap_after_s,
        lead_s=lead_s,
    )
    loop = simulation.loop
    # once the filter window of a minute is full
    assert simulation.p95(60) <= 0.5
    assert max(simulation.errors(60)) <= 0.75
    # a fast DAC plays early: negative drift, and frames are repeated to hold it back
    assert loop.drift_ppm == pytest.approx(-dac_ppm, abs=5.0)
    assert loop.rate_ppm == pytest.approx(-dac_ppm, abs=25.0)
    assert simulation.clock.drift_ppm == pytest.approx(-clock_ppm, abs=5.0)
    assert simulation.clock.generation == 0
    # only single frames: the drift of the session plus the little the start was off
    assert {abs(change) for _, change in simulation.changes} == {1}
    drifted = -dac_ppm * 1e-6 * 180 * RATE
    assert sum(change for _, change in simulation.changes) == pytest.approx(drifted, abs=120)
    assert loop.start_error_ms is not None
    assert abs(loop.start_error_ms) < 2.0
    _assert_rate_limited(simulation)


@pytest.mark.parametrize("lead_s", SIM_LEADS)
@pytest.mark.parametrize("output_delay_ms", [420.0, -420.0])
def test_simulation_removes_a_large_start_error_in_one_jump(
    output_delay_ms: float, lead_s: float
) -> None:
    """A start that is 420 ms off is measured and corrected in a single jump."""
    simulation = _simulate(60, seed=2, dac_ppm=20.0, output_delay_ms=output_delay_ms, lead_s=lead_s)
    assert simulation.loop.start_error_ms == pytest.approx(output_delay_ms, abs=1.5)
    jumps = [change for _, change in simulation.changes if abs(change) > 1]
    assert jumps == [pytest.approx(output_delay_ms * RATE / 1000, abs=1.5 * RATE / 1000)]
    # off by the full delay until the jump is played, which went out before the lead
    # grew, and on time from then on
    assert simulation.loop.on_timeline
    assert min(simulation.errors(0.5, 2.0)) > 418.0
    assert max(simulation.errors(3.0)) <= 1.0
    assert simulation.p95(30) <= 0.5
    _assert_rate_limited(simulation)


@pytest.mark.parametrize("lead_s", SIM_LEADS)
@pytest.mark.parametrize("output_delay_ms", [5.0, -5.0])
def test_simulation_slews_a_small_start_error_away(output_delay_ms: float, lead_s: float) -> None:
    """A start that is 5 ms off is removed by the rate alone, without a jump."""
    simulation = _simulate(
        60, seed=3, dac_ppm=-20.0, output_delay_ms=output_delay_ms, lead_s=lead_s
    )
    assert simulation.loop.start_error_ms == pytest.approx(output_delay_ms, abs=1.5)
    assert {abs(change) for _, change in simulation.changes} == {1}
    assert min(simulation.errors(0.5, 3.0)) > 3.5
    # the rate starts near its maximum and comes down with the error, a tenth per second
    rates = [rate for at_s, _, rate, _ in simulation.polls if 2.0 <= at_s < 6.0]
    assert max(abs(rate) for rate in rates) >= 400.0
    assert {math.copysign(1, rate) for rate in rates if rate} == {math.copysign(1, output_delay_ms)}
    assert 1.0 < max(simulation.errors(10, 11)) < 3.5
    assert max(simulation.errors(30, 31)) < 1.0
    assert max(simulation.errors(40)) <= 0.5
    _assert_rate_limited(simulation)


@pytest.mark.parametrize("lead_s", SIM_LEADS)
@pytest.mark.parametrize("step_ms", [50.0, -50.0])
def test_simulation_rides_out_a_clock_step(step_ms: float, lead_s: float) -> None:
    """A step of the player's clock pauses the corrections, and little of it is heard."""
    simulation = _simulate(
        240, seed=4, dac_ppm=60.0, clock_ppm=-25.0, clock_step=(120.0, step_ms), lead_s=lead_s
    )
    assert simulation.clock.generation == 1
    assert simulation.p95(60, 120) <= 0.5
    # noticed within three buckets of samples, and the corrections stop for four reports
    noticed = [(at_s, rate) for at_s, _, rate, generation in simulation.polls if generation == 1]
    assert 120.0 < noticed[0][0] <= 127.0
    assert [rate for _, rate in noticed[:4]] == [0.0, 0.0, 0.0, 0.0]
    # of the 50 ms less than one reaches the listener, and the loop picks up again
    assert max(simulation.errors(120)) <= 1.0
    assert simulation.p95(180) <= 0.5
    assert simulation.loop.drift_ppm == pytest.approx(-60.0, abs=5.0)
    assert simulation.loop.rate_ppm == pytest.approx(-60.0, abs=25.0)
    _assert_rate_limited(simulation)


@pytest.mark.parametrize("output_delay_ms", [0.0, 8.0, -8.0, 15.0, -15.0])
def test_simulation_pulls_a_start_error_in_as_fast_with_a_long_lead(output_delay_ms: float) -> None:
    """What is left of a start is corrected a short lead ahead: the long one comes when it is gone."""
    short, long = (
        _simulate(60, seed=5, dac_ppm=30.0, output_delay_ms=output_delay_ms, lead_s=lead_s)
        for lead_s in SIM_LEADS
    )
    # 8 ms is slewed away in half a minute and 15 ms is jumped: heard the same either way
    differences = [
        abs(heard - heard_short)
        for (_, heard, _, _), (_, heard_short, _, _) in zip(long.polls, short.polls, strict=True)
    ]
    assert max(differences) <= 0.3
    assert long.loop.on_timeline
    assert long.loop.frames_sent - short.loop.frames_sent == pytest.approx(
        (SIM_LEAD_S - SIM_START_LEAD_S) * RATE, abs=RATE // 100
    )
    assert max(long.errors(45)) <= 0.75
