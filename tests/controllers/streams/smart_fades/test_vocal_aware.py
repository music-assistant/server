"""
Tests for the vocal-aware Smart Fades planner.

Covers the FireRed-driven layer on top of the energy/downbeat-aligned
planner: collision-avoiding candidate search, outgoing-vocal retention,
the click-free equal-power handoff, and equivalence with the energy-only
planner when vocal data is missing or invalid.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.smart_fades.filters import CrossfadeFilter
from music_assistant.controllers.streams.smart_fades.models import (
    TransitionPlan,
    TransitionStrategy,
)
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner
from music_assistant.controllers.streams.smart_fades.renderer import TransitionRenderer
from music_assistant.models.audio_analysis import AudioAnalysisData

LOGGER = logging.getLogger(__name__)


def _beats(start: float, count: int, interval: float) -> np.ndarray:
    return np.arange(count, dtype=np.float32) * interval + start


def _analysis(
    bpm: float,
    duration: float = 240.0,
    rms_energy: np.ndarray | None = None,
    key: str | None = "A",
    mode: str | None = "minor",
) -> AudioAnalysisData:
    interval = 60.0 / bpm
    count = int(duration / interval) + 1
    beats = _beats(0.0, count, interval)
    # a normal (non-silent) track with a known key earns the full-blend tier, so a
    # colliding 16-bar candidate genuinely exercises the remediation ladder
    energy = rms_energy if rms_energy is not None else np.full(1800, 0.5, dtype=np.float32)
    return AudioAnalysisData(
        duration=duration,
        bpm=bpm,
        beats=beats.tolist(),
        downbeats=beats[::4].tolist(),
        rms_energy=energy.tolist(),
        key=key,
        mode=mode,
    )


def _vocal_probabilities(
    duration: float,
    active_windows: list[tuple[float, float]],
) -> list[float]:
    """Build a probability timeline that is quiet except inside ``active_windows``."""
    n_frames = 1800
    frame_duration = duration / n_frames
    probabilities = [0.05] * n_frames
    for start, end in active_windows:
        start_index = max(0, int(start / frame_duration))
        end_index = min(n_frames, int(end / frame_duration) + 1)
        for i in range(start_index, end_index):
            probabilities[i] = 0.9
    return probabilities


def _with_vocal_activity(
    analysis: AudioAnalysisData,
    active_windows: list[tuple[float, float]],
) -> AudioAnalysisData:
    """Attach a valid vocal_activity list, active only inside ``active_windows``."""
    assert analysis.duration is not None
    analysis.extra_data = {
        "vocal_activity": _vocal_probabilities(analysis.duration, active_windows)
    }
    return analysis


def _rms_with_silence(duration: float, silence_start: float) -> np.ndarray:
    """1800-bin RMS envelope that drops to near-silence at ``silence_start``."""
    bins = np.full(1800, 0.5, dtype=np.float32)
    t = np.linspace(0, duration, 1800)
    bins[t >= silence_start] = 0.001
    return bins


def _plan(
    fade_out: AudioAnalysisData, fade_in: AudioAnalysisData, buffer: float = 45.0
) -> TransitionPlan:
    return SmartCrossFadePlanner(LOGGER).plan(fade_out, fade_in, buffer)


class TestCleanBlendWithoutCollision:
    """When no vocal collision exists, planning matches the plain energy-only ladder."""

    def test_clean_blend_with_real_vocals_matches_the_energy_ladder(self) -> None:
        """Tempo-compatible tracks with non-colliding (real-duty) vocals keep the 8-bar blend."""
        baseline = _plan(_analysis(120.0, duration=240.0), _analysis(120.0, duration=240.0))

        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [(220.0, 226.0)])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [(35.0, 41.0)])
        plan = _plan(out, inc)

        assert plan.crossfade_duration == pytest.approx(baseline.crossfade_duration)
        assert plan.metrics.strategy is TransitionStrategy.ENERGY_ALIGNED
        assert plan.metrics.collision_seconds == 0.0
        assert plan.metrics.weighted_collision_seconds == 0.0

    def test_verified_instrumental_pair_earns_the_16_bar_blend(self) -> None:
        """Near-zero vocal duty on BOTH decks earns the doubled 16-bar overlap."""
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [])
        plan = _plan(out, inc)

        bar = 4 * 60.0 / 120.0
        assert round(plan.crossfade_duration / bar) == 16
        assert plan.metrics.strategy is TransitionStrategy.ENERGY_ALIGNED

    def test_one_vocal_deck_denies_the_16_bar_blend(self) -> None:
        """Real vocal duty on either side keeps the corpus-backed 8-bar default."""
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [(200.0, 230.0)])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [])
        plan = _plan(out, inc)

        bar = 4 * 60.0 / 120.0
        assert round(plan.crossfade_duration / bar) <= 8


class TestVocalCollisionAvoidance:
    """A colliding candidate is rejected in favor of a smaller, collision-free one."""

    def test_collision_on_the_largest_candidate_falls_back_to_a_smaller_one(self) -> None:
        """
        Near-instrumental decks whose lone phrases collide only at 16 bars drop a rung.

        Both decks' vocal duty is under the instrumental threshold, so the pair
        earns the 16-bar overlap — where A's phrase (media 228s → rendered ~20s)
        stacks exactly on B's phrase at 20s. The 8-bar overlap misses both.
        """
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [(228.0, 230.05)])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [(20.0, 22.05)])
        plan = _plan(out, inc)

        assert plan.crossfade_duration == pytest.approx(16.0)
        assert plan.metrics.strategy is TransitionStrategy.ENERGY_ALIGNED
        assert plan.metrics.collision_seconds == 0.0

    def test_the_rejected_larger_candidate_would_indeed_have_collided(self) -> None:
        """Confirm the 16-bar candidate this scenario skips really does breach the guard."""
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [(228.0, 230.05)])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [(20.0, 22.05)])
        planner = SmartCrossFadePlanner(LOGGER)
        planner._prepare_decks(out, inc, 45.0)
        candidate = planner._build_candidate(16)
        assert candidate is not None
        _, metrics = planner._protect_outgoing_vocal(16, candidate)
        assert (
            metrics.collision_seconds >= planner.collision_seconds_limit
            or metrics.weighted_collision_seconds >= planner.weighted_collision_limit
        )


class TestShortVocalHandoff:
    """When every phrased candidate collides, ship the click-free equal-power fallback."""

    def test_short_handoff_when_every_candidate_collides(self) -> None:
        """Vocals spanning almost the whole tail/head collide at every ladder rung."""
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [(200.0, 239.9)])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [(0.0, 40.0)])
        plan = _plan(out, inc)

        assert plan.metrics.strategy is TransitionStrategy.SHORT_VOCAL_HANDOFF
        assert 0.4 <= plan.crossfade_duration <= 1.0
        assert not plan.tempo_plan.steps
        assert plan.fadein_trim_start is None
        eq = plan.eq_plan
        assert eq.low_out is None
        assert eq.low_in is None
        assert eq.high_out is None
        assert eq.high_in is None
        assert eq.mid_out is None
        assert eq.mid_in is None

    def test_handoff_still_uses_the_qsin_crossfade_filter(self) -> None:
        """The handoff plan renders through the same equal-power CrossfadeFilter as any other."""
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [(200.0, 239.9)])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [(0.0, 40.0)])
        plan = _plan(out, inc)
        assert plan.metrics.strategy is TransitionStrategy.SHORT_VOCAL_HANDOFF

        pcm_format = AudioFormat(
            content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32, channels=2
        )
        filters, _timing = TransitionRenderer(LOGGER).render(
            plan, pcm_format, fade_in_bytes_len=int(45.0 * pcm_format.pcm_sample_size)
        )
        crossfade_filters = [f for f in filters if isinstance(f, CrossfadeFilter)]
        assert len(crossfade_filters) == 1


class TestOutgoingVocalRetention:
    """The outgoing track's own vocal is never truncated, even at the cost of a longer tail."""

    def test_retention_extends_the_anchor_into_the_downbeat_snap_gap(self) -> None:
        """
        A vocal ending just past the downbeat-snapped anchor pulls the anchor back out.

        The RMS-audible boundary sits slightly after the nearest downbeat the
        energy-only planner would normally snap to; when FireRed shows the
        outgoing vocal running into that gap, the anchor must move to cover it,
        never past the RMS boundary itself.
        """
        duration = 200.0
        bpm = 100.0
        probe_out = _analysis(bpm, duration, rms_energy=_rms_with_silence(duration, 190.3))
        probe = SmartCrossFadePlanner(LOGGER)
        probe.plan(probe_out, _analysis(bpm, duration), 45.0)
        assert probe._audio_end > probe.effective_end + 0.1, "fixture needs a real snap gap"

        buffer_offset = duration - 45.0
        vocal_end_buffer_local = (probe.effective_end + probe._audio_end) / 2
        vocal_end_media = vocal_end_buffer_local + buffer_offset

        out = _analysis(bpm, duration, rms_energy=_rms_with_silence(duration, 190.3))
        out = _with_vocal_activity(out, [(vocal_end_media - 0.5, vocal_end_media)])
        inc = _with_vocal_activity(_analysis(bpm, duration), [])

        plan = _plan(out, inc, 45.0)
        assert plan.fade_out_window >= vocal_end_buffer_local - 1e-6
        assert plan.fade_out_window > probe.effective_end + 1e-6

    def test_retention_never_exceeds_the_rms_audible_boundary(self) -> None:
        """Even a vocal claiming to run past the RMS boundary caps the anchor at that boundary."""
        duration = 200.0
        bpm = 100.0
        probe = SmartCrossFadePlanner(LOGGER)
        probe.plan(
            _analysis(bpm, duration, rms_energy=_rms_with_silence(duration, 190.3)),
            _analysis(bpm, duration),
            45.0,
        )

        out = _analysis(bpm, duration, rms_energy=_rms_with_silence(duration, 190.3))
        # a vocal window reaching all the way to the buffer end (past audio_end)
        out = _with_vocal_activity(out, [(duration - 5.0, duration - 0.05)])
        inc = _with_vocal_activity(_analysis(bpm, duration), [])

        plan = _plan(out, inc, 45.0)
        assert plan.fade_out_window <= probe._audio_end + 1e-6


