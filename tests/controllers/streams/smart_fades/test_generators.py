"""Tests for the smart fades candidate generators and the instrumental cross-check."""

from __future__ import annotations

import dataclasses
import logging

import numpy as np

from music_assistant.controllers.streams.smart_fades.bands import build_band_profile
from music_assistant.controllers.streams.smart_fades.models import Deck, TransitionTier
from music_assistant.controllers.streams.smart_fades.planner_pkg.candidates import (
    CodaAnchorGenerator,
    EnergyLadderGenerator,
    ProtectiveAnchorGenerator,
    VocalOnsetEntryGenerator,
    default_generators,
    instrumental_blend_bars,
)
from music_assistant.controllers.streams.smart_fades.planner_pkg.context import (
    TransitionContext,
    build_transition_context,
)
from music_assistant.controllers.streams.smart_fades.structure import CodaZone
from music_assistant.controllers.streams.smart_fades.vocal import VocalMask
from music_assistant.models.audio_analysis import AudioAnalysisData

from .conftest import _analysis_with_bands

LOGGER = logging.getLogger(__name__)


def _analysis(
    bpm: float, duration: float = 240.0, key: str | None = "A", mode: str | None = "minor"
) -> AudioAnalysisData:
    """Build a plain (no band_rms) analysis row for a track."""
    interval = 60.0 / bpm
    beats = np.arange(0.0, duration, interval, dtype=np.float32)
    return AudioAnalysisData(
        duration=duration,
        bpm=bpm,
        beats=beats.tolist(),
        downbeats=beats[::4].tolist(),
        rms_energy=np.full(1800, 0.5, dtype=np.float32).tolist(),
        key=key,
        mode=mode,
    )


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
    """Attach a valid vocal_activity list, merged into any existing extra_data."""
    assert analysis.duration is not None
    extra = dict(analysis.extra_data or {})
    extra["vocal_activity"] = _vocal_probabilities(analysis.duration, active_windows)
    analysis.extra_data = extra
    return analysis


def _base_ctx(**overrides: object) -> TransitionContext:
    """Build a manually-controlled TransitionContext; only the fields under test vary."""
    deck = Deck(
        analysis=AudioAnalysisData(),
        bpm=120.0,
        beats=np.array([], dtype=np.float32),
        downbeats=np.array([], dtype=np.float32),
    )
    base = TransitionContext(
        outgoing=deck,
        incoming=deck,
        outgoing_profile=None,
        incoming_profile=None,
        buffer_duration=45.0,
        buffer_offset=0.0,
        audio_end=45.0,
        default_anchor=45.0,
        mix_out_anchor=None,
        kick_anchor=None,
        fade_onset=None,
        coda_zone=None,
        tier=TransitionTier.FULL_BLEND,
        cross_meter=False,
        bpm_diff_percent=0.0,
        vocal_out_placement=None,
        vocal_in_placement=None,
        vocal_out_scoring=None,
        vocal_in_scoring=None,
        natural_entry=0.0,
        protective_downbeats=(),
    )
    return dataclasses.replace(base, **overrides)


def _out_analysis_with_quiet_tail(*, elevated_spike: bool) -> AudioAnalysisData:
    """
    Build an outgoing analysis: loud mid before media 195s, quiet mid after (the blend region).

    :param elevated_spike: When True, injects one elevated mid bar at media
        210-212s inside the otherwise-quiet blend region.
    """
    t = np.linspace(0.0, 240.0, 1800)
    mid = np.where(t < 195.0, 0.5, 0.02).astype(np.float32)
    if elevated_spike:
        mid[(t >= 210.0) & (t < 212.0)] = 0.5
    low = np.full(1800, 0.05, dtype=np.float32)
    analysis = _analysis_with_bands(low, low, mid, low, duration=240.0)
    return _with_vocal_activity(analysis, [])


