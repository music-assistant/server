"""Tests for build_transition_context — the immutable per-transition fact extraction."""

from __future__ import annotations

import dataclasses
import logging

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import (
    QuickFadeTrigger,
    SmartFadeNotApplicable,
    TransitionStyle,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner
from music_assistant.controllers.streams.smart_fades.planner.context import (
    TransitionContext,
    build_transition_context,
    choose_tier,
)
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
    # tests that need something else override these
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


def _context(
    fade_out: AudioAnalysisData, fade_in: AudioAnalysisData, buffer: float = 45.0
) -> TransitionContext:
    return build_transition_context(fade_out, fade_in, buffer, LOGGER)


def _vocal_probabilities(duration: float, active_windows: list[tuple[float, float]]) -> list[float]:
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
    analysis: AudioAnalysisData, active_windows: list[tuple[float, float]]
) -> AudioAnalysisData:
    """Attach a valid vocal_activity list, active only inside ``active_windows``."""
    assert analysis.duration is not None
    analysis.vocal_activity = _vocal_probabilities(analysis.duration, active_windows)
    return analysis


def _with_meter(analysis: AudioAnalysisData, beats_per_bar: int) -> AudioAnalysisData:
    """Set the track's time signature numerator."""
    analysis.beats_per_bar = beats_per_bar
    return analysis


def _with_irregular_downbeats(analysis: AudioAnalysisData) -> AudioAnalysisData:
    """Shift every other downbeat so the bar intervals alternate well past the 0.1s std limit."""
    downbeats = np.asarray(analysis.downbeats, dtype=np.float32)
    downbeats[1::2] += 0.3
    analysis.downbeats = downbeats.tolist()
    return analysis


def _with_downbeats_before(analysis: AudioAnalysisData, end: float) -> AudioAnalysisData:
    """Drop every downbeat from ``end`` on, leaving the buffered tail a few downbeats only."""
    analysis.downbeats = [downbeat for downbeat in analysis.downbeats or [] if downbeat < end]
    return analysis


def test_context_energy_only_when_vocal_data_missing() -> None:
    """Analyses without a stored vocal-activity timeline disable all vocal-aware masks."""
    context = _context(_analysis(120.0), _analysis(120.0))

    assert context.vocal_out_placement is None
    assert context.vocal_in_placement is None
    assert context.vocal_out_scoring is None
    assert context.vocal_in_scoring is None


def test_context_builds_outgoing_masks_when_only_outgoing_timeline_is_valid() -> None:
    """A validated outgoing-only timeline builds the outgoing masks, leaving incoming ones None."""
    out = _with_vocal_activity(_analysis(120.0), [(220.0, 226.0)])
    context = _context(out, _analysis(120.0))

    assert context.vocal_out_placement is not None
    assert context.vocal_out_scoring is not None
    assert context.vocal_in_placement is None
    assert context.vocal_in_scoring is None


def test_context_builds_incoming_masks_when_only_incoming_timeline_is_valid() -> None:
    """A validated incoming-only timeline builds the incoming masks, leaving outgoing ones None."""
    inc = _with_vocal_activity(_analysis(120.0), [(10.0, 16.0)])
    context = _context(_analysis(120.0), inc)

    assert context.vocal_in_placement is not None
    assert context.vocal_in_scoring is not None
    assert context.vocal_out_placement is None
    assert context.vocal_out_scoring is None


def test_context_all_masks_none_when_both_timelines_missing() -> None:
    """Neither side carrying a timeline still yields all four masks as None (unchanged)."""
    context = _context(_analysis(120.0), _analysis(120.0))

    assert context.vocal_out_placement is None
    assert context.vocal_in_placement is None
    assert context.vocal_out_scoring is None
    assert context.vocal_in_scoring is None


def test_context_tier_full_blend_for_compatible_pair() -> None:
    """A clean, key-compatible, same-tempo 4/4 pair earns the full-blend tier."""
    # A minor and C major are relative major/minor: the same Camelot slot
    context = _context(
        _analysis(120.0, key="A", mode="minor"), _analysis(120.0, key="C", mode="major")
    )

    assert context.tier is TransitionTier.FULL_BLEND