class TestScheduleRecomputationAfterAnchorMovement:
    """A re-anchored candidate is rebuilt whole, not patched — every schedule reflects it."""

    def test_eq_and_trim_schedules_match_the_new_anchor(self) -> None:
        """After retention moves the anchor, EQ/trim bounds are consistent with the new plan."""
        duration = 200.0
        bpm = 100.0
        probe_out = _analysis(bpm, duration, rms_energy=_rms_with_silence(duration, 190.3))
        probe = SmartCrossFadePlanner(LOGGER)
        probe.plan(probe_out, _analysis(bpm, duration), 45.0)
        assert probe._audio_end > probe.effective_end + 0.1, "fixture needs a real snap gap"

        buffer_offset = duration - 45.0
        vocal_end_buffer_local = (probe.effective_end + probe._audio_end) / 2
        vocal_end_media = vocal_end_buffer_local + buffer_offset

        out = _analysis(bpm, duration, rms_energy=_rms_with_silence(duration, 190.3))
        out = _with_vocal_activity(out, [(vocal_end_media - 0.5, vocal_end_media)])
        inc = _with_vocal_activity(_analysis(bpm, duration), [])

        plan = _plan(out, inc, 45.0)
        # the anchor really did move, so this exercises the rebuild, not a no-op
        assert plan.fade_out_window > probe.effective_end + 1e-6

        assert 0.0 <= plan.eq_plan.swap_at <= plan.crossfade_duration
        if plan.eq_plan.low_out is not None:
            assert plan.eq_plan.low_out.steps[-1][0] <= plan.fade_out_window + 1e-6
        if plan.eq_plan.low_in is not None:
            assert plan.eq_plan.low_in.steps[-1][0] <= plan.crossfade_duration + 1e-6
        if plan.tempo_plan:
            crossfade_start = plan.fade_out_window - plan.crossfade_duration
            assert plan.tempo_plan.steps[-1][0] <= crossfade_start + 1e-6

    def test_rebuilt_candidate_is_not_a_patched_copy_of_the_pristine_one(self) -> None:
        """The rebuilt plan's timing differs from the pristine (unprotected) anchor entirely."""
        duration = 200.0
        bpm = 100.0
        probe_out = _analysis(bpm, duration, rms_energy=_rms_with_silence(duration, 190.3))
        probe = SmartCrossFadePlanner(LOGGER)
        probe.plan(probe_out, _analysis(bpm, duration), 45.0)

        buffer_offset = duration - 45.0
        vocal_end_buffer_local = (probe.effective_end + probe._audio_end) / 2
        vocal_end_media = vocal_end_buffer_local + buffer_offset

        out = _analysis(bpm, duration, rms_energy=_rms_with_silence(duration, 190.3))
        out = _with_vocal_activity(out, [(vocal_end_media - 0.5, vocal_end_media)])
        inc = _with_vocal_activity(_analysis(bpm, duration), [])

        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        pristine_candidate = planner._build_candidate(1)
        assert pristine_candidate is not None
        assert pristine_candidate.fade_out_window != plan.fade_out_window
        assert pristine_candidate.fadeout_trim != plan.fadeout_trim

    def test_rebuild_preserves_a_remediated_incoming_entry(self) -> None:
        """Moving the outgoing anchor keeps the incoming entry selected by remediation."""
        duration = 200.0
        bpm = 100.0
        out = _analysis(bpm, duration, rms_energy=_rms_with_silence(duration, 190.3))
        out = _with_vocal_activity(out, [(189.0, 190.0)])
        inc = _with_vocal_activity(_analysis(bpm, duration), [])

        planner = SmartCrossFadePlanner(LOGGER)
        planner._prepare_decks(out, inc, 45.0)
        candidate = planner._build_candidate(2, entry=4.8)
        assert candidate is not None
        assert candidate.fadein_trim_start == pytest.approx(4.8)

        protected, _metrics = planner._protect_outgoing_vocal(2, candidate)

        assert protected.fade_out_window > candidate.fade_out_window
        assert protected.fadein_trim_start == pytest.approx(candidate.fadein_trim_start)


