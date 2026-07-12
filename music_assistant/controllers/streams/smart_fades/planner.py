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
from typing import TYPE_CHECKING

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.streams.smart_fades.bands import (
    build_band_profile,
    loudness_referenced_level,
    smoothstep,
    window_duty,
    window_fraction,
    window_level,
)
from music_assistant.controllers.streams.smart_fades.filters import ShelfType
from music_assistant.controllers.streams.smart_fades.helpers import (
    MIN_EFFECTIVE_FADE_BUFFER,
    SMART_CROSSFADE_DURATION,
    compute_gradual_tempo_steps,
    db_ramp,
    detect_effective_audio_end,
    detect_groove_entry,
    extrapolate_downbeats,
    generate_synthetic_timestamps,
)
from music_assistant.controllers.streams.smart_fades.models import (
    BAND_RMS_BANDS,
    BandProfile,
    Deck,
    EqPlan,
    FadeOutTrim,
    ShelfSchedule,
    SmartFadeNotApplicable,
    TempoPlan,
    TransitionPlan,
)

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

    from music_assistant.models.audio_analysis import AudioAnalysisData


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
    time_stretch_bpm_percentage_threshold: float = 5.0

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
    mid_cap_db: float = -8.0
    mid_bypass_below_db: float = -1.0

    # Dip guard: combined qsin-weighted power of both decks may never sag more than this
    # below its plateau across the overlap (outside the intentional bass-handover notch).
    max_predicted_dip_db: float = 3.0

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
    # set by _choose_eq: the low crossover's rendered ramp window, exempt from
    # the dip guard (its notch is the intentional bass-handover gesture)
    _swap_notch: tuple[float, float]

    def plan(
        self,
        fade_out_analysis: AudioAnalysisData,
        fade_in_analysis: AudioAnalysisData,
        buffer_duration: float,
    ) -> TransitionPlan:
        """
        Build a smart-crossfade ``TransitionPlan`` from the two tracks' analysis.

        :param fade_out_analysis: Analysis data for the outgoing track.
        :param fade_in_analysis: Analysis data for the incoming track.
        :param buffer_duration: Length in seconds of the available fade-out holdback.
        """
        self._prepare_decks(fade_out_analysis, fade_in_analysis, buffer_duration)

        crossfade_bars = self._choose_crossfade_bars()
        fadein_start_pos = self._choose_fadein_entry(crossfade_bars)
        crossfade_duration = self._calculate_crossfade_duration(crossfade_bars=crossfade_bars)

        tempo_plan = self._choose_tempo_ramp(crossfade_bars, crossfade_duration)

        crossfade_duration, fadein_trim_start = self._lock_in_timing(
            crossfade_duration, fadein_start_pos, tempo_plan
        )

        eq_plan = self._choose_eq(crossfade_duration, tempo_plan, fadein_trim_start)

        return TransitionPlan(
            fade_out_window=self.effective_end,
            crossfade_duration=crossfade_duration,
            eq_plan=eq_plan,
            tempo_plan=tempo_plan,
            fadeout_trim=self.fadeout_trim,
            fadein_trim_start=fadein_trim_start,
        )

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
        )
        self.outgoing_profile = build_band_profile(fade_out_analysis)
        self.incoming_profile = build_band_profile(fade_in_analysis)
        self._cue_outgoing_tail(buffer_duration)
        self.extrapolated_downbeats = extrapolate_downbeats(
            self.outgoing.downbeats,
            buffer_size=self.effective_end,
            bpm=self.outgoing.bpm,
        )
        # Computed once so the swap point and the reciprocal decision windows agree
        self._incoming_entry = detect_groove_entry(
            self.incoming.analysis.rms_energy,
            self.incoming.analysis.duration,
            self.incoming.downbeats,
        )
        # Additional verbose logging to debug rare failures
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "SmartCrossFade plan: fade_out: %s, fade_in: %s",
            fade_out_analysis,
            fade_in_analysis,
        )

    def _cue_outgoing_tail(self, buffer_duration: float) -> None:
        """Locate the audible end of the outgoing tail and shift its grids to buffer-local time."""
        self.fadeout_trim = None
        self.effective_end = detect_effective_audio_end(
            self.outgoing.analysis.rms_energy,
            self.outgoing.analysis.duration,
            buffer_duration,
        )
        if self.effective_end < MIN_EFFECTIVE_FADE_BUFFER:
            raise SmartFadeNotApplicable(
                f"outgoing tail is mostly silent ({self.effective_end:.1f}s audible)"
            )
        # Sub-half-second slack is not worth trimming: RMS bin granularity is
        # ~0.1-0.2s for typical track lengths, so finer precision is illusory
        if self.effective_end < buffer_duration - 0.5:
            self.fadeout_trim = FadeOutTrim(
                end_pos=self.effective_end,
                trimmed_seconds=buffer_duration - self.effective_end,
            )
        else:
            # Without the trim the rendered stream still ends at buffer_duration,
            # so the anchor must follow it or every schedule lands early
            self.effective_end = buffer_duration

        # Shift fade-out beats from full-track to buffer-local coordinates using the
        # ACTUAL buffer length: the holdback yield loop leaves up to ~1s less than the
        # constant 45s depending on chunk boundaries, and effective_end above is in real
        # buffer coordinates — a constant-45 shift would misalign every beat by the difference
        self._buffer_offset = max(0.0, (self.outgoing.analysis.duration or 0.0) - buffer_duration)
        beats = self.outgoing.beats - self._buffer_offset
        downbeats = self.outgoing.downbeats - self._buffer_offset

        # Mask fade-out beats to the audible buffer window; negative timestamps are
        # beats before the buffer, beats past effective_end sit in the silent tail
        self.outgoing.beats = beats[(beats >= 0.0) & (beats <= self.effective_end)]
        self.outgoing.downbeats = downbeats[(downbeats >= 0.0) & (downbeats <= self.effective_end)]

    def _choose_crossfade_bars(self) -> int:
        """Choose the overlap length in bars that fits in the available buffer."""
        # Calculate ideal bars based on BPM compatibility
        ideal_bars = (
            10 if self._bpm_diff_percent <= self.time_stretch_bpm_percentage_threshold else 6
        )

        # Reduce bars until it fits in the fadein buffer
        for bars in [ideal_bars, 8, 6, 4, 2, 1]:
            if bars > ideal_bars:
                continue

            fadein_start_pos = self._choose_fadein_entry(bars)
            if fadein_start_pos is None:
                continue

            # Calculate what the duration would be
            test_duration = self._calculate_crossfade_duration(crossfade_bars=bars)

            # Check if it fits in fadein buffer
            fadein_buffer = SMART_CROSSFADE_DURATION - fadein_start_pos
            if test_duration <= fadein_buffer:
                if bars < ideal_bars:
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Reduced crossfade from %d to %d bars (fadein buffer=%.1fs, needed=%.1fs)",
                        ideal_bars,
                        bars,
                        fadein_buffer,
                        test_duration,
                    )
                return bars

        # Fall back to 1 bar if nothing else fits
        return 1

    def _choose_fadein_entry(self, crossfade_bars: int) -> float | None:
        """Choose where the incoming track enters, aligned to its beat grid."""
        beats_per_bar = 4

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
        required_beats = crossfade_bars * beats_per_bar
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
        # Calculate crossfade duration based on incoming track's BPM
        beats_per_bar = 4
        seconds_per_beat = 60.0 / self.incoming.bpm
        musical_duration = crossfade_bars * beats_per_bar * seconds_per_beat

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

    def _choose_tempo_ramp(self, crossfade_bars: int, crossfade_duration: float) -> TempoPlan:
        """Choose the gradual tempo ramp that beatmatches the outgoing track, if any."""
        stretch_eligible = (
            0.1 < self._bpm_diff_percent <= self.time_stretch_bpm_percentage_threshold
            and crossfade_bars > 4
        )
        if not stretch_eligible:
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
                stretch_end - stretch_start, self.outgoing.bpm
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

        # Adjust crossfade duration to align with outgoing track's downbeats.
        # When stretching, only consider downbeats after the stretch window
        # to ensure the outgoing track has reached the target tempo.
        crossfade_start = self.effective_end - crossfade_duration
        crossfade_duration = self._adjust_crossfade_to_downbeats(
            crossfade_duration=crossfade_duration,
            fadein_start_pos=fadein_start_pos,
            min_downbeat_pos=crossfade_start if tempo_plan else 0.0,
        )

        # Compensate crossfade duration for time-stretch compression.
        # Gate on the tempo plan (not stretch eligibility) so a guard-skipped
        # stretch doesn't apply a compensation for a stretch that never ran.
        if tempo_plan:
            crossfade_duration = crossfade_duration / self._bpm_ratio

        return crossfade_duration, fadein_trim_start

    def _adjust_crossfade_to_downbeats(
        self,
        crossfade_duration: float,
        fadein_start_pos: float | None,
        min_downbeat_pos: float = 0.0,
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
            if fadein_start_pos + adjusted_duration <= SMART_CROSSFADE_DURATION:
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
            if fadein_start_pos + adjusted_duration <= SMART_CROSSFADE_DURATION:
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
    ) -> EqPlan:
        """Plan the low/mid/high EQ handover, centered on the swap point."""
        # A-side schedules are in A-input time (rendered before the tempo stretch);
        # B-side schedules are in B's post-trim time, where t=0 is the crossfade start
        bar_in = 4 * 60.0 / self.incoming.bpm
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

        depth_mid_a, depth_mid_b = self._choose_mid_swap_depths()
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
        bar_out = 4 * 60.0 / self.outgoing.bpm
        bar_in = 4 * 60.0 / self.incoming.bpm
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
        bar_out = 4 * 60.0 / self.outgoing.bpm
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

    def _choose_mid_swap_depths(self) -> tuple[float | None, float | None]:
        """Gate and scale the measured mid-band (vocal) swap depth; ``None`` means bypass."""
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
        depth = self.mid_cap_db * min(score_a, score_b)
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
