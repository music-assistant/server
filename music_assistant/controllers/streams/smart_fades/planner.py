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

import numpy as np
import numpy.typing as npt

from music_assistant.constants import VERBOSE_LOG_LEVEL
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
    Deck,
    EqPlan,
    FadeOutTrim,
    ShelfSchedule,
    SmartFadeNotApplicable,
    TempoPlan,
    TransitionPlan,
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

    # Bass-swap EQ: shelf corners/depths as on real club mixers
    low_shelf_freq: int = 100
    high_shelf_freq: int = 13000
    eq_kill_db: float = -26.0
    high_ease_db: float = -20.0
    # bass handover spans half the overlap; <2 bars reads as an event, >8 bars is masked
    bass_swap_fraction: float = 0.5
    bass_swap_min_bars: int = 2
    bass_swap_max_bars: int = 8

    # Working state for one plan() run, (re)set by _prepare_decks
    outgoing: Deck
    incoming: Deck
    effective_end: float
    fadeout_trim: FadeOutTrim | None
    extrapolated_downbeats: npt.NDArray[np.float32]

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
        """
        Load both tracks onto the decks and cue the outgoing tail.

        Validates the analysis, derives the usable beat grids and locates the
        audible end of the outgoing tail.  Raises ``SmartFadeNotApplicable``
        when the tail is too short to be useful.

        :param fade_out_analysis: Analysis data for the outgoing track.
        :param fade_in_analysis: Analysis data for the incoming track.
        :param buffer_duration: Length in seconds of the fade-out holdback buffer.
        """
        if (
            fade_out_analysis.bpm is None
            or fade_in_analysis.bpm is None
            or fade_out_analysis.beats is None
            or fade_in_analysis.beats is None
        ):
            raise ValueError("AudioAnalysisData must have bpm and beats set for smart crossfade")
        incoming_downbeats = (
            fade_in_analysis.downbeats
            if fade_in_analysis.downbeats is not None
            else fade_in_analysis.beats
        )
        self.incoming = Deck(
            analysis=fade_in_analysis,
            bpm=fade_in_analysis.bpm,
            # Only beats within the buffered head are usable for alignment decisions
            beats=fade_in_analysis.beats[fade_in_analysis.beats <= SMART_CROSSFADE_DURATION],
            downbeats=incoming_downbeats[incoming_downbeats <= SMART_CROSSFADE_DURATION],
        )
        self.outgoing = Deck(
            analysis=fade_out_analysis,
            bpm=fade_out_analysis.bpm,
            # Raw full-track grids; the shift to buffer-local coordinates happens
            # in _cue_outgoing_tail where the actual buffer length is known
            beats=fade_out_analysis.beats,
            downbeats=(
                fade_out_analysis.downbeats
                if fade_out_analysis.downbeats is not None
                else np.array([], dtype=np.float32)
            ),
        )
        self._cue_outgoing_tail(buffer_duration)
        self.extrapolated_downbeats = extrapolate_downbeats(
            self.outgoing.downbeats,
            buffer_size=self.effective_end,
            bpm=self.outgoing.bpm,
        )
        # Additional verbose logging to debug rare failures
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "SmartCrossFade plan: fade_out: %s, fade_in: %s",
            fade_out_analysis,
            fade_in_analysis,
        )

    def _cue_outgoing_tail(self, buffer_duration: float) -> None:
        """
        Locate the audible end of the outgoing tail and align its grids to it.

        Sets ``self.effective_end``, ``self.fadeout_trim`` (when trailing silence
        is worth dropping), converts the outgoing deck's grids from full-track to
        buffer-local coordinates using the actual buffer length, and masks them
        to the audible window.  Raises ``SmartFadeNotApplicable`` when the tail
        is too short to be useful.

        :param buffer_duration: Length in seconds of the fade-out holdback buffer.
        """
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
        buffer_offset = max(0.0, (self.outgoing.analysis.duration or 0.0) - buffer_duration)
        beats = self.outgoing.beats - buffer_offset
        downbeats = self.outgoing.downbeats - buffer_offset

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
        """
        Plan the bass swap between the two tracks.

        B enters bass-killed; both low shelves trade over a proportional window
        (half the overlap, clamped to 2-8 bars) CENTERED on the swap point, so
        the handover reads as a morph around the musical moment rather than an
        event.  Highs ease in before the exchange and out after it.  A-side
        schedules are in the outgoing track's input time (the renderer places
        them before the tempo stretch); B-side schedules are in the incoming
        track's post-trim time, where t=0 equals the crossfade start.

        :param crossfade_duration: Rendered overlap length in seconds.
        :param tempo_plan: Tempo ramp (maps rendered offsets to A-input offsets).
        :param fadein_trim_start: Incoming head trim, if beat alignment applied one.
        """
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

        low_out = ShelfSchedule(
            ShelfType.LOW,
            self.low_shelf_freq,
            [(0.0, 0.0), *db_ramp(start_out, swap_len_input, 0.0, self.eq_kill_db)],
        )
        low_in = ShelfSchedule(
            ShelfType.LOW,
            self.low_shelf_freq,
            [(0.0, self.eq_kill_db), *db_ramp(start_in, swap_len, self.eq_kill_db, 0.0)],
        )
        high_out = ShelfSchedule(
            ShelfType.HIGH,
            self.high_shelf_freq,
            [
                (0.0, 0.0),
                *db_ramp(start_out + swap_len_input, ease * ratio, 0.0, self.high_ease_db),
            ],
        )
        high_in = ShelfSchedule(
            ShelfType.HIGH,
            self.high_shelf_freq,
            [
                (0.0, self.high_ease_db),
                *db_ramp(max(0.0, start_in - ease), ease, self.high_ease_db, 0.0),
            ],
        )
        return EqPlan(
            swap_at=swap_at, low_out=low_out, low_in=low_in, high_out=high_out, high_in=high_in
        )

    def _choose_swap_point(
        self, crossfade_duration: float, fadein_trim_start: float | None
    ) -> float:
        """
        Pick the bass-swap moment, in rendered seconds into the crossfade.

        Prefers the incoming track's groove entry (drums coming in) when it lands
        anywhere inside the overlap; otherwise the incoming downbeat nearest to
        60% through the overlap.

        :param crossfade_duration: Rendered overlap length in seconds.
        :param fadein_trim_start: Incoming head trim, if beat alignment applied one.
        """
        trim = fadein_trim_start or 0.0
        entry = detect_groove_entry(
            self.incoming.analysis.rms_energy,
            self.incoming.analysis.duration,
            self.incoming.downbeats,
        )
        candidate = entry - trim
        if not 0.0 < candidate <= crossfade_duration:
            candidate = 0.6 * crossfade_duration
        # snap to the incoming grid so the new bassline lands on its own 1
        post_trim = self.incoming.downbeats - trim
        post_trim = post_trim[(post_trim > 0.0) & (post_trim < crossfade_duration)]
        if len(post_trim):
            candidate = float(post_trim[np.argmin(np.abs(post_trim - candidate))])
        return candidate