class TestShortFadeAudibleTrimBound:
    """For crossfades at or under 8s, dropped audible material never exceeds the overlap."""

    def test_audible_trim_never_exceeds_the_overlap_length(self) -> None:
        """
        A tiny forced overlap must not drop more audible material than it covers.

        A sparse outgoing downbeat grid combined with a fast incoming track forces a
        tiny synthetic overlap far shorter than the downbeat-snap gap; the planner
        must re-anchor so the audible material dropped never exceeds that overlap.
        """
        duration = 200.0
        buffer_duration = 45.0
        out_bpm = 100.0
        in_bpm = 300.0

        dense_beats = np.arange(0.0, 150.0, 0.6, dtype=np.float32)
        sparse_downbeat_media = 191.5  # one real downbeat, 3.5s before the RMS boundary
        out = AudioAnalysisData(
            duration=duration,
            bpm=out_bpm,
            beats=np.concatenate([dense_beats, [sparse_downbeat_media]]).tolist(),
            downbeats=sorted([sparse_downbeat_media, *dense_beats[::4].tolist()]),
            rms_energy=_rms_with_silence(duration, 195.0).tolist(),
        )
        out = _with_vocal_activity(out, [])
        inc = AudioAnalysisData(
            duration=duration,
            bpm=in_bpm,
            beats=np.arange(0.0, 60.0, 60.0 / in_bpm, dtype=np.float32).tolist(),
            downbeats=np.arange(0.0, 60.0, 4 * 60.0 / in_bpm, dtype=np.float32).tolist(),
        )
        inc = _with_vocal_activity(inc, [])

        planner = SmartCrossFadePlanner(LOGGER)
        planner._prepare_decks(out, inc, buffer_duration)
        naive_candidate = planner._build_candidate(1)
        assert naive_candidate is not None
        naive_gap = planner._audio_end - naive_candidate.fade_out_window
        assert naive_gap > naive_candidate.crossfade_duration, "fixture needs a real violation"

        _protected, metrics = planner._protect_outgoing_vocal(1, naive_candidate)
        assert metrics.audible_outgoing_trim <= naive_candidate.crossfade_duration + 1e-6