@pytest.mark.parametrize(
    ("fade_out", "fade_in", "trigger"),
    [
        pytest.param(
            _analysis(120.0), _with_meter(_analysis(120.0), 3), QuickFadeTrigger.METER, id="meter"
        ),
        pytest.param(
            _with_meter(_analysis(120.0), 3),
            _analysis(150.0),
            QuickFadeTrigger.METER,
            id="meter-before-tempo",
        ),
        pytest.param(
            _with_irregular_downbeats(_analysis(120.0)),
            _analysis(120.0),
            QuickFadeTrigger.BEAT_GRID,
            id="irregular-grid",
        ),
        pytest.param(
            _with_downbeats_before(_analysis(120.0), 200.0),
            _analysis(120.0),
            QuickFadeTrigger.BEAT_GRID,
            id="few-downbeats",
        ),
        pytest.param(
            _with_irregular_downbeats(_analysis(120.0)),
            _analysis(150.0),
            QuickFadeTrigger.TEMPO,
            id="tempo-before-grid",
        ),
        pytest.param(_analysis(120.0), _analysis(150.0), QuickFadeTrigger.TEMPO, id="tempo"),
    ],
)
def test_context_records_the_quick_fade_trigger(
    fade_out: AudioAnalysisData, fade_in: AudioAnalysisData, trigger: QuickFadeTrigger
) -> None:
    """A quick fade context names the first check, in tier order, that ruled out a blend."""
    context = _context(fade_out, fade_in)

    assert context.tier is TransitionTier.QUICK_FADE
    assert context.quick_fade_trigger is trigger
    # the candidate builders' tier call agrees with the context's
    assert choose_tier(context.outgoing, context.incoming, context.default_anchor) == (
        context.cross_meter,
        context.tier,
    )


@pytest.mark.parametrize(
    ("in_key", "in_mode", "tier"),
    [
        pytest.param("C", "major", TransitionTier.FULL_BLEND, id="full-blend"),
        pytest.param("F#", "major", TransitionTier.TEMPO_BLEND, id="tempo-blend"),
    ],
)
def test_context_blend_has_no_quick_fade_trigger(
    in_key: str, in_mode: str, tier: TransitionTier
) -> None:
    """A blend tier carries no quick fade trigger."""
    context = _context(_analysis(120.0), _analysis(120.0, key=in_key, mode=in_mode))

    assert context.tier is tier
    assert context.quick_fade_trigger is None


def test_context_mix_out_anchor_is_downbeat_snapped() -> None:
    """An outro whose energy decays below the mix-out floor anchors at that (downbeat) point."""
    bins = np.full(1800, 0.5, dtype=np.float32)
    t = np.linspace(0, 240.0, 1800)
    bins[t >= 220.0] = 0.2  # audible but below 0.7*sustained: mixed out, not silent

    context = _context(_analysis(120.0, rms_energy=bins), _analysis(120.0))

    # anchored at the mix-out point (media 220 -> buffer-local 25), well before the
    # RMS-audible boundary, and snapped to a real downbeat
    anchor = context.mix_out_anchor
    assert anchor is not None
    assert anchor == pytest.approx(25.0, abs=0.3)
    assert context.audio_end > anchor + 1.0
    assert float(np.min(np.abs(context.outgoing.downbeats - anchor))) < 0.05


def test_context_quiet_audible_outro_anchors_at_its_audible_end() -> None:
    """An outro that stays audible but under the mix-out floor is a quiet outro, not an error."""
    bins = np.full(1800, 0.5, dtype=np.float32)
    t = np.linspace(0, 240.0, 1800)
    bins[t >= 190.0] = 0.2  # audible but below 0.7*sustained for the whole buffered tail

    context = _context(_analysis(120.0, rms_energy=bins), _analysis(120.0))

    assert context.quiet_outro
    assert context.audio_end == pytest.approx(45.0)
    assert context.default_anchor == pytest.approx(context.audio_end)


def test_context_ordinary_outro_is_no_quiet_outro() -> None:
    """A tail at its sustained level up to the end anchors as usual."""
    assert not _context(_analysis(120.0), _analysis(120.0)).quiet_outro


def test_context_silent_tail_reports_mostly_silent() -> None:
    """A tail that goes silent early is reported as silent, with its audible length."""
    bins = np.full(1800, 0.5, dtype=np.float32)
    t = np.linspace(0, 240.0, 1800)
    bins[t >= 200.0] = 0.001  # silent from buffer-local 5s on

    with pytest.raises(
        SmartFadeNotApplicable, match=r"^outgoing tail is mostly silent \(5\.0s audible\)$"
    ):
        _context(_analysis(120.0, rms_energy=bins), _analysis(120.0))


