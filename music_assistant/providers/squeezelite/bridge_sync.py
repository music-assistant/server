"""
Clock mapping and sync loop for a slimproto player that has to follow a server timeline.

A slimproto player plays one endless PCM stream. On request it reports its own clock
(jiffies, whole milliseconds) and how much of the stream it has played (elapsed, whole
milliseconds). ``PlayerClock`` maps that clock onto the server clock, and ``SyncLoop``
keeps the audio on the timeline by removing or repeating single frames in the PCM that
is sent to the player.

Server times are microseconds on one monotonic clock. An error is the server time at
which the player plays a frame minus the time that frame should play: positive when the
player is late, which is corrected by dropping frames (a positive rate).

This module does no I/O and knows nothing about the rest of the server.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Collection
from math import sqrt

# defaults of the SyncLoop tunables
GAIN_PPM_PER_MS = 100.0
MAX_RATE_PPM = 500.0
ERROR_WINDOW_S = 60.0
MIN_SPAN_S = 10.0
SETTLE_S = 1.0
START_JUMP_MS = 10.0

# an error beyond this is not corrected in a jump: the session has to start over
MAX_START_JUMP_MS = 2000.0

_JIFFIES_WRAP = 1 << 32

# clock samples are filtered per bucket: only the ones with (nearly) the lowest round trip count
_BUCKET_MS = 2000
_RTT_MARGIN_US = 300
# the very first samples are used at once, so a timed start is possible within a second
_FIRST_SAMPLES = 3
# the mapping is a line through the samples of this window, level until they span enough time
_CLOCK_WINDOW_MS = 120_000
_CLOCK_MIN_SPAN_MS = 10_000
_CLOCK_MIN_SLOPE_POINTS = 5
# a clock jump: this many samples in a row that are this far off the mapping. The whole
# millisecond jiffies and a clean round trip keep a normal sample within about 1 ms.
_JUMP_THRESHOLD_US = 5000
_JUMP_SAMPLES = 3

# reports needed before the loop acts on the filtered error
_MIN_REPORTS = 5
# a report with the elapsed time of the previous one and this close to it is the same reading
# of the player, sent twice: only a refreshed clock mapping moved its time a little
_SAME_REPORT_US = 500
# from this many reports on, the filter leaves out the ones that are this many standard
# deviations (and at least this many ms) off its line, as long as enough of them remain
_OUTLIER_MIN_POINTS = 20
_OUTLIER_SIGMAS = 3
_OUTLIER_MIN_MS = 1.5
_OUTLIER_MIN_KEPT = 10


class PlayerClock:
    """
    Maps the jiffies clock of a player onto the server clock.

    Lives as long as the player's connection, across playback sessions, and learns from
    the status replies to timed requests.
    """

    def __init__(self) -> None:
        """Initialize a clock that knows nothing yet."""
        # jiffies of the latest sample. Stored jiffies are unwrapped: they count on past 32 bits.
        self._last_jiffies: int | None = None
        # samples waiting for the round trip filter: (jiffies, offset in us, round trip in us),
        # where the offset is the server time minus the jiffies
        self._bucket: list[tuple[int, float, int]] = []
        # samples behind the mapping: (jiffies, offset in us)
        self._points: deque[tuple[int, float]] = deque()
        # the latest samples in a row that are far off the mapping
        self._suspects: list[tuple[int, float]] = []
        self._jiffies_mean = 0.0
        self._offset_mean = 0.0
        self._slope = 0.0
        self._generation = 0

    @property
    def drift_ppm(self) -> float:
        """Return how much slower than the server clock the player's clock runs, in ppm."""
        return self._slope * 1000

    @property
    def generation(self) -> int:
        """Return how often the mapping had to start over because the player's clock jumped."""
        return self._generation

    def add_sample(self, jiffies: int, sent_us: int, received_us: int) -> None:
        """
        Add the status reply to a timed request.

        :param jiffies: The raw 32-bit jiffies of the reply.
        :param sent_us: Server time at which the request was sent.
        :param received_us: Server time at which the reply arrived.
        """
        ticks = self._unwrap(jiffies)
        self._last_jiffies = ticks
        offset = (sent_us + received_us) / 2 - ticks * 1000
        self._bucket.append((ticks, offset, received_us - sent_us))
        first_samples = not self._points and len(self._bucket) >= _FIRST_SAMPLES
        # a clock that went backwards closes the bucket too
        if first_samples or not 0 <= ticks - self._bucket[0][0] < _BUCKET_MS:
            self._flush()

    def to_server_us(self, jiffies: int) -> int | None:
        """
        Return the server time of a jiffies reading of the player.

        The player's clock shows a value for a whole millisecond: the time returned is
        the middle of it.

        :param jiffies: The raw 32-bit jiffies.
        :return: The server time, or None while the mapping is unknown.
        """
        if not self._points:
            return None
        ticks = self._unwrap(jiffies)
        return round(ticks * 1000 + self._offset_at(ticks))

    def to_jiffies(self, server_us: int) -> int | None:
        """
        Return the jiffies reading of the player that is closest to a server time.

        :param server_us: The server time.
        :return: The raw 32-bit jiffies, or None while the mapping is unknown.
        """
        if not self._points:
            return None
        centre_us = self._jiffies_mean * 1000 + self._offset_mean
        ticks = self._jiffies_mean + (server_us - centre_us) / (1000 + self._slope)
        return round(ticks) % _JIFFIES_WRAP

    def _unwrap(self, jiffies: int) -> int:
        """Return the unwrapped value of raw jiffies: the one closest to the latest sample."""
        if self._last_jiffies is None:
            return jiffies
        half = _JIFFIES_WRAP // 2
        return self._last_jiffies + (jiffies - self._last_jiffies + half) % _JIFFIES_WRAP - half

    def _offset_at(self, ticks: int) -> float:
        """Return the mapped offset (server time minus jiffies, in us) at unwrapped jiffies."""
        return self._offset_mean + self._slope * (ticks - self._jiffies_mean)

    def _flush(self) -> None:
        """Move the samples of the bucket with the lowest round trip into the mapping."""
        best_rtt = min(sample[2] for sample in self._bucket)
        mapped = bool(self._points)
        for ticks, offset, rtt in self._bucket:
            if rtt > best_rtt + _RTT_MARGIN_US:
                continue
            if mapped and abs(offset - self._offset_at(ticks)) > _JUMP_THRESHOLD_US:
                self._suspects.append((ticks, offset))
                if len(self._suspects) < _JUMP_SAMPLES:
                    continue
                # the player's clock jumped: start over from the samples taken since.
                # Refit right away, the rest of the bucket is judged against the new mapping.
                self._points.clear()
                self._points.extend(self._suspects)
                self._generation += 1
                self._refit()
            else:
                self._points.append((ticks, offset))
            self._suspects.clear()
        self._bucket.clear()
        self._refit()

    def _refit(self) -> None:
        """Fit the mapping through the samples of the sliding window."""
        points = self._points
        newest = points[-1][0]
        while points[0][0] < newest - _CLOCK_WINDOW_MS:
            points.popleft()
        with_slope = (
            len(points) >= _CLOCK_MIN_SLOPE_POINTS and newest - points[0][0] > _CLOCK_MIN_SPAN_MS
        )
        self._jiffies_mean, self._offset_mean, self._slope = _fit(points, with_slope)


