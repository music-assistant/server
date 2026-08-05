"""Tests for the candidate generators' rung emission."""

from __future__ import annotations

import logging

from music_assistant.controllers.streams.smart_fades.models import (
    TransitionStrategy,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.planner.candidates import (
    _LAZY_OVERLAY_SECONDS,
    EnergyLadderGenerator,
    LazyOverlayGenerator,
    TrimClosingAnchorGenerator,
    _entry_options,
    _vocal_duties,
    _window_duties,
    earns_instrumental_blend,
)
from music_assistant.controllers.streams.smart_fades.planner.context import (
    TransitionContext,
    build_transition_context,
)
from music_assistant.controllers.streams.smart_fades.planner.planner import SmartCrossFadePlanner
from music_assistant.models.audio_analysis import AudioAnalysisData


def _analysis(
    bpm: float,
    duration: float = 240.0,
    grid_until: float | None = None,
) -> AudioAnalysisData:
    """Synthetic AudioAnalysisData with an even beat/downbeat grid, optionally truncated early."""
    interval = 60.0 / bpm
    count = int(duration / interval) + 1
    beats = [i * interval for i in range(count)]
    if grid_until is not None:
        beats = [b for b in beats if b <= grid_until]
    return AudioAnalysisData(
        duration=duration,
        bpm=bpm,
        beats=beats,
        downbeats=beats[::4],
        beats_per_bar=4,
        rms_energy=[0.8] * 1800,
        key="C",
        mode="minor",
        extra_data={},
    )


def _instrumental_vs_vocal_ctx() -> TransitionContext:
    """Build a transition context: outgoing instrumental, incoming vocal, both 128 BPM."""
    beats = [i * 60 / 128 for i in range(int(180 * 128 / 60))]
    downbeats = beats[::4]
    aa_out = AudioAnalysisData(
        duration=180.0,
        bpm=128.0,
        beats=beats,
        downbeats=downbeats,
        beats_per_bar=4,
        rms_energy=[0.8] * 1800,
        key="C",
        mode="minor",
        extra_data={"vocal_activity": [0.0] * 1800},
    )
    aa_in = AudioAnalysisData(
        duration=180.0,
        bpm=128.0,
        beats=beats,
        downbeats=downbeats,
        beats_per_bar=4,
        rms_energy=[0.8] * 1800,
        key="C",
        mode="minor",
        extra_data={"vocal_activity": [0.9] * 1800},
    )
    return build_transition_context(aa_out, aa_in, 45.0, logging.getLogger("test"))


def _big_trim_gap_ctx() -> TransitionContext:
    """
    Build a context whose energy anchor lands early, stranding audible tail behind it.

    rms_energy holds at 0.9 for the first 70% of the buffer, drops to a
    still-audible 0.25 until 95%, then to silence - no vocal data, so the
    gap can only be closed by an energy-path generator.
    """
    beats = [i * 60 / 128 for i in range(int(45 * 128 / 60))]
    downbeats = beats[::4]
    rms_energy = [0.9] * 1260 + [0.25] * 450 + [0.0] * 90
    aa_out = AudioAnalysisData(
        duration=45.0,
        bpm=128.0,
        beats=beats,
        downbeats=downbeats,
        beats_per_bar=4,
        rms_energy=rms_energy,
        key="C",
        mode="minor",
        extra_data={},
    )
    aa_in = AudioAnalysisData(
        duration=45.0,
        bpm=128.0,
        beats=beats,
        downbeats=downbeats,
        beats_per_bar=4,
        rms_energy=[0.8] * 1800,
        key="C",
        mode="minor",
        extra_data={},
    )
    ctx = build_transition_context(aa_out, aa_in, 45.0, logging.getLogger("test"))
    assert ctx.audio_end - ctx.default_anchor >= 8.0
    return ctx


def _small_trim_gap_ctx() -> TransitionContext:
    """Build a context with flat rms_energy, so the energy anchor already sits at the audible end."""
    beats = [i * 60 / 128 for i in range(int(45 * 128 / 60))]
    downbeats = beats[::4]
    aa_out = AudioAnalysisData(
        duration=45.0,
        bpm=128.0,
        beats=beats,
        downbeats=downbeats,
        beats_per_bar=4,
        rms_energy=[0.8] * 1800,
        key="C",
        mode="minor",
        extra_data={},
    )
    aa_in = AudioAnalysisData(
        duration=45.0,
        bpm=128.0,
        beats=beats,
        downbeats=downbeats,
        beats_per_bar=4,
        rms_energy=[0.8] * 1800,
        key="C",
        mode="minor",
        extra_data={},
    )
    return build_transition_context(aa_out, aa_in, 45.0, logging.getLogger("test"))


def test_energy_ladder_emits_only_plain_rungs() -> None:
    """A one-instrumental/one-vocal pair gets the plain ladder, no 16-bar spec."""
    instrumental_vs_vocal_ctx = _instrumental_vs_vocal_ctx()
    specs = list(EnergyLadderGenerator().generate(instrumental_vs_vocal_ctx))
    assert specs
    assert all(spec.bars <= 8 for spec in specs)


def test_trim_closing_ladder_emitted_for_big_trim_gap() -> None:
    """An instrumental tail with a large audible gap past the energy anchor gets late-anchored rungs."""
    ctx = _big_trim_gap_ctx()
    specs = list(TrimClosingAnchorGenerator().generate(ctx))
    assert specs
    for spec in specs:
        assert spec.anchor_s is not None
        assert spec.anchor_s > ctx.default_anchor
        assert spec.anchor_s <= ctx.audio_end
    # the ladder is walked, not just one rung
    assert len({spec.bars for spec in specs}) >= 2
    # every rung shares the single late anchor, including the longest one
    assert 8 in {spec.bars for spec in specs}


def test_trim_closing_not_emitted_for_small_gap() -> None:
    """A tail whose energy anchor already sits near the audible end emits nothing."""
    specs = list(TrimClosingAnchorGenerator().generate(_small_trim_gap_ctx()))
    assert specs == []


def _late_blendable_only_ctx() -> TransitionContext:
    """
    Build a context whose early window is too sparse to blend but the late one qualifies.

    A full 4/4 grid runs to the end of both 124 BPM decks with matching keys,
    so the tier at the audible end is FULL_BLEND. The outgoing rms_energy
    stays loud for only a few bars into the buffer before dropping to a
    still-audible level and then real silence, so the early mix-out anchor
    lands with too few downbeats behind it for the early window's tier check
    to pass, while the audible tail runs on for many more bars past it.
    """
    beats = [i * 60 / 124 for i in range(int(240 * 124 / 60))]
    downbeats = beats[::4]
    rms_energy = [0.9] * 1550 + [0.25] * (1730 - 1550) + [0.0] * (1800 - 1730)
    aa_out = AudioAnalysisData(
        duration=240.0,
        bpm=124.0,
        beats=beats,
        downbeats=downbeats,
        beats_per_bar=4,
        rms_energy=rms_energy,
        key="A",
        mode="minor",
        extra_data={},
    )
    aa_in = AudioAnalysisData(
        duration=240.0,
        bpm=124.0,
        beats=beats,
        downbeats=downbeats,
        beats_per_bar=4,
        rms_energy=[0.8] * 1800,
        key="A",
        mode="minor",
        extra_data={},
    )
    ctx = build_transition_context(aa_out, aa_in, 45.0, logging.getLogger("test"))
    assert ctx.audio_end - ctx.default_anchor >= 8.0
    early_downbeats = [d for d in ctx.outgoing.downbeats if d <= ctx.default_anchor]
    assert len(early_downbeats) < 8
    return ctx


def test_trim_closing_ladder_uses_the_tier_at_its_own_anchor() -> None:
    """A grid that only becomes blendable at the audible end still earns the long rungs."""
    ctx = _late_blendable_only_ctx()
    assert ctx.tier is TransitionTier.QUICK_FADE  # the early window has too few downbeats
    specs = list(TrimClosingAnchorGenerator().generate(ctx))
    assert specs
    assert max(spec.bars for spec in specs) == 8
    assert all(spec.tier is not TransitionTier.QUICK_FADE for spec in specs)
    assert all(spec.ideal_bars == 8 for spec in specs)


def _ctx_with_late_natural_entry() -> TransitionContext:
    """Build a context where B grooves late: its natural entry lands deep in the 45s head."""
    aa_out = _analysis(bpm=124.0, duration=200.0)
    aa_in = _analysis(bpm=124.0, duration=45.0)
    aa_in.rms_energy = [0.05] * 720 + [0.9] * 1080
    ctx = build_transition_context(aa_out, aa_in, 45.0, logging.getLogger("test"))
    assert ctx.natural_entry > 10.0
    return ctx


def _ambient_unblendable_ctx() -> tuple[AudioAnalysisData, AudioAnalysisData]:
    """
    Build an outgoing/incoming pair whose grid is unusable but both decks are ambient.

    The outgoing downbeat grid dies at 10s (rubato tail, like the 3.2 sparse-tail
    fixture); its energy stays quiet-but-audible out to ~43s before real silence,
    stranding a large gap past the energy anchor. Both decks carry a validated
    all-zero vocal timeline, so both duties read 0.0 (ambient).
    """
    grid_beats = [i * 60 / 128 for i in range(int(10.0 * 128 / 60) + 1)]
    rms_energy = [0.9] * 1260 + [0.25] * 450 + [0.0] * 90
    aa_out = AudioAnalysisData(
        duration=45.0,
        bpm=128.0,
        beats=grid_beats,
        downbeats=grid_beats[::4],
        beats_per_bar=4,
        rms_energy=rms_energy,
        key="C",
        mode="minor",
        extra_data={"vocal_activity": [0.0] * 1800},
    )
    full_beats = [i * 60 / 128 for i in range(int(45 * 128 / 60))]
    aa_in = AudioAnalysisData(
        duration=45.0,
        bpm=128.0,
        beats=full_beats,
        downbeats=full_beats[::4],
        beats_per_bar=4,
        rms_energy=[0.8] * 1800,
        key="C",
        mode="minor",
        extra_data={"vocal_activity": [0.0] * 1800},
    )
    return aa_out, aa_in


def _vocal_unblendable_ctx() -> TransitionContext:
    """Build the same ambient pair, but with the incoming deck fully sung: never qualifies."""
    aa_out, aa_in = _ambient_unblendable_ctx()
    aa_in.extra_data = {"vocal_activity": [0.9] * 1800}
    return build_transition_context(aa_out, aa_in, 45.0, logging.getLogger("test"))


def _clean_full_blend_ctx() -> TransitionContext:
    """Build a context with a full, evenly-spaced grid: earns the ordinary full-blend tier."""
    aa_out = _analysis(bpm=124.0, duration=200.0)
    aa_in = _analysis(bpm=124.0, duration=200.0)
    return build_transition_context(aa_out, aa_in, 45.0, logging.getLogger("test"))


def test_lazy_overlay_wins_for_both_ambient_unblendable_pair() -> None:
    """Quiet-tail + ambient incoming: the long overlay replaces the 2-bar rescue."""
    ctx_out_aa, ctx_in_aa = _ambient_unblendable_ctx()
    plan = SmartCrossFadePlanner(logging.getLogger("test")).plan(ctx_out_aa, ctx_in_aa, 45.0)
    assert plan.metrics.strategy is TransitionStrategy.LAZY_OVERLAY
    assert plan.crossfade_duration >= 12.0
    assert plan.fadein_trim_start is None  # B keeps its intro


def test_lazy_overlay_not_emitted_for_vocal_material() -> None:
    """A singing deck never gets the unphrased long overlay."""
    specs = list(LazyOverlayGenerator().generate(_vocal_unblendable_ctx()))
    assert specs == []


def test_lazy_overlay_not_emitted_when_grid_blendable() -> None:
    """A clean, blendable pair never falls back to the unphrased overlay."""
    specs = list(LazyOverlayGenerator().generate(_clean_full_blend_ctx()))
    assert specs == []


def test_lazy_overlay_beats_trim_closing_on_a_qualifying_pair() -> None:
    """
    The overlay must win the tie against trim-closing's equally-cheap short rungs.

    Both generators anchor near the audible end with ~zero trim on this
    context, so this exercises the actual tie-break (generator order), not
    just an absence of competition.
    """
    aa_out, aa_in = _ambient_unblendable_ctx()
    ctx = build_transition_context(aa_out, aa_in, 45.0, logging.getLogger("test"))
    # trim-closing must actually compete here, or this proves nothing
    assert list(TrimClosingAnchorGenerator().generate(ctx))

    plan = SmartCrossFadePlanner(logging.getLogger("test")).plan(aa_out, aa_in, 45.0)
    assert plan.metrics.strategy is TransitionStrategy.LAZY_OVERLAY


def _lazy_gate_outgoing() -> AudioAnalysisData:
    """
    Outgoing analysis shared by the lazy-gate vocal-window fixtures.

    Same shape as ``_late_blendable_only_ctx``'s outgoing deck: a full 4/4
    grid at 124 BPM, but the early mix-out anchor leaves fewer than 8
    downbeats before it, so the pair reaches QUICK_FADE and the lazy gate.
    The vocal timeline is all-zero, so the outgoing side never contributes duty.
    """
    beats = [i * 60 / 124 for i in range(int(240 * 124 / 60))]
    downbeats = beats[::4]
    rms_energy = [0.9] * 1550 + [0.25] * (1730 - 1550) + [0.0] * (1800 - 1730)
    return AudioAnalysisData(
        duration=240.0,
        bpm=124.0,
        beats=beats,
        downbeats=downbeats,
        beats_per_bar=4,
        rms_energy=rms_energy,
        key="A",
        mode="minor",
        extra_data={"vocal_activity": [0.0] * 1800},
    )


def _lazy_gate_incoming(vocal_run: tuple[float, float]) -> AudioAnalysisData:
    """Incoming analysis for the lazy-gate fixtures: a 45s head with vocal only over ``vocal_run``."""
    beats = [i * 60 / 124 for i in range(int(45 * 124 / 60))]
    vocal_activity = [0.0] * 1800
    frame_duration = 45.0 / 1800
    start_bin = int(vocal_run[0] / frame_duration)
    end_bin = int(vocal_run[1] / frame_duration)
    for i in range(start_bin, end_bin):
        vocal_activity[i] = 0.95
    return AudioAnalysisData(
        duration=45.0,
        bpm=124.0,
        beats=beats,
        downbeats=beats[::4],
        beats_per_bar=4,
        rms_energy=[0.8] * 1800,
        key="A",
        mode="minor",
        extra_data={"vocal_activity": vocal_activity},
    )


def _front_loaded_vocal_ctx() -> TransitionContext:
    """
    Build a lazy-gate context where B's vocal sits inside the overlay's first 16s.

    B's vocal run covers media 4.0-7.2s: ~3.2s of a 16s overlay (~0.20 duty)
    but only ~0.07 over the full 45s head, so the whole-window gate would
    pass it while the windowed gate correctly blocks it.
    """
    ctx = build_transition_context(
        _lazy_gate_outgoing(), _lazy_gate_incoming((4.0, 7.2)), 45.0, logging.getLogger("test")
    )
    assert ctx.tier is TransitionTier.QUICK_FADE
    whole = _vocal_duties(ctx)
    assert whole is not None
    assert whole[1] <= 0.10
    windowed = _window_duties(ctx, _LAZY_OVERLAY_SECONDS)
    assert windowed is not None
    assert windowed[1] > 0.10
    return ctx


def _late_vocal_ctx() -> TransitionContext:
    """
    Build a lazy-gate context where B's vocal sits entirely outside the overlay's first 16s.

    B's vocal run covers media 20.0-30.0s: 0.0 duty inside a 16s overlay but
    ~0.22 over the full 45s head, so the whole-window gate wrongly blocks it
    while the windowed gate correctly allows it.
    """
    ctx = build_transition_context(
        _lazy_gate_outgoing(), _lazy_gate_incoming((20.0, 30.0)), 45.0, logging.getLogger("test")
    )
    assert ctx.tier is TransitionTier.QUICK_FADE
    whole = _vocal_duties(ctx)
    assert whole is not None
    assert whole[1] > 0.10
    windowed = _window_duties(ctx, _LAZY_OVERLAY_SECONDS)
    assert windowed is not None
    assert windowed[1] <= 0.10
    return ctx


def test_lazy_overlay_denied_when_vocals_sit_inside_the_overlay() -> None:
    """Vocals concentrated in B's first 16s block the overlay even when its 45s duty is low."""
    ctx = _front_loaded_vocal_ctx()
    assert list(LazyOverlayGenerator().generate(ctx)) == []


def test_lazy_overlay_allowed_when_vocals_sit_outside_the_overlay() -> None:
    """Vocals late in B's head leave the overlay window ambient, so the overlay still fires."""
    ctx = _late_vocal_ctx()
    specs = list(LazyOverlayGenerator().generate(ctx))
    assert len(specs) == 1
    assert specs[0].strategy is TransitionStrategy.LAZY_OVERLAY


def test_instrumental_blend_gate_unchanged_by_window_duties() -> None:
    """The both-instrumental 16-bar gate keeps reading whole-window duty."""
    ctx = _front_loaded_vocal_ctx()
    assert _vocal_duties(ctx) is not None
    # the 16-bar gate's own verdict must not move when the lazy gate narrows its window
    assert earns_instrumental_blend(ctx) is False


def test_short_rungs_offer_intro_keeping_entry() -> None:
    """At 1-2 bars an entry at 0.0 (keep B's intro) is offered alongside the natural entry."""
    ctx = _ctx_with_late_natural_entry()
    options = _entry_options(ctx, 2)
    assert 0.0 in options
    assert ctx.natural_entry in options
    # 0.0 must precede the natural entry: the selector ties break to the
    # earlier candidate, so order decides which one a tie actually prefers
    assert options.index(0.0) < options.index(ctx.natural_entry)
    assert 0.0 not in _entry_options(ctx, 8)