def test_context_is_frozen() -> None:
    """TransitionContext is immutable: attribute assignment raises."""
    context = _context(_analysis(120.0), _analysis(120.0))

    with pytest.raises(dataclasses.FrozenInstanceError):
        context.buffer_duration = 999.0  # type: ignore[misc]


def test_context_tier_folds_kick_anchor_like_the_old_planner() -> None:
    """
    A kick drop that shortens the anchored tail demotes the tier, matching the old planner.

    The old planner folded the kick anchor into effective_end before masking
    the downbeat grid its blendability check reads; the context must reproduce
    that even though it keeps mix_out_anchor and kick_anchor as separate facts.
    """

    def env(value: float) -> np.ndarray:
        return np.full(1800, value, dtype=np.float32)

    t = np.linspace(0, 240.0, 1800)
    low = env(1.0)
    low[t >= 208.0] = 0.05  # kick dies at media 208 -> kick anchor at buffer-local 11
    out = _analysis_with_bands(low, env(0.5), env(0.5), env(0.3))
    rms = np.full(1800, 0.5, dtype=np.float32)
    rms[t >= 225.0] = 0.2  # full-band mix-out at buffer-local ~30, audible to the end
    out.rms_energy = rms.tolist()
    inc = _analysis_with_bands(env(1.0), env(0.5), env(0.5), env(0.3))

    context = _context(out, inc)

    # both anchors are separate facts on the context...
    assert context.kick_anchor == pytest.approx(11.0, abs=0.1)
    assert context.mix_out_anchor == pytest.approx(29.0, abs=2.0)
    # ...but the tier keys on the kick-folded window: only 6 downbeats fit
    # before the kick anchor, so the tail is not blendable
    assert context.tier is TransitionTier.QUICK_FADE
    # the early kick-folded anchor strands a large audible gap behind it, so
    # trim-closing directly re-anchors at the audible boundary itself
    # (re-deriving the tier there) - no rescue-pass fallback needed
    plan = SmartCrossFadePlanner(LOGGER).plan(out, inc, 45.0)
    assert plan.tier is TransitionTier.FULL_BLEND
    assert plan.fade_out_window == pytest.approx(context.audio_end, abs=0.1)
    assert plan.metrics.audible_outgoing_trim <= plan.crossfade_duration


def _quiet_from(start: float, duration: float = 240.0) -> np.ndarray:
    """Build a 1800-bin rms array that is loud before media ``start`` and quiet after."""
    bins = np.full(1800, 0.5, dtype=np.float32)
    bins[np.arange(1800) * (duration / 1800) >= start] = 0.1
    return bins


def _quiet_until(end: float, duration: float = 240.0) -> np.ndarray:
    """Build a 1800-bin rms array that is quiet before media ``end`` and loud after."""
    bins = np.full(1800, 0.5, dtype=np.float32)
    bins[np.arange(1800) * (duration / 1800) < end] = 0.05
    return bins


