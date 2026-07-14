"""Tests for SmartCrossFadePlanner — the pure decision step of smart fades."""

from __future__ import annotations

import itertools
import logging
import math

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.bands import window_fraction
from music_assistant.controllers.streams.smart_fades.filters import ShelfType
from music_assistant.controllers.streams.smart_fades.helpers import SMART_CROSSFADE_DURATION
from music_assistant.controllers.streams.smart_fades.models import (
    EqPlan,
    ShelfSchedule,
    SmartFadeNotApplicable,
    TransitionPlan,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner
from music_assistant.models.audio_analysis import AudioAnalysisData
from tests.controllers.streams.smart_fades.conftest import _analysis_with_bands

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
    # a normal (non-silent) track with a known key earns the full-blend tier;
    # tests that need a silent tail or a bare energy-only track override these
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


def _rms_with_silent_tail(duration: float, silent_tail: float) -> np.ndarray:
    bins = np.full(1800, 0.5, dtype=np.float32)
    bins[0] = 1.0
    if silent_tail > 0:
        bins[-int(silent_tail / duration * 1800) :] = 0.001
    return bins


def _plan(
    fade_out: AudioAnalysisData, fade_in: AudioAnalysisData, buffer: float = 45.0
) -> TransitionPlan:
    return SmartCrossFadePlanner(LOGGER).plan(fade_out, fade_in, buffer)


class TestSmartCrossFadePlanner:
    """The planner turns two analysis rows into a TransitionPlan, no bytes involved."""

    def test_returns_transition_plan(self) -> None:
        """A compatible pair yields a populated TransitionPlan."""
        plan = _plan(_analysis(120.0), _analysis(122.0))
        assert isinstance(plan, TransitionPlan)
        assert plan.crossfade_duration > 0
        eq = plan.eq_plan
        assert eq.low_out is not None
        assert eq.low_in is not None
        assert eq.high_out is not None
        assert eq.high_in is not None
        assert eq.low_out.shelf_type is ShelfType.LOW
        assert eq.low_in.shelf_type is ShelfType.LOW
        assert eq.high_out.shelf_type is ShelfType.HIGH
        assert eq.high_in.shelf_type is ShelfType.HIGH
        # B enters with the low end killed and ends open
        assert eq.low_in.steps[0] == (0.0, -26.0)
        assert eq.low_in.steps[-1][1] == pytest.approx(0.0)
        # A starts open and ends killed
        assert eq.low_out.steps[0][1] == pytest.approx(0.0)
        assert eq.low_out.steps[-1][1] == pytest.approx(-26.0)
        # the swap sits inside the overlap
        assert 0.0 < eq.swap_at < plan.crossfade_duration

    def test_is_deterministic(self) -> None:
        """Planning the same inputs twice yields identical plans (pure function)."""
        out, inc = _analysis(120.0), _analysis(123.0)
        first, second = _plan(out, inc), _plan(out, inc)
        assert first == second

    def test_compatible_bpm_plans_a_tempo_ramp(self) -> None:
        """A small BPM difference (<5%) schedules a gradual stretch."""
        plan = _plan(_analysis(120.0), _analysis(123.0))
        assert plan.tempo_plan

    def test_large_bpm_difference_skips_stretch(self) -> None:
        """A BPM gap beyond the stretch threshold leaves the tempo plan empty."""
        plan = _plan(_analysis(120.0), _analysis(150.0))
        assert not plan.tempo_plan

    def test_silent_tail_is_not_applicable(self) -> None:
        """A mostly-silent outro raises so the caller falls back."""
        out = _analysis(120.0, rms_energy=_rms_with_silent_tail(240.0, 40.0))
        with pytest.raises(SmartFadeNotApplicable, match="silent"):
            _plan(out, _analysis(120.0))

    def test_silent_tail_sets_fadeout_trim(self) -> None:
        """A trimmable silent tail produces a FadeOutTrim at the audible end."""
        out = _analysis(120.0, rms_energy=_rms_with_silent_tail(240.0, 10.0))
        plan = _plan(out, _analysis(120.0))
        assert plan.fadeout_trim is not None
        assert plan.fadeout_trim.end_pos == pytest.approx(plan.fade_out_window)
        assert plan.fade_out_window == pytest.approx(35.0, abs=0.3)

    def test_no_silence_has_no_fadeout_trim(self) -> None:
        """Without trailing silence the plan keeps the full window and no trim."""
        plan = _plan(_analysis(120.0), _analysis(120.0))
        assert plan.fadeout_trim is None
        assert plan.fade_out_window == pytest.approx(45.0, abs=0.3)

    def test_missing_rhythm_data_raises(self) -> None:
        """Analysis without bpm/beats cannot be planned."""
        with pytest.raises(ValueError, match="bpm and beats"):
            _plan(AudioAnalysisData(duration=200.0), _analysis(120.0))

    def test_fade_out_window_never_exceeds_buffer(self) -> None:
        """The planned window is bounded by the available holdback."""
        plan = _plan(_analysis(120.0), _analysis(120.0), buffer=float(SMART_CROSSFADE_DURATION))
        assert plan.fade_out_window <= SMART_CROSSFADE_DURATION + 1e-6

    def test_late_incoming_entry_reduces_the_candidate_bars(self) -> None:
        """A late-starting incoming grid rejects the 16/8-bar rungs and lands on a smaller one."""
        inc = _analysis(120.0, duration=240.0)
        # incoming's first downbeat at 33s leaves only ~6 downbeats before the 45s
        # buffer end — not enough to align a 16- or 8-bar overlap, but plenty for 4
        inc.beats = (np.asarray(inc.beats, dtype=np.float32) + 33.0).tolist()
        inc.downbeats = (np.asarray(inc.downbeats, dtype=np.float32) + 33.0).tolist()
        plan = _plan(_analysis(120.0, duration=240.0), inc)
        assert plan.crossfade_duration == pytest.approx(8.0)
        assert plan.fadein_trim_start == pytest.approx(33.0)

    def test_valid_entry_too_late_for_the_bars_duration_reduces_the_rungs_too(self) -> None:
        """A found entry point that still overflows the buffer also demotes the candidate."""
        out = _analysis(120.0, duration=240.0)
        inc = _analysis(120.0, duration=240.0)
        # a beat-level entry is found at 20s (too few downbeats for the downbeat
        # branch), which fits the 8-bar (16s) overlap but not the 16-bar (32s) one
        inc.beats = np.arange(20.0, 45.0, 0.1, dtype=np.float32).tolist()
        inc.downbeats = [20.0]
        plan = _plan(out, inc)
        assert plan.crossfade_duration == pytest.approx(16.0)
        assert plan.fadein_trim_start == pytest.approx(20.0)

    def test_entry_fit_uses_the_tempo_compensated_duration(self) -> None:
        """A valid entry is not rejected when tempo compensation makes the overlap fit."""
        planner = SmartCrossFadePlanner(LOGGER)
        planner._prepare_decks(_analysis(120.0), _analysis(124.0), 45.0)

        plan = planner._build_candidate(16, entry=14.0)

        assert plan is not None
        assert plan.tempo_plan
        assert plan.fadein_trim_start == pytest.approx(14.0)
        assert plan.fadein_trim_start is not None
        assert plan.fadein_trim_start + plan.crossfade_duration <= SMART_CROSSFADE_DURATION

    def test_swap_lands_on_incoming_groove_entry(self) -> None:
        """B's groove entry inside the overlap becomes the bass-swap moment."""
        inc = _analysis(120.0, duration=240.0)
        bins = np.full(1800, 0.5, dtype=np.float32)
        t = np.linspace(0, 240.0, 1800)
        bins[t < 8.0] = 0.05
        inc.rms_energy = bins.tolist()
        plan = _plan(_analysis(120.0, duration=240.0), inc)
        trim = plan.fadein_trim_start or 0.0
        assert plan.eq_plan.swap_at == pytest.approx(8.0 - trim, abs=0.1)

    def test_swap_window_fits_inside_the_overlap(self) -> None:
        """A late swap point never pushes the low ramps past the crossfade end."""
        inc = _analysis(120.0, duration=240.0)
        bins = np.full(1800, 0.5, dtype=np.float32)
        t = np.linspace(0, 240.0, 1800)
        bins[t < 20.0] = 0.05
        inc.rms_energy = bins.tolist()
        plan = _plan(_analysis(120.0, duration=240.0), inc)
        assert plan.eq_plan.low_in is not None
        assert plan.eq_plan.low_out is not None
        # B's bass must be fully restored before the rendered mix ends
        assert plan.eq_plan.low_in.steps[-1][0] <= plan.crossfade_duration + 1e-6
        # A's bass kill must complete before A's audible end
        assert plan.eq_plan.low_out.steps[-1][0] <= plan.fade_out_window + 1e-6

    def test_bass_swap_is_proportional_and_centered(self) -> None:
        """The low exchange spans half the overlap (clamped) centered on the swap."""
        plan = _plan(_analysis(120.0, duration=240.0), _analysis(120.0, duration=240.0))
        bar = 4 * 60.0 / 120.0
        expected = min(max(plan.crossfade_duration / 2, 2 * bar), 8 * bar)
        assert plan.eq_plan.low_in is not None
        ramp = plan.eq_plan.low_in.steps[1:]  # steps[0] is the (0.0, kill) pin
        span = ramp[-1][0] - ramp[0][0]
        assert span == pytest.approx(expected, rel=0.05)
        midpoint = (ramp[0][0] + ramp[-1][0]) / 2
        assert midpoint == pytest.approx(plan.eq_plan.swap_at, abs=0.2)


class TestTransitionTier:
    """The transition tier is chosen from meter, blendability, tempo and key."""

    def test_compatible_key_4_4_pair_is_full_blend_8_bars(self) -> None:
        """
        A clean, key-compatible, same-tempo 4/4 pair earns the full blend at 8 bars.

        The doubled 16-bar overlap must be EARNED by verified near-instrumental
        FireRed data on both decks; without vocal data the corpus-backed 8-bar
        default rules (real DJ transitions cluster at 32 beats).
        """
        # A minor and C major are relative major/minor: the same Camelot slot
        plan = _plan(
            _analysis(120.0, key="A", mode="minor"), _analysis(120.0, key="C", mode="major")
        )
        assert plan.tier is TransitionTier.FULL_BLEND
        bar = 4 * 60.0 / 120.0
        assert round(plan.crossfade_duration / bar) == 8

    def test_incompatible_key_is_tempo_blend_8_bars(self) -> None:
        """Clashing keys demote a same-tempo pair to the shorter 8-bar tempo blend."""
        plan = _plan(
            _analysis(120.0, key="A", mode="minor"), _analysis(120.0, key="F#", mode="major")
        )
        assert plan.tier is TransitionTier.TEMPO_BLEND
        bar = 4 * 60.0 / 120.0
        assert round(plan.crossfade_duration / bar) == 8

    def test_missing_key_is_tempo_blend(self) -> None:
        """An unknown key can never earn the full blend; it demotes to a tempo blend."""
        plan = _plan(_analysis(120.0, key=None, mode=None), _analysis(120.0, key=None, mode=None))
        assert plan.tier is TransitionTier.TEMPO_BLEND
        assert round(plan.crossfade_duration / (4 * 60.0 / 120.0)) == 8

    def test_large_bpm_gap_is_quick_fade(self) -> None:
        """A tempo gap beyond the stretch threshold has no beat-matched blend: quick fade."""
        plan = _plan(_analysis(120.0), _analysis(150.0))
        assert plan.tier is TransitionTier.QUICK_FADE
        assert not plan.tempo_plan

    def test_bpm_gap_within_8_percent_still_earns_a_ramped_blend(self) -> None:
        """The stretch window is ±8% (research: tier-1 population triples vs ±5%)."""
        plan = _plan(_analysis(120.0), _analysis(128.0))
        assert plan.tier is not TransitionTier.QUICK_FADE
        assert plan.tempo_plan

    def test_cross_meter_is_quick_fade(self) -> None:
        """Two different meters share no bar grid to blend across: quick fade."""
        out = _analysis(120.0)
        out.beats_per_bar = 4
        inc = _analysis(120.0)
        inc.beats_per_bar = 3
        plan = _plan(out, inc)
        assert plan.tier is TransitionTier.QUICK_FADE


class TestEnergyMixOut:
    """The outgoing anchor follows the energy/kick mix-out point, snapped to a downbeat."""

    def test_energy_decay_moves_anchor_to_the_mix_out_downbeat(self) -> None:
        """An outro whose energy decays below the mix-out floor anchors at that (downbeat) point."""
        bins = np.full(1800, 0.5, dtype=np.float32)
        t = np.linspace(0, 240.0, 1800)
        bins[t >= 220.0] = 0.2  # audible but below 0.7*sustained: mixed out, not silent
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(_analysis(120.0, rms_energy=bins), _analysis(120.0), 45.0)
        # anchored at the mix-out point (media 220 -> buffer-local 25), well before the
        # RMS-audible boundary, and snapped to a real downbeat
        assert plan.fade_out_window == pytest.approx(25.0, abs=0.3)
        assert planner._audio_end > plan.fade_out_window + 1.0
        assert float(np.min(np.abs(planner._grid_downbeats - plan.fade_out_window))) < 0.05

    def test_low_band_kick_drop_anchors_after_the_last_kick(self) -> None:
        """A kick-timed track whose low band drops out anchors just after the last kick bar."""

        def env(value: float) -> np.ndarray:
            return np.full(1800, value, dtype=np.float32)

        t = np.linspace(0, 240.0, 1800)
        low = env(1.0)
        low[t >= 218.0] = 0.05  # the kick drops out ~22s before the buffer end
        out = _analysis_with_bands(low, env(0.5), env(0.5), env(0.3))
        inc = _analysis_with_bands(env(1.0), env(0.5), env(0.5), env(0.3))
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        # the low anchor lands just after the last kick bar, never past the RMS boundary,
        # and on a real downbeat
        assert plan.fade_out_window < planner._audio_end - 1.0
        assert plan.fade_out_window == pytest.approx(21.0, abs=1.5)
        assert float(np.min(np.abs(planner._grid_downbeats - plan.fade_out_window))) < 0.05

    def test_low_band_entry_beyond_the_buffer_is_ignored(self) -> None:
        """A future kick cannot drive entry or EQ decisions for the buffered head."""
        t = np.linspace(0, 240.0, 1800)
        low = np.where(t < 60.0, 0.01, 1.0).astype(np.float32)
        other = np.full(1800, 0.2, dtype=np.float32)
        incoming = _analysis_with_bands(low, other, other, other)
        planner = SmartCrossFadePlanner(LOGGER)

        planner._prepare_decks(_analysis(120.0), incoming, 45.0)

        assert planner._incoming_entry == 0.0


class TestCandidateBuildGuards:
    """Regression guards for the candidate build state machine."""

    def test_dead_grid_before_energetic_buffer_end_does_not_inflate_quick_fade(self) -> None:
        """
        A beatless-but-energetic outro keeps the quick fade at its intended bar count.

        When the downbeat grid dies well before the buffer end, the anchor↔grid
        gap must not be added to the overlap: a 1-bar quick fade on a 30% tempo
        clash must stay near one bar, never span the whole gap.
        """
        out = _analysis(120.0, duration=240.0)
        beats = np.asarray(out.beats, dtype=np.float32)
        out.beats = beats[beats <= 230.0].tolist()
        downbeats = np.asarray(out.downbeats, dtype=np.float32)
        out.downbeats = downbeats[downbeats <= 230.0].tolist()
        inc = _analysis(156.0, duration=240.0)

        plan = _plan(out, inc)

        assert plan.tier is TransitionTier.QUICK_FADE
        bar_out = 4 * 60.0 / 120.0
        assert plan.crossfade_duration <= 2 * bar_out + 0.1

    def test_protected_intro_before_late_drop_degrades_instead_of_asserting(self) -> None:
        """
        A sung intro ahead of a late bass drop must yield a plan, never raise.

        The rolling-intro deep-trim guard finds no legal cut anywhere in the
        buffer (every bar is low-silent but voice-active until the drop, which
        no overlap can reach); the 1-bar floor must still ship a candidate.
        """

        def env(value: float) -> np.ndarray:
            return np.full(1800, value, dtype=np.float32)

        t = np.linspace(0, 240.0, 1800)
        low = np.where(t < 60.0, 0.02, 1.0).astype(np.float32)
        inc = _analysis_with_bands(low, env(0.6), env(0.6), env(0.3))
        rms = np.full(1800, 0.2, dtype=np.float32)
        rms[t >= 35.0] = 1.0
        inc.rms_energy = rms.tolist()
        out = _analysis_with_bands(env(1.0), env(0.5), env(0.5), env(0.3))

        plan = SmartCrossFadePlanner(LOGGER).plan(out, inc, 45.0)

        assert plan.crossfade_duration > 0.0

    def test_re_anchored_tier_downgrade_caps_the_bar_count(self) -> None:
        """
        A re-anchor that downgrades the tier also caps the inherited bar count.

        The shortened tail flips FULL_BLEND to QUICK_FADE (which drops the
        tempo ramp); the overlap must then shrink to the quick-fade ladder too,
        or a long un-beatmatched overlap ships gated only by the vocal guard.
        """
        planner = SmartCrossFadePlanner(LOGGER)
        planner._prepare_decks(_analysis(80.0), _analysis(83.2), 45.0)

        candidate = planner._build_candidate(8, anchor=20.0, entry=0.0)

        assert candidate is not None
        assert candidate.tier is TransitionTier.QUICK_FADE
        assert not candidate.tempo_plan
        # 4-bar quick-fade cap at 80 BPM (3s bars) plus sub-bar anchor slack
        assert candidate.crossfade_duration <= 15.0


def _bands_pair(f_low_out: float, f_low_in: float) -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """
    Build an outgoing/incoming pair whose low band carries the given power fractions.

    ``band_rms`` envelopes store amplitude, but ``window_fraction`` compares
    squared (power) sums, so the fixture solves for the amplitude that yields
    the requested power fraction rather than passing it through directly.
    """

    def _levels(f_low: float) -> tuple[float, float, float, float]:
        low = 1.0
        rest = ((1.0 - f_low) / (3.0 * f_low)) ** 0.5 * low
        return low, rest, rest, rest

    out = _analysis_with_bands(*_levels(f_low_out), duration=240.0)
    inc = _analysis_with_bands(*_levels(f_low_in), duration=240.0)
    return out, inc


def _mid_pair(f_mid_out: float, f_mid_in: float) -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """
    Build an outgoing/incoming pair whose mid band carries the given power fractions.

    Low band is fixed bass-light on both sides so the low swap never engages,
    isolating the mid gate. Mid bands are constant over time, so ``duty_mid``
    is trivially 1.0 (every window bar sits at the track's own reference).
    """

    def _levels(f_mid: float) -> tuple[float, float, float, float]:
        mid = 1.0
        low = 0.05**0.5 * mid  # low power fraction ~0.05: well under the bass-swap gate
        rest = (((1.0 - 0.05 - f_mid) / 2.0) / f_mid) ** 0.5 * mid
        return low, rest, mid, rest

    out = _analysis_with_bands(*_levels(f_mid_out), duration=240.0)
    inc = _analysis_with_bands(*_levels(f_mid_in), duration=240.0)
    return out, inc


def _rich_pair() -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """
    Bass-rich AND mid-heavy pair: both the low and mid swap engage.

    Its combined dip sits inside the low-swap notch, which the dip guard
    exempts (the intentional handover gesture), so its schedules are never
    remediated.
    """
    out = _analysis_with_bands(1.0, 0.3, 1.0, 0.3, duration=240.0)
    inc = _analysis_with_bands(1.0, 0.3, 1.0, 0.3, duration=240.0)
    return out, inc


class TestMeasuredMidSwap:
    """The mid-band (vocal) handover is gated by measured mid-band content."""

    def test_vocal_vs_vocal_engages_at_full_depth(self) -> None:
        """Both sides mid-heavy: mid swap engages at the -8dB cap."""
        out, inc = _mid_pair(0.5, 0.5)
        plan = _plan(out, inc)
        assert plan.eq_plan.mid_out is not None
        assert plan.eq_plan.mid_in is not None
        assert plan.eq_plan.mid_out.steps[-1][1] == pytest.approx(-8.0, abs=0.2)
        assert plan.eq_plan.mid_in.steps[0][1] == pytest.approx(-8.0, abs=0.2)
        assert plan.eq_plan.mid_out.steps[0][1] == pytest.approx(0.0)
        assert plan.eq_plan.mid_in.steps[-1][1] == pytest.approx(0.0)
        assert plan.eq_plan.mid_out.shelf_type == ShelfType.PEAK
        assert plan.eq_plan.mid_out.frequency == 1200

    def test_mid_corridor_fraction_scales_the_depth(self) -> None:
        """F_mid at the corridor midpoint (0.24, smoothstep 0.5) scales the depth to -4dB."""
        # the fixture's fixed bass-light term makes F_mid = f/(0.05f+0.95),
        # so 0.231 lands the measured fraction on the 0.18-0.30 midpoint
        out, inc = _mid_pair(0.231, 0.231)
        plan = _plan(out, inc)
        assert plan.eq_plan.mid_out is not None
        assert plan.eq_plan.mid_out.steps[-1][1] == pytest.approx(-4.0, abs=0.3)

    def test_mid_schedule_mirrors_low_schedule_geometry(self) -> None:
        """Mid ramps share start/end timing with the low swap (scaled gain only)."""
        out, inc = _rich_pair()
        plan = _plan(out, inc)
        eq = plan.eq_plan
        assert eq.low_out is not None
        assert eq.mid_out is not None
        assert [t for t, _ in eq.low_out.steps] == pytest.approx([t for t, _ in eq.mid_out.steps])
        assert eq.low_in is not None
        assert eq.mid_in is not None
        assert [t for t, _ in eq.low_in.steps] == pytest.approx([t for t, _ in eq.mid_in.steps])

    def test_instrumental_pair_bypasses_mid_swap(self) -> None:
        """Both sides mid-light (F_mid under the 0.18 gate floor): mid swap stays bypassed."""
        out, inc = _mid_pair(0.12, 0.12)
        plan = _plan(out, inc)
        assert plan.eq_plan.mid_out is None
        assert plan.eq_plan.mid_in is None

    def test_one_sided_mid_bypasses_via_min_gate(self) -> None:
        """One side mid-light: the min(score_A, score_B) gate bypasses both."""
        out, inc = _mid_pair(0.5, 0.12)
        plan = _plan(out, inc)
        assert plan.eq_plan.mid_out is None
        assert plan.eq_plan.mid_in is None

    def test_missing_profile_on_either_side_bypasses_mid_swap(self) -> None:
        """No band data on either side: mid swap stays bypassed (no shipped mid filter)."""
        plan = _plan(_analysis(120.0, duration=240.0), _analysis(120.0, duration=240.0))
        assert plan.eq_plan.mid_out is None
        assert plan.eq_plan.mid_in is None

    def test_a_mid_never_starts_earlier_than_a_low_when_both_exist(self) -> None:
        """Constraint: A's mid ramp never starts before A's low ramp when both engage."""
        out, inc = _rich_pair()
        plan = _plan(out, inc)
        # engagement asserted first so this test can never silently vacuate
        assert plan.eq_plan.low_out is not None
        assert plan.eq_plan.mid_out is not None
        low_start = next(t for t, g in plan.eq_plan.low_out.steps if g != 0.0)
        mid_start = next(t for t, g in plan.eq_plan.mid_out.steps if g != 0.0)
        assert mid_start >= low_start - 1e-6


class TestReciprocalBassSwapDepth:
    """Each deck's low-shelf kill scales with the OTHER deck's measured bass."""

    def test_both_bass_rich_matches_full_depth_plan(self) -> None:
        """Both decks bass-rich: the EQ schedules' gain endpoints stay at full depth."""
        out, inc = _bands_pair(0.4, 0.4)
        plan = _plan(out, inc)
        assert plan.eq_plan.low_out is not None
        assert plan.eq_plan.low_in is not None
        assert plan.eq_plan.low_out.steps[-1][1] == pytest.approx(-26.0)
        assert plan.eq_plan.low_in.steps[0][1] == pytest.approx(-26.0)
        assert plan.eq_plan.low_in.steps[-1][1] == pytest.approx(0.0)
        assert plan.eq_plan.low_out.steps[0][1] == pytest.approx(0.0)

    def test_bass_light_incoming_bypasses_outgoing_low_shelf(self) -> None:
        """B bass-light: A's low-shelf kill is bypassed (no low filter on A)."""
        out, inc = _bands_pair(0.4, 0.05)
        plan = _plan(out, inc)
        assert plan.eq_plan.low_out is None
        assert plan.eq_plan.low_in is not None

    def test_both_bass_light_bypasses_both_low_shelves(self) -> None:
        """Both decks bass-light: no low filters at all (pure equal-power crossfade)."""
        out, inc = _bands_pair(0.05, 0.05)
        plan = _plan(out, inc)
        assert plan.eq_plan.low_out is None
        assert plan.eq_plan.low_in is None

    def test_mid_corridor_bass_scales_the_depth(self) -> None:
        """A's kill depth follows B's mid-corridor f_low (~0.175, smoothstep 0.5) to -13 dB."""
        out, inc = _bands_pair(0.4, 0.175)
        plan = _plan(out, inc)
        assert plan.eq_plan.low_out is not None
        assert plan.eq_plan.low_out.steps[-1][1] == pytest.approx(-13.0, abs=0.5)

    def test_missing_profile_on_either_side_keeps_full_depth(self) -> None:
        """No band data on either side falls back to the shipped full-depth kill."""
        plan = _plan(_analysis(120.0, duration=240.0), _analysis(120.0, duration=240.0))
        assert plan.eq_plan.low_out is not None
        assert plan.eq_plan.low_in is not None
        assert plan.eq_plan.low_out.steps[-1][1] == pytest.approx(-26.0)
        assert plan.eq_plan.low_in.steps[0][1] == pytest.approx(-26.0)

    def test_swap_at_always_set_even_when_both_bypassed(self) -> None:
        """swap_at stays populated regardless of whether the low shelves bypass."""
        out, inc = _bands_pair(0.05, 0.05)
        plan = _plan(out, inc)
        assert plan.eq_plan.swap_at > 0.0


def _high_pair(f_high_out: float, f_high_in: float) -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """
    Build a pair whose high band carries the given power fractions.

    Same amplitude-solving approach as ``_bands_pair``/``_mid_pair``, targeting
    the high band. Flat envelopes, so each side's own window level always
    equals its own reference (the "dark own side" case needs a time-varying
    fixture instead, see ``_dark_own_side_pair``).
    """

    def _levels(f_high: float) -> tuple[float, float, float, float]:
        high = 1.0
        rest = ((1.0 - f_high) / (3.0 * f_high)) ** 0.5 * high
        return rest, rest, rest, high

    out = _analysis_with_bands(*_levels(f_high_out), duration=240.0)
    inc = _analysis_with_bands(*_levels(f_high_in), duration=240.0)
    return out, inc


def _dark_own_side_pair() -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """Build A: bright overall but its own outgoing window (last bars) gone dark; B: bright."""
    duration = 240.0
    t = np.linspace(0, duration, 1800)
    high_out_env = np.where(t < 220.0, 1.0, 0.05).astype(np.float32)
    out = _analysis_with_bands(0.577, 0.577, 0.577, high_out_env, duration=duration)
    _, inc = _high_pair(0.5, 0.5)
    return out, inc


def _wash_pair(*, level_gap: bool = False) -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """
    Both sides bright (high duty 1.0) on a long enough blend, sized for wash mode.

    With ``level_gap=True``, B's non-high bands are boosted well outside its
    own incoming window (media >= 150s) so its loudness-referenced high level
    drops >6dB below A's while B's window-local high duty/fraction (0-16s) are
    untouched — B looks bright locally but the mix as a whole is far quieter,
    the "loudness normalization gone wrong" case the wash gate must reject.
    """
    out, _ = _high_pair(0.8, 0.8)
    if not level_gap:
        _, inc = _high_pair(0.8, 0.8)
        return out, inc
    duration = 240.0
    t = np.linspace(0, duration, 1800)
    low_mid = np.where(t >= 150.0, 3.0, 0.577).astype(np.float32)
    mid = np.where(t >= 150.0, 3.0, 0.577).astype(np.float32)
    inc = _analysis_with_bands(0.577, low_mid, mid, 1.0, duration=duration)
    return out, inc


class TestHighShelfReciprocalAndWash:
    """The high-ease shelf makes room only for highs the other deck actually brings."""

    def test_dark_incoming_keeps_outgoing_air(self) -> None:
        """B has almost no highs: A's post-swap duck is bypassed (A keeps its air)."""
        out, inc = _high_pair(0.5, 0.005)
        plan = _plan(out, inc)
        assert plan.eq_plan.high_out is None
        # B's own restore still follows A's (bright) gate
        assert plan.eq_plan.high_in is not None
        assert plan.eq_plan.high_in.steps[0][1] == pytest.approx(-20.0, abs=0.5)

    def test_dark_own_side_emits_no_shelf_regardless_of_other_side(self) -> None:
        """A's own outgoing window has gone dark: no high shelf on A, whatever B brings."""
        out, inc = _dark_own_side_pair()
        plan = _plan(out, inc)
        assert plan.eq_plan.high_out is None

    def test_wash_mode_deepens_and_mirrors_b_restore(self) -> None:
        """Wash mode: A's duck deepens to -26 over the SAME rendered window as B's restore."""
        out, inc = _wash_pair()
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        bar_out = 4 * 60.0 / 120.0
        assert plan.crossfade_duration >= 8 * bar_out
        eq = plan.eq_plan
        assert eq.high_out is not None
        assert eq.high_in is not None
        assert eq.high_out.steps[-1][1] == pytest.approx(-26.0, abs=0.2)
        # complementarity: A's highs fall in lockstep while B's rise, so the
        # summed high energy stays ~flat; steps[0] is the t=0 pin, the ramp
        # itself starts at steps[1] — high_out is A INPUT time, high_in is
        # rendered (B post-trim) time, so map A's ramp back before comparing
        ratio = 1.0  # equal BPMs on both sides -> no tempo stretch
        cf_start_input = planner.effective_end - plan.crossfade_duration * ratio
        duck_start_rendered = (eq.high_out.steps[1][0] - cf_start_input) / ratio
        duck_end_rendered = (eq.high_out.steps[-1][0] - cf_start_input) / ratio
        assert duck_start_rendered == pytest.approx(eq.high_in.steps[1][0], abs=0.2)
        assert duck_end_rendered == pytest.approx(eq.high_in.steps[-1][0], abs=0.2)

    def test_wash_duck_never_overruns_the_crossfade_end(self) -> None:
        """With a late-clamped swap point, the wash duck stays inside the overlap."""
        out, inc = _wash_pair()
        # B's full-band energy steps in late (14s) so the swap point clamps
        # against the overlap end — the reviewer's overrun reproduction
        t = np.linspace(0, 240.0, 1800)
        inc.rms_energy = np.where(t < 14.0, 0.05, 0.5).astype(np.float32).tolist()
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        eq = plan.eq_plan
        assert eq.high_out is not None
        assert eq.high_out.steps[-1][1] == pytest.approx(-26.0, abs=0.2)  # wash engaged
        # A INPUT time: the duck must complete at or before the crossfade end
        assert eq.high_out.steps[-1][0] <= planner.effective_end + 1e-6

    def test_wash_mode_skipped_when_levels_differ_more_than_6db(self) -> None:
        """Both sides bright but loudness-referenced levels differ >6dB: normal reciprocal ease."""
        out, inc = _wash_pair(level_gap=True)
        plan = _plan(out, inc)
        eq = plan.eq_plan
        assert eq.high_out is not None
        # not the wash depth
        assert eq.high_out.steps[-1][1] != pytest.approx(-26.0, abs=0.2)

    def test_missing_profile_matches_shipped_high_schedule(self) -> None:
        """No band data on either side: high schedules are byte-identical to the shipped ease."""
        plan = _plan(_analysis(120.0, duration=240.0), _analysis(120.0, duration=240.0))
        eq = plan.eq_plan
        assert eq.high_out is not None
        assert eq.high_in is not None
        assert eq.high_out.steps[-1][1] == pytest.approx(-20.0)
        assert eq.high_in.steps[0][1] == pytest.approx(-20.0)
        assert eq.high_out.steps[-1][0] == pytest.approx(47.0, abs=0.1)
        assert eq.high_in.steps[-1][0] == pytest.approx(6.0, abs=0.1)

    def test_mid_corridor_incoming_scales_duck_to_half_depth(self) -> None:
        """B's F_high in the gate's mid-corridor (~0.04) scales A's duck to ~-10dB."""
        out, inc = _high_pair(0.5, 0.04)
        plan = _plan(out, inc)
        assert plan.eq_plan.high_out is not None
        assert plan.eq_plan.high_out.steps[-1][1] == pytest.approx(-10.0, abs=0.5)

    def test_dark_steady_pair_never_washes(self) -> None:
        """Duty 1.0 on both sides but F_high 0.03 (dark): the brightness floor blocks wash."""
        # duty measures consistency vs a track's OWN reference — a dark steady
        # track scores 1.0, which is exactly what the absolute floor exists for
        out, inc = _high_pair(0.03, 0.03)
        plan = _plan(out, inc)
        # wash would force A's duck to -26; instead the reciprocal ease applies
        # and at F_high 0.03 it gates shallow enough to bypass entirely
        assert plan.eq_plan.high_out is None


def _gain_at(
    schedule: object, t: float, *, cf_start_input: float, ratio: float, side: str
) -> float:
    """Interpolate a ShelfSchedule's gain (dB) at rendered time ``t``; 0.0 when bypassed."""
    if schedule is None:
        return 0.0
    tt = cf_start_input + t * ratio if side == "A" else t
    steps = schedule.steps  # type: ignore[attr-defined]
    if tt <= steps[0][0]:
        return float(steps[0][1])
    if tt >= steps[-1][0]:
        return float(steps[-1][1])
    for (t0, g0), (t1, g1) in itertools.pairwise(steps):
        if t0 <= tt <= t1:
            frac = (tt - t0) / (t1 - t0) if t1 > t0 else 0.0
            return float(g0 + frac * (g1 - g0))
    return float(steps[-1][1])


def _predicted_power_curve(
    planner: SmartCrossFadePlanner, plan: TransitionPlan, n_samples: int = 200
) -> list[tuple[float, float]]:
    """
    Sample the predicted combined power curve P(t) across the overlap.

    Mirrors the plan-time dip guard's own math: reuses the reciprocal-gate
    windows' band fractions and combines each deck's scheduled gain with the
    qsin equal-power crossfade weights. Independent re-derivation from the
    plan's public schedules, not a call into the guard itself, so the test
    doesn't just check the implementation against itself.
    """
    eq = plan.eq_plan
    duration = plan.crossfade_duration
    ratio = plan.tempo_plan.steps[-1][1] if plan.tempo_plan else 1.0
    cf_start_input = planner.effective_end - duration * ratio
    w_a_out, w_b_in = planner._swap_windows(planner.bass_swap_window_bars)
    assert planner.outgoing_profile is not None
    assert planner.incoming_profile is not None
    f_a = {
        band: window_fraction(planner.outgoing_profile, band, *w_a_out)
        for band in ("low", "low_mid", "mid", "high")
    }
    f_b = {
        band: window_fraction(planner.incoming_profile, band, *w_b_in)
        for band in ("low", "low_mid", "mid", "high")
    }
    schedules_a = {"low": eq.low_out, "mid": eq.mid_out, "high": eq.high_out}
    schedules_b = {"low": eq.low_in, "mid": eq.mid_in, "high": eq.high_in}

    curve = []
    for i in range(n_samples + 1):
        t = duration * i / n_samples
        w_a = math.cos(math.pi / 2 * t / duration) ** 2
        w_b = math.sin(math.pi / 2 * t / duration) ** 2
        p_a = sum(
            f_a[band]
            * 10
            ** (
                _gain_at(
                    schedules_a.get(band), t, cf_start_input=cf_start_input, ratio=ratio, side="A"
                )
                / 10
            )
            for band in ("low", "low_mid", "mid", "high")
        )
        p_b = sum(
            f_b[band]
            * 10
            ** (
                _gain_at(
                    schedules_b.get(band), t, cf_start_input=cf_start_input, ratio=ratio, side="B"
                )
                / 10
            )
            for band in ("low", "low_mid", "mid", "high")
        )
        curve.append((t, w_a * p_a + w_b * p_b))
    return curve


def _max_plateau_to_valley_drop_db(
    curve: list[tuple[float, float]], exclude: tuple[float, float] | None = None
) -> float:
    """
    Max running-max-to-current drop in dB across a sampled power curve (max-drawdown).

    :param curve: Sampled ``(t, power)`` points across the overlap.
    :param exclude: Rendered interval to skip (the exempt low-swap notch), if any.
    """
    max_drop_db = 0.0
    running_max = 0.0
    for t, p in curve:
        if exclude is not None and exclude[0] <= t <= exclude[1]:
            continue
        running_max = max(running_max, p)
        if running_max > 0.0 and p > 0.0:
            max_drop_db = max(max_drop_db, 10.0 * math.log10(running_max / p))
    return max_drop_db


def _wash_mid_stack_pair() -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """
    Bright + vocal pair whose dip lands OUTSIDE the low-swap notch.

    Both sides bright enough for wash mode (F_high 0.45) and mid-heavy enough
    for the vocal swap (F_mid 0.42), bass-light (low swap bypassed, so the
    exempt notch contains no low crossing). B's groove enters late (14s) so
    the swap window sits at the end of the overlap; the wash duck mirrors B's
    high restore BEFORE it, where it stacks with B's pinned mid kill — a
    >3dB combined dip outside the notch, recoverable by shrinking the mid.
    """
    amps = (0.065**0.5, 0.065**0.5, 0.42**0.5, 0.45**0.5)
    out = _analysis_with_bands(*amps, duration=240.0)
    inc = _analysis_with_bands(*amps, duration=240.0)
    t = np.linspace(0, 240.0, 1800)
    inc.rms_energy = np.where(t < 14.0, 0.05, 0.5).astype(np.float32).tolist()
    return out, inc


def _wash_low_stack_pair() -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """
    Like ``_wash_mid_stack_pair`` but with the low swap engaged and a residual dip.

    F_high 0.55 makes the wash stack deep enough that mid bypass alone cannot
    clear the budget, so remediation reaches step 2 (low-ramp steepening) —
    the fixture for the never-shallow-the-low-depth and 2-bar-floor tests.
    F_low 0.15 sits in the reciprocal gate's corridor (partial-depth kill).
    """
    amps = (0.15**0.5, 0.05**0.5, 0.25**0.5, 0.55**0.5)
    out = _analysis_with_bands(*amps, duration=240.0)
    inc = _analysis_with_bands(*amps, duration=240.0)
    t = np.linspace(0, 240.0, 1800)
    inc.rms_energy = np.where(t < 14.0, 0.05, 0.5).astype(np.float32).tolist()
    return out, inc


def _wash_no_low_stack_pair() -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """
    Like ``_wash_low_stack_pair`` but bass-light, so the low swap stays bypassed.

    The deep wash overruns the budget even after mid bypass, so remediation
    reaches step 2 with no low schedules to steepen — the fixture for the
    "step 2 must not fabricate an in-overlap notch exemption" regression.
    """
    amps = (0.05**0.5, 0.05**0.5, 0.25**0.5, 0.55**0.5)
    out = _analysis_with_bands(*amps, duration=240.0)
    inc = _analysis_with_bands(*amps, duration=240.0)
    t = np.linspace(0, 240.0, 1800)
    inc.rms_energy = np.where(t < 14.0, 0.05, 0.5).astype(np.float32).tolist()
    return out, inc


def _unguarded_plan(
    out: AudioAnalysisData, inc: AudioAnalysisData
) -> tuple[SmartCrossFadePlanner, TransitionPlan]:
    """Plan with the dip guard effectively disabled (budget set beyond any real dip)."""
    planner = SmartCrossFadePlanner(LOGGER)
    planner.max_predicted_dip_db = 1000.0
    return planner, planner.plan(out, inc, 45.0)


class TestScaleMidDepth:
    """Scaling the mid depth toward bypass never ships a schedule under the bypass floor."""

    def _plan_with_mid(self, depth: float) -> EqPlan:
        mid_out = ShelfSchedule(ShelfType.PEAK, 1200, [(0.0, 0.0), (5.0, depth), (10.0, depth)])
        mid_in = ShelfSchedule(ShelfType.PEAK, 1200, [(0.0, depth), (5.0, depth), (10.0, 0.0)])
        return EqPlan(
            swap_at=5.0,
            low_out=None,
            low_in=None,
            high_out=None,
            high_in=None,
            mid_out=mid_out,
            mid_in=mid_in,
        )

    def test_shrink_below_bypass_floor_yields_none_not_a_shallow_schedule(self) -> None:
        """Shrinking -2dB by 0.25 (-0.5dB, under the -1dB floor) bypasses instead of shipping it."""
        eq = self._plan_with_mid(-2.0)
        candidate = eq.with_mid_depth_scaled(0.25, bypass_below_db=-1.0)
        assert candidate.mid_out is None
        assert candidate.mid_in is None

    def test_shrink_at_the_bypass_floor_is_kept(self) -> None:
        """Shrinking -2dB by 0.5 (exactly -1dB, at the floor) is kept, not bypassed."""
        eq = self._plan_with_mid(-2.0)
        candidate = eq.with_mid_depth_scaled(0.5, bypass_below_db=-1.0)
        assert candidate.mid_out is not None
        assert candidate.mid_out.steps[-1][1] == pytest.approx(-1.0)


class TestPredictedDipGuard:
    """
    The dip check polices the overlap OUTSIDE the low-swap notch.

    The notch (the low crossover's ramp window) is the intentional,
    ear-approved bass-handover gesture and is exempt — like the
    band-conservation check's notch. Only dips leaking outside it are
    remediated (mid depth first, then low-ramp steepening; the low depth is
    never shallowed).
    """

    def test_outside_notch_dip_reduces_mid_depth(self) -> None:
        """A >3dB dip outside the notch (wash + pinned mid stack) shrinks the mid depth."""
        out, inc = _wash_mid_stack_pair()
        _, unguarded = _unguarded_plan(out, inc)
        assert unguarded.eq_plan.mid_out is not None
        gate_depth = unguarded.eq_plan.mid_out.steps[-1][1]
        assert gate_depth == pytest.approx(-8.0, abs=0.2)

        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        eq = plan.eq_plan
        curve = _predicted_power_curve(planner, plan)
        dip = _max_plateau_to_valley_drop_db(curve, exclude=planner._swap_notch)
        assert dip <= planner.max_predicted_dip_db + 0.2
        # remediation shrank the mid depth (or bypassed it) vs the gate's value
        if eq.mid_out is not None:
            assert abs(eq.mid_out.steps[-1][1]) < abs(gate_depth)
        else:
            assert eq.mid_in is None

    def test_full_depth_house_notch_is_not_remediated(self) -> None:
        """A full-depth bass swap's >3dB in-notch dip is the intentional gesture: untouched."""
        out, inc = _bands_pair(0.7, 0.7)
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        eq = plan.eq_plan
        curve = _predicted_power_curve(planner, plan)
        # the notch itself dips well past the budget...
        assert _max_plateau_to_valley_drop_db(curve) > planner.max_predicted_dip_db
        # ...but outside it the plan is within budget, so nothing is remediated:
        assert _max_plateau_to_valley_drop_db(curve, exclude=planner._swap_notch) <= (
            planner.max_predicted_dip_db
        )
        assert eq.low_out is not None
        assert eq.low_out.steps[-1][1] == pytest.approx(-26.0)
        assert eq.low_in is not None
        assert eq.low_in.steps[0][1] == pytest.approx(-26.0)
        # ramp span keeps the shipped proportional length (half the overlap),
        # clamped to the 8-bar ceiling — no steepening remediation happened
        bar_in = 4 * 60.0 / planner.incoming.bpm
        expected_span = min(plan.crossfade_duration / 2, planner.bass_swap_max_bars * bar_in)
        ramp = eq.low_in.steps[1:]
        assert ramp[-1][0] - ramp[0][0] == pytest.approx(expected_span, rel=0.05)

    def test_normal_house_pair_is_untouched(self) -> None:
        """A normal (mid-bypassed) house pair keeps its shipped schedules unchanged."""
        out, inc = _bands_pair(0.4, 0.4)
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        eq = plan.eq_plan
        curve = _predicted_power_curve(planner, plan)
        assert _max_plateau_to_valley_drop_db(curve, exclude=planner._swap_notch) <= (
            planner.max_predicted_dip_db
        )
        assert eq.mid_out is None
        assert eq.mid_in is None
        assert eq.low_out is not None
        assert eq.low_out.steps[-1][1] == pytest.approx(-26.0)
        assert eq.low_in is not None
        assert eq.low_in.steps[0][1] == pytest.approx(-26.0)

    def test_remediation_never_shallows_the_low_endpoint(self) -> None:
        """Even when remediation reaches the low ramps, their endpoint gains are untouched."""
        out, inc = _wash_low_stack_pair()
        _, unguarded = _unguarded_plan(out, inc)
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        eq, ueq = plan.eq_plan, unguarded.eq_plan
        # remediation fired (mid differs from the unguarded plan)...
        assert (eq.mid_out is None) or (
            ueq.mid_out is not None and abs(eq.mid_out.steps[-1][1]) < abs(ueq.mid_out.steps[-1][1])
        )
        # ...but the low endpoints are bit-identical to the unguarded plan's
        assert eq.low_out is not None
        assert ueq.low_out is not None
        assert eq.low_out.steps[-1][1] == ueq.low_out.steps[-1][1]
        assert eq.low_in is not None
        assert ueq.low_in is not None
        assert eq.low_in.steps[0][1] == ueq.low_in.steps[0][1]

    def test_remediated_low_ramp_steepens_to_the_two_bar_floor(self) -> None:
        """When steepening engages, the ramp lands exactly at the 2-bar floor, never below."""
        out, inc = _wash_low_stack_pair()
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        eq = plan.eq_plan
        assert eq.low_in is not None
        bar_in = 4 * 60.0 / 120.0
        ramp = eq.low_in.steps[1:]
        span = ramp[-1][0] - ramp[0][0]
        assert span == pytest.approx(planner.bass_swap_min_bars * bar_in, rel=0.01)

    def test_notch_narrows_after_low_ramp_steepening(self) -> None:
        """After step-2 remediation, the notch reflects the steepened (2-bar) ramp."""
        out, inc = _wash_low_stack_pair()
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        assert plan.eq_plan.low_in is not None
        ramp = plan.eq_plan.low_in.steps[1:]
        assert planner._swap_notch == pytest.approx((ramp[0][0], ramp[-1][0]), abs=0.01)

    def test_bass_light_pair_gets_no_notch_exemption(self) -> None:
        """No low swap engaged: the notch never exempts a sample (no handover to exempt)."""
        out, inc = _bands_pair(0.05, 0.05)
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        assert plan.eq_plan.low_out is None
        assert plan.eq_plan.low_in is None
        # the notch must not swallow any real sample in [0, crossfade_duration]
        assert not (0.0 <= planner._swap_notch[0] <= plan.crossfade_duration) or not (
            0.0 <= planner._swap_notch[1] <= plan.crossfade_duration
        )

    def test_step2_on_bass_light_pair_keeps_the_sentinel_notch(self) -> None:
        """Reaching step 2 with no low swap must not fabricate an in-overlap notch exemption."""
        out, inc = _wash_no_low_stack_pair()
        _, unguarded = _unguarded_plan(out, inc)
        # the mid swap engaged and remediation had to fire (deep wash dip)...
        assert unguarded.eq_plan.mid_out is not None
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        # ...on a bass-light pair, so there are no low schedules to steepen
        assert plan.eq_plan.low_out is None
        assert plan.eq_plan.low_in is None
        # the notch must stay the sentinel: no real sample in [0, crossfade_duration]
        assert not (0.0 <= planner._swap_notch[0] <= plan.crossfade_duration) or not (
            0.0 <= planner._swap_notch[1] <= plan.crossfade_duration
        )

    def test_dip_guard_is_a_no_op_when_already_within_budget(self) -> None:
        """A plan whose dip is already within budget produces bit-identical schedules."""
        out, inc = _bands_pair(0.4, 0.4)
        plan_a = _plan(out, inc)
        plan_b = _plan(out, inc)
        assert plan_a == plan_b


class TestBandConservation:
    """TEST-ONLY: where both sides pass their gate floor, one deck stays within 6dB of unity."""

    def _conserves(
        self, planner: SmartCrossFadePlanner, plan: TransitionPlan, band: str, gate_floor: float
    ) -> bool:
        """
        Check conservation for one band: True when the invariant holds.

        The exception zone is the swap ramp's own span (derived from the
        engaging schedule's steps), not a fixed 1-bar constant: a reciprocal
        swap symmetric around ``swap_at`` necessarily drives BOTH sides away
        from unity across its whole ramp (that is the handover), so a fixed
        ≤1-bar notch as literally read in the plan would be violated by any
        shipped ramp longer than ~1 bar (this pair's ramps are 2-8 bars by
        design, see ``bass_swap_min_bars``/``bass_swap_max_bars``). Outside
        the ramp, one side always sits at its 0dB/unengaged endpoint, so the
        invariant holds trivially there.
        """
        eq = plan.eq_plan
        ratio = plan.tempo_plan.steps[-1][1] if plan.tempo_plan else 1.0
        duration = plan.crossfade_duration
        cf_start_input = planner.effective_end - duration * ratio
        w_a_out, w_b_in = planner._swap_windows(planner.bass_swap_window_bars)
        assert planner.outgoing_profile is not None
        assert planner.incoming_profile is not None
        f_a = window_fraction(planner.outgoing_profile, band, *w_a_out)
        f_b = window_fraction(planner.incoming_profile, band, *w_b_in)
        if f_a < gate_floor or f_b < gate_floor:
            return True  # gate floor not cleared on both sides: invariant N/A
        schedule_a = {"low": eq.low_out, "mid": eq.mid_out, "high": eq.high_out}[band]
        schedule_b = {"low": eq.low_in, "mid": eq.mid_in, "high": eq.high_in}[band]
        if schedule_a is None or schedule_b is None:
            return True  # one side bypassed: no double-engagement possible
        # B's schedule is already in rendered time; its ramp span IS the swap window
        ramp_start, ramp_end = schedule_b.steps[0][0], schedule_b.steps[-1][0]
        n_samples = 200
        for i in range(n_samples + 1):
            t = duration * i / n_samples
            g_a = _gain_at(schedule_a, t, cf_start_input=cf_start_input, ratio=ratio, side="A")
            g_b = _gain_at(schedule_b, t, cf_start_input=cf_start_input, ratio=ratio, side="B")
            in_ramp = ramp_start <= t <= ramp_end
            if in_ramp:
                continue
            if abs(g_a) > 6.0 and abs(g_b) > 6.0:
                return False
        return True

    def test_holds_for_bass_rich_plan(self) -> None:
        """Representative engaged plan: both bass-rich, full low swap engaged."""
        out, inc = _bands_pair(0.4, 0.4)
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        assert self._conserves(planner, plan, "low", planner.low_gate_lo)

    def test_holds_for_mid_and_low_engaged_plan(self) -> None:
        """Representative engaged plan: both low and mid swaps engaged (_rich_pair)."""
        out, inc = _rich_pair()
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        assert self._conserves(planner, plan, "low", planner.low_gate_lo)
        assert self._conserves(planner, plan, "mid", planner.mid_gate_lo)

    def test_holds_for_wash_mode_plan(self) -> None:
        """Representative engaged plan: cymbal-wash mode on the high band."""
        out, inc = _wash_pair()
        planner = SmartCrossFadePlanner(LOGGER)
        plan = planner.plan(out, inc, 45.0)
        assert self._conserves(planner, plan, "high", planner.high_gate_lo)