class TestEnergyLadderGenerator:
    """The primary energy ladder: default anchor, kick anchor, one-sided instrumental rule."""

    def test_emits_full_ladder_at_default_anchor_when_energy_only(self) -> None:
        """Without vocal data, every rung is emitted once at the default anchor."""
        ctx = build_transition_context(
            _analysis(120.0, duration=240.0), _analysis(120.0, duration=240.0), 45.0, LOGGER
        )
        specs = list(EnergyLadderGenerator().generate(ctx))

        assert {s.bars for s in specs} == {8, 4, 2, 1}
        assert {s.anchor_s for s in specs} == {None}
        assert all(s.ideal_bars == 8 for s in specs)
        assert all(s.source == "energy-ladder" for s in specs)
        assert all(s.one_sided_vocal is None for s in specs)

    def test_kick_anchor_variant_emitted_when_present_and_differing(self) -> None:
        """A kick anchor distinct from the default anchor doubles every rung at that anchor."""
        ctx = _base_ctx(kick_anchor=10.0, default_anchor=45.0)
        specs = list(EnergyLadderGenerator().generate(ctx))

        assert {s.anchor_s for s in specs} == {None, 10.0}
        assert {s.bars for s in specs if s.anchor_s == 10.0} == {8, 4, 2, 1}

    def test_no_kick_anchor_variant_when_equal_to_default(self) -> None:
        """A kick anchor equal to the default anchor emits no duplicate rungs."""
        ctx = _base_ctx(kick_anchor=45.0, default_anchor=45.0)
        specs = list(EnergyLadderGenerator().generate(ctx))

        assert {s.anchor_s for s in specs} == {None}

    def test_no_kick_anchor_variant_when_absent(self) -> None:
        """No kick anchor at all emits only the default-anchor rungs."""
        ctx = _base_ctx(kick_anchor=None)
        specs = list(EnergyLadderGenerator().generate(ctx))

        assert {s.anchor_s for s in specs} == {None}

    def test_both_instrumental_decks_earn_16_bars_untagged(self) -> None:
        """Near-zero vocal duty on both decks earns 16 bars with no one-sided tag."""
        out = _with_vocal_activity(_analysis(120.0, duration=240.0), [])
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [])
        ctx = build_transition_context(out, inc, 45.0, LOGGER)

        specs = list(EnergyLadderGenerator().generate(ctx))
        sixteens = [s for s in specs if s.bars == instrumental_blend_bars]

        assert sixteens
        assert all(s.one_sided_vocal is None for s in sixteens)
        assert all(s.ideal_bars == instrumental_blend_bars for s in sixteens)

    def test_one_sided_instrumental_confirmed_tags_extra_16_bar_rung(self) -> None:
        """A near-instrumental outgoing deck, cross-check confirmed, earns a tagged 16-bar rung."""
        out = _out_analysis_with_quiet_tail(elevated_spike=False)
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [(5.0, 25.0)])
        ctx = build_transition_context(out, inc, 45.0, LOGGER)

        specs = list(EnergyLadderGenerator().generate(ctx))
        sixteens = [s for s in specs if s.bars == instrumental_blend_bars]

        assert sixteens
        assert all(s.one_sided_vocal == "incoming" for s in sixteens)

    def test_one_sided_instrumental_denied_emits_no_extra_16_bar_rung(self) -> None:
        """An elevated mid bar inside the VAD-silent blend region denies the one-sided rule."""
        out = _out_analysis_with_quiet_tail(elevated_spike=True)
        inc = _with_vocal_activity(_analysis(120.0, duration=240.0), [(5.0, 25.0)])
        ctx = build_transition_context(out, inc, 45.0, LOGGER)

        specs = list(EnergyLadderGenerator().generate(ctx))

        assert not [s for s in specs if s.bars == instrumental_blend_bars]


class TestCodaAnchorGenerator:
    """Anchors and rungs derived from the outgoing track's validated coda zone."""

    def _profile_ctx(self, zone: CodaZone, **overrides: object) -> TransitionContext:
        """Build a context with a real outgoing band profile and the given coda zone."""
        profile = _analysis_with_bands(0.3, 0.3, 0.3, 0.3, duration=240.0)
        merged: dict[str, object] = {
            "coda_zone": zone,
            "outgoing_profile": build_band_profile(profile),
            "buffer_offset": 195.0,
            **overrides,
        }
        return _base_ctx(**merged)

    def test_emits_nothing_when_zone_is_none(self) -> None:
        """No coda zone means no coda-anchored candidates at all."""
        ctx = _base_ctx(coda_zone=None)
        assert list(CodaAnchorGenerator().generate(ctx)) == []

    def test_emits_nothing_when_outgoing_profile_missing(self) -> None:
        """A zone without a band profile to derive bar starts from emits nothing."""
        zone = CodaZone(start_s=200.0, end_s=216.0)
        ctx = _base_ctx(coda_zone=zone, outgoing_profile=None)
        assert list(CodaAnchorGenerator().generate(ctx)) == []

    def test_emits_top_rung_and_2_bar_floor_anchored_inside_the_zone(self) -> None:
        """An 8-bar-wide zone emits the 8-bar and 2-bar rungs, anchored at the zone's last bar."""
        zone = CodaZone(start_s=200.0, end_s=216.0)
        ctx = self._profile_ctx(zone)

        specs = list(CodaAnchorGenerator().generate(ctx))

        assert {s.bars for s in specs} == {8, 2}
        assert {s.anchor_s for s in specs} == {216.0 - 195.0}
        assert all(s.ideal_bars == 8 for s in specs)
        assert all(s.source == "coda-anchor" for s in specs)

    def test_emits_nothing_when_zone_shorter_than_smallest_rung(self) -> None:
        """A zone narrower than 2 bars fits no rung, so nothing is emitted."""
        zone = CodaZone(start_s=200.0, end_s=201.0)
        ctx = self._profile_ctx(zone)
        assert list(CodaAnchorGenerator().generate(ctx)) == []

    def test_emits_nothing_when_anchor_below_min_effective_buffer(self) -> None:
        """An anchor too close to the buffer offset leaves no usable overlap room."""
        zone = CodaZone(start_s=200.0, end_s=216.0)
        ctx = self._profile_ctx(zone, buffer_offset=210.0)
        assert list(CodaAnchorGenerator().generate(ctx)) == []


