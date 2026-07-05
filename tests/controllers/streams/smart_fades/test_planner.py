"""Tests for SmartCrossFadePlanner — the pure decision step of smart fades."""

from __future__ import annotations

import logging

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.filters import ShelfType
from music_assistant.controllers.streams.smart_fades.helpers import SMART_CROSSFADE_DURATION
from music_assistant.controllers.streams.smart_fades.models import (
    SmartFadeNotApplicable,
    TransitionPlan,
)
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner
from music_assistant.models.audio_analysis import AudioAnalysisData

LOGGER = logging.getLogger(__name__)


def _beats(start: float, count: int, interval: float) -> np.ndarray:
    return np.arange(count, dtype=np.float32) * interval + start


def _analysis(
    bpm: float,
    duration: float = 240.0,
    rms_energy: np.ndarray | None = None,
) -> AudioAnalysisData:
    interval = 60.0 / bpm
    count = int(duration / interval) + 1
    beats = _beats(0.0, count, interval)
    return AudioAnalysisData(
        duration=duration,
        bpm=bpm,
        beats=beats,
        downbeats=beats[::4],
        rms_energy=rms_energy,
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

    def test_swap_lands_on_incoming_groove_entry(self) -> None:
        """B's groove entry inside the overlap becomes the bass-swap moment."""
        inc = _analysis(120.0, duration=240.0)
        bins = np.full(1800, 0.5, dtype=np.float32)
        t = np.linspace(0, 240.0, 1800)
        bins[t < 8.0] = 0.05
        inc.rms_energy = bins
        plan = _plan(_analysis(120.0, duration=240.0), inc)
        trim = plan.fadein_trim_start or 0.0
        assert plan.eq_plan.swap_at == pytest.approx(8.0 - trim, abs=0.1)

    def test_swap_window_fits_inside_the_overlap(self) -> None:
        """A late swap point never pushes the low ramps past the crossfade end."""
        inc = _analysis(120.0, duration=240.0)
        bins = np.full(1800, 0.5, dtype=np.float32)
        t = np.linspace(0, 240.0, 1800)
        bins[t < 20.0] = 0.05
        inc.rms_energy = bins
        plan = _plan(_analysis(120.0, duration=240.0), inc)
        # B's bass must be fully restored before the rendered mix ends
        assert plan.eq_plan.low_in.steps[-1][0] <= plan.crossfade_duration + 1e-6
        # A's bass kill must complete before A's audible end
        assert plan.eq_plan.low_out.steps[-1][0] <= plan.fade_out_window + 1e-6

    def test_bass_swap_is_proportional_and_centered(self) -> None:
        """The low exchange spans half the overlap (clamped) centered on the swap."""
        plan = _plan(_analysis(120.0, duration=240.0), _analysis(120.0, duration=240.0))
        bar = 4 * 60.0 / 120.0
        expected = min(max(plan.crossfade_duration / 2, 2 * bar), 8 * bar)
        ramp = plan.eq_plan.low_in.steps[1:]  # steps[0] is the (0.0, kill) pin
        span = ramp[-1][0] - ramp[0][0]
        assert span == pytest.approx(expected, rel=0.05)
        midpoint = (ramp[0][0] + ramp[-1][0]) / 2
        assert midpoint == pytest.approx(plan.eq_plan.swap_at, abs=0.2)