class TestHighHopesStyleFalsePositive:
    """FireRed activity in a genuinely silent tail must never extend the audible boundary."""

    def test_false_positive_past_the_rms_boundary_does_not_move_the_anchor(self) -> None:
        """A high-confidence FireRed run entirely inside the RMS-silent tail is ignored."""
        duration = 240.0
        bpm = 120.0
        baseline = _plan(
            _analysis(bpm, duration, rms_energy=_rms_with_silence(duration, 225.0)),
            _analysis(bpm, duration),
        )

        out = _analysis(bpm, duration, rms_energy=_rms_with_silence(duration, 225.0))
        # FireRed hallucinates vocal-like activity in the low-energy tail, past the boundary
        out = _with_vocal_activity(out, [(229.5, 238.5)])
        inc = _with_vocal_activity(_analysis(bpm, duration), [])

        plan = _plan(out, inc)
        assert plan.fade_out_window == pytest.approx(baseline.fade_out_window)
        assert plan.crossfade_duration == pytest.approx(baseline.crossfade_duration)

    def test_false_positive_window_is_dropped_entirely_from_the_mask(self) -> None:
        """The clamp drops the false-positive window outright rather than clipping a sliver."""
        duration = 240.0
        bpm = 120.0
        out = _analysis(bpm, duration, rms_energy=_rms_with_silence(duration, 225.0))
        out = _with_vocal_activity(out, [(229.5, 238.5)])
        inc = _with_vocal_activity(_analysis(bpm, duration), [])

        planner = SmartCrossFadePlanner(LOGGER)
        planner.plan(out, inc, 45.0)
        assert planner._vocal_out_mask is not None
        assert planner._vocal_out_mask.windows == []