class TestProtectiveAnchorGenerator:
    """Anchors chosen to keep A's last outgoing vocal phrase intact."""

    def test_emits_nothing_when_out_placement_is_none(self) -> None:
        """No outgoing placement mask at all emits nothing."""
        ctx = _base_ctx(vocal_out_placement=None)
        assert list(ProtectiveAnchorGenerator().generate(ctx)) == []

    def test_emits_nothing_when_out_placement_has_no_windows(self) -> None:
        """An empty (no-vocal) outgoing placement mask emits nothing."""
        ctx = _base_ctx(vocal_out_placement=VocalMask(windows=[]))
        assert list(ProtectiveAnchorGenerator().generate(ctx)) == []

    def test_emits_ladder_at_earliest_qualifying_protective_downbeat(self) -> None:
        """Every ladder rung is anchored at the earliest protective downbeat past the phrase."""
        ctx = _base_ctx(
            vocal_out_placement=VocalMask(windows=[(0.0, 30.0)]),
            protective_downbeats=(10.0, 20.0, 32.0, 35.0, 40.0),
            audio_end=45.0,
        )
        specs = list(ProtectiveAnchorGenerator().generate(ctx))

        assert {s.bars for s in specs} == {8, 4, 2, 1}
        assert {s.anchor_s for s in specs} == {32.0}
        assert all(s.entry_s is None for s in specs)
        assert all(s.ideal_bars == 8 for s in specs)

    def test_falls_back_to_target_when_no_downbeat_qualifies(self) -> None:
        """No qualifying protective downbeat falls back to the (clamped) target itself."""
        ctx = _base_ctx(
            vocal_out_placement=VocalMask(windows=[(0.0, 30.0)]),
            protective_downbeats=(5.0, 10.0),
            audio_end=45.0,
        )
        specs = list(ProtectiveAnchorGenerator().generate(ctx))

        assert {s.anchor_s for s in specs} == {30.0}


class TestVocalOnsetEntryGenerator:
    """An entry that lands B's first vocal onset exactly at the overlap end."""

    def test_emits_nothing_when_in_placement_is_none(self) -> None:
        """No incoming placement mask at all emits nothing."""
        ctx = _base_ctx(vocal_in_placement=None)
        assert list(VocalOnsetEntryGenerator().generate(ctx)) == []

    def test_emits_nothing_when_in_placement_has_no_windows(self) -> None:
        """An empty (no-vocal) incoming placement mask emits nothing."""
        ctx = _base_ctx(vocal_in_placement=VocalMask(windows=[]))
        assert list(VocalOnsetEntryGenerator().generate(ctx)) == []

    def test_emits_nothing_when_placement_mask_is_saturated(self) -> None:
        """A near-continuous incoming vocal mask supplies no legal onset entry."""
        ctx = _base_ctx(vocal_in_placement=VocalMask(windows=[(0.0, 44.0)]))
        assert list(VocalOnsetEntryGenerator().generate(ctx)) == []

    def test_emits_onset_aligned_entry_at_the_ideal_rung(self) -> None:
        """The entry lands B's first onset exactly ``ideal_bars`` bars before the overlap end."""
        ctx = _base_ctx(vocal_in_placement=VocalMask(windows=[(20.0, 25.0)]))
        specs = list(VocalOnsetEntryGenerator().generate(ctx))

        assert len(specs) == 1
        (spec,) = specs
        assert spec.bars == 8
        assert spec.ideal_bars == 8
        assert spec.anchor_s is None
        assert spec.entry_s == 20.0 - 8 * (4 * 60.0 / 120.0)
        assert spec.source == "vocal-onset-entry"

    def test_emits_nothing_when_the_derived_entry_is_negative(self) -> None:
        """An onset too early in the buffer leaves no legal (non-negative) entry."""
        ctx = _base_ctx(vocal_in_placement=VocalMask(windows=[(5.0, 10.0)]))
        assert list(VocalOnsetEntryGenerator().generate(ctx)) == []

    def test_emits_nothing_when_the_derived_entry_falls_inside_another_window(self) -> None:
        """An onset-aligned entry landing inside a vocal window is not a legal cut."""
        ctx = _base_ctx(vocal_in_placement=VocalMask(windows=[(20.0, 25.0), (2.0, 5.0)]))
        assert list(VocalOnsetEntryGenerator().generate(ctx)) == []


class TestDefaultGenerators:
    """The standard generator set and its preference order."""

    def test_default_generators_preference_order(self) -> None:
        """Generators run best-first: energy ladder, coda anchor, protective anchor, onset entry."""
        names = [g.name for g in default_generators()]
        assert names == [
            "energy-ladder",
            "coda-anchor",
            "protective-anchor",
            "vocal-onset-entry",
        ]
