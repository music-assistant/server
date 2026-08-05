"""Tests for the 1800-bin vocal-activity contract and vocal-collision math."""

from __future__ import annotations

import math

import pytest

from music_assistant.controllers.streams.smart_fades.vocal import (
    VocalMask,
    VocalTimeline,
    build_vocal_windows,
    collision_metrics,
    merge_windows,
    parse_vocal_probabilities,
)
from music_assistant.models.audio_analysis import AudioAnalysisData


def _analysis(vocal_activity: object, duration: float | None = 240.0) -> AudioAnalysisData:
    return AudioAnalysisData(duration=duration, extra_data={"vocal_activity": vocal_activity})


# Used only by TestBuildVocalWindowsSustainedEvidenceGate below.
FRAME = 0.1  # 10 bins per second


def _timeline(spec: list[tuple[float, int]]) -> list[float]:
    """Flat probability segments: [(prob, n_bins), ...]."""
    probs: list[float] = []
    for prob, bins in spec:
        probs.extend([prob] * bins)
    return probs


class TestParseVocalProbabilities:
    """The 1800-bin contract is validated in full; any defect disables vocal logic entirely."""

    @pytest.mark.parametrize("duration", [90.0, 180.0, 240.0, 360.0])
    def test_valid_contract_infers_timing_from_duration(self, duration: float) -> None:
        """A valid list round-trips with exact per-bin timing for any canonical duration."""
        probabilities = [0.1] * 1800
        analysis = _analysis(probabilities, duration=duration)
        assert parse_vocal_probabilities(analysis) == VocalTimeline(
            probabilities=probabilities,
            frame_duration=duration / 1800,
        )

    def test_frame_duration_is_derived_exactly_from_analysis_duration(self) -> None:
        """Stored duration divided by 1800 is the timeline's only timing source."""
        timeline = parse_vocal_probabilities(_analysis([0.1] * 1800, duration=241.7))
        assert timeline is not None
        assert timeline.frame_duration == 241.7 / 1800

    def test_no_extra_data_returns_none(self) -> None:
        """A row with no extra_data at all has no vocal timeline."""
        assert parse_vocal_probabilities(AudioAnalysisData(duration=240.0)) is None

    def test_missing_vocal_activity_key_returns_none(self) -> None:
        """extra_data present but without a vocal_activity entry."""
        analysis = AudioAnalysisData(duration=240.0, extra_data={"band_rms": {}})
        assert parse_vocal_probabilities(analysis) is None

    @pytest.mark.parametrize(
        "vocal_activity",
        [
            "nope",
            0.5,
            tuple([0.1] * 1800),
            {"model": "firered_aed", "frame_duration": 0.1, "probabilities": [0.1] * 1800},
        ],
    )
    def test_non_list_or_old_contract_returns_none(self, vocal_activity: object) -> None:
        """Only the direct list contract is accepted; old wrapped rows are stale."""
        assert parse_vocal_probabilities(_analysis(vocal_activity)) is None

    @pytest.mark.parametrize("length", [0, 1, 1799, 1801, 2400])
    def test_wrong_probability_count_returns_none(self, length: int) -> None:
        """The canonical timeline must contain exactly 1800 bins."""
        assert parse_vocal_probabilities(_analysis([0.1] * length)) is None

    @pytest.mark.parametrize(
        "bad_value",
        [-0.1, 1.1, float("nan"), float("inf"), "0.5", True],
    )
    def test_out_of_range_or_non_finite_value_returns_none(self, bad_value: object) -> None:
        """Any bin outside 0..1, non-finite, non-numeric, or bool invalidates the row."""
        probabilities: list[object] = [0.1] * 1800
        probabilities[900] = bad_value
        assert parse_vocal_probabilities(_analysis(probabilities)) is None

    def test_huge_integer_returns_none_instead_of_raising(self) -> None:
        """An arbitrary-size malformed integer is rejected before float conversion."""
        probabilities: list[object] = [0.1] * 1800
        probabilities[900] = 10**10000
        assert parse_vocal_probabilities(_analysis(probabilities)) is None

    def test_boundary_values_zero_and_one_are_accepted(self) -> None:
        """0.0 and 1.0 are valid probabilities, not out-of-range."""
        probabilities = [0.1] * 1800
        probabilities[0] = 0.0
        probabilities[1] = 1.0
        assert parse_vocal_probabilities(_analysis(probabilities)) is not None

    @pytest.mark.parametrize(
        "duration",
        [None, 0.0, -1.0, float("nan"), float("inf"), True],
    )
    def test_missing_or_invalid_duration_returns_none(self, duration: float | None) -> None:
        """Bin timing requires a finite positive analysis duration."""
        assert parse_vocal_probabilities(_analysis([0.1] * 1800, duration=duration)) is None


