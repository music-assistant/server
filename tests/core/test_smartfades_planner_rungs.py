"""Tests for the candidate generators' rung emission."""

from __future__ import annotations

import logging
from dataclasses import fields

from music_assistant.controllers.streams.smart_fades.planner.candidates import (
    CandidateSpec,
    EnergyLadderGenerator,
)
from music_assistant.controllers.streams.smart_fades.planner.context import (
    TransitionContext,
    build_transition_context,
)
from music_assistant.models.audio_analysis import AudioAnalysisData


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


def test_candidate_spec_has_no_one_sided_field() -> None:
    """The one-sided 16-bar relaxation was removed (1/12k win rate, scored 0.5)."""
    assert "one_sided_vocal" not in {f.name for f in fields(CandidateSpec)}


def test_energy_ladder_emits_only_plain_rungs() -> None:
    """A one-instrumental/one-vocal pair gets the plain ladder, no 16-bar spec."""
    instrumental_vs_vocal_ctx = _instrumental_vs_vocal_ctx()
    specs = list(EnergyLadderGenerator().generate(instrumental_vs_vocal_ctx))
    assert specs
    assert all(spec.bars <= 8 for spec in specs)