class TestSegueFacts:
    """The context measures where the outgoing tail and the incoming head are quiet."""

    def test_quiet_tail_and_head_add_up_to_the_overlap(self) -> None:
        """A 10s quiet tail on a downbeat and a 4s quiet head give a 14s overlap."""
        out = _analysis(120.0, rms_energy=_quiet_from(230.0))
        inc = _analysis(130.0, rms_energy=_quiet_until(4.0))

        segue = _context(out, inc).segue

        assert segue is not None
        assert segue.point == pytest.approx(35.0, abs=0.15)
        assert segue.snapped_out
        assert segue.quiet_tail == pytest.approx(10.0, abs=0.15)
        # fewer than 4 incoming downbeats precede the rise, so it stays where it was measured
        assert not segue.snapped_in
        assert segue.quiet_head == pytest.approx(4.0, abs=0.15)
        assert segue.overlap == pytest.approx(14.0, abs=0.3)

    def test_points_snap_into_their_quiet_side(self) -> None:
        """Off-grid points move to the next downbeat inside the quiet tail or head."""
        out = _analysis(120.0, rms_energy=_quiet_from(230.6))
        inc = _analysis(120.0, rms_energy=_quiet_until(9.4))

        segue = _context(out, inc).segue

        assert segue is not None
        # buffer-local downbeats sit on odd seconds: 35.6 snaps on to 37, not back to 35
        assert segue.snapped_out
        assert segue.point == pytest.approx(37.0)
        # the rise at 9.4 snaps back to 8, not on to 10
        assert segue.snapped_in
        assert segue.rise == pytest.approx(8.0)

    def test_a_loud_end_never_snaps_back_into_loud_bars(self) -> None:
        """A loud tail keeps no quiet tail, whether or not a downbeat sits a bar earlier."""
        # buffer-local downbeats sit on even seconds here: the last one is at 44 of 45
        segue = _context(_analysis(120.0, duration=241.0), _analysis(150.0)).segue

        assert segue is not None
        assert not segue.snapped_out
        assert segue.point == pytest.approx(45.0, abs=0.15)
        assert segue.quiet_tail == pytest.approx(0.0, abs=0.15)

    def test_an_irregular_grid_leaves_the_point_where_it_was_measured(self) -> None:
        """Rubato downbeats give no grid to snap to."""
        out = _with_irregular_downbeats(_analysis(120.0, rms_energy=_quiet_from(230.6)))

        segue = _context(out, _analysis(120.0)).segue

        assert segue is not None
        assert not segue.snapped_out
        assert segue.point == pytest.approx(35.6, abs=0.15)

    def test_a_grid_that_ends_early_leaves_the_point_unsnapped(self) -> None:
        """Only detected downbeats count: an extrapolated grid past the real one is no snap."""
        out = _with_downbeats_before(_analysis(120.0, rms_energy=_quiet_from(230.6)), 210.0)

        context = _context(out, _analysis(150.0))

        assert context.segue is not None
        # the protective grid runs on, regular, past the 15s where the real grid ends
        assert max(context.protective_downbeats) > 36.0
        assert not context.segue.snapped_out
        assert context.segue.point == pytest.approx(35.6, abs=0.15)

    def test_a_long_tail_caps_the_overlap(self) -> None:
        """A 30s quiet tail caps the overlap at 15s."""
        segue = _context(_analysis(120.0, rms_energy=_quiet_from(210.0)), _analysis(120.0)).segue

        assert segue is not None
        assert segue.quiet_tail == pytest.approx(30.0, abs=0.15)
        assert segue.overlap == 15.0

    def test_loud_ends_have_no_quiet_material(self) -> None:
        """Two decks loud up to their edges leave nothing to overlap."""
        segue = _context(_analysis(120.0), _analysis(150.0)).segue

        assert segue is not None
        assert segue.quiet_tail == 0.0
        assert segue.quiet_head == 0.0
        assert segue.overlap == 0.0

    def test_missing_energy_has_no_segue_facts(self) -> None:
        """Without RMS energy on a deck the segue cannot be measured."""
        inc = _analysis(120.0)
        inc.rms_energy = None

        assert _context(_analysis(120.0), inc).segue is None


class TestKickFacts:
    """The context reads each deck's kick bars from its band profile."""

    def test_kick_runs_are_buffer_local_and_head_local(self) -> None:
        """The outgoing kick dies at media 220s; the incoming kick plays from the start."""
        low = np.full(1800, 0.5, dtype=np.float32)
        low[np.arange(1800) * (240.0 / 1800) >= 220.0] = 0.01
        out = _analysis_with_bands(low, 0.3, 0.3, 0.3)
        inc = _analysis_with_bands(0.5, 0.3, 0.3, 0.3)

        context = _context(out, inc)

        assert context.kick_out is not None
        assert context.kick_in is not None
        assert list(context.kick_out) == [pytest.approx((0.0, 25.0))]
        assert list(context.kick_in) == [pytest.approx((0.0, 45.0))]
        assert context.out_kickless
        assert not context.in_kickless

    def test_no_band_profile_leaves_the_kick_unknown(self) -> None:
        """Without band envelopes the kick facts stay unknown, never kickless."""
        context = _context(_analysis(120.0), _analysis(120.0))

        assert context.kick_out is None
        assert context.kick_in is None
        assert not context.out_kickless
        assert not context.in_kickless


def test_preferred_style_follows_the_tier() -> None:
    """A beatmatchable tier prefers the blend, a quick fade tier the segue."""
    assert _context(_analysis(120.0), _analysis(120.0)).preferred_style is TransitionStyle.BLEND
    assert _context(_analysis(120.0), _analysis(150.0)).preferred_style is TransitionStyle.SEGUE