class TestBuildVocalWindows:
    """Hysteresis run detection, padding and gap bridging over a probability timeline."""

    def test_single_run_is_padded_left_and_right(self) -> None:
        """A run opens at OPEN and closes below CLOSE; the window gets padded both sides."""
        probabilities = [0.0] * 100
        for i in range(40, 60):
            probabilities[i] = 0.9
        mask = build_vocal_windows(probabilities, 0.1, 0.0, 10.0)
        assert len(mask.windows) == 1
        left, right = mask.windows[0]
        assert left == pytest.approx(4.0 - 0.25)
        assert right == pytest.approx(6.0 + 0.75)

    def test_probability_never_reaching_open_threshold_yields_no_window(self) -> None:
        """A run must reach the OPEN threshold, not just exceed CLOSE."""
        probabilities = [0.4] * 100
        mask = build_vocal_windows(probabilities, 0.1, 0.0, 10.0)
        assert mask.windows == []

    def test_sub_min_run_blip_yields_no_window(self) -> None:
        """A one-off detector blip (crowd shout, vocal-formant one-shot) is not a phrase."""
        probabilities = [0.0] * 100
        probabilities[50] = 0.9
        probabilities[51] = 0.9  # 0.2s: still under the 0.3s run floor
        mask = build_vocal_windows(probabilities, 0.1, 0.0, 10.0)
        assert mask.windows == []

    @pytest.mark.parametrize("start", [0, 1, 17, 50, 89])
    def test_min_run_length_is_stable_at_every_timeline_position(self, start: int) -> None:
        """Identical runs exactly at the floor survive regardless of their absolute index."""
        probabilities = [0.0] * 100
        for i in range(start, start + 3):
            probabilities[i] = 0.9
        mask = build_vocal_windows(probabilities, 0.1, 0.0, 10.0)
        assert len(mask.windows) == 1

    @pytest.mark.parametrize(
        ("frame_duration", "short_frames", "minimum_frames"),
        [
            (90.0 / 1800, 5, 6),
            (240.0 / 1800, 2, 3),
            (360.0 / 1800, 1, 2),
        ],
    )
    def test_min_run_remains_seconds_based_for_variable_bin_durations(
        self,
        frame_duration: float,
        short_frames: int,
        minimum_frames: int,
    ) -> None:
        """The 0.3-second floor maps to the correct whole-bin count at each duration."""
        short = [0.0] * 100
        short[20 : 20 + short_frames] = [0.9] * short_frames
        sufficient = [0.0] * 100
        sufficient[20 : 20 + minimum_frames] = [0.9] * minimum_frames

        end_s = len(short) * frame_duration
        assert build_vocal_windows(short, frame_duration, 0.0, end_s).windows == []
        assert len(build_vocal_windows(sufficient, frame_duration, 0.0, end_s).windows) == 1

    def test_dip_below_open_but_above_close_does_not_split_the_run(self) -> None:
        """Hysteresis: a dip that stays above CLOSE keeps the run open (no re-triggering)."""
        probabilities = [0.0] * 100
        for i in range(10, 20):
            probabilities[i] = 0.9
        for i in range(20, 25):
            probabilities[i] = 0.4  # between CLOSE (0.3) and OPEN (0.5): stays open
        for i in range(25, 35):
            probabilities[i] = 0.9
        mask = build_vocal_windows(probabilities, 0.1, 0.0, 10.0)
        assert len(mask.windows) == 1

    def test_close_gap_bridges_into_one_window(self) -> None:
        """Two runs closer than the gap bridge into a single window."""
        probabilities = [0.0] * 200
        for i in range(10, 20):
            probabilities[i] = 0.9
        for i in range(23, 33):  # 0.3s gap after padding, under the 0.5s minimum gap
            probabilities[i] = 0.9
        mask = build_vocal_windows(probabilities, 0.1, 0.0, 20.0, beat_duration=0.5)
        assert len(mask.windows) == 1

    def test_far_apart_runs_stay_separate(self) -> None:
        """Two runs well beyond the max gap remain distinct windows."""
        probabilities = [0.0] * 200
        for i in range(10, 20):
            probabilities[i] = 0.9
        for i in range(150, 160):
            probabilities[i] = 0.9
        mask = build_vocal_windows(probabilities, 0.1, 0.0, 20.0, beat_duration=0.5)
        assert len(mask.windows) == 2

    def test_gap_bridge_is_clamped_between_min_and_max(self) -> None:
        """The bridge distance follows the beat length but is clamped to [min_gap, max_gap]."""
        probabilities = [0.0] * 300
        for i in range(10, 20):
            probabilities[i] = 0.9
        # padded gap of 0.7s: strictly between min_gap (0.5) and max_gap (1.0), so a tiny
        # beat_duration (clamped up to min_gap) does not bridge but a long one does
        for i in range(37, 47):
            probabilities[i] = 0.9
        tight = build_vocal_windows(probabilities, 0.1, 0.0, 30.0, beat_duration=0.1)
        loose = build_vocal_windows(probabilities, 0.1, 0.0, 30.0, beat_duration=5.0)
        assert len(tight.windows) == 2
        assert len(loose.windows) == 1

    def test_windows_clipped_to_range(self) -> None:
        """A run whose padding would spill past start_s/end_s is clipped, not dropped."""
        probabilities = [0.0] * 100
        for i in range(5):
            probabilities[i] = 0.9
        mask = build_vocal_windows(probabilities, 0.1, 0.0, 10.0)
        assert mask.windows[0][0] == 0.0  # left padding clipped at the range start

    def test_trailing_run_without_a_close_frame_still_closes(self) -> None:
        """A run still active at the end of the timeline closes at its last frame."""
        probabilities = [0.0] * 50
        for i in range(40, 50):
            probabilities[i] = 0.9
        mask = build_vocal_windows(probabilities, 0.1, 0.0, 5.0)
        assert len(mask.windows) == 1
        assert mask.windows[0][1] == pytest.approx(5.0)