class SyncLoop:
    """
    Keeps one playback session of a player on the server timeline.

    Source frame ``n`` of the session has to be heard at ``start_us + n / sample_rate``.
    The status reports of the player tell how far off it is, and ``process`` removes or
    repeats frames in the PCM on its way to the player to make up for it.
    """

    def __init__(
        self,
        sample_rate: int,
        frame_size: int,
        start_us: int,
        *,
        gain_ppm_per_ms: float = GAIN_PPM_PER_MS,
        max_rate_ppm: float = MAX_RATE_PPM,
        window_s: float = ERROR_WINDOW_S,
        min_span_s: float = MIN_SPAN_S,
        settle_s: float = SETTLE_S,
        start_jump_ms: float = START_JUMP_MS,
    ) -> None:
        """
        Initialize the loop for a new session.

        :param sample_rate: Sample rate of the PCM in Hz.
        :param frame_size: Size of one PCM frame (a sample of every channel) in bytes.
        :param start_us: Server time at which source frame 0 has to be heard.
        :param gain_ppm_per_ms: Correction rate per millisecond of error.
        :param max_rate_ppm: Highest correction rate.
        :param window_s: Length of the window over which the error is filtered.
        :param min_span_s: Time the reports have to span before their drift is trusted.
        :param settle_s: Time after the start during which reports are ignored.
        :param start_jump_ms: Error above which it is corrected in one jump, at the start
            and after a jump of the player's clock.
        """
        self._sample_rate = sample_rate
        self._frame_size = frame_size
        self._start_us = start_us
        self._gain_ppm_per_ms = gain_ppm_per_ms
        self._max_rate_ppm = max_rate_ppm
        self._window_s = window_s
        self._min_span_s = min_span_s
        self._settled_us = start_us + round(settle_s * 1_000_000)
        self._start_jump_ms = start_jump_ms
        # stream frame p carries source frame p + offset. The ledger holds the offset per
        # stream position, (position, offset from there on), for what is not played yet.
        self._ledger: deque[tuple[int, int]] = deque([(0, 0)])
        self._offset = 0
        self._frames_sent = 0
        # frames of a jump still to drop (positive) or to insert (negative)
        self._pending_jump = 0
        # a large error is corrected in one jump once per run of reports: the one after
        # the start and the one after each jump of the player's clock
        self._jump_armed = True
        self._residual = 0.0
        # reports in the filter window: (seconds since the start, error without corrections)
        self._points: deque[tuple[float, float]] = deque()
        self._last_report: tuple[int, int] | None = None
        self._clock_generation = 0
        self._rate_ppm = 0.0
        self._drift_ppm = 0.0
        self._error_ms: float | None = None
        self._predicted_ms: float | None = None
        self._start_error_ms: float | None = None

    @property
    def rate_ppm(self) -> float:
        """Return the correction rate in ppm: positive drops frames, negative repeats them."""
        return self._rate_ppm

    @property
    def drift_ppm(self) -> float:
        """Return how much slower than the timeline the player plays, in ppm (0 until known)."""
        return self._drift_ppm

    @property
    def error_ms(self) -> float | None:
        """Return the filtered error of what the player plays now, or None while unknown."""
        return self._error_ms

    @property
    def start_error_ms(self) -> float | None:
        """
        Return the error the session started with, or None until it is measured.

        A start that is off by more than ``MAX_START_JUMP_MS`` is not corrected: such a
        session has to be started over.
        """
        return self._start_error_ms

    @property
    def frames_sent(self) -> int:
        """Return the number of stream frames returned by ``process`` so far."""
        return self._frames_sent

    @property
    def frames_consumed(self) -> int:
        """Return the number of source frames taken by ``process`` so far."""
        return self._frames_sent + self._offset

    def on_report(self, server_us: int, elapsed_ms: int, clock_generation: int = 0) -> float | None:
        """
        Take in a status report of the player.

        A report is ignored while the player settles after the start, when nothing was
        played yet and when it repeats the previous report.

        :param server_us: Server time of the report, from ``PlayerClock.to_server_us``.
        :param elapsed_ms: Elapsed milliseconds of the stream as reported by the player.
        :param clock_generation: ``PlayerClock.generation`` at the time of the report.
        :return: The error of this report in ms, or None when the report was ignored.
        """
        if clock_generation != self._clock_generation:
            # report times from before a clock jump do not compare with the ones after it
            self._clock_generation = clock_generation
            self._jump_armed = True
            self._points.clear()
            self._rate_ppm = self._drift_ppm = 0.0
            self._error_ms = self._predicted_ms = None
        previous, self._last_report = self._last_report, (server_us, elapsed_ms)
        if elapsed_ms <= 0 or server_us < self._settled_us:
            return None
        if (
            previous is not None
            and elapsed_ms == previous[1]
            and abs(server_us - previous[0]) < _SAME_REPORT_US
        ):
            return None

        # elapsed is truncated to whole milliseconds: half a millisecond removes the bias
        elapsed = elapsed_ms + 0.5
        position = int(elapsed * self._sample_rate / 1000)
        ledger = self._ledger
        while len(ledger) > 1 and ledger[1][0] <= position:
            ledger.popleft()
        played_ms = self._to_ms(ledger[0][1])

        # the error with every correction taken out again is the start error plus the
        # drift: a straight line whatever the loop did, which is what the filter fits
        raw_ms = (server_us - self._start_us) / 1000 - elapsed
        seconds = (server_us - self._start_us) / 1_000_000
        points = self._points
        points.append((seconds, raw_ms))
        while points[0][0] < seconds - self._window_s:
            points.popleft()
        raw_now, slope = self._filter(seconds)
        self._error_ms = raw_now - played_ms
        self._drift_ppm = slope * 1000

        if len(points) >= _MIN_REPORTS:
            if self._start_error_ms is None:
                # nothing was corrected yet, so this is the error of the start itself
                self._start_error_ms = raw_now
            # what is left of the error once everything that was sent has been played
            predicted_ms = raw_now - self._to_ms(self._offset + self._pending_jump)
            if self._jump_armed:
                self._jump_armed = False
                if self._start_jump_ms < abs(predicted_ms) <= MAX_START_JUMP_MS:
                    self._pending_jump += round(predicted_ms * self._sample_rate / 1000)
                    predicted_ms = raw_now - self._to_ms(self._offset + self._pending_jump)
            self._predicted_ms = predicted_ms
            rate_ppm = self._drift_ppm + self._gain_ppm_per_ms * self._predicted_ms
            self._rate_ppm = max(-self._max_rate_ppm, min(self._max_rate_ppm, rate_ppm))
        return raw_ms - played_ms

    def process(self, pcm: bytes) -> bytes:
        """
        Turn the next block of source PCM into the PCM to send to the player.

        The result is the block itself as long as nothing has to be corrected. A large
        error at the start, or after the player's clock jumped, is corrected in one jump:
        the result then starts with silence, or audio is skipped, which can leave nothing
        of a block.

        :param pcm: The next source PCM, a whole number of frames.
        :return: The same audio with frames dropped or repeated where the loop requires it.
        """
        jump = self._pending_jump
        if not jump:
            return self._correct(pcm)
        if jump > 0:
            # the player is late: skip source frames, over several blocks when needed
            jump = min(jump, len(pcm) // self._frame_size)
            silence = b""
            pcm = pcm[jump * self._frame_size :]
        else:
            # the player is early: let it play silence first
            silence = bytes(-jump * self._frame_size)
        self._pending_jump -= jump
        self._offset += jump
        self._record(self._frames_sent)
        self._frames_sent += len(silence) // self._frame_size
        return silence + self._correct(pcm)

    def _correct(self, pcm: bytes) -> bytes:
        """Drop or repeat the single frames that are due in a block at the current rate."""
        size = self._frame_size
        frames = len(pcm) // size
        self._residual += self._rate_ppm * 1e-6 * frames
        count = int(self._residual)
        if not count:
            self._frames_sent += frames
            return pcm
        self._residual -= count
        view = memoryview(pcm)
        parts: list[memoryview] = []
        start = 0
        # the corrections go in the middle of equal parts of the block
        if count > 0:
            for done in range(count):
                index = (2 * done + 1) * frames // (2 * count)
                parts.append(view[start * size : index * size])
                start = index + 1
                self._offset += 1
                self._record(self._frames_sent + index - done)
        else:
            for done in range(-count):
                index = (2 * done + 1) * frames // (-2 * count)
                parts.append(view[start * size : (index + 1) * size])
                start = index
                self._offset -= 1
                self._record(self._frames_sent + index + 1 + done)
        parts.append(view[start * size :])
        self._frames_sent += frames - count
        return b"".join(parts)

    def _record(self, position: int) -> None:
        """Note in the ledger that the stream carries the current offset from a position on."""
        if self._ledger[-1][0] == position:
            self._ledger[-1] = (position, self._offset)
        else:
            self._ledger.append((position, self._offset))

    def _to_ms(self, frames: int) -> float:
        """Return the duration of a number of frames in milliseconds."""
        return frames * 1000 / self._sample_rate

    def _filter(self, seconds: float) -> tuple[float, float]:
        """
        Fit a line through the reports in the window.

        :param seconds: The time to evaluate the line at, in seconds since the start.
        :return: The error without corrections at that time (ms) and the slope (ms/s).
        """
        points = self._points
        with_slope = points[-1][0] - points[0][0] >= self._min_span_s
        x_mean, y_mean, slope = _fit(points, with_slope)
        count = len(points)
        if count >= _OUTLIER_MIN_POINTS:
            # one refit without the outliers: a scheduling hiccup on either side moves a
            # report by several milliseconds
            residuals = [y - y_mean - slope * (x - x_mean) for x, y in points]
            deviation = sqrt(sum(r * r for r in residuals) / count)
            limit = max(_OUTLIER_MIN_MS, _OUTLIER_SIGMAS * deviation)
            kept = [point for point, r in zip(points, residuals, strict=True) if abs(r) <= limit]
            if _OUTLIER_MIN_KEPT <= len(kept) < count:
                x_mean, y_mean, slope = _fit(kept, with_slope)
        return y_mean + slope * (seconds - x_mean), slope


def _fit(points: Collection[tuple[float, float]], with_slope: bool) -> tuple[float, float, float]:
    """
    Fit a line through points by least squares.

    :param points: The (x, y) points, at least one.
    :param with_slope: Fit the slope too; without it the line is level at the mean.
    :return: The mean of x, the mean of y and the slope.
    """
    count = len(points)
    x_mean = sum(x for x, _ in points) / count
    y_mean = sum(y for _, y in points) / count
    slope = 0.0
    if with_slope:
        sxx = sxy = 0.0
        for x, y in points:
            dx = x - x_mean
            sxx += dx * dx
            sxy += dx * (y - y_mean)
        if sxx > 0:
            slope = sxy / sxx
    return x_mean, y_mean, slope
