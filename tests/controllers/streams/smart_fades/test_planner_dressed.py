"""Planner scenarios for the dressed short transitions: filter out and echo out."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import numpy as np
import pytest

from music_assistant.controllers.streams.smart_fades.models import (
    DRESSED_STYLES,
    TransitionPlan,
    TransitionStyle,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.planner import SmartCrossFadePlanner
from music_assistant.controllers.streams.smart_fades.planner.candidates import Candidate
from music_assistant.controllers.streams.smart_fades.planner.context import TransitionContext
from music_assistant.controllers.streams.smart_fades.planner.selection import (
    CandidateSelector,
    ScoredCandidate,
)
from music_assistant.models.audio_analysis import AudioAnalysisData

from .conftest import disable_dressed_transitions

LOGGER = logging.getLogger(__name__)
DURATION = 240.0


def _envelope(base: float, *segments: tuple[float, float, float]) -> list[float]:
    """Build an 1800-bin envelope at ``base``, set to ``value`` per ``(start, end, value)``."""
    t = np.arange(1800) * (DURATION / 1800)
    env = np.full(1800, base, dtype=np.float32)
    for start, end, value in segments:
        env[(t >= start) & (t < end)] = value
    return env.tolist()


def _vocals(*windows: tuple[float, float]) -> list[float]:
    """Build a validated vocal-activity timeline that sings only inside ``windows``."""
    t = np.arange(1800) * (DURATION / 1800)
    probabilities = np.full(1800, 0.05, dtype=np.float32)
    for start, end in windows:
        probabilities[(t >= start) & (t < end)] = 0.9
    return probabilities.tolist()


def _track(
    bpm: float, *, rms: list[float] | None = None, vocals: list[float] | None = None
) -> AudioAnalysisData:
    """Build a 240s loud-to-the-end track with a regular grid and a kick in every bar."""
    beats = np.arange(0.0, DURATION, 60.0 / bpm, dtype=np.float32)
    other = _envelope(0.3)
    return AudioAnalysisData(
        duration=DURATION,
        bpm=bpm,
        beats=beats.tolist(),
        downbeats=beats[::4].tolist(),
        beats_per_bar=4,
        rms_energy=rms if rms is not None else _envelope(0.5),
        key="A",
        mode="minor",
        band_rms_low=_envelope(0.5),
        band_rms_low_mid=other,
        band_rms_mid=other,
        band_rms_high=other,
        vocal_activity=vocals if vocals is not None else _vocals(),
    )


def _plan(fade_out: AudioAnalysisData, fade_in: AudioAnalysisData) -> TransitionPlan:
    return SmartCrossFadePlanner(LOGGER).plan(fade_out, fade_in, 45.0)


def _plan_undressed(
    monkeypatch: pytest.MonkeyPatch, fade_out: AudioAnalysisData, fade_in: AudioAnalysisData
) -> TransitionPlan:
    """Plan the pair as the planner did before the dressed transitions existed."""
    with monkeypatch.context() as patch:
        disable_dressed_transitions(patch)
        return _plan(fade_out, fade_in)


def _main_pass(
    monkeypatch: pytest.MonkeyPatch, fade_out: AudioAnalysisData, fade_in: AudioAnalysisData
) -> tuple[list[ScoredCandidate], ScoredCandidate | None]:
    """Plan a pair and return the main pass's scoreboard and winner."""
    passes: list[tuple[list[ScoredCandidate], ScoredCandidate | None]] = []
    select, score = CandidateSelector.select, CandidateSelector._score

    def keep_select(
        selector: CandidateSelector, built: Sequence[Candidate], ctx: TransitionContext
    ) -> ScoredCandidate | None:
        passes.append(([], None))
        winner = select(selector, built, ctx)
        passes[-1] = (passes[-1][0], winner)
        return winner

    def keep_score(
        selector: CandidateSelector, candidate: Candidate, ctx: TransitionContext
    ) -> ScoredCandidate:
        entry = score(selector, candidate, ctx)
        passes[-1][0].append(entry)
        return entry

    monkeypatch.setattr(CandidateSelector, "select", keep_select)
    monkeypatch.setattr(CandidateSelector, "_score", keep_score)
    _plan(fade_out, fade_in)
    return passes[0]