class TestBuildVocalWindowsSustainedEvidenceGate:
    """A run only becomes a window when its peak OR its mean shows real confidence."""

    def test_weak_backing_run_is_dropped(self) -> None:
        """A 1.5s run peaking 0.69 (background 'aahh') never becomes a window."""
        probs = _timeline([(0.0, 100), (0.55, 10), (0.69, 5), (0.0, 100)])
        mask = build_vocal_windows(probs, FRAME, 0.0, len(probs) * FRAME)
        assert mask.windows == []

    def test_confident_peak_run_is_kept(self) -> None:
        """A short run with a confident peak (real singing) still opens a window."""
        probs = _timeline([(0.0, 100), (0.95, 15), (0.0, 100)])
        mask = build_vocal_windows(probs, FRAME, 0.0, len(probs) * FRAME)
        assert len(mask.windows) == 1

    def test_sustained_medium_run_is_kept(self) -> None:
        """A long medium-confidence run (soft verse, mean >= 0.65) is kept."""
        # 0.70, not 0.65, so the assertion doesn't ride the mean-threshold boundary
        probs = _timeline([(0.0, 50), (0.70, 80), (0.0, 50)])
        mask = build_vocal_windows(probs, FRAME, 0.0, len(probs) * FRAME)
        assert len(mask.windows) == 1

    def test_moderate_mean_backing_run_is_dropped(self) -> None:
        """A ~1.1s run, peak 0.84 / mean ~0.648, between the old (0.60) and new (0.65) floor."""
        probs = _timeline([(0.0, 100), (0.55, 5), (0.62, 3), (0.84, 3), (0.0, 100)])
        mask = build_vocal_windows(probs, FRAME, 0.0, len(probs) * FRAME)
        assert mask.windows == []


class TestVocalMaskClampedTo:
    """The hard boundary clamp: FireRed may never claim activity past a given bound."""

    def test_window_entirely_past_bound_is_dropped(self) -> None:
        """A window starting at/after the bound is removed outright."""
        mask = VocalMask(windows=[(10.0, 12.0)])
        assert mask.clamped_to(10.0).windows == []

    def test_window_straddling_bound_is_clipped(self) -> None:
        """A window that starts before the bound but reaches past it is clipped, not dropped."""
        mask = VocalMask(windows=[(8.0, 12.0)])
        assert mask.clamped_to(10.0).windows == [(8.0, 10.0)]

    def test_window_fully_inside_bound_is_untouched(self) -> None:
        """A window entirely within the bound passes through unchanged."""
        mask = VocalMask(windows=[(1.0, 2.0)])
        assert mask.clamped_to(10.0).windows == [(1.0, 2.0)]

    def test_last_end_of_empty_mask_is_zero(self) -> None:
        """An empty mask reports no activity."""
        assert VocalMask(windows=[]).last_end() == 0.0

    def test_last_end_returns_final_window_end(self) -> None:
        """last_end reflects the rightmost window's end."""
        assert VocalMask(windows=[(1.0, 2.0), (5.0, 8.5)]).last_end() == 8.5


