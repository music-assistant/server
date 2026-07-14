"""
Smart Fades - Transition planners.

A planner is the DJ brain of smart fades: it turns the two tracks' stored
``AudioAnalysisData`` into a ``TransitionPlan`` — a pure decision with no audio
bytes and no FFmpeg filters.  Like a real DJ it prepares the decks, chooses the
overlap, chooses a tempo ramp, locks the timing to downbeats and chooses the EQ
handover; the renderer then picks the matching tools from the filter toolset.
Alternative transition strategies slot in as sibling subclasses of
``TransitionPlanner``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.streams.smart_fades.bands import (
    build_band_profile,
    detect_low_groove_entry,
    detect_low_mix_out,
    loudness_referenced_level,
    smoothstep,
    window_duty,
    window_fraction,
    window_level,
)
from music_assistant.controllers.streams.smart_fades.filters import ShelfType
from music_assistant.controllers.streams.smart_fades.helpers import (
    MIN_EFFECTIVE_FADE_BUFFER,
    MIX_OUT_ENERGY_FRACTION,
    SMART_CROSSFADE_DURATION,
    compute_gradual_tempo_steps,
    db_ramp,
    detect_effective_audio_end,
    detect_groove_entry,
    detect_mix_out_point,
    extrapolate_downbeats,
    generate_synthetic_timestamps,
    keys_compatible,
)
from music_assistant.controllers.streams.smart_fades.models import (
    BAND_RMS_BANDS,
    BandProfile,
    Deck,
    EqPlan,
    FadeOutTrim,
    PlanMetrics,
    ShelfSchedule,
    SmartFadeNotApplicable,
    TempoPlan,
    TransitionPlan,
    TransitionStrategy,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.structure import (
    CodaZone,
    detect_coda_zone,
    detect_mastered_fadeout,
    point_in_mask,
)
from music_assistant.controllers.streams.smart_fades.vocal import (
    COLLISION_SECONDS_LIMIT,
    MAX_HANDOFF_SECONDS,
    MAX_VOCAL_GAP,
    MIN_HANDOFF_SECONDS,
    MIN_VOCAL_GAP,
    MIN_VOCAL_RUN,
    SHORT_FADE_SECONDS,
    VOCAL_CLOSE_THRESHOLD,
    VOCAL_LEFT_PADDING,
    VOCAL_OPEN_THRESHOLD,
    VOCAL_RIGHT_PADDING,
    WEIGHTED_COLLISION_LIMIT,
    VocalHysteresisConfig,
    VocalMask,
    VocalTimeline,
    build_vocal_windows,
    collision_metrics,
    merge_windows,
    parse_vocal_probabilities,
)

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

    from music_assistant.models.audio_analysis import AudioAnalysisData


class _DemoteTier:
    """Sentinel: the rolling-intro deep-trim guard found no legal cut and wants a shorter overlap."""


_DEMOTE_TIER = _DemoteTier()


def _band_gain(
    schedule: ShelfSchedule | None,
    rendered_t: float,
    cf_start_input: float,
    ratio: float,
    *,
    side: str,
) -> float:
    """Gain (dB) of a band schedule at rendered overlap time; ``None`` is 0dB."""
    if schedule is None:
        return 0.0
    # A-side schedules live in pre-stretch input time; B-side already in rendered time
    schedule_time = cf_start_input + rendered_t * ratio if side == "A" else rendered_t
    return schedule.gain_at(schedule_time)


class TransitionPlanner(ABC):
    """Abstract base class for transition planners."""

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize the planner."""
        self.logger = logger

    @abstractmethod
    def plan(
        self,
        fade_out_analysis: AudioAnalysisData,
        fade_in_analysis: AudioAnalysisData,
        buffer_duration: float,
    ) -> TransitionPlan:
        """
        Build a ``TransitionPlan`` from the two tracks' analysis data.

        Pure over the analysis rows and the available holdback window — touches
        no audio bytes.  Raises ``SmartFadeNotApplicable`` when the tracks cannot
        yield this transition and the caller should fall back.

        :param fade_out_analysis: Analysis data for the outgoing track.
        :param fade_in_analysis: Analysis data for the incoming track.
        :param buffer_duration: Length in seconds of the available fade-out holdback.
        """


