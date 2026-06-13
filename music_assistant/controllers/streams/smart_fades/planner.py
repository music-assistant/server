"""Smart Fades - Transition planners.

A planner turns the two tracks' stored ``AudioAnalysisData`` into a
``TransitionPlan`` — a pure decision with no audio bytes and no FFmpeg filters.
``SmartCrossFadePlanner`` reproduces the defensive smart-crossfade behaviour; a
future ``DjModePlanner`` slots in as a sibling subclass.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.streams.smart_fades.helpers import (
    MIN_EFFECTIVE_FADE_BUFFER,
    SMART_CROSSFADE_DURATION,
    compute_gradual_tempo_steps,
    detect_effective_audio_end,
    extrapolate_downbeats,
    generate_synthetic_timestamps,
)
from music_assistant.controllers.streams.smart_fades.plan import (
    EqPlan,
    FadeOutTrim,
    SmartFadeNotApplicable,
    SweepSpec,
    TempoPlan,
    TransitionPlan,
    TransitionStyle,
)

if TYPE_CHECKING:
    from music_assistant.models.audio_analysis import AudioAnalysisData


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
        self._init_grids(fade_out_analysis, fade_in_analysis)

        self._setup_fadeout_window(buffer_duration)

        bpm_ratio = self.fade_in_bpm / self.fade_out_bpm
        bpm_diff_percent = abs(1.0 - bpm_ratio) * 100

        self.extrapolated_fadeout_downbeats = extrapolate_downbeats(
            self.fade_out_downbeats,
            buffer_size=self.effective_end,
            bpm=self.fade_out_bpm,
        )

        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "SmartCrossFade plan: fade_out: %s, fade_in: %s",
            self.fade_out_analysis,
            self.fade_in_analysis,
        )

        crossfade_bars = self._calculate_optimal_crossfade_bars()
        fadein_start_pos = self._calculate_optimal_fade_timing(crossfade_bars)
        crossfade_duration = self._calculate_crossfade_duration(crossfade_bars=crossfade_bars)

        tempo_steps: list[tuple[float, float]] = []
        stretch_eligible = (
            0.1 < bpm_diff_percent <= self.time_stretch_bpm_percentage_threshold
            and crossfade_bars > 4
        )
        if stretch_eligible:
            tempo_steps = self._compute_tempo_steps(bpm_ratio, bpm_diff_percent, crossfade_duration)
        tempo_plan = TempoPlan(steps=tempo_steps)

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
            min_downbeat_pos=crossfade_start if tempo_steps else 0.0,
        )

        # Compensate crossfade duration for time-stretch compression.
        # Gate on tempo_steps (not stretch_eligible) so a guard-skipped stretch
        # doesn't apply a compensation for a stretch that never ran.
        if tempo_steps:
            crossfade_duration = crossfade_duration / bpm_ratio

        eq_plan = self._build_eq_plan(bpm_ratio, crossfade_bars, crossfade_duration, tempo_plan)

        style = TransitionStyle.EXTENDED_BLEND if crossfade_bars >= 8 else TransitionStyle.BLEND
        return TransitionPlan(
            style=style,
            fade_out_window=self.effective_end,
            crossfade_duration=crossfade_duration,
            eq_plan=eq_plan,
            tempo_plan=tempo_plan,
            fadeout_trim=self.fadeout_trim,
            fadein_trim_start=fadein_trim_start,
            mix_in_point=0.0,
            holdback_needed=self.effective_end,
        )

    def _init_grids(
        self,
        fade_out_analysis: AudioAnalysisData,
        fade_in_analysis: AudioAnalysisData,
    ) -> None:
        """Validate and derive the working beat/downbeat grids for this plan."""
        if (
            fade_out_analysis.bpm is None
            or fade_in_analysis.bpm is None
            or fade_out_analysis.beats is None
            or fade_in_analysis.beats is None
        ):
            raise ValueError("AudioAnalysisData must have bpm and beats set for smart crossfade")
        self.fade_out_analysis = fade_out_analysis
        self.fade_in_analysis = fade_in_analysis
        self.fade_out_bpm: float = fade_out_analysis.bpm
        self.fade_in_bpm: float = fade_in_analysis.bpm
        self.fade_in_beats: npt.NDArray[np.float32] = fade_in_analysis.beats
        self.fade_in_downbeats: npt.NDArray[np.float32] = (
            fade_in_analysis.downbeats
            if fade_in_analysis.downbeats is not None
            else fade_in_analysis.beats
        )
        # Store raw full-track beat grids; the shift to buffer-local coordinates
        # happens in _setup_fadeout_window where the actual buffer length is known
        self.fade_out_beats: npt.NDArray[np.float32] = fade_out_analysis.beats
        self.fade_out_downbeats: npt.NDArray[np.float32] = (
            fade_out_analysis.downbeats
            if fade_out_analysis.downbeats is not None
            else np.array([], dtype=np.float32)
        )
        # Only beats within the buffered head are usable for alignment decisions
        self.fade_in_beats = self.fade_in_beats[self.fade_in_beats <= SMART_CROSSFADE_DURATION]
        self.fade_in_downbeats = self.fade_in_downbeats[
            self.fade_in_downbeats <= SMART_CROSSFADE_DURATION
        ]
        self.effective_end: float = SMART_CROSSFADE_DURATION
        self.fadeout_trim: FadeOutTrim | None = None

    def _setup_fadeout_window(self, buffer_duration: float) -> None:
        """
        Compute the effective audio end of the fade-out tail.

        Sets ``self.effective_end``, ``self.fadeout_trim`` (when trailing silence
        is worth dropping), converts ``self.fade_out_beats`` /
        ``self.fade_out_downbeats`` from full-track to buffer-local coordinates
        using the actual buffer length, and masks them to the audible window.
        Raises ``SmartFadeNotApplicable`` when the tail is too short to be useful.

        :param buffer_duration: Length in seconds of the fade-out holdback buffer.
        """
        self.effective_end = detect_effective_audio_end(
            self.fade_out_analysis.rms_energy,
            self.fade_out_analysis.duration,
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
        buffer_offset = max(0.0, (self.fade_out_analysis.duration or 0.0) - buffer_duration)
        self.fade_out_beats = self.fade_out_beats - buffer_offset
        self.fade_out_downbeats = self.fade_out_downbeats - buffer_offset

        # Mask fade-out beats to the audible buffer window; negative timestamps are
        # beats before the buffer, beats past effective_end sit in the silent tail
        beat_mask = (self.fade_out_beats >= 0.0) & (self.fade_out_beats <= self.effective_end)
        self.fade_out_beats = self.fade_out_beats[beat_mask]
        db_mask = (self.fade_out_downbeats >= 0.0) & (self.fade_out_downbeats <= self.effective_end)
        self.fade_out_downbeats = self.fade_out_downbeats[db_mask]

    def _compute_tempo_steps(
        self,
        bpm_ratio: float,
        bpm_diff_percent: float,
        crossfade_duration: float,
    ) -> list[tuple[float, float]]:
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
        beat_mask = (self.fade_out_beats >= stretch_start) & (self.fade_out_beats <= stretch_end)
        db_mask = (self.extrapolated_fadeout_downbeats >= stretch_start) & (
            self.extrapolated_fadeout_downbeats <= stretch_end
        )
        window_beats = self.fade_out_beats[beat_mask] - stretch_start
        window_downbeats = self.extrapolated_fadeout_downbeats[db_mask] - stretch_start

        # >3% BPM diff: beat-level stepping (more steps = smoother)
        # <=3%: downbeat-level stepping, fall back to beats if too few
        if bpm_diff_percent > 3.0:
            stretch_timestamps = window_beats
        elif len(window_downbeats) >= 2:
            stretch_timestamps = window_downbeats
        else:
            stretch_timestamps = window_beats

        # Fall back to synthetic timestamps when < 2 real timestamps
        if len(stretch_timestamps) < 2:
            stretch_timestamps = generate_synthetic_timestamps(
                stretch_end - stretch_start, self.fade_out_bpm
            )

        tempo_steps = compute_gradual_tempo_steps(
            start_ratio=1.0,
            end_ratio=bpm_ratio,
            downbeats=stretch_timestamps,
        )
        if not tempo_steps:
            tempo_steps = [(0.0, bpm_ratio)]

        # Shift timestamps back to buffer-relative coordinates for FFmpeg
        return [(ts + stretch_start, ratio) for ts, ratio in tempo_steps]

    def _build_eq_plan(
        self,
        bpm_ratio: float,
        crossfade_bars: int,
        crossfade_duration: float,
        tempo_plan: TempoPlan,
    ) -> EqPlan:
        """Compute the crossover frequency, curves, and both frequency sweeps."""
        # 90 BPM -> 1500Hz, 140 BPM -> 2500Hz
        avg_bpm = (self.fade_out_bpm + self.fade_in_bpm) / 2
        crossover_freq = int(np.clip(1500 + (avg_bpm - 90) * 20, 1500, 2500))

        # Adjust for BPM mismatch
        if abs(bpm_ratio - 1.0) > 0.3:
            crossover_freq = int(crossover_freq * 0.85)

        # For shorter fades, use exp/exp curves to avoid abruptness
        if crossfade_bars < 8:
            fadeout_curve = "exponential"
            fadein_curve = "exponential"
        # For long fades, use log/linear curves
        else:
            # Use logarithmic curve to give the next track more space
            fadeout_curve = "logarithmic"
            # Use linear curve for transition, predictable and not too abrupt
            fadein_curve = "linear"

        # Create lowpass filter on the outgoing track (unfiltered → low-pass)
        # Extended lowpass effect to gradually remove bass frequencies
        fadeout_eq_duration = min(max(crossfade_duration * 2.5, 8.0), self.effective_end)
        # The crossfade always happens at the END of the audible tail
        fadeout_eq_start = max(0.0, self.effective_end - fadeout_eq_duration)
        if tempo_plan:
            # post-rubberband filters run on OUTPUT time: remap the schedule so the
            # sweep still completes exactly when the rendered tail ends
            rendered_end = self.effective_end - tempo_plan.savings_until(self.effective_end)
            fadeout_eq_start -= tempo_plan.savings_until(fadeout_eq_start)
            # defensive floor keeping the sweep non-degenerate; cannot bind given
            # the 5% stretch cap and the 10s minimum audible tail
            fadeout_eq_duration = max(rendered_end - fadeout_eq_start, 1.0)
        fadeout = SweepSpec(
            sweep_type="lowpass",
            target_freq=crossover_freq,
            duration=fadeout_eq_duration,
            start_time=fadeout_eq_start,
            sweep_direction="fade_in",
            poles=1,
            curve_type=fadeout_curve,
            stream_type="fadeout",
        )

        # Create high pass filter on the incoming track (high-pass → unfiltered)
        # Quicker highpass removal to avoid lingering vocals after crossfade
        fadein = SweepSpec(
            sweep_type="highpass",
            target_freq=crossover_freq,
            duration=crossfade_duration / 1.5,
            start_time=0,
            sweep_direction="fade_out",
            poles=1,
            curve_type=fadein_curve,
            stream_type="fadein",
        )
        return EqPlan(crossover_freq=crossover_freq, fadeout=fadeout, fadein=fadein)

    def _calculate_crossfade_duration(self, crossfade_bars: int) -> float:
        """Calculate final crossfade duration based on musical bars and BPM."""
        # Calculate crossfade duration based on incoming track's BPM
        beats_per_bar = 4
        seconds_per_beat = 60.0 / self.fade_in_bpm
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

    def _calculate_optimal_crossfade_bars(self) -> int:
        """Calculate optimal crossfade bars that fit in available buffer."""
        bpm_in = self.fade_in_bpm
        bpm_out = self.fade_out_bpm
        bpm_diff_percent = abs(1.0 - bpm_in / bpm_out) * 100

        # Calculate ideal bars based on BPM compatibility
        ideal_bars = 10 if bpm_diff_percent <= self.time_stretch_bpm_percentage_threshold else 6

        # Reduce bars until it fits in the fadein buffer
        for bars in [ideal_bars, 8, 6, 4, 2, 1]:
            if bars > ideal_bars:
                continue

            fadein_start_pos = self._calculate_optimal_fade_timing(bars)
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

    def _calculate_optimal_fade_timing(self, crossfade_bars: int) -> float | None:
        """Calculate beat positions for alignment."""
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
            self.extrapolated_fadeout_downbeats, self.fade_in_downbeats, crossfade_bars
        )
        if downbeat_positions is not None:
            return downbeat_positions

        # Try regular beats if downbeats insufficient
        required_beats = crossfade_bars * beats_per_bar
        beat_positions = calculate_beat_positions(
            self.fade_out_beats, self.fade_in_beats, required_beats
        )
        if beat_positions is not None:
            return beat_positions

        # Fallback: No beat alignment possible
        self.logger.log(VERBOSE_LOG_LEVEL, "No beat alignment possible (insufficient beats)")
        return None

    def _adjust_crossfade_to_downbeats(
        self,
        crossfade_duration: float,
        fadein_start_pos: float | None,
        min_downbeat_pos: float = 0.0,
    ) -> float:
        """Adjust crossfade duration to align with outgoing track's downbeats."""
        # If we don't have downbeats or beat alignment is disabled, return original duration
        if len(self.extrapolated_fadeout_downbeats) == 0 or fadein_start_pos is None:
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

        for downbeat in self.extrapolated_fadeout_downbeats:
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