class TestRemediationAltersTheCandidate:
    """A colliding candidate 0 is remediated deterministically (bar rung / entry / anchor)."""

    def test_collision_remediation_drops_the_bar_rung_deterministically(self) -> None:
        """The colliding 16-bar candidate 0 is remediated to a shorter, collision-free overlap."""
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [(228.0, 230.05)])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [(20.0, 22.05)])

        planner = SmartCrossFadePlanner(LOGGER)
        planner._prepare_decks(out, inc, 45.0)
        tier = planner._choose_tier()
        bars0, cand0 = planner._energy_candidate(tier)
        # the full-blend candidate 0 spans 16 bars and genuinely collides
        assert bars0 == 16
        assert round(cand0.crossfade_duration / (4 * 60.0 / 120.0)) == 16
        assert planner._guard_fires(cand0)

        plan = _plan(out, inc)
        # remediation shipped a shorter, collision-free overlap on a different bar rung
        assert plan.crossfade_duration < cand0.crossfade_duration
        assert plan.metrics.strategy is TransitionStrategy.ENERGY_ALIGNED
        assert plan.metrics.collision_seconds < planner.collision_seconds_limit
        assert plan.metrics.weighted_collision_seconds < planner.weighted_collision_limit
        # the search is a pure function of the inputs
        assert _plan(out, inc) == plan


class TestMissingOrInvalidVocalDataFallsBackToEnergyOnly:
    """Any defect in either side's vocal timeline disables vocal logic entirely."""

    def test_missing_vocal_activity_matches_the_energy_only_plan(self) -> None:
        """No vocal_activity at all on either side yields exactly the energy-only plan."""
        baseline = _plan(_analysis(120.0, duration=240.0), _analysis(120.0, duration=240.0))
        plan = _plan(_analysis(120.0, duration=240.0), _analysis(120.0, duration=240.0))
        assert plan == baseline

    def test_old_wrapped_contract_matches_the_energy_only_plan(self) -> None:
        """A row using the old wrapped contract disables vocal logic."""
        baseline = _plan(_analysis(120.0, duration=240.0), _analysis(120.0, duration=240.0))
        out = _analysis(120.0, duration=240.0)
        out.extra_data = {
            "vocal_activity": {
                "model": "some_other_model",
                "frame_duration": 0.1,
                "probabilities": [0.9] * 2400,
            }
        }
        plan = _plan(out, _analysis(120.0, duration=240.0))
        assert plan == baseline

    def test_non_list_timeline_matches_the_energy_only_plan(self) -> None:
        """A tuple timeline is malformed because the provider contract stores a list."""
        baseline = _plan(_analysis(120.0, duration=240.0), _analysis(120.0, duration=240.0))
        out = _analysis(120.0, duration=240.0)
        out.extra_data = {"vocal_activity": tuple([0.9] * 1800)}
        plan = _plan(out, _analysis(120.0, duration=240.0))
        assert plan == baseline

    def test_partial_timeline_matches_the_energy_only_plan(self) -> None:
        """A timeline with fewer than 1800 bins is rejected."""
        baseline = _plan(_analysis(120.0, duration=240.0), _analysis(120.0, duration=240.0))
        out = _analysis(120.0, duration=240.0)
        out.extra_data = {"vocal_activity": [0.9] * 1000}
        plan = _plan(out, _analysis(120.0, duration=240.0))
        assert plan == baseline

    def test_one_sided_valid_data_still_falls_back_to_energy_only(self) -> None:
        """Valid data on only one side is not enough; both sides must validate."""
        baseline = _plan(_analysis(120.0, duration=240.0), _analysis(120.0, duration=240.0))
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [(238.0, 239.0)])
        plan = _plan(out, _analysis(120.0, duration=240.0))
        assert plan == baseline