class TestDressedScenarios:
    """Two loud kicked ends at very different tempos get a dressed short transition."""

    def test_kick_against_kick_25_percent_apart_echoes_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The outgoing track stops on a downbeat and echoes over the next for one bar."""
        out, inc = _track(120.0), _track(150.0)

        plan = _plan(out, inc)

        assert plan.style is TransitionStyle.ECHO_OUT
        assert plan.tier is TransitionTier.QUICK_FADE
        assert plan.echo is not None
        assert plan.echo.cut_s == pytest.approx(41.0)
        assert plan.crossfade_duration == pytest.approx(4 * 0.5)
        assert not plan.tempo_plan
        # as long as the 1-bar cut it replaces
        today = _plan_undressed(monkeypatch, out, inc)
        assert today.style is TransitionStyle.CUT
        assert plan.crossfade_duration >= today.crossfade_duration - 1e-6

    def test_kick_against_kick_12_percent_apart_filters_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Four outgoing bars lose their low end while the next track fades in at full range."""
        out, inc = _track(120.0), _track(134.4)

        plan = _plan(out, inc)

        assert plan.style is TransitionStyle.FILTER_OUT
        assert plan.crossfade_duration == pytest.approx(8.0)
        assert plan.highpass is not None
        assert plan.highpass.end_s == pytest.approx(plan.fade_out_window)
        assert plan.highpass.start_s == pytest.approx(plan.fade_out_window - 8.0)
        assert plan.eq_plan.low_in is None
        assert not plan.tempo_plan
        # the 4-bar filter out keeps the 4 bars the quick fade had
        assert plan.crossfade_duration >= _plan_undressed(monkeypatch, out, inc).crossfade_duration

    def test_a_four_bar_cut_that_stacks_two_kicks_gives_way_to_a_filter_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """11.7% apart the 4-bar cut clashes past the drum limit; a 4-bar filter out replaces it."""
        out, inc = _track(120.0), _track(134.0)

        scored, winner = _main_pass(monkeypatch, out, inc)

        four_bar_cut = next(
            e
            for e in scored
            if e.candidate.plan.style is TransitionStyle.CUT and e.candidate.spec.bars == 4
        )
        assert four_bar_cut.candidate.plan.crossfade_duration == pytest.approx(8.0)
        assert four_bar_cut.rejected
        assert any(v.reason == "kick clash exceeds the guard limit" for v in four_bar_cut.verdicts)
        assert winner is not None
        assert winner.candidate.plan.style is TransitionStyle.FILTER_OUT
        assert winner.candidate.plan.crossfade_duration == pytest.approx(8.0)
        # without the dressed transitions a shorter cut would ship
        assert _plan_undressed(monkeypatch, out, inc).crossfade_duration == pytest.approx(4.0)

    def test_a_clean_dressed_transition_beats_every_clean_cut(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without kick or vocal clashes the echo out wins and no tie decides it."""
        out = _track(120.0)
        inc = _track(150.0)
        for track in (out, inc):
            track.band_rms_low = _envelope(0.01)

        scored, winner = _main_pass(monkeypatch, out, inc)

        assert winner is not None
        assert winner.candidate.plan.style is TransitionStyle.ECHO_OUT
        cuts = [
            e.total_penalty
            for e in scored
            if e.candidate.plan.style is TransitionStyle.CUT and not e.rejected
        ]
        assert min(cuts) > winner.total_penalty


class TestDressedLeavesTheOtherStyles:
    """A dressed transition only ever replaces a cut: blends and segues ship as before."""

    def test_a_beatmatchable_pair_keeps_its_blend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A same-tempo pair emits no dressed spec and blends exactly as before."""
        out, inc = _track(120.0), _track(120.0)

        plan = _plan(out, inc)

        assert plan.style is TransitionStyle.BLEND
        assert plan == _plan_undressed(monkeypatch, out, inc)

    def test_a_quiet_tail_keeps_its_segue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A quiet outgoing tail segues as before, though a dressed transition scores lower."""
        out = _track(120.0, rms=_envelope(0.5, (228.0, DURATION, 0.1)))
        out.band_rms_low = _envelope(0.5, (228.0, DURATION, 0.01))
        inc = _track(150.0)

        plan = _plan(out, inc)

        assert plan.style is TransitionStyle.SEGUE
        assert plan == _plan_undressed(monkeypatch, out, inc)


def test_the_plan_line_names_the_dressed_style(caplog: pytest.LogCaptureFixture) -> None:
    """A shipped echo out logs its style without a bar count, a filter out with its bars."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER.name):
        _plan(_track(120.0), _track(150.0))
        _plan(_track(120.0), _track(134.4))

    lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("planned transition: ")
    ]
    assert lines[0].startswith("planned transition: style=echo_out tier=quick_fade trigger=tempo ")
    assert " source=echo-out overlap=2.00s " in lines[0]
    assert " bars=" not in lines[0]
    assert lines[1].startswith(
        "planned transition: style=filter_out tier=quick_fade trigger=tempo "
    )
    assert " source=filter-out bars=4 overlap=8.00s " in lines[1]


def test_dressed_styles_are_filter_out_and_echo_out() -> None:
    """The dressed styles are the two short styles with an outgoing effect."""
    assert {TransitionStyle.FILTER_OUT, TransitionStyle.ECHO_OUT} == DRESSED_STYLES