class TestMergeWindows:
    """Interval merging that both collision_metrics and the planner's metrics rely on."""

    def test_overlapping_windows_merge(self) -> None:
        """Overlapping intervals combine into one."""
        assert merge_windows([(0.0, 5.0), (3.0, 8.0)]) == [(0.0, 8.0)]

    def test_touching_windows_merge(self) -> None:
        """Intervals that exactly touch at the boundary also merge."""
        assert merge_windows([(0.0, 5.0), (5.0, 8.0)]) == [(0.0, 8.0)]

    def test_disjoint_windows_stay_separate(self) -> None:
        """Non-overlapping intervals are returned independently, sorted."""
        assert merge_windows([(5.0, 8.0), (0.0, 2.0)]) == [(0.0, 2.0), (5.0, 8.0)]

    def test_empty_input_returns_empty(self) -> None:
        """No intervals in, none out."""
        assert merge_windows([]) == []


class TestCollisionMetrics:
    """The two-sided vocal-collision guard: simultaneous seconds and gain-weighted seconds."""

    def test_no_overlap_yields_zero(self) -> None:
        """Disjoint outgoing/incoming windows collide for zero seconds."""
        seconds, weighted = collision_metrics([(0.0, 2.0)], [(5.0, 7.0)], duration=10.0)
        assert seconds == 0.0
        assert weighted == 0.0

    def test_full_overlap_matches_analytic_integral(self) -> None:
        """A full-duration overlap on both sides integrates to the exact analytic value."""
        duration = 10.0
        seconds, weighted = collision_metrics([(0.0, duration)], [(0.0, duration)], duration)
        assert seconds == pytest.approx(duration)
        # closed-form integral of 4p(1-p) over p in [0,1] is 2/3
        assert weighted == pytest.approx(duration * 2.0 / 3.0)

    def test_partial_overlap_is_clipped_to_the_render_window(self) -> None:
        """Windows extending outside [0, duration] are clipped before scoring."""
        seconds, _ = collision_metrics([(-5.0, 3.0)], [(1.0, 20.0)], duration=10.0)
        assert seconds == pytest.approx(2.0)  # overlap is [1,3]

    def test_zero_duration_yields_zero(self) -> None:
        """A zero-length crossfade collides for zero seconds, avoiding a division by zero."""
        seconds, weighted = collision_metrics([(0.0, 1.0)], [(0.0, 1.0)], duration=0.0)
        assert seconds == 0.0
        assert weighted == 0.0

    def test_overlapping_outgoing_windows_are_not_double_counted(self) -> None:
        """Two outgoing windows both colliding with the same incoming window merge first."""
        seconds, _ = collision_metrics([(0.0, 4.0), (3.0, 6.0)], [(0.0, 10.0)], duration=10.0)
        assert seconds == pytest.approx(6.0)

    def test_weight_peaks_at_the_midpoint(self) -> None:
        """The simultaneous-power weight is highest at the crossfade's midpoint."""
        duration = 10.0
        _, edge = collision_metrics([(0.0, 1.0)], [(0.0, 1.0)], duration)
        _, middle = collision_metrics([(4.5, 5.5)], [(4.5, 5.5)], duration)
        assert middle > edge

    def test_matches_manual_analytic_primitive(self) -> None:
        """Cross-check against the hand-derived antiderivative for an arbitrary interval."""

        def primitive(t: float, duration: float) -> float:
            phase = t / duration
            return duration * (2.0 * phase**2 - (4.0 / 3.0) * phase**3)

        duration, left, right = 12.0, 3.2, 7.9
        _, weighted = collision_metrics([(left, right)], [(left, right)], duration)
        expected = primitive(right, duration) - primitive(left, duration)
        assert weighted == pytest.approx(expected)
        assert math.isfinite(weighted)
