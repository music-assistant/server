"""Tests for build_transition_context — the immutable per-transition fact extraction."""

from __future__ import annotations

import dataclasses
import logging

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import TransitionTier
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner
from music_assistant.controllers.streams.smart_fades.planner.context import (
    TransitionContext,
    build_transition_context,
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


def test_context_energy_only_when_vocal_data_missing() -> None:
    """Analyses without a stored vocal-activity timeline disable all vocal-aware masks."""
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


def test_context_mix_out_anchor_is_downbeat_snapped() -> None:
    """An outro whose energy decays below the mix-out floor anchors at that (downbeat) point."""
    bins = np.full(1800, 0.5, dtype=np.float32)
    t = np.linspace(0, 240.0, 1800)
    bins[t >= 220.0] = 0.2  # audible but below 0.7*sustained: mixed out, not silent

    context = _context(_analysis(120.0, rms_energy=bins), _analysis(120.0))

    # anchored at the mix-out point (media 220 -> buffer-local 25), well before the
    # RMS-audible boundary, and snapped to a real downbeat
    assert context.mix_out_anchor == pytest.approx(25.0, abs=0.3)
    assert context.audio_end > (context.mix_out_anchor or 0.0) + 1.0
    assert float(np.min(np.abs(context.outgoing.downbeats - context.mix_out_anchor))) < 0.05


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
    assert context.tier is SmartCrossFadePlanner(LOGGER).plan(out, inc, 45.0).tier
