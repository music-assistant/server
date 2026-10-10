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
)
from music_assistant.controllers.streams.smart_fades.planner import (
    SmartCrossFadePlanner,
    planner,
)
from music_assistant.controllers.streams.smart_fades.planner.candidates import Candidate
from music_assistant.controllers.streams.smart_fades.planner.context import TransitionContext
from music_assistant.controllers.streams.smart_fades.planner.policies import (
    Policy,
    Verdict,
    default_policies,
)
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
    bpm: float,
    *,
    rms: list[float] | None = None,
    vocals: list[float] | None = None,
    beats_per_bar: int = 4,
) -> AudioAnalysisData:
    """Build a 240s loud-to-the-end track with a regular grid and a kick in every bar."""
    beats = np.arange(0.0, DURATION, 60.0 / bpm, dtype=np.float32)
    other = _envelope(0.3)
    return AudioAnalysisData(
        duration=DURATION,
        bpm=bpm,
        beats=beats.tolist(),
        downbeats=beats[::beats_per_bar].tolist(),
        beats_per_bar=beats_per_bar,
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
    """A cut that stacks two kicks for more than a beat gives way to a dressed transition."""

    def test_kick_against_kick_12_percent_apart_filters_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The 4-bar cut stacks two kicks for 2.67 weighted bars; a 4-bar filter out replaces it.

        The outgoing low end is swept away while the next track fades in at full range,
        and the filter out lasts as long as the cut it replaces.
        """
        out, inc = _track(120.0), _track(134.0)

        scored, winner = _main_pass(monkeypatch, out, inc)
        plan = _plan(out, inc)

        four_bar_cut = next(
            e
            for e in scored
            if e.candidate.plan.style is TransitionStyle.CUT and e.candidate.spec.bars == 4
        )
        assert not four_bar_cut.rejected
        assert four_bar_cut.candidate.metrics.rhythm_clash_bars == pytest.approx(8 / 3)
        assert winner is not None
        assert plan.style is TransitionStyle.FILTER_OUT
        assert plan.crossfade_duration == pytest.approx(8.0)
        assert plan.highpass is not None
        assert plan.highpass.end_s == pytest.approx(plan.fade_out_window)
        assert plan.highpass.start_s == pytest.approx(plan.fade_out_window - 8.0)
        assert plan.eq_plan.low_in is None
        assert not plan.tempo_plan
        today = _plan_undressed(monkeypatch, out, inc)
        assert today.style is TransitionStyle.CUT
        assert today.crossfade_duration == pytest.approx(8.0)

    def test_a_filter_out_ends_where_the_clashing_cut_does(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A vocal past the energy anchor moves the cut to the audible end; the filter follows."""
        out, inc = _track(120.0, vocals=_vocals((150.0, 239.5))), _track(134.0)

        plan = _plan(out, inc)

        today = _plan_undressed(monkeypatch, out, inc)
        assert today.style is TransitionStyle.CUT
        assert today.metrics.rhythm_clash_bars > 2.0
        assert plan.style is TransitionStyle.FILTER_OUT
        assert plan.fade_out_window == pytest.approx(today.fade_out_window)
        assert plan.crossfade_duration == pytest.approx(today.crossfade_duration)

    def test_kick_against_kick_25_percent_apart_echoes_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 1-bar cut stacks two kicks for 2/3 of a bar; an echo out of one bar replaces it."""
        out, inc = _track(120.0), _track(150.0)

        plan = _plan(out, inc)

        today = _plan_undressed(monkeypatch, out, inc)
        assert today.style is TransitionStyle.CUT
        assert today.metrics.rhythm_clash_bars == pytest.approx(2 / 3)
        assert plan.style is TransitionStyle.ECHO_OUT
        assert plan.echo is not None
        assert plan.metrics.rhythm_clash_bars == 0.0
        assert plan.crossfade_duration == pytest.approx(today.crossfade_duration)
        assert not plan.tempo_plan

    def test_a_cross_meter_kick_clash_echoes_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two meters at the same tempo share no bar grid, so their clashing cut echoes out."""
        out, inc = _track(120.0), _track(120.0, beats_per_bar=3)

        plan = _plan(out, inc)

        assert _plan_undressed(monkeypatch, out, inc).style is TransitionStyle.CUT
        assert plan.style is TransitionStyle.ECHO_OUT

    def test_a_clean_cut_stays_a_cut(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without kicks on both decks the cut ships, though a dressed transition scores lower."""
        # a sung head keeps the kickless intro from riding a long segue
        out, inc = _track(120.0), _track(134.0, vocals=_vocals((0.0, 30.0)))
        inc.band_rms_low = _envelope(0.5, (0.0, 20.0, 0.01))

        scored, winner = _main_pass(monkeypatch, out, inc)

        assert winner is not None
        assert winner.candidate.plan.style is TransitionStyle.CUT
        dressed = [
            e.total_penalty
            for e in scored
            if e.candidate.plan.style is TransitionStyle.FILTER_OUT and not e.rejected
        ]
        assert min(dressed) < winner.total_penalty
        assert _plan(out, inc) == _plan_undressed(monkeypatch, out, inc)

    def test_a_clashing_cut_without_a_dressed_alternative_ships_as_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When every dressed transition is rejected the clashing cut ships unchanged."""

        class _RejectDressed(Policy):
            def evaluate(self, candidate: Candidate, ctx: TransitionContext) -> Verdict:
                if candidate.plan.style in DRESSED_STYLES:
                    return Verdict.reject("dressed rejected")
                return Verdict.ok()

        out, inc = _track(120.0), _track(134.0)
        today = _plan_undressed(monkeypatch, out, inc)
        monkeypatch.setattr(
            planner, "default_policies", lambda: (*default_policies(), _RejectDressed())
        )

        plan = _plan(out, inc)

        assert plan.style is TransitionStyle.CUT
        assert plan.metrics.rhythm_clash_bars > 2.0
        assert plan == today


class TestDressedLeavesTheOtherStyles:
    """A dressed transition only ever replaces a clashing cut: blends and segues ship as before."""

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


def test_the_plan_line_names_the_dressed_style_and_the_cut_it_replaced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A filter out logs its bars, an echo out none; both name the kick clash of their cut."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER.name):
        _plan(_track(120.0), _track(134.0))
        _plan(_track(120.0), _track(150.0))

    lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("planned transition: ")
    ]
    assert lines[0].startswith(
        "planned transition: style=filter_out tier=quick_fade trigger=tempo "
    )
    assert " source=filter-out bars=4 overlap=8.00s " in lines[0]
    assert lines[0].endswith(' reason="cut kick clash 2.67 bars"')
    assert lines[1].startswith("planned transition: style=echo_out tier=quick_fade trigger=tempo ")
    assert " source=echo-out overlap=2.00s " in lines[1]
    assert lines[1].endswith(' reason="cut kick clash 0.67 bars"')
