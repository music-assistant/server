"""Tests for the candidate generators' rung emission."""

from __future__ import annotations

import logging
from dataclasses import fields

from music_assistant.controllers.streams.smart_fades.models import TransitionTier
from music_assistant.controllers.streams.smart_fades.planner.candidates import (
    CandidateSpec,
    EnergyLadderGenerator,
    TrimClosingAnchorGenerator,
    _entry_options,
)
from music_assistant.controllers.streams.smart_fades.planner.context import (
    TransitionContext,
    build_transition_context,
)
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


def test_candidate_spec_has_no_one_sided_field() -> None:
    """The one-sided 16-bar relaxation was removed (1/12k win rate, scored 0.5)."""
    assert "one_sided_vocal" not in {f.name for f in fields(CandidateSpec)}


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
        assert spec.anchor_s is not None and spec.anchor_s > ctx.default_anchor
        assert spec.anchor_s <= ctx.audio_end
    # the ladder is walked, not just one rung
    assert len({spec.bars for spec in specs}) >= 2


def test_trim_closing_not_emitted_for_small_gap() -> None:
    """A tail whose energy anchor already sits near the audible end emits nothing."""
    specs = list(TrimClosingAnchorGenerator().generate(_small_trim_gap_ctx()))
    assert specs == []


def test_grid_blendable_false_for_sparse_tail() -> None:
    """A tail whose grid dies early reports grid_blendable=False (rubato/ambient outro)."""
    aa_out = _analysis(bpm=124.0, duration=200.0, grid_until=170.0)  # grid stops 30s early
    aa_in = _analysis(bpm=124.0, duration=200.0)
    ctx = build_transition_context(aa_out, aa_in, 45.0, logging.getLogger("test"))
    assert ctx.grid_blendable is False
    assert ctx.tier is TransitionTier.QUICK_FADE


def _ctx_with_late_natural_entry() -> TransitionContext:
    """Build a context where B grooves late: its natural entry lands deep in the 45s head."""
    aa_out = _analysis(bpm=124.0, duration=200.0)
    aa_in = _analysis(bpm=124.0, duration=45.0)
    aa_in.rms_energy = [0.05] * 720 + [0.9] * 1080
    ctx = build_transition_context(aa_out, aa_in, 45.0, logging.getLogger("test"))
    assert ctx.natural_entry > 10.0
    return ctx


def test_short_rungs_offer_intro_keeping_entry() -> None:
    """At 1-2 bars an entry at 0.0 (keep B's intro) is offered alongside the natural entry."""
    ctx = _ctx_with_late_natural_entry()
    options = _entry_options(ctx, 2)
    assert 0.0 in options
    assert ctx.natural_entry in options
    assert 0.0 not in _entry_options(ctx, 8)