class SmartCrossFadePlanner(TransitionPlanner):
    """Plans a defensive, musically-aligned crossfade that never edits the music."""

    # Only apply time stretching if BPM difference is < this %
    # (research: the tier-1 dance-cluster population triples at ±8 vs ±5)
    time_stretch_bpm_percentage_threshold: float = 8.0

    # Fraction of sustained energy below which the outro no longer carries the mix
    mix_out_energy_fraction: float = MIX_OUT_ENERGY_FRACTION

    # Overlap length per tier, in bars of the outgoing grid (research: real DJ
    # transitions cluster at 32 beats = 8 bars; the doubled 16-bar blend is
    # earned only when FireRed shows both decks near-instrumental, where the
    # long exposure carries no vocal-collision risk)
    full_blend_bars: int = 8
    instrumental_blend_bars: int = 16
    # vocal duty (unpadded mask coverage) at or under this on BOTH decks
    # qualifies as near-instrumental
    instrumental_duty_max: float = 0.05
    tempo_blend_bars: int = 8
    # QUICK_FADE bars by BPM incompatibility: (max diff %, bars); beyond -> 1 bar
    quick_fade_ladder: tuple[tuple[float, int], ...] = ((12.0, 4), (20.0, 2))

    # Bass-swap EQ: shelf corners/depths as on real club mixers
    low_shelf_freq: int = 100
    high_shelf_freq: int = 13000
    eq_kill_db: float = -26.0
    high_ease_db: float = -20.0
    # bass handover spans half the overlap; <2 bars reads as an event, >8 bars is masked
    bass_swap_fraction: float = 0.5
    bass_swap_min_bars: int = 2
    bass_swap_max_bars: int = 8
    # decision window for the reciprocal swap depth gates, in A/B-bars
    bass_swap_window_bars: int = 8

    # Reciprocal bass-swap gate: smoothstep each side's low-band fraction (over the
    # OTHER deck's window) to scale its own kill depth; dropped below eq_bypass_below_db.
    # Corridor sits deliberately below the published dance-master figures (~0.34-0.45
    # of power under 120Hz; pop ~0.14 — Elowsson & Friberg 2017, Pestana et al. 2013):
    # it reads an 8-bar transition window, not a whole-track LTAS, and putting lo under
    # the pop mean lets ordinary pop still earn a partial swap. Final values corpus-tuned.
    low_gate_lo: float = 0.10
    low_gate_hi: float = 0.25
    eq_bypass_below_db: float = -6.0

    # Reciprocal high-ease gate, same shape on the high band. Mean-music LTAS is ~0.02 in
    # 4-11kHz (Elowsson & Friberg 2017): lo = an average track earns no duck, hi = clearly
    # brighter than average earns the full ease. A deck whose own-window high level is below
    # high_own_side_floor of its own reference gets no shelf at all.
    high_gate_lo: float = 0.02
    high_gate_hi: float = 0.06
    high_own_side_floor: float = 0.25
    # Cymbal-wash mode: when both own-windows read bright and comparably loud a plain
    # reciprocal ease would stack their highs, so duck A complementary with B's restore.
    wash_duty: float = 0.8
    wash_depth_db: float = -26.0
    wash_level_tolerance_db: float = 6.0
    wash_min_blend_bars: int = 8

    # Measured mid-band (vocal) swap: bass-swap gate shape on the mid band, gated also on
    # duty so one loud mid bar can't unlock a swap over otherwise instrumental material.
    mid_freq: int = 1200
    mid_width_oct: float = 2.5
    # Corridor sits in the gap between vocal-forward mixes (~0.25-0.45 mid fraction) and
    # instrumental dance (~0.10-0.15); absolute placement verified on our unweighted
    # pipeline (LTAS: Elowsson & Friberg 2017, Pestana et al. 2013).
    mid_gate_lo: float = 0.18
    mid_gate_hi: float = 0.30
    mid_duty_lo: float = 0.60
    mid_duty_hi: float = 0.85
    # the mid (vocal) swap is capped shallower on a tempo blend than a full blend
    mid_cap_full_db: float = -8.0
    mid_cap_tempo_db: float = -6.0
    mid_bypass_below_db: float = -1.0

    # A track qualifies for kick-following anchors when its median active-bar
    # low-band power fraction reaches this share of its total power
    low_timing_eligibility: float = 0.10
    # Reference multiplier a bar's low power must reach to count as "kick present"
    low_anchor_bar_fraction: float = 0.5
    # Beyond this many outgoing bars of disagreement with the full-band anchor,
    # the low anchor is untrustworthy and the full-band anchor wins
    low_anchor_divergence_bars: int = 16

    # A bar of B is "protected" (never cut into) when its low band stays under
    # this fraction of the track's low reference power...
    trim_guard_low_floor: float = 0.25
    # ...while voice or melody still carries this fraction of the low-mid or
    # mid reference power - low-silent but vocally active, e.g. a sung pickup
    trim_guard_voice_floor: float = 0.4

    # Dip guard: combined qsin-weighted power of both decks may never sag more than this
    # below its plateau across the overlap (outside the intentional bass-handover notch).
    max_predicted_dip_db: float = 3.0

    # D2 mastered-fadeout gates: engineered fades run 5-15s past -10dB with a
    # frozen spectrum (a post-mix gain ramp); musical decrescendos rarely
    # exceed ~6dB and re-orchestrate (band fractions move)
    fade_min_bars: int = 4
    fade_drop_db: float = 10.0
    fade_monotone_share: float = 0.8
    fade_jitter_db: float = 0.5
    fade_frac_drift: float = 0.15
    fade_audible_floor: float = 0.01
    fade_frac_floor: float = 0.10
    # D4 coda gates: the bed must still carry audible program under an
    # equal-power fade; one musical gesture minimum; designed outros sustain
    # while any fade halves across the zone
    coda_total_floor: float = 0.15
    coda_min_seconds: float = 4.0
    coda_min_bars: int = 2
    coda_level_hold: float = 0.5

    # FireRed hysteresis: a run opens at OPEN and only closes below CLOSE so a phrase's
    # quieter syllables don't fragment its window; padding absorbs the detector's
    # attack/release lag and the gap bridges a breath inside one phrase.
    vocal_open_threshold: float = VOCAL_OPEN_THRESHOLD
    vocal_close_threshold: float = VOCAL_CLOSE_THRESHOLD
    vocal_left_padding: float = VOCAL_LEFT_PADDING
    vocal_right_padding: float = VOCAL_RIGHT_PADDING
    vocal_min_run: float = MIN_VOCAL_RUN
    vocal_min_gap: float = MIN_VOCAL_GAP
    vocal_max_gap: float = MAX_VOCAL_GAP
    # Two-sided vocal-collision guard, in rendered-crossfade seconds; weighted uses the
    # acrossfade curve's simultaneous-power integral (4*phase*(1-phase))
    collision_seconds_limit: float = COLLISION_SECONDS_LIMIT
    weighted_collision_limit: float = WEIGHTED_COLLISION_LIMIT
    # click-free equal-power fallback when no phrased candidate avoids collision
    min_handoff_seconds: float = MIN_HANDOFF_SECONDS
    max_handoff_seconds: float = MAX_HANDOFF_SECONDS
    # crossfades at or under this length may never drop more audible outgoing
    # material than the overlap itself covers
    short_fade_seconds: float = SHORT_FADE_SECONDS

    # Working state for one plan() run, (re)set by _prepare_decks
    outgoing: Deck
    incoming: Deck
    effective_end: float
    # outgoing media time minus buffer-local time, i.e. media_time = buffer_local + offset
    _buffer_offset: float
    fadeout_trim: FadeOutTrim | None
    extrapolated_downbeats: npt.NDArray[np.float32]
    outgoing_profile: BandProfile | None
    incoming_profile: BandProfile | None
    # incoming groove entry (media time), computed once so the swap point and
    # the reciprocal decision windows agree
    _incoming_entry: float
    # set by _choose_tier: True when the two decks don't share a meter
    _cross_meter: bool = False
    # set by _choose_eq: the low crossover's rendered ramp window, exempt from
    # the dip guard (its notch is the intentional bass-handover gesture)
    _swap_notch: tuple[float, float]
    # the full available holdback, and the unmasked (only >=0) buffer-local grids:
    # a vocal re-anchor may move LATER than effective_end but never past _audio_end
    _buffer_duration: float
    _grid_beats: npt.NDArray[np.float32]
    _grid_downbeats: npt.NDArray[np.float32]
    # the RMS-audible boundary (never downbeat-snapped): a hard cap no re-anchor,
    # vocal-protected or not, may ever move the tail anchor past
    _audio_end: float
    # outgoing downbeats extrapolated up to _audio_end, used only to find a
    # vocal-protective anchor between the (possibly snapped) effective_end and it
    _protective_downbeats: npt.NDArray[np.float32]
    # pristine copies of the state _cue_outgoing_tail produced: the clean slate
    # every candidate build restores first, so candidates never leak state
    _pristine_effective_end: float
    _pristine_fadeout_trim: FadeOutTrim | None
    _pristine_beats: npt.NDArray[np.float32]
    _pristine_downbeats: npt.NDArray[np.float32]
    _pristine_extrapolated_downbeats: npt.NDArray[np.float32]
    # validated FireRed vocal-activity windows for each deck; None on either
    # side (missing/invalid/older/partial data) disables all vocal-aware logic.
    # The padded masks place cuts and anchors; the unpadded scoring masks feed
    # the collision guard (padding is silence — it must not fail a candidate)
    _vocal_out_mask: VocalMask | None
    _vocal_in_mask: VocalMask | None
    _vocal_out_scoring: VocalMask | None
    _vocal_in_scoring: VocalMask | None

    def plan(
        self,
        fade_out_analysis: AudioAnalysisData,
        fade_in_analysis: AudioAnalysisData,
        buffer_duration: float,
    ) -> TransitionPlan:
        """
        Build a smart-crossfade ``TransitionPlan`` from the two tracks' analysis.

        Vocal-aware logic engages only when both tracks carry a validated
        FireRed vocal-activity timeline; otherwise this returns exactly the
        energy-only plan, with default (energy-only) metrics.

        :param fade_out_analysis: Analysis data for the outgoing track.
        :param fade_in_analysis: Analysis data for the incoming track.
        :param buffer_duration: Length in seconds of the available fade-out holdback.
        """
        self._prepare_decks(fade_out_analysis, fade_in_analysis, buffer_duration)
        tier = self._choose_tier()
        bars0, cand0 = self._energy_candidate(tier)
        if self._vocal_out_mask is None or self._vocal_in_mask is None:
            return cand0
        return self._plan_vocal_aware(tier, bars0, cand0)

    @property
    def _bpm_ratio(self) -> float:
        """Tempo ratio between the incoming and outgoing track."""
        return self.incoming.bpm / self.outgoing.bpm

    @property
    def _bpm_diff_percent(self) -> float:
        """Tempo difference between the two decks as a percentage."""
        return abs(1.0 - self._bpm_ratio) * 100

    def _prepare_decks(
        self,
        fade_out_analysis: AudioAnalysisData,
        fade_in_analysis: AudioAnalysisData,
        buffer_duration: float,
    ) -> None:
        """Load both tracks onto the decks, cue the outgoing tail and detect B's entry."""
        # numpy is imported inside the methods here to keep it off the server startup path
        import numpy as np  # noqa: PLC0415

        if (
            fade_out_analysis.bpm is None
            or fade_in_analysis.bpm is None
            or fade_out_analysis.beats is None
            or fade_in_analysis.beats is None
        ):
            raise ValueError("AudioAnalysisData must have bpm and beats set for smart crossfade")
        # AudioAnalysisData stores the grids as plain float lists (numpy-free model);
        # the planner works in numpy, so convert once here.
        incoming_beats = np.asarray(fade_in_analysis.beats, dtype=np.float32)
        incoming_downbeats = (
            np.asarray(fade_in_analysis.downbeats, dtype=np.float32)
            if fade_in_analysis.downbeats is not None
            else incoming_beats
        )
        self.incoming = Deck(
            analysis=fade_in_analysis,
            bpm=fade_in_analysis.bpm,
            # Only beats within the buffered head are usable for alignment decisions
            beats=incoming_beats[incoming_beats <= SMART_CROSSFADE_DURATION],
            downbeats=incoming_downbeats[incoming_downbeats <= SMART_CROSSFADE_DURATION],
            beats_per_bar=fade_in_analysis.beats_per_bar or 4,
        )
        self.outgoing = Deck(
            analysis=fade_out_analysis,
            bpm=fade_out_analysis.bpm,
            # Raw full-track grids; the shift to buffer-local coordinates happens
            # in _cue_outgoing_tail where the actual buffer length is known
            beats=np.asarray(fade_out_analysis.beats, dtype=np.float32),
            downbeats=(
                np.asarray(fade_out_analysis.downbeats, dtype=np.float32)
                if fade_out_analysis.downbeats is not None
                else np.array([], dtype=np.float32)
            ),
            beats_per_bar=fade_out_analysis.beats_per_bar or 4,
        )
        self.outgoing_profile = build_band_profile(fade_out_analysis)
        self.incoming_profile = build_band_profile(fade_in_analysis)
        self._cue_outgoing_tail(buffer_duration)
        self.extrapolated_downbeats = extrapolate_downbeats(
            self.outgoing.downbeats,
            buffer_size=self.effective_end,
            bpm=self.outgoing.bpm,
            beats_per_bar=self.outgoing.beats_per_bar,
        )
        # Extrapolated up to the true RMS-audible boundary rather than the
        # (possibly downbeat-snapped) anchor, so a vocal re-anchor can always
        # find a real downbeat between the two
        self._protective_downbeats = extrapolate_downbeats(
            self._grid_downbeats,
            buffer_size=self._audio_end,
            bpm=self.outgoing.bpm,
            beats_per_bar=self.outgoing.beats_per_bar,
        )
        # Computed once so the swap point and the reciprocal decision windows agree
        self._incoming_entry = self._detect_incoming_entry()
        # Snapshot the tail state produced above: every candidate build restores
        # this clean slate first, so a re-anchored candidate never leaks into the next
        self._buffer_duration = buffer_duration
        self._pristine_effective_end = self.effective_end
        self._pristine_fadeout_trim = self.fadeout_trim
        self._pristine_beats = self.outgoing.beats
        self._pristine_downbeats = self.outgoing.downbeats
        self._pristine_extrapolated_downbeats = self.extrapolated_downbeats
        self._prepare_vocal_masks(fade_out_analysis, fade_in_analysis)
        # Additional verbose logging to debug rare failures
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "SmartCrossFade plan: fade_out: %s, fade_in: %s",
            fade_out_analysis,
            fade_in_analysis,
        )

    def _cue_outgoing_tail(self, buffer_duration: float) -> None:
        """Anchor the outgoing tail at its energy mix-out point, snap to a downbeat, shift grids."""
        self.fadeout_trim = None
        # ACTUAL buffer length, not the constant 45s: the holdback yield loop leaves
        # up to ~1s less depending on chunk boundaries, and every buffer-local
        # coordinate below (mix-out detection, grid shift) must agree on it
        self._buffer_offset = max(0.0, (self.outgoing.analysis.duration or 0.0) - buffer_duration)
        self._buffer_duration = buffer_duration
        silence_end = detect_effective_audio_end(
            self.outgoing.analysis.rms_energy,
            self.outgoing.analysis.duration,
            buffer_duration,
        )
        mix_out = detect_mix_out_point(
            self.outgoing.analysis.rms_energy,
            self.outgoing.analysis.duration,
            buffer_duration,
            self.outgoing.bpm,
            fraction=self.mix_out_energy_fraction,
            beats_per_bar=self.outgoing.beats_per_bar,
        )
        mix_out = self._apply_low_mix_out(mix_out)
        self.effective_end = min(silence_end, mix_out)
        if self.effective_end < MIN_EFFECTIVE_FADE_BUFFER:
            raise SmartFadeNotApplicable(
                f"outgoing tail is mostly silent ({self.effective_end:.1f}s audible)"
            )

        # Shift fade-out beats from full-track to buffer-local coordinates
        beats = self.outgoing.beats - self._buffer_offset
        downbeats = self.outgoing.downbeats - self._buffer_offset

        # Unmasked (only dropping pre-buffer beats) grids: a later vocal/coda re-anchor
        # reads from these rather than re-deriving buffer-local coordinates
        self._grid_beats = beats[beats >= 0.0]
        self._grid_downbeats = downbeats[downbeats >= 0.0]

        # the RMS-audible boundary: a hard upper bound a later vocal/coda re-anchor
        # may extend the (about to be downbeat-snapped) anchor back up to, never past
        self._audio_end = min(silence_end, buffer_duration)

        # Sub-half-second slack is not worth trimming: RMS bin granularity is
        # ~0.1-0.2s for typical track lengths, so finer precision is illusory
        if self.effective_end >= buffer_duration - 0.5:
            # Without the trim the rendered stream still ends at buffer_duration,
            # so the anchor must follow it or every schedule lands early
            self.effective_end = buffer_duration
        else:
            # Snap the anchor back to the last real downbeat within ~2 bars so the
            # crossfade ends cleanly on the 1 rather than at an arbitrary RMS bin edge
            bar_seconds = self.outgoing.beats_per_bar * 60.0 / self.outgoing.bpm
            in_window = self._grid_downbeats[self._grid_downbeats <= self.effective_end]
            if len(in_window) and self.effective_end - float(in_window[-1]) < 2 * bar_seconds:
                self.effective_end = float(in_window[-1])
            if self.effective_end < MIN_EFFECTIVE_FADE_BUFFER:
                raise SmartFadeNotApplicable(
                    f"outgoing tail too short after anchoring ({self.effective_end:.1f}s)"
                )
            self.fadeout_trim = FadeOutTrim(
                end_pos=self.effective_end,
                trimmed_seconds=buffer_duration - self.effective_end,
            )

        # Mask fade-out beats to the anchored window; negative timestamps are
        # beats before the buffer, beats past effective_end sit in the dropped tail
        self.outgoing.beats = self._grid_beats[self._grid_beats <= self.effective_end]
        self.outgoing.downbeats = self._grid_downbeats[self._grid_downbeats <= self.effective_end]

    def _prepare_vocal_masks(
        self, fade_out_analysis: AudioAnalysisData, fade_in_analysis: AudioAnalysisData
    ) -> None:
        """Validate and build both decks' FireRed vocal-activity masks, if the contract holds."""
        out_timeline = parse_vocal_probabilities(fade_out_analysis)
        in_timeline = parse_vocal_probabilities(fade_in_analysis)
        if out_timeline is None or in_timeline is None:
            self._vocal_out_mask = None
            self._vocal_in_mask = None
            self._vocal_out_scoring = None
            self._vocal_in_scoring = None
            return
        duration = fade_out_analysis.duration or self._buffer_offset
        config = VocalHysteresisConfig(
            open_threshold=self.vocal_open_threshold,
            close_threshold=self.vocal_close_threshold,
            left_padding=self.vocal_left_padding,
            right_padding=self.vocal_right_padding,
            min_run=self.vocal_min_run,
            min_gap=self.vocal_min_gap,
            max_gap=self.vocal_max_gap,
        )
        # the scoring variant differs only in padding: padded edges are silence,
        # so they place cuts and anchors but never count as collision
        scoring_config = replace(config, left_padding=0.0, right_padding=0.0)
        self._vocal_out_mask = self._build_outgoing_mask(out_timeline, duration, config)
        self._vocal_out_scoring = self._build_outgoing_mask(out_timeline, duration, scoring_config)
        self._vocal_in_mask = self._build_incoming_mask(in_timeline, config)
        self._vocal_in_scoring = self._build_incoming_mask(in_timeline, scoring_config)

    def _build_outgoing_mask(
        self, timeline: VocalTimeline, duration: float, config: VocalHysteresisConfig
    ) -> VocalMask:
        """
        Build the outgoing deck's buffer-local vocal mask for one config.

        :param timeline: The outgoing deck's validated FireRed timeline.
        :param duration: The outgoing track's media duration in seconds.
        :param config: Hysteresis/padding thresholds for the mask.
        """
        media_time_mask = build_vocal_windows(
            timeline.probabilities,
            timeline.frame_duration,
            self._buffer_offset,
            duration,
            beat_duration=60.0 / self.outgoing.bpm,
            config=config,
        )
        # shift media time to buffer-local time, then clamp to the RMS-audible
        # boundary: FireRed may never be trusted to extend it, so any window
        # (or trailing sliver of one) past it is simply dropped
        buffer_local = VocalMask(
            windows=[
                (left - self._buffer_offset, right - self._buffer_offset)
                for left, right in media_time_mask.windows
            ]
        )
        return buffer_local.clamped_to(self._audio_end)

    def _build_incoming_mask(
        self, timeline: VocalTimeline, config: VocalHysteresisConfig
    ) -> VocalMask:
        """
        Build the incoming deck's head vocal mask for one config.

        :param timeline: The incoming deck's validated FireRed timeline.
        :param config: Hysteresis/padding thresholds for the mask.
        """
        return build_vocal_windows(
            timeline.probabilities,
            timeline.frame_duration,
            0.0,
            float(SMART_CROSSFADE_DURATION),
            beat_duration=60.0 / self.incoming.bpm,
            config=config,
        )

    def _restore_pristine_tail(self) -> None:
        """Reset the tail state to what _cue_outgoing_tail produced, undoing any prior re-anchor."""
        self.effective_end = self._pristine_effective_end
        self.fadeout_trim = self._pristine_fadeout_trim
        self.outgoing.beats = self._pristine_beats
        self.outgoing.downbeats = self._pristine_downbeats
        self.extrapolated_downbeats = self._pristine_extrapolated_downbeats

    def _re_anchor(self, anchor: float) -> None:
        """Move the tail anchor to a new position, never later than the RMS-audible boundary."""
        self.effective_end = min(anchor, self._audio_end)
        # same sub-half-second slack rule as _cue_outgoing_tail: the rendered
        # stream still ends at the buffer end, so the anchor must follow it
        if self.effective_end >= self._buffer_duration - 0.5:
            self.effective_end = self._buffer_duration
            self.fadeout_trim = None
        else:
            self.fadeout_trim = FadeOutTrim(
                end_pos=self.effective_end,
                trimmed_seconds=self._buffer_duration - self.effective_end,
            )
        self.outgoing.beats = self._grid_beats[self._grid_beats <= self.effective_end]
        self.outgoing.downbeats = self._grid_downbeats[self._grid_downbeats <= self.effective_end]
        # _protective_downbeats reaches all the way to _audio_end, so it covers
        # any position this re-anchor could have chosen, unlike the pristine array
        self.extrapolated_downbeats = self._protective_downbeats[
            self._protective_downbeats <= self.effective_end
        ]

    def _bars_ladder(self, tier: TransitionTier) -> list[int]:
        """Candidate bar counts to try for a tier, largest first (shorter rungs fit smaller buffers)."""
        if tier is TransitionTier.QUICK_FADE:
            # a mismatched meter has no shared bar grid to blend across; cap short
            # regardless of how close the tempos happen to be
            ladder = ((0.0, 2),) if self._cross_meter else self.quick_fade_ladder
            ideal = next((bars for limit, bars in ladder if self._bpm_diff_percent <= limit), 1)
        elif tier is TransitionTier.TEMPO_BLEND:
            ideal = self.tempo_blend_bars
        elif self._earns_instrumental_blend():
            ideal = self.instrumental_blend_bars
        else:
            ideal = self.full_blend_bars
        return [bars for bars in (16, 8, 4, 2, 1) if bars <= ideal]

    def _earns_instrumental_blend(self) -> bool:
        """Whether verified near-instrumental decks earn the doubled full-blend overlap."""
        if self._vocal_out_scoring is None or self._vocal_in_scoring is None:
            return False
        out_duty = sum(right - left for left, right in self._vocal_out_scoring.windows) / max(
            self._audio_end, 0.001
        )
        in_duty = sum(right - left for left, right in self._vocal_in_scoring.windows) / float(
            SMART_CROSSFADE_DURATION
        )
        return out_duty <= self.instrumental_duty_max and in_duty <= self.instrumental_duty_max

    def _build_candidate(
        self,
        bars: int,
        anchor: float | None = None,
        entry: float | None = None,
        *,
        pin_entry: bool = False,
        force_mid_swap: bool = False,
    ) -> TransitionPlan | None:
        """
        Build one complete, self-contained candidate plan.

        Always starts from the tail state ``_cue_outgoing_tail`` produced, so a
        candidate never inherits leftover state from a previously built one;
        every timing, tempo, trim and EQ decision is recomputed for the (possibly
        re-anchored) tail.  Returns ``None`` when ``bars`` needs more room than
        the incoming buffer has (the caller should try a smaller rung); a 1-bar
        request never fails this way, matching the plan's floor.

        :param bars: Outgoing bar count the overlap should span.
        :param anchor: Buffer-local position to re-anchor the tail to before
            building, or ``None`` to keep the pristine (mix-out) anchor.
        :param entry: Explicit incoming fade-in entry (media seconds); ``None``
            lets the planner align the entry from the beat grids.
        :param pin_entry: Preserve ``entry`` as-is, including an unaligned
            ``None`` entry, instead of choosing a new one.
        :param force_mid_swap: Force the mid (vocal) EQ swap on, even when the
            measured mid content wouldn't normally unlock it (remediation floor).
        """
        self._restore_pristine_tail()
        if anchor is not None:
            self._re_anchor(anchor)
        tier = self._choose_tier()
        # a re-anchored tail can downgrade the tier (shorter/irregular grid); the
        # requested bar count still reflects the old tier, so cap it at the new
        # tier's largest rung or a long overlap ships without its tempo ramp
        bars = min(bars, self._bars_ladder(tier)[0])

        fadein_start_pos = (
            entry if pin_entry or entry is not None else self._choose_fadein_entry(bars)
        )
        if bars > 1 and fadein_start_pos is None:
            return None
        crossfade_duration = self._calculate_crossfade_duration(bars)

        tempo_plan = self._choose_tempo_ramp(tier, crossfade_duration)
        crossfade_duration, fadein_trim_start = self._lock_in_timing(
            crossfade_duration, fadein_start_pos, tempo_plan
        )
        if bars > 1 and fadein_start_pos is not None and fadein_trim_start is None:
            return None
        # Rolling-intro alignment: on a full blend with no pinned entry, deepen B's
        # trim so its groove entry lands at the overlap END (B's intro runs under A,
        # its drop hits where A's music dies). A sung run that no legal cut clears
        # returns None so the caller's ladder drops to a shorter overlap instead —
        # except at the 1-bar floor, which must always yield a candidate: there the
        # un-deepened trim ships as-is.
        if tier is TransitionTier.FULL_BLEND and entry is None and not pin_entry:
            aligned = self._align_rolling_intro(crossfade_duration, fadein_trim_start)
            if isinstance(aligned, _DemoteTier):
                if bars > 1:
                    return None
            else:
                fadein_trim_start = aligned

        eq_plan = self._choose_eq(
            crossfade_duration, tempo_plan, fadein_trim_start, tier, force_mid_swap=force_mid_swap
        )
        return TransitionPlan(
            tier=tier,
            fade_out_window=self.effective_end,
            crossfade_duration=crossfade_duration,
            eq_plan=eq_plan,
            tempo_plan=tempo_plan,
            fadeout_trim=self.fadeout_trim,
            fadein_trim_start=fadein_trim_start,
        )

    def _energy_candidate(self, tier: TransitionTier) -> tuple[int, TransitionPlan]:
        """Build candidate 0: the largest tier rung whose overlap fits both buffers (1 always fits)."""
        for bars in self._bars_ladder(tier):
            candidate = self._build_candidate(bars)
            if candidate is not None:
                return bars, candidate
        raise AssertionError("unreachable: the 1-bar rung always yields a candidate")

    def _plan_vocal_aware(
        self, tier: TransitionTier, bars0: int, cand0: TransitionPlan
    ) -> TransitionPlan:
        """
        Layer FireRed vocal awareness over candidate 0.

        Remediates a colliding candidate 0 (retrim, coda-shift, shrink, floor),
        then always protects the outgoing vocal; when the protected plan still
        collides, ships the click-free equal-power handoff.  This is the whole
        vocal policy order: energy candidate/remediation, outgoing
        protection/rebuild, collision guard, short handoff.
        """
        bars, plan = bars0, cand0
        if self._guard_fires(cand0):
            bars, plan = self._remediate(tier, bars0, cand0)
        protected, metrics = self._protect_outgoing_vocal(bars, plan)
        if (
            metrics.collision_seconds < self.collision_seconds_limit
            and metrics.weighted_collision_seconds < self.weighted_collision_limit
        ):
            return replace(protected, metrics=metrics)
        return self._build_short_handoff(protected, metrics.anchor_on_downbeat)

    def _guard_fires(self, plan: TransitionPlan) -> bool:
        """Whether a candidate's rendered overlap breaches the two-sided vocal-collision guard."""
        metrics = self._score_candidate(plan, anchor_on_downbeat=False)
        return (
            metrics.collision_seconds >= self.collision_seconds_limit
            or metrics.weighted_collision_seconds >= self.weighted_collision_limit
        )

    def _remediate(
        self, tier: TransitionTier, bars0: int, cand0: TransitionPlan
    ) -> tuple[int, TransitionPlan]:
        """
        Walk the remediation ladder for a guard-fired candidate 0; the first guard-pass ships.

        Rungs, in order: retrim B so its vocal onset lands at the overlap end;
        shift the anchor into a validated instrumental coda; shrink the overlap
        at the pinned anchor; and finally the minimum-tier floor with the mid
        (vocal) swap forced on to mask the residual.  Returns the winning
        ``(bars, plan)`` so the caller can protect the outgoing vocal on it.

        :param tier: Candidate 0's transition tier.
        :param bars0: Candidate 0's outgoing bar count.
        :param cand0: The guard-fired energy candidate.
        """
        assert self._vocal_out_mask is not None  # narrowed: plan() gates on both masks
        assert self._vocal_in_mask is not None
        duration = self.outgoing.analysis.duration or 0.0
        tail_start = max(0.0, duration - self._buffer_duration)
        bar_b = self.incoming.beats_per_bar * 60.0 / self.incoming.bpm
        media_out = self._vocal_out_media_mask()

        # fade detection is remediation-scoped: it may never touch candidate 0
        fade_onset = (
            detect_mastered_fadeout(
                self.outgoing_profile,
                tail_start,
                duration,
                min_bars=self.fade_min_bars,
                drop_db=self.fade_drop_db,
                monotone_share=self.fade_monotone_share,
                jitter_db=self.fade_jitter_db,
                frac_drift=self.fade_frac_drift,
                audible_floor=self.fade_audible_floor,
                frac_floor=self.fade_frac_floor,
            )
            if self.outgoing_profile is not None
            else None
        )
        a_pin = self._pristine_effective_end
        if fade_onset is not None:
            # never pin inside A's own vocal window: exiting mid-phrase is the exact
            # defect this ladder fixes, so the vocal end floors the pin
            lead_end = media_out.windows[-1][1] if media_out.windows else 0.0
            onset_local = max(fade_onset, lead_end) - self._buffer_offset
            if onset_local >= MIN_EFFECTIVE_FADE_BUFFER:
                a_pin = min(a_pin, onset_local)

        # Rung 1: retrim B so its vocal onset lands exactly at the overlap end
        in_saturated = self._mask_saturated(self._vocal_in_mask, float(SMART_CROSSFADE_DURATION))
        if not in_saturated and self._vocal_in_mask.windows:
            entry = self._vocal_in_mask.windows[0][0] - bars0 * bar_b
            if entry >= 0.0 and not point_in_mask(self._vocal_in_mask, entry):
                cand = self._build_candidate(bars0, anchor=a_pin, entry=entry)
                if cand is not None and not self._guard_fires(cand):
                    self._log_rung(1, cand)
                    return bars0, cand

        # Rung 2: shift the anchor into a validated coda (a saturated A supplies
        # no fine structure, so it never earns the shift)
        out_saturated = self._mask_saturated(media_out, max(0.001, duration - tail_start))
        anchor_outro, zone = self._coda_anchor(media_out, fade_onset, tail_start, out_saturated)
        if anchor_outro is not None and zone is not None:
            bar_a = self.outgoing.beats_per_bar * 60.0 / self.outgoing.bpm
            zone_bars = int((zone.end_s - zone.start_s) / bar_a)
            lz = next((n for n in (16, 8, 4, 2) if n <= zone_bars), None)
            if lz is not None:
                for length in dict.fromkeys((lz, 2)):
                    for entry_opt in self._entry_options(length, bar_b):
                        cand = self._build_candidate(length, anchor=anchor_outro, entry=entry_opt)
                        if cand is not None and not self._guard_fires(cand):
                            self._log_rung(2, cand)
                            return length, cand

        # Rung 3: shrink at the pinned anchor (never at the coda anchor)
        for length in [n for n in (16, 8, 4, 2) if n < bars0]:
            for entry_opt in self._entry_options(length, bar_b):
                cand = self._build_candidate(length, anchor=a_pin, entry=entry_opt)
                if cand is not None and not self._guard_fires(cand):
                    self._log_rung(3, cand)
                    return length, cand

        # Rung 4 floor: today's worst case — minimum tier at the best anchor, with
        # the mid (vocal) swap forced on unconditionally to mask the residual
        anchor = anchor_outro if anchor_outro is not None else a_pin
        floor_bars = 1 if self._bpm_diff_percent > self.quick_fade_ladder[-1][0] else 2
        for length in dict.fromkeys((floor_bars, 1)):
            cand = self._build_candidate(
                length, anchor=anchor, entry=self._natural_entry(), force_mid_swap=True
            )
            if cand is not None:
                self._log_rung(4, cand)
                return length, cand
        raise AssertionError("unreachable: the 1-bar rung always yields a candidate")

    def _coda_anchor(
        self,
        media_out: VocalMask,
        fade_onset: float | None,
        tail_start: float,
        out_saturated: bool,
    ) -> tuple[float | None, CodaZone | None]:
        """Detect a validated outro/coda zone and its buffer-local re-anchor, if any."""
        if self.outgoing_profile is None or out_saturated:
            return None, None
        duration = self.outgoing.analysis.duration or 0.0
        zone = detect_coda_zone(
            self.outgoing_profile,
            media_out,
            self._coda_earliest(),
            fade_onset,
            tail_start,
            duration,
            total_floor=self.coda_total_floor,
            min_seconds=self.coda_min_seconds,
            min_bars=self.coda_min_bars,
            level_hold=self.coda_level_hold,
        )
        if zone is None:
            return None, None
        bar_starts = self.outgoing_profile.bar_starts
        in_zone = bar_starts[(bar_starts >= zone.start_s) & (bar_starts <= zone.end_s)]
        if not len(in_zone):
            return None, zone
        anchor_outro = float(in_zone[-1]) - self._buffer_offset
        if anchor_outro < MIN_EFFECTIVE_FADE_BUFFER:
            return None, zone
        return anchor_outro, zone

    def _coda_earliest(self) -> float:
        """Media time before which no coda bar may start: past A's vocal and its kick."""
        media_out = self._vocal_out_media_mask()
        lead_end = media_out.windows[-1][1] if media_out.windows else 0.0
        kick_end: float | None = None
        if self._is_low_timing_eligible(self.outgoing_profile):
            assert self.outgoing_profile is not None  # narrowed by the eligibility check
            kick_end = detect_low_mix_out(self.outgoing_profile, self.low_anchor_bar_fraction)
            if kick_end is not None:
                # detect_low_mix_out returns the last kick bar's START; the coda
                # begins after that bar rings out
                kick_end += self.outgoing.beats_per_bar * 60.0 / self.outgoing.bpm
        if kick_end is None:
            kick_end = self._buffer_offset + self._pristine_effective_end
        return max(lead_end, kick_end)

    def _entry_options(self, length: int, bar_b: float) -> list[float]:
        """B entries for a remediation candidate: exact groove alignment first, else natural."""
        import numpy as np  # noqa: PLC0415

        assert self._vocal_in_mask is not None  # narrowed by the caller
        options: list[float] = []
        deep = self._incoming_entry - length * bar_b
        if deep > 0.0 and len(self.incoming.downbeats):
            downbeats = self.incoming.downbeats
            snapped = float(downbeats[np.argmin(np.abs(downbeats - deep))])
            # no partial trims: a cut is only legal when it lands the groove at the
            # overlap end AND falls outside B's vocal windows
            if snapped >= 0.0 and not point_in_mask(self._vocal_in_mask, snapped):
                options.append(snapped)
        natural = self._natural_entry()
        if natural not in options:
            options.append(natural)
        return options

    def _natural_entry(self) -> float:
        """B's unaligned entry: its first downbeat (leading silence only)."""
        return float(self.incoming.downbeats[0]) if len(self.incoming.downbeats) else 0.0

    def _log_rung(self, rung: int, cand: TransitionPlan) -> None:
        """Log which remediation rung shipped and its final timeline."""
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Vocal-collision remediation rung %d: anchor=%.1fs bars~%.0f trim=%s",
            rung,
            cand.fade_out_window,
            cand.crossfade_duration / (self.incoming.beats_per_bar * 60.0 / self.incoming.bpm),
            f"{cand.fadein_trim_start:.1f}" if cand.fadein_trim_start is not None else "None",
        )

    def _vocal_out_media_mask(self) -> VocalMask:
        """Return the outgoing vocal mask shifted from buffer-local back to media time (coda scope)."""
        assert self._vocal_out_mask is not None  # narrowed by the caller
        return VocalMask(
            windows=[
                (left + self._buffer_offset, right + self._buffer_offset)
                for left, right in self._vocal_out_mask.windows
            ]
        )

    @staticmethod
    def _mask_saturated(mask: VocalMask, span: float) -> bool:
        """Whether a mask's windows cover >=90% of a span (near-continuous vocal, no fine structure)."""
        covered = sum(right - left for left, right in mask.windows)
        return covered >= 0.9 * max(0.001, span)

    def _choose_tier(self) -> TransitionTier:
        """Pick the transition tier; anything that casts doubt on a long blend picks a shorter one."""
        self._cross_meter = self.outgoing.beats_per_bar != self.incoming.beats_per_bar
        if self._cross_meter:
            # no shared bar grid to beatmatch or blend across
            return TransitionTier.QUICK_FADE
        if not self._tail_is_blendable():
            return TransitionTier.QUICK_FADE
        if self._bpm_diff_percent > self.time_stretch_bpm_percentage_threshold:
            return TransitionTier.QUICK_FADE
        out_a, in_a = self.outgoing.analysis, self.incoming.analysis
        # the 16-bar tier is earned by a verifiable energy anchor: without RMS data
        # the blend could land on a mastered fade-out unnoticed
        if out_a.rms_energy is not None and keys_compatible(
            out_a.key, out_a.mode, in_a.key, in_a.mode
        ):
            # a non-4/4 meter has no corpus evidence to support a 16-bar blend
            if self.outgoing.beats_per_bar != 4:
                return TransitionTier.TEMPO_BLEND
            return TransitionTier.FULL_BLEND
        return TransitionTier.TEMPO_BLEND

    def _tail_is_blendable(self) -> bool:
        """Return True when the anchored tail has enough regular downbeats for a blend."""
        import numpy as np  # noqa: PLC0415

        downbeats = self.outgoing.downbeats  # buffer-local, masked to the anchor window
        if len(downbeats) < 8:
            return False
        # metronomic grid: research measured 74% of library tails under 0.1s interval std
        return float(np.std(np.diff(downbeats))) < 0.1

    def _align_rolling_intro(
        self, crossfade_duration: float, fadein_trim_start: float | None
    ) -> float | _DemoteTier | None:
        """Deepen B's trim when the overlap can't cover its intro (else B's groove lands on dead air)."""
        import numpy as np  # noqa: PLC0415

        entry = self._incoming_entry
        trim = fadein_trim_start or 0.0
        if entry <= 0.0 or entry - trim <= crossfade_duration:
            return fadein_trim_start
        if entry > SMART_CROSSFADE_DURATION:
            # groove enters beyond the buffered head — unreachable defensively
            return fadein_trim_start
        deep_trim = entry - crossfade_duration
        downbeats = self.incoming.downbeats
        if len(downbeats):
            deep_trim = float(downbeats[np.argmin(np.abs(downbeats - deep_trim))])
        guarded = self._guard_deep_trim(deep_trim, crossfade_duration)
        if isinstance(guarded, _DemoteTier):
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Rolling intro: no legal cut clears the sung run within the buffer; "
                "dropping to a shorter overlap instead",
            )
            return _DEMOTE_TIER
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Rolling intro: trimming %.1fs of pre-groove intro to keep the handover anchored",
            guarded,
        )
        return guarded

    def _guard_deep_trim(self, deep_trim: float, crossfade_duration: float) -> float | _DemoteTier:
        """Push a deep-trim cut off a protected run; ``_DEMOTE_TIER`` when no legal cut fits the buffer."""
        import numpy as np  # noqa: PLC0415

        # the cut is a minimum, so only search later: first unprotected downbeat at/after wins
        if self.incoming_profile is None:
            return deep_trim
        bar_starts = self.incoming_profile.bar_starts
        protected = self._protected_bars(self.incoming_profile)
        start_idx = int(np.searchsorted(bar_starts, deep_trim - 1e-6))
        if start_idx >= len(bar_starts) or not protected[start_idx]:
            return deep_trim
        for i in range(start_idx, len(protected)):
            if protected[i]:
                continue
            candidate = float(bar_starts[i])
            if candidate + crossfade_duration <= SMART_CROSSFADE_DURATION:
                return candidate
            break
        return _DEMOTE_TIER

    def _protected_bars(self, profile: BandProfile) -> npt.NDArray[np.bool_]:
        """Mark each bar of ``profile`` as protected: low-silent but voice/melody-active."""
        low = profile.bar_power["low"]
        low_mid = profile.bar_power["low_mid"]
        mid = profile.bar_power["mid"]
        low_silent = low < self.trim_guard_low_floor * profile.reference["low"]
        voice_active = (low_mid >= self.trim_guard_voice_floor * profile.reference["low_mid"]) | (
            mid >= self.trim_guard_voice_floor * profile.reference["mid"]
        )
        return low_silent & voice_active

    def _is_low_timing_eligible(self, profile: BandProfile | None) -> bool:
        """Return True when a track's low band carries enough of its power to time off."""
        import numpy as np  # noqa: PLC0415

        if profile is None:
            return False
        f_low = profile.bar_power["low"][profile.active] / profile.total_power[profile.active]
        return float(np.median(f_low)) >= self.low_timing_eligibility

    def _apply_low_mix_out(self, full_band_mix_out: float) -> float:
        """Swap in the low-band (kick) anchor when eligible and trustworthy, else keep full-band."""
        if not self._is_low_timing_eligible(self.outgoing_profile):
            return full_band_mix_out
        assert self.outgoing_profile is not None  # narrowed by the eligibility check
        low_mix_out = detect_low_mix_out(self.outgoing_profile, self.low_anchor_bar_fraction)
        if low_mix_out is None:
            return full_band_mix_out
        low_mix_out_local = low_mix_out - self._buffer_offset
        bar_seconds = self.outgoing.beats_per_bar * 60.0 / self.outgoing.bpm
        divergence_bars = abs(low_mix_out_local - full_band_mix_out) / bar_seconds
        if divergence_bars > self.low_anchor_divergence_bars:
            return full_band_mix_out
        if low_mix_out_local < MIN_EFFECTIVE_FADE_BUFFER:
            return full_band_mix_out
        return low_mix_out_local

    def _detect_incoming_entry(self) -> float:
        """Detect B's groove entry, preferring the kick when B is low-timing eligible."""
        if self._is_low_timing_eligible(self.incoming_profile):
            assert self.incoming_profile is not None  # narrowed by the eligibility check
            low_entry = detect_low_groove_entry(self.incoming_profile, self.low_anchor_bar_fraction)
            if low_entry is not None and low_entry < SMART_CROSSFADE_DURATION:
                return low_entry
        return detect_groove_entry(
            self.incoming.analysis.rms_energy,
            self.incoming.analysis.duration,
            self.incoming.downbeats,
        )

    def _protect_outgoing_vocal(
        self, bars: int, candidate: TransitionPlan
    ) -> tuple[TransitionPlan, PlanMetrics]:
        """
        Re-anchor and fully rebuild the candidate so it never truncates a protected vocal.

        Extends ``fade_out_window`` (never past the RMS-audible boundary) far
        enough to cover any outgoing vocal window the candidate would
        otherwise cut short, and to keep a short crossfade's audible trim
        within its own overlap length.

        :param bars: The candidate's outgoing bar count, used to rebuild it
            identically at a new anchor.
        :param candidate: The energy-only candidate built at the pristine anchor.
        """
        assert self._vocal_out_mask is not None  # narrowed by the caller
        audio_end = self._audio_end
        last_vocal_end = min(self._vocal_out_mask.last_end(), audio_end)
        vocal_would_be_cut = last_vocal_end > candidate.fade_out_window + 1e-9
        overtrims_short_fade = (
            candidate.crossfade_duration <= self.short_fade_seconds
            and audio_end - candidate.fade_out_window > candidate.crossfade_duration + 1e-9
        )
        if not vocal_would_be_cut and not overtrims_short_fade:
            anchor_on_downbeat = self._is_on_downbeat(candidate.fade_out_window)
            return candidate, self._score_candidate(
                candidate, anchor_on_downbeat=anchor_on_downbeat
            )

        target = max(candidate.fade_out_window, last_vocal_end)
        if overtrims_short_fade:
            target = max(target, audio_end - candidate.crossfade_duration)
        target = min(target, audio_end)
        anchor, anchor_on_downbeat = self._nearest_protective_anchor(
            target, audio_end, prefer_earliest=vocal_would_be_cut
        )
        rebuilt = self._build_candidate(
            bars,
            anchor=anchor,
            entry=candidate.fadein_trim_start,
            pin_entry=True,
        ) or self._build_candidate(
            1,
            anchor=anchor,
            entry=candidate.fadein_trim_start,
            pin_entry=True,
        )
        assert rebuilt is not None  # the 1-bar rung always yields a candidate
        return rebuilt, self._score_candidate(rebuilt, anchor_on_downbeat=anchor_on_downbeat)

    def _nearest_protective_anchor(
        self, target: float, audio_end: float, *, prefer_earliest: bool
    ) -> tuple[float, bool]:
        """
        Choose the re-anchor position at/after ``target``, snapped to a downbeat when possible.

        :param target: Earliest buffer-local position the new anchor may take.
        :param audio_end: Latest buffer-local position the new anchor may take
            (the RMS-audible boundary; never exceeded).
        :param prefer_earliest: Use the first qualifying downbeat (protecting a
            vocal needs only just enough extra room) instead of the last
            (closing an audible-trim gap as tightly as possible).
        """
        candidates = [
            float(downbeat)
            for downbeat in self._protective_downbeats
            if target <= downbeat <= audio_end
        ]
        if candidates:
            return (candidates[0] if prefer_earliest else candidates[-1]), True
        return min(target, audio_end), False

    def _is_on_downbeat(self, position: float, tolerance: float = 0.05) -> bool:
        """Whether a buffer-local position sits within tolerance of an outgoing downbeat."""
        import numpy as np  # noqa: PLC0415

        downbeats = self._protective_downbeats
        return bool(len(downbeats) and np.min(np.abs(downbeats - position)) <= tolerance)

    def _build_short_handoff(
        self, protected: TransitionPlan, anchor_on_downbeat: bool
    ) -> TransitionPlan:
        """
        Build the final click-free fallback: a tiny equal-power handoff with no tempo or EQ.

        Used only when no phrased candidate can avoid a vocal collision; keeps
        the protected anchor (the outgoing vocal stays intact) and shrinks the
        overlap to the auditioned click-free window, favoring the incoming
        track's own vocal onset so the handoff needs no EQ to hide anything.

        :param protected: The smallest ladder rung's vocal-protected candidate.
        :param anchor_on_downbeat: Whether ``protected``'s anchor sits on a downbeat.
        """
        assert self._vocal_in_mask is not None  # narrowed by the caller
        incoming_onset = self._vocal_in_mask.windows[0][0] if self._vocal_in_mask.windows else 0.0
        duration = max(
            self.min_handoff_seconds, min(self.max_handoff_seconds, incoming_onset - 0.1)
        )
        handoff = self._as_handoff(protected, duration)
        metrics = self._score_candidate(
            handoff,
            anchor_on_downbeat=anchor_on_downbeat,
            strategy=TransitionStrategy.SHORT_VOCAL_HANDOFF,
        )
        if (
            metrics.collision_seconds >= self.collision_seconds_limit
            or metrics.weighted_collision_seconds >= self.weighted_collision_limit
        ) and duration > self.min_handoff_seconds:
            handoff = self._as_handoff(protected, self.min_handoff_seconds)
            metrics = self._score_candidate(
                handoff,
                anchor_on_downbeat=anchor_on_downbeat,
                strategy=TransitionStrategy.SHORT_VOCAL_HANDOFF,
            )
        return replace(handoff, metrics=metrics)

    @staticmethod
    def _as_handoff(protected: TransitionPlan, duration: float) -> TransitionPlan:
        """Return a copy of ``protected`` shrunk to a tempo/EQ-free equal-power handoff."""
        return replace(
            protected,
            crossfade_duration=duration,
            fadein_trim_start=None,
            tempo_plan=TempoPlan(),
            eq_plan=EqPlan(
                swap_at=duration / 2.0,
                low_out=None,
                low_in=None,
                high_out=None,
                high_in=None,
            ),
        )

    def _score_candidate(
        self,
        candidate: TransitionPlan,
        *,
        anchor_on_downbeat: bool,
        strategy: TransitionStrategy = TransitionStrategy.ENERGY_ALIGNED,
    ) -> PlanMetrics:
        """Score a vocal-aware candidate: trims, retained vocal time, downbeat alignment, collision."""
        audio_end = self._audio_end
        outgoing_windows = self._rendered_outgoing_windows(candidate)
        incoming_windows = self._rendered_incoming_windows(candidate)
        collision_seconds, weighted_collision = collision_metrics(
            outgoing_windows, incoming_windows, candidate.crossfade_duration
        )
        in_fade = [
            (max(0.0, left), min(candidate.crossfade_duration, right))
            for left, right in outgoing_windows
            if right > 0.0 and left < candidate.crossfade_duration
        ]
        outgoing_vocal_fade_seconds = sum(right - left for left, right in merge_windows(in_fade))
        return PlanMetrics(
            strategy=strategy,
            audible_outgoing_trim=max(0.0, audio_end - candidate.fade_out_window),
            outgoing_vocal_fade_seconds=outgoing_vocal_fade_seconds,
            anchor_on_downbeat=anchor_on_downbeat,
            collision_seconds=collision_seconds,
            weighted_collision_seconds=weighted_collision,
        )

    def _rendered_outgoing_windows(self, candidate: TransitionPlan) -> list[tuple[float, float]]:
        """Map the outgoing (unpadded) vocal scoring mask into rendered crossfade-local seconds."""
        assert self._vocal_out_scoring is not None  # narrowed by the caller
        rendered_anchor = self._rendered_time(candidate, candidate.fade_out_window)
        rendered_start = rendered_anchor - candidate.crossfade_duration
        return [
            (
                self._rendered_time(candidate, left) - rendered_start,
                self._rendered_time(candidate, right) - rendered_start,
            )
            for left, right in self._vocal_out_scoring.windows
        ]

    def _rendered_incoming_windows(self, candidate: TransitionPlan) -> list[tuple[float, float]]:
        """Map the incoming (unpadded) scoring mask into the candidate's fadein-trim-relative seconds."""
        assert self._vocal_in_scoring is not None  # narrowed by the caller
        trim = candidate.fadein_trim_start or 0.0
        return [(left - trim, right - trim) for left, right in self._vocal_in_scoring.windows]

    @staticmethod
    def _rendered_time(candidate: TransitionPlan, input_time: float) -> float:
        """Map a buffer-local outgoing input-time position to its rendered-stream position."""
        clamped = max(0.0, min(input_time, candidate.fade_out_window))
        return clamped - candidate.tempo_plan.savings_until(clamped)

    def _choose_fadein_entry(self, crossfade_bars: int) -> float | None:
        """Choose where the incoming track enters, aligned to its beat grid."""

        def calculate_beat_positions(
            fade_out_beats: npt.NDArray[np.float32],
            fade_in_beats: npt.NDArray[np.float32],
            num_beats: int,
        ) -> float | None:
            """Calculate start positions from beat arrays."""
            if len(fade_out_beats) < num_beats or len(fade_in_beats) < num_beats:
                return None

            fade_in_slice = fade_in_beats[:num_beats]
            return float(fade_in_slice[0])

        # Try downbeats first for most musical timing
        downbeat_positions = calculate_beat_positions(
            self.extrapolated_downbeats, self.incoming.downbeats, crossfade_bars
        )
        if downbeat_positions is not None:
            return downbeat_positions

        # Try regular beats if downbeats insufficient
        required_beats = crossfade_bars * self.incoming.beats_per_bar
        beat_positions = calculate_beat_positions(
            self.outgoing.beats, self.incoming.beats, required_beats
        )
        if beat_positions is not None:
            return beat_positions

        # Fallback: No beat alignment possible
        self.logger.log(VERBOSE_LOG_LEVEL, "No beat alignment possible (insufficient beats)")
        return None

    def _calculate_crossfade_duration(self, crossfade_bars: int) -> float:
        """Calculate the crossfade duration for a bar count, capped to the audible tail."""
        downbeats = self.outgoing.downbeats
        bar_seconds = self.outgoing.beats_per_bar * 60.0 / self.outgoing.bpm
        # the downbeat span assumes the anchor sits (near) the last downbeat;
        # when the grid dies early (unsnapped anchor), the anchor gap would be
        # added to EVERY rung — a "1-bar" quick fade could span the whole gap
        if (
            len(downbeats) > crossfade_bars
            and self.effective_end - float(downbeats[-1]) < bar_seconds
        ):
            # the real span between downbeats honors the track's own tempo/rubato
            # more precisely than a constant-BPM estimate
            musical_duration = float(
                self.effective_end - downbeats[len(downbeats) - 1 - crossfade_bars]
            )
        else:
            seconds_per_beat = 60.0 / self.incoming.bpm
            musical_duration = crossfade_bars * self.incoming.beats_per_bar * seconds_per_beat

        # Cap at the audible fade-out room so crossfade_start never goes negative
        # downstream (effective_end <= SMART_CROSSFADE_DURATION always)
        actual_duration = min(musical_duration, self.effective_end)

        # Log if we had to constrain the duration
        if musical_duration > actual_duration:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Constraining crossfade duration from %.1fs to %.1fs (audible tail limit)",
                musical_duration,
                actual_duration,
            )

        return actual_duration

    def _choose_tempo_ramp(self, tier: TransitionTier, crossfade_duration: float) -> TempoPlan:
        """Choose the gradual tempo ramp that beatmatches the outgoing track, if any."""
        if tier is TransitionTier.QUICK_FADE:
            return TempoPlan()
        if not 0.1 < self._bpm_diff_percent <= self.time_stretch_bpm_percentage_threshold:
            return TempoPlan()
        return TempoPlan(steps=self._compute_tempo_steps(crossfade_duration))

    def _compute_tempo_steps(self, crossfade_duration: float) -> list[tuple[float, float]]:
        """Compute the gradual tempo ramp in the 10s window before the crossfade."""
        stretch_duration = 10.0
        crossfade_start = self.effective_end - crossfade_duration
        # A crossfade consuming the whole audible tail leaves no room for a
        # pre-fade tempo ramp
        if crossfade_start <= 0:
            return []
        stretch_start = max(0.0, crossfade_start - stretch_duration)
        stretch_end = crossfade_start

        # Collect timing points within the stretch window
        beats = self.outgoing.beats
        beat_mask = (beats >= stretch_start) & (beats <= stretch_end)
        db_mask = (self.extrapolated_downbeats >= stretch_start) & (
            self.extrapolated_downbeats <= stretch_end
        )
        window_beats = beats[beat_mask] - stretch_start
        window_downbeats = self.extrapolated_downbeats[db_mask] - stretch_start

        # >3% BPM diff: beat-level stepping (more steps = smoother)
        # <=3%: downbeat-level stepping, fall back to beats if too few
        if self._bpm_diff_percent > 3.0:
            stretch_timestamps = window_beats
        elif len(window_downbeats) >= 2:
            stretch_timestamps = window_downbeats
        else:
            stretch_timestamps = window_beats

        # Fall back to synthetic timestamps when < 2 real timestamps
        if len(stretch_timestamps) < 2:
            stretch_timestamps = generate_synthetic_timestamps(
                stretch_end - stretch_start,
                self.outgoing.bpm,
                beats_per_bar=self.outgoing.beats_per_bar,
            )

        tempo_steps = compute_gradual_tempo_steps(
            start_ratio=1.0,
            end_ratio=self._bpm_ratio,
            downbeats=stretch_timestamps,
        )
        if not tempo_steps:
            tempo_steps = [(0.0, self._bpm_ratio)]

        # Shift timestamps back to buffer-relative coordinates for FFmpeg
        return [(ts + stretch_start, ratio) for ts, ratio in tempo_steps]

    def _lock_in_timing(
        self,
        crossfade_duration: float,
        fadein_start_pos: float | None,
        tempo_plan: TempoPlan,
    ) -> tuple[float, float | None]:
        """
        Lock the overlap timing: confirm the fade-in entry and snap to downbeats.

        Returns the final crossfade duration (downbeat-snapped and compensated
        for time-stretch compression) and the fade-in trim position, or ``None``
        when beat alignment is skipped.

        :param crossfade_duration: Draft crossfade duration in seconds.
        :param fadein_start_pos: Chosen entry point in the incoming track, if any.
        :param tempo_plan: The tempo ramp chosen for this transition.
        """
        # Adjust crossfade duration to align with outgoing track's downbeats.
        # When stretching, only consider downbeats after the stretch window
        # to ensure the outgoing track has reached the target tempo.
        crossfade_start = self.effective_end - crossfade_duration
        crossfade_duration = self._adjust_crossfade_to_downbeats(
            crossfade_duration=crossfade_duration,
            fadein_start_pos=fadein_start_pos,
            min_downbeat_pos=crossfade_start if tempo_plan else 0.0,
            render_ratio=self._bpm_ratio if tempo_plan else 1.0,
        )

        # Compensate crossfade duration for time-stretch compression.
        # Gate on the tempo plan (not stretch eligibility) so a guard-skipped
        # stretch doesn't apply a compensation for a stretch that never ran.
        if tempo_plan:
            crossfade_duration = crossfade_duration / self._bpm_ratio

        fadein_trim_start: float | None = None
        if (
            fadein_start_pos is not None
            and fadein_start_pos + crossfade_duration <= SMART_CROSSFADE_DURATION
        ):
            fadein_trim_start = fadein_start_pos
        else:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Skipping beat alignment: not enough audio after trim (%s + %.1fs > %.1fs)",
                fadein_start_pos,
                crossfade_duration,
                SMART_CROSSFADE_DURATION,
            )

        return crossfade_duration, fadein_trim_start

    def _adjust_crossfade_to_downbeats(
        self,
        crossfade_duration: float,
        fadein_start_pos: float | None,
        min_downbeat_pos: float = 0.0,
        render_ratio: float = 1.0,
    ) -> float:
        """Adjust crossfade duration to align with outgoing track's downbeats."""
        # If we don't have downbeats or beat alignment is disabled, return original duration
        if len(self.extrapolated_downbeats) == 0 or fadein_start_pos is None:
            return crossfade_duration

        # Calculate where the crossfade would start in the buffer
        ideal_start_pos = self.effective_end - crossfade_duration

        # Debug logging
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Downbeat adjustment - ideal_start=%.2fs (effective_end=%.1fs - crossfade=%.2fs), "
            "fadein_start=%.2fs",
            ideal_start_pos,
            self.effective_end,
            crossfade_duration,
            fadein_start_pos,
        )

        # Find the closest downbeats (earlier and later)
        earlier_downbeat = None
        later_downbeat = None

        for downbeat in self.extrapolated_downbeats:
            if downbeat < min_downbeat_pos:
                continue
            if downbeat <= ideal_start_pos:
                earlier_downbeat = downbeat
            elif downbeat > ideal_start_pos and later_downbeat is None:
                later_downbeat = downbeat
                break

        # Try earlier downbeat first (longer crossfade)
        if earlier_downbeat is not None:
            adjusted_duration = float(self.effective_end - earlier_downbeat)
            if fadein_start_pos + adjusted_duration / render_ratio <= SMART_CROSSFADE_DURATION:
                if abs(adjusted_duration - crossfade_duration) > 0.1:
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Adjusted crossfade duration from %.2fs to %.2fs to align with "
                        "downbeat at %.2fs (earlier)",
                        crossfade_duration,
                        adjusted_duration,
                        earlier_downbeat,
                    )
                return adjusted_duration

        # Try later downbeat (shorter crossfade)
        if later_downbeat is not None:
            adjusted_duration = float(self.effective_end - later_downbeat)
            if fadein_start_pos + adjusted_duration / render_ratio <= SMART_CROSSFADE_DURATION:
                if abs(adjusted_duration - crossfade_duration) > 0.1:
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Adjusted crossfade duration from %.2fs to %.2fs to align with "
                        "downbeat at %.2fs (later)",
                        crossfade_duration,
                        adjusted_duration,
                        later_downbeat,
                    )
                return adjusted_duration

        # If no suitable downbeat found, return original duration
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Could not adjust crossfade duration to downbeats, using original %.2fs",
            crossfade_duration,
        )
        return crossfade_duration

    def _choose_eq(
        self,
        crossfade_duration: float,
        tempo_plan: TempoPlan,
        fadein_trim_start: float | None,
        tier: TransitionTier,
        *,
        force_mid_swap: bool = False,
    ) -> EqPlan:
        """Plan the low/mid/high EQ handover, centered on the swap point."""
        # A-side schedules are in A-input time (rendered before the tempo stretch);
        # B-side schedules are in B's post-trim time, where t=0 is the crossfade start
        bar_in = self.incoming.beats_per_bar * 60.0 / self.incoming.bpm
        swap_at = self._choose_swap_point(crossfade_duration, fadein_trim_start)
        swap_len = min(
            max(self.bass_swap_fraction * crossfade_duration, self.bass_swap_min_bars * bar_in),
            self.bass_swap_max_bars * bar_in,
            crossfade_duration,
        )
        # pull a late swap point back so the centered window still fits the overlap
        swap_at = min(swap_at, crossfade_duration - swap_len / 2)
        ease = 0.25 * crossfade_duration

        # the ramp completes before the crossfade, so rendered-to-A-input mapping is linear
        ratio = tempo_plan.steps[-1][1] if tempo_plan else 1.0
        cf_start_input = self.effective_end - crossfade_duration * ratio
        swap_at_input = cf_start_input + swap_at * ratio
        swap_len_input = swap_len * ratio
        start_in = max(0.0, swap_at - swap_len / 2)
        start_out = max(cf_start_input, swap_at_input - swap_len_input / 2)

        depth_a, depth_b = self._choose_swap_depths()

        low_out = (
            ShelfSchedule(
                ShelfType.LOW,
                self.low_shelf_freq,
                [(0.0, 0.0), *db_ramp(start_out, swap_len_input, 0.0, depth_a)],
            )
            if abs(depth_a) >= abs(self.eq_bypass_below_db)
            else None
        )
        low_in = (
            ShelfSchedule(
                ShelfType.LOW,
                self.low_shelf_freq,
                [(0.0, depth_b), *db_ramp(start_in, swap_len, depth_b, 0.0)],
            )
            if abs(depth_b) >= abs(self.eq_bypass_below_db)
            else None
        )
        high_out, high_in = self._choose_high_swap(
            start_out, start_in, cf_start_input, swap_len_input, ease, ratio, crossfade_duration
        )

        depth_mid_a, depth_mid_b = self._choose_mid_swap_depths(tier, force=force_mid_swap)
        mid_out = (
            ShelfSchedule(
                ShelfType.PEAK,
                self.mid_freq,
                [(0.0, 0.0), *db_ramp(start_out, swap_len_input, 0.0, depth_mid_a)],
                width_oct=self.mid_width_oct,
            )
            if depth_mid_a is not None
            else None
        )
        mid_in = (
            ShelfSchedule(
                ShelfType.PEAK,
                self.mid_freq,
                [(0.0, depth_mid_b), *db_ramp(start_in, swap_len, depth_mid_b, 0.0)],
                width_oct=self.mid_width_oct,
            )
            if depth_mid_b is not None
            else None
        )

        eq_plan = EqPlan(
            swap_at=swap_at,
            low_out=low_out,
            low_in=low_in,
            high_out=high_out,
            high_in=high_in,
            mid_out=mid_out,
            mid_in=mid_in,
        )
        # the low crossover's rendered ramp window; the dip guard exempts it as
        # the intentional bass-handover gesture, but ONLY when a low swap is
        # actually engaged — a bass-light pair has no handover to exempt, so
        # the notch collapses to an interval that never matches a sample time
        self._swap_notch = (
            (start_in, start_in + swap_len)
            if (low_out is not None or low_in is not None)
            else (-1.0, -1.0)
        )
        return self._apply_dip_guard(
            eq_plan,
            crossfade_duration=crossfade_duration,
            cf_start_input=cf_start_input,
            ratio=ratio,
            swap_at=swap_at,
            bar_in=bar_in,
            notch=self._swap_notch,
        )

    def _choose_swap_point(
        self, crossfade_duration: float, fadein_trim_start: float | None
    ) -> float:
        """Pick the bass-swap moment: B's groove entry when inside the overlap, else 60% through."""
        import numpy as np  # noqa: PLC0415

        trim = fadein_trim_start or 0.0
        candidate = self._incoming_entry - trim
        if not 0.0 < candidate <= crossfade_duration:
            candidate = 0.6 * crossfade_duration
        # snap to the incoming grid so the new bassline lands on its own 1
        post_trim = self.incoming.downbeats - trim
        post_trim = post_trim[(post_trim > 0.0) & (post_trim < crossfade_duration)]
        if len(post_trim):
            candidate = float(post_trim[np.argmin(np.abs(post_trim - candidate))])
        return candidate

    def _swap_windows(self, window_bars: int) -> tuple[tuple[float, float], tuple[float, float]]:
        """Reciprocal decision windows for the swap gates: A's outgoing tail, B's incoming head."""
        bar_out = self.outgoing.beats_per_bar * 60.0 / self.outgoing.bpm
        bar_in = self.incoming.beats_per_bar * 60.0 / self.incoming.bpm
        anchor_media = self._buffer_offset + self.effective_end
        w_a_out = (anchor_media - window_bars * bar_out, anchor_media)
        entry = self._incoming_entry
        w_b_in = (entry, entry + window_bars * bar_in)
        return w_a_out, w_b_in

    def _choose_swap_depths(self) -> tuple[float, float]:
        """Reciprocally scale each side's bass-kill depth to the other deck's measured bass."""
        # missing band data on either side keeps the shipped full-depth kill (bit-identical)
        if self.outgoing_profile is None or self.incoming_profile is None:
            return self.eq_kill_db, self.eq_kill_db
        w_a_out, w_b_in = self._swap_windows(self.bass_swap_window_bars)
        f_low_b_in = window_fraction(self.incoming_profile, "low", *w_b_in)
        f_low_a_out = window_fraction(self.outgoing_profile, "low", *w_a_out)
        depth_a = self.eq_kill_db * smoothstep(f_low_b_in, self.low_gate_lo, self.low_gate_hi)
        depth_b = self.eq_kill_db * smoothstep(f_low_a_out, self.low_gate_lo, self.low_gate_hi)
        return depth_a, depth_b

    def _choose_high_swap(
        self,
        start_out: float,
        start_in: float,
        cf_start_input: float,
        swap_len_input: float,
        ease: float,
        ratio: float,
        crossfade_duration: float,
    ) -> tuple[ShelfSchedule | None, ShelfSchedule | None]:
        """Build the high-ease shelves: reciprocal depths, own-side no-op skip, cymbal-wash mode."""
        wash = False
        if self.outgoing_profile is None or self.incoming_profile is None:
            depth_a = depth_b = self.high_ease_db
        else:
            w_a_out, w_b_in = self._swap_windows(self.bass_swap_window_bars)
            f_high_b_in = window_fraction(self.incoming_profile, "high", *w_b_in)
            f_high_a_out = window_fraction(self.outgoing_profile, "high", *w_a_out)
            depth_a = self.high_ease_db * smoothstep(
                f_high_b_in, self.high_gate_lo, self.high_gate_hi
            )
            depth_b = self.high_ease_db * smoothstep(
                f_high_a_out, self.high_gate_lo, self.high_gate_hi
            )

            own_dark_a = window_level(self.outgoing_profile, "high", *w_a_out) < (
                self.high_own_side_floor * self.outgoing_profile.reference["high"]
            )
            own_dark_b = window_level(self.incoming_profile, "high", *w_b_in) < (
                self.high_own_side_floor * self.incoming_profile.reference["high"]
            )
            if own_dark_a:
                depth_a = 0.0
            if own_dark_b:
                depth_b = 0.0

            wash = self._wash_mode_engages(w_a_out, w_b_in, crossfade_duration)
            if wash:
                depth_a = self.wash_depth_db

        high_out = (
            ShelfSchedule(
                ShelfType.HIGH,
                self.high_shelf_freq,
                self._high_out_steps(
                    depth_a,
                    start_out,
                    start_in,
                    cf_start_input,
                    swap_len_input,
                    ease,
                    ratio,
                    wash=wash,
                ),
            )
            if abs(depth_a) >= abs(self.eq_bypass_below_db)
            else None
        )
        high_in = (
            ShelfSchedule(
                ShelfType.HIGH,
                self.high_shelf_freq,
                [
                    (0.0, depth_b),
                    *db_ramp(max(0.0, start_in - ease), ease, depth_b, 0.0),
                ],
            )
            if abs(depth_b) >= abs(self.eq_bypass_below_db)
            else None
        )
        return high_out, high_in

    def _high_out_steps(
        self,
        depth_a: float,
        start_out: float,
        start_in: float,
        cf_start_input: float,
        swap_len_input: float,
        ease: float,
        ratio: float,
        *,
        wash: bool,
    ) -> list[tuple[float, float]]:
        """Build A's high-duck ramp: shipped post-swap placement, or wash mode's mirrored duck."""
        if wash:
            # mirror B's restore window; it ends at start_in inside the overlap,
            # so the duck can never overrun the crossfade end
            start = cf_start_input + max(0.0, start_in - ease) * ratio
        else:
            start = start_out + swap_len_input
        return [(0.0, 0.0), *db_ramp(start, ease * ratio, 0.0, depth_a)]

    def _wash_mode_engages(
        self,
        w_a_out: tuple[float, float],
        w_b_in: tuple[float, float],
        crossfade_duration: float,
    ) -> bool:
        """Return True when both decks read bright, comparably loud, over a long enough blend."""
        import numpy as np  # noqa: PLC0415

        assert self.outgoing_profile is not None  # narrowed by the caller
        assert self.incoming_profile is not None
        bar_out = self.outgoing.beats_per_bar * 60.0 / self.outgoing.bpm
        if crossfade_duration < self.wash_min_blend_bars * bar_out:
            return False
        # absolute brightness floor: duty vs a track's OWN reference measures
        # consistency, not brightness — a dark steady track has duty 1.0
        f_high_a = window_fraction(self.outgoing_profile, "high", *w_a_out)
        f_high_b = window_fraction(self.incoming_profile, "high", *w_b_in)
        if f_high_a < self.high_gate_hi or f_high_b < self.high_gate_hi:
            return False
        duty_a = window_duty(self.outgoing_profile, "high", *w_a_out, k=0.5)
        duty_b = window_duty(self.incoming_profile, "high", *w_b_in, k=0.5)
        if duty_a < self.wash_duty or duty_b < self.wash_duty:
            return False
        level_a = loudness_referenced_level(self.outgoing_profile, "high", *w_a_out)
        level_b = loudness_referenced_level(self.incoming_profile, "high", *w_b_in)
        if level_a <= 0.0 or level_b <= 0.0:
            return False
        # the planner has no access to playback state, so this presumes loudness-
        # normalized playback; that assumption is strictly more conservative than
        # a duty-only fallback, which would engage wash mode more often
        level_gap_db = abs(10.0 * float(np.log10(level_a / level_b)))
        return level_gap_db <= self.wash_level_tolerance_db

    def _choose_mid_swap_depths(
        self, tier: TransitionTier, *, force: bool = False
    ) -> tuple[float | None, float | None]:
        """Gate and scale the measured mid-band (vocal) swap depth; ``None`` means bypass."""
        cap = self.mid_cap_full_db if tier is TransitionTier.FULL_BLEND else self.mid_cap_tempo_db
        if force:
            # the remediation floor masks residual stacked vocals unconditionally
            return cap, cap
        if tier is TransitionTier.QUICK_FADE:
            return None, None
        if self.outgoing_profile is None or self.incoming_profile is None:
            return None, None
        w_a_out, w_b_in = self._swap_windows(self.bass_swap_window_bars)

        def _score(profile: BandProfile, window: tuple[float, float]) -> float:
            f_mid = window_fraction(profile, "mid", *window)
            duty_mid = window_duty(profile, "mid", *window, k=0.5)
            return smoothstep(f_mid, self.mid_gate_lo, self.mid_gate_hi) * smoothstep(
                duty_mid, self.mid_duty_lo, self.mid_duty_hi
            )

        score_a = _score(self.outgoing_profile, w_a_out)
        score_b = _score(self.incoming_profile, w_b_in)
        # the weaker side rules: a swap only reads as a handover when both decks carry a mid element
        depth = cap * min(score_a, score_b)
        if abs(depth) < abs(self.mid_bypass_below_db):
            return None, None
        return depth, depth

    def _apply_dip_guard(
        self,
        eq_plan: EqPlan,
        *,
        crossfade_duration: float,
        cf_start_input: float,
        ratio: float,
        swap_at: float,
        bar_in: float,
        notch: tuple[float, float],
    ) -> EqPlan:
        """Remediate a predicted outside-notch dip: shrink mid depth, then steepen low ramps."""
        if self.outgoing_profile is None or self.incoming_profile is None:
            return eq_plan
        w_a_out, w_b_in = self._swap_windows(self.bass_swap_window_bars)
        f_a = {
            band: window_fraction(self.outgoing_profile, band, *w_a_out) for band in BAND_RMS_BANDS
        }
        f_b = {
            band: window_fraction(self.incoming_profile, band, *w_b_in) for band in BAND_RMS_BANDS
        }

        def dip_db(plan: EqPlan) -> float:
            return self._predicted_dip_db(
                plan, crossfade_duration, cf_start_input, ratio, f_a, f_b, notch
            )

        if dip_db(eq_plan) <= self.max_predicted_dip_db:
            return eq_plan

        # (1) reduce mid depth toward bypass, in steps, until the dip clears
        # or mid is fully bypassed
        shrink_steps = (0.75, 0.5, 0.25, 0.0)
        for shrink in shrink_steps:
            candidate = eq_plan.with_mid_depth_scaled(
                shrink, bypass_below_db=self.mid_bypass_below_db
            )
            if dip_db(candidate) <= self.max_predicted_dip_db or shrink == 0.0:
                eq_plan = candidate
                break
        if dip_db(eq_plan) <= self.max_predicted_dip_db:
            return eq_plan

        # (2) steepen the low ramps to the 2-bar floor; endpoints untouched.
        # This is the last remediation step (never shallow the low depth —
        # there is no further knob beyond the floor), so its result is
        # returned whether or not it fully clears the budget.
        if eq_plan.low_out is None and eq_plan.low_in is None:
            # a bass-light pair has no low handover to tighten and no notch to
            # narrow; mid-scaling was the only lever, so leave the sentinel notch
            return eq_plan
        floor_len = self.bass_swap_min_bars * bar_in
        eq_plan = eq_plan.with_low_ramps_steepened(swap_at, floor_len, ratio, cf_start_input)
        # the notch narrowed along with the ramp span; keep it in sync so it
        # still reflects the plan's actual bass-handover window
        start_in = max(0.0, swap_at - floor_len / 2)
        self._swap_notch = (start_in, start_in + floor_len)
        return eq_plan

    def _predicted_dip_db(
        self,
        eq_plan: EqPlan,
        crossfade_duration: float,
        cf_start_input: float,
        ratio: float,
        f_a: dict[str, float],
        f_b: dict[str, float],
        notch: tuple[float, float],
        n_samples: int = 64,
    ) -> float:
        """Max plateau-to-valley drop, in dB, of the predicted combined qsin-weighted power."""
        import numpy as np  # noqa: PLC0415

        # qsin weights match the renderer's acrossfade=...:c1=qsin:c2=qsin curve
        schedules_a = {"low": eq_plan.low_out, "mid": eq_plan.mid_out, "high": eq_plan.high_out}
        schedules_b = {"low": eq_plan.low_in, "mid": eq_plan.mid_in, "high": eq_plan.high_in}
        running_max = 0.0
        max_drop_db = 0.0
        for i in range(n_samples + 1):
            t = crossfade_duration * i / n_samples
            if notch[0] <= t <= notch[1]:
                continue
            w_a = np.cos(np.pi / 2 * t / crossfade_duration) ** 2 if crossfade_duration else 1.0
            w_b = np.sin(np.pi / 2 * t / crossfade_duration) ** 2 if crossfade_duration else 0.0
            p_a = sum(
                f_a[band]
                * 10.0
                ** (_band_gain(schedules_a.get(band), t, cf_start_input, ratio, side="A") / 10.0)
                for band in BAND_RMS_BANDS
            )
            p_b = sum(
                f_b[band]
                * 10.0
                ** (_band_gain(schedules_b.get(band), t, cf_start_input, ratio, side="B") / 10.0)
                for band in BAND_RMS_BANDS
            )
            power = float(w_a * p_a + w_b * p_b)
            running_max = max(running_max, power)
            if running_max > 0.0 and power > 0.0:
                max_drop_db = max(max_drop_db, 10.0 * float(np.log10(running_max / power)))
        return max_drop_db
