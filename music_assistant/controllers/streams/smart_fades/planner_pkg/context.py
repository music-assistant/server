"""
Smart Fades - immutable per-transition context.

A ``TransitionContext`` is every PER-TRANSITION FACT a planner needs: the two
decks, their band profiles, the outgoing tail's energy/kick anchors and audible
boundary, the chosen tier, the vocal-activity masks and the coda/fade-onset
detections. None of it depends on a candidate's chosen overlap length or
re-anchor - those anchor-DEPENDENT derivations (re-anchored grids, trims) are
a candidate factory's job. Building this once, frozen, replaces the old
planner's mutable ``self`` scratchpad (``_pristine_*`` snapshots, re-anchoring
in place) with a value every candidate build reads from but never mutates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.streams.smart_fades.bands import (
    build_band_profile,
    detect_low_groove_entry,
    detect_low_mix_out,
)
from music_assistant.controllers.streams.smart_fades.helpers import (
    MIN_EFFECTIVE_FADE_BUFFER,
    MIX_OUT_ENERGY_FRACTION,
    SMART_CROSSFADE_DURATION,
    detect_effective_audio_end,
    detect_groove_entry,
    detect_mix_out_point,
    extrapolate_downbeats,
    keys_compatible,
)
from music_assistant.controllers.streams.smart_fades.models import (
    Deck,
    SmartFadeNotApplicable,
    TransitionTier,
)
from music_assistant.controllers.streams.smart_fades.structure import (
    detect_coda_zone,
    detect_mastered_fadeout,
)
from music_assistant.controllers.streams.smart_fades.vocal import (
    MAX_VOCAL_GAP,
    MIN_VOCAL_GAP,
    MIN_VOCAL_RUN,
    VOCAL_CLOSE_THRESHOLD,
    VOCAL_LEFT_PADDING,
    VOCAL_OPEN_THRESHOLD,
    VOCAL_RIGHT_PADDING,
    VocalHysteresisConfig,
    VocalMask,
    build_vocal_windows,
    parse_vocal_probabilities,
)

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

    from music_assistant.controllers.streams.smart_fades.models import BandProfile
    from music_assistant.controllers.streams.smart_fades.structure import CodaZone
    from music_assistant.controllers.streams.smart_fades.vocal import VocalTimeline
    from music_assistant.models.audio_analysis import AudioAnalysisData

# Only apply time stretching if BPM difference is < this %
# (research: the tier-1 dance-cluster population triples at ±8 vs ±5)
time_stretch_bpm_percentage_threshold: float = 8.0

# Fraction of sustained energy below which the outro no longer carries the mix
mix_out_energy_fraction: float = MIX_OUT_ENERGY_FRACTION

# A track qualifies for kick-following anchors when its median active-bar
# low-band power fraction reaches this share of its total power
low_timing_eligibility: float = 0.10
# Reference multiplier a bar's low power must reach to count as "kick present"
low_anchor_bar_fraction: float = 0.5
# Beyond this many outgoing bars of disagreement with the full-band anchor,
# the low anchor is untrustworthy and the full-band anchor wins
low_anchor_divergence_bars: int = 16

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


@dataclass(frozen=True, slots=True)
class TransitionContext:
    """
    Every per-transition fact a planner needs, computed once and never mutated.

    All buffer-local times share one origin: the outgoing track's
    ``buffer_offset`` media position. ``outgoing``/``incoming`` carry the
    unmasked (only dropped-before-buffer beats removed) beat grids; a
    candidate factory masks them to whichever anchor it is building.
    """

    outgoing: Deck
    incoming: Deck
    outgoing_profile: BandProfile | None
    incoming_profile: BandProfile | None
    buffer_duration: float
    buffer_offset: float
    audio_end: float
    # the kick-folded, downbeat-snapped anchor (the old planner's pristine
    # effective_end): the tier keyed on it, and it is where a candidate with
    # no explicit anchor cues the tail
    default_anchor: float
    mix_out_anchor: float | None
    kick_anchor: float | None
    fade_onset: float | None
    coda_zone: CodaZone | None
    tier: TransitionTier
    cross_meter: bool
    bpm_diff_percent: float
    vocal_out_placement: VocalMask | None
    vocal_in_placement: VocalMask | None
    vocal_out_scoring: VocalMask | None
    vocal_in_scoring: VocalMask | None
    natural_entry: float
    protective_downbeats: tuple[float, ...]


def build_transition_context(
    fade_out_analysis: AudioAnalysisData,
    fade_in_analysis: AudioAnalysisData,
    buffer_duration: float,
    logger: logging.Logger,
) -> TransitionContext:
    """
    Build the immutable per-transition context from the two tracks' analysis.

    Raises ``SmartFadeNotApplicable`` when the outgoing tail is too short
    (mostly silent, or too short once anchored) for any candidate to be built.

    :param fade_out_analysis: Analysis data for the outgoing track.
    :param fade_in_analysis: Analysis data for the incoming track.
    :param buffer_duration: Length in seconds of the available fade-out holdback.
    :param logger: Logger for verbose per-transition diagnostics.
    """
    # numpy is imported inside the function to keep it off the server startup path
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
    incoming = Deck(
        analysis=fade_in_analysis,
        bpm=fade_in_analysis.bpm,
        # Only beats within the buffered head are usable for alignment decisions
        beats=incoming_beats[incoming_beats <= SMART_CROSSFADE_DURATION],
        downbeats=incoming_downbeats[incoming_downbeats <= SMART_CROSSFADE_DURATION],
        beats_per_bar=fade_in_analysis.beats_per_bar or 4,
    )
    outgoing = Deck(
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
    outgoing_profile = build_band_profile(fade_out_analysis)
    incoming_profile = build_band_profile(fade_in_analysis)

    (
        buffer_offset,
        audio_end,
        tier_anchor,
        mix_out_anchor,
        kick_anchor,
        grid_beats,
        grid_downbeats,
    ) = _cue_outgoing_tail(outgoing, outgoing_profile, buffer_duration)
    outgoing = replace(outgoing, beats=grid_beats, downbeats=grid_downbeats)

    # Extrapolated up to the true RMS-audible boundary rather than the (possibly
    # downbeat-snapped) mix_out_anchor, so a later vocal re-anchor can always
    # find a real downbeat between the two
    protective_downbeats = extrapolate_downbeats(
        grid_downbeats,
        buffer_size=audio_end,
        bpm=outgoing.bpm,
        beats_per_bar=outgoing.beats_per_bar,
    )
    # Computed once so every candidate's swap point and reciprocal decision windows agree
    natural_entry = _detect_incoming_entry(incoming, incoming_profile)

    vocal_out_placement, vocal_in_placement, vocal_out_scoring, vocal_in_scoring = (
        _build_vocal_masks(
            fade_out_analysis, fade_in_analysis, outgoing, incoming, buffer_offset, audio_end
        )
    )

    # the tier reads the kick-folded anchor (the old planner's effective_end),
    # never the pure full-band mix_out_anchor: a kick-timed track's blendability
    # window ends where its kick dies, exactly as the old masked grid did
    cross_meter, tier = choose_tier(outgoing, incoming, tier_anchor)
    bpm_diff_percent = _bpm_diff_percent(outgoing.bpm, incoming.bpm)

    # fade detection is a per-transition fact regardless of which anchor a
    # candidate later picks; it never touches candidate state
    fade_onset_media = (
        detect_mastered_fadeout(
            outgoing_profile,
            buffer_offset,
            fade_out_analysis.duration or 0.0,
            min_bars=fade_min_bars,
            drop_db=fade_drop_db,
            monotone_share=fade_monotone_share,
            jitter_db=fade_jitter_db,
            frac_drift=fade_frac_drift,
            audible_floor=fade_audible_floor,
            frac_floor=fade_frac_floor,
        )
        if outgoing_profile is not None
        else None
    )
    fade_onset = fade_onset_media - buffer_offset if fade_onset_media is not None else None

    coda_zone = _detect_coda_zone(
        outgoing,
        outgoing_profile,
        vocal_out_placement,
        fade_onset_media,
        buffer_offset,
        tier_anchor,
        fade_out_analysis.duration or 0.0,
    )

    # Additional verbose logging to debug rare failures
    logger.log(
        VERBOSE_LOG_LEVEL,
        "SmartCrossFade context: fade_out: %s, fade_in: %s",
        fade_out_analysis,
        fade_in_analysis,
    )

    return TransitionContext(
        outgoing=outgoing,
        incoming=incoming,
        outgoing_profile=outgoing_profile,
        incoming_profile=incoming_profile,
        buffer_duration=buffer_duration,
        buffer_offset=buffer_offset,
        audio_end=audio_end,
        default_anchor=tier_anchor,
        mix_out_anchor=mix_out_anchor,
        kick_anchor=kick_anchor,
        fade_onset=fade_onset,
        coda_zone=coda_zone,
        tier=tier,
        cross_meter=cross_meter,
        bpm_diff_percent=bpm_diff_percent,
        vocal_out_placement=vocal_out_placement,
        vocal_in_placement=vocal_in_placement,
        vocal_out_scoring=vocal_out_scoring,
        vocal_in_scoring=vocal_in_scoring,
        natural_entry=natural_entry,
        protective_downbeats=tuple(float(x) for x in protective_downbeats),
    )


def _cue_outgoing_tail(
    outgoing: Deck, outgoing_profile: BandProfile | None, buffer_duration: float
) -> tuple[
    float, float, float, float, float | None, npt.NDArray[np.float32], npt.NDArray[np.float32]
]:
    """
    Anchor the outgoing tail at its energy mix-out point, snapped to a downbeat.

    Returns ``(buffer_offset, audio_end, tier_anchor, mix_out_anchor,
    kick_anchor, grid_beats, grid_downbeats)``. ``tier_anchor`` is the
    kick-FOLDED anchor the old planner called ``effective_end``: applicability
    and the tier decision key on it, so a kick-timed track keeps its original
    (shorter) blendability window. ``mix_out_anchor`` (pure full-band) and
    ``kick_anchor`` (low-band, ``None`` if ineligible) stay separate facts -
    which one a candidate should start from is a candidate-factory decision.
    ``grid_beats``/``grid_downbeats`` are the unmasked (only dropping
    pre-buffer beats) buffer-local grids, for a later candidate to mask to
    whichever anchor it picks.
    """
    # ACTUAL buffer length, not the constant 45s: the holdback yield loop leaves
    # up to ~1s less depending on chunk boundaries, and every buffer-local
    # coordinate below (mix-out detection, grid shift) must agree on it
    buffer_offset = max(0.0, (outgoing.analysis.duration or 0.0) - buffer_duration)
    silence_end = detect_effective_audio_end(
        outgoing.analysis.rms_energy, outgoing.analysis.duration, buffer_duration
    )
    raw_mix_out = detect_mix_out_point(
        outgoing.analysis.rms_energy,
        outgoing.analysis.duration,
        buffer_duration,
        outgoing.bpm,
        fraction=mix_out_energy_fraction,
        beats_per_bar=outgoing.beats_per_bar,
    )
    kick_anchor = _apply_low_mix_out(outgoing, outgoing_profile, raw_mix_out, buffer_offset)

    # the old planner folded the kick anchor into effective_end BEFORE snapping
    # and masking the grids, so applicability and the blendability window (and
    # thus the tier) are kick-aware; the fold stays tier-decision-local here
    folded_mix_out = kick_anchor if kick_anchor is not None else raw_mix_out
    tier_anchor = min(silence_end, folded_mix_out)
    if tier_anchor < MIN_EFFECTIVE_FADE_BUFFER:
        raise SmartFadeNotApplicable(f"outgoing tail is mostly silent ({tier_anchor:.1f}s audible)")

    # Shift fade-out beats from full-track to buffer-local coordinates
    beats = outgoing.beats - buffer_offset
    downbeats = outgoing.downbeats - buffer_offset

    # Unmasked (only dropping pre-buffer beats) grids: a later vocal/coda
    # re-anchor reads from these rather than re-deriving buffer-local coordinates
    grid_beats = beats[beats >= 0.0]
    grid_downbeats = downbeats[downbeats >= 0.0]

    # the RMS-audible boundary: a hard upper bound a later vocal/coda re-anchor
    # may extend the (about to be downbeat-snapped) anchor back up to, never past
    audio_end = min(silence_end, buffer_duration)

    tier_anchor = _snap_anchor(tier_anchor, buffer_duration, grid_downbeats, outgoing)
    if tier_anchor < MIN_EFFECTIVE_FADE_BUFFER:
        raise SmartFadeNotApplicable(
            f"outgoing tail too short after anchoring ({tier_anchor:.1f}s)"
        )
    mix_out_anchor = (
        tier_anchor
        if kick_anchor is None
        else _snap_anchor(min(silence_end, raw_mix_out), buffer_duration, grid_downbeats, outgoing)
    )

    return (
        buffer_offset,
        audio_end,
        tier_anchor,
        mix_out_anchor,
        kick_anchor,
        grid_beats,
        grid_downbeats,
    )


def _snap_anchor(
    anchor: float,
    buffer_duration: float,
    grid_downbeats: npt.NDArray[np.float32],
    outgoing: Deck,
) -> float:
    """Snap a tail anchor back to the last real downbeat within ~2 bars, or to the buffer end."""
    # Sub-half-second slack is not worth trimming: RMS bin granularity is
    # ~0.1-0.2s for typical track lengths, so finer precision is illusory
    if anchor >= buffer_duration - 0.5:
        # Without the trim the rendered stream still ends at buffer_duration,
        # so the anchor must follow it or every schedule lands early
        return buffer_duration
    # Snap the anchor back to the last real downbeat within ~2 bars so the
    # crossfade ends cleanly on the 1 rather than at an arbitrary RMS bin edge
    bar_seconds = outgoing.beats_per_bar * 60.0 / outgoing.bpm
    in_window = grid_downbeats[grid_downbeats <= anchor]
    if len(in_window) and anchor - float(in_window[-1]) < 2 * bar_seconds:
        return float(in_window[-1])
    return anchor


def _apply_low_mix_out(
    outgoing: Deck,
    outgoing_profile: BandProfile | None,
    full_band_mix_out: float,
    buffer_offset: float,
) -> float | None:
    """Return the low-band (kick) mix-out anchor when eligible and trustworthy, else None."""
    if not _is_low_timing_eligible(outgoing_profile):
        return None
    assert outgoing_profile is not None  # narrowed by the eligibility check
    low_mix_out = detect_low_mix_out(outgoing_profile, low_anchor_bar_fraction)
    if low_mix_out is None:
        return None
    low_mix_out_local = low_mix_out - buffer_offset
    bar_seconds = outgoing.beats_per_bar * 60.0 / outgoing.bpm
    divergence_bars = abs(low_mix_out_local - full_band_mix_out) / bar_seconds
    if divergence_bars > low_anchor_divergence_bars:
        return None
    if low_mix_out_local < MIN_EFFECTIVE_FADE_BUFFER:
        return None
    return low_mix_out_local


def _is_low_timing_eligible(profile: BandProfile | None) -> bool:
    """Return True when a track's low band carries enough of its power to time off."""
    import numpy as np  # noqa: PLC0415

    if profile is None:
        return False
    f_low = profile.bar_power["low"][profile.active] / profile.total_power[profile.active]
    return float(np.median(f_low)) >= low_timing_eligibility


def _detect_incoming_entry(incoming: Deck, incoming_profile: BandProfile | None) -> float:
    """Detect B's groove entry, preferring the kick when B is low-timing eligible."""
    if _is_low_timing_eligible(incoming_profile):
        assert incoming_profile is not None  # narrowed by the eligibility check
        low_entry = detect_low_groove_entry(incoming_profile, low_anchor_bar_fraction)
        if low_entry is not None and low_entry < SMART_CROSSFADE_DURATION:
            return low_entry
    return detect_groove_entry(
        incoming.analysis.rms_energy, incoming.analysis.duration, incoming.downbeats
    )


def _build_vocal_masks(
    fade_out_analysis: AudioAnalysisData,
    fade_in_analysis: AudioAnalysisData,
    outgoing: Deck,
    incoming: Deck,
    buffer_offset: float,
    audio_end: float,
) -> tuple[VocalMask | None, VocalMask | None, VocalMask | None, VocalMask | None]:
    """Validate and build both decks' FireRed vocal-activity masks, if the contract holds."""
    out_timeline = parse_vocal_probabilities(fade_out_analysis)
    in_timeline = parse_vocal_probabilities(fade_in_analysis)
    if out_timeline is None or in_timeline is None:
        return None, None, None, None
    duration = fade_out_analysis.duration or buffer_offset
    config = VocalHysteresisConfig(
        open_threshold=vocal_open_threshold,
        close_threshold=vocal_close_threshold,
        left_padding=vocal_left_padding,
        right_padding=vocal_right_padding,
        min_run=vocal_min_run,
        min_gap=vocal_min_gap,
        max_gap=vocal_max_gap,
    )
    # the scoring variant differs only in padding: padded edges are silence,
    # so they place cuts and anchors but never count as collision
    scoring_config = replace(config, left_padding=0.0, right_padding=0.0)
    vocal_out_placement = _build_outgoing_mask(
        out_timeline, duration, config, outgoing, buffer_offset, audio_end
    )
    vocal_out_scoring = _build_outgoing_mask(
        out_timeline, duration, scoring_config, outgoing, buffer_offset, audio_end
    )
    vocal_in_placement = _build_incoming_mask(in_timeline, config, incoming)
    vocal_in_scoring = _build_incoming_mask(in_timeline, scoring_config, incoming)
    return vocal_out_placement, vocal_in_placement, vocal_out_scoring, vocal_in_scoring


def _build_outgoing_mask(
    timeline: VocalTimeline,
    duration: float,
    config: VocalHysteresisConfig,
    outgoing: Deck,
    buffer_offset: float,
    audio_end: float,
) -> VocalMask:
    """
    Build the outgoing deck's buffer-local vocal mask for one config.

    :param timeline: The outgoing deck's validated FireRed timeline.
    :param duration: The outgoing track's media duration in seconds.
    :param config: Hysteresis/padding thresholds for the mask.
    :param outgoing: The outgoing deck, for its BPM.
    :param buffer_offset: Media time where the buffer starts.
    :param audio_end: Buffer-local RMS-audible boundary.
    """
    media_time_mask = build_vocal_windows(
        timeline.probabilities,
        timeline.frame_duration,
        buffer_offset,
        duration,
        beat_duration=60.0 / outgoing.bpm,
        config=config,
    )
    # shift media time to buffer-local time, then clamp to the RMS-audible
    # boundary: FireRed may never be trusted to extend it, so any window
    # (or trailing sliver of one) past it is simply dropped
    buffer_local = VocalMask(
        windows=[
            (left - buffer_offset, right - buffer_offset) for left, right in media_time_mask.windows
        ]
    )
    return buffer_local.clamped_to(audio_end)


def _build_incoming_mask(
    timeline: VocalTimeline, config: VocalHysteresisConfig, incoming: Deck
) -> VocalMask:
    """
    Build the incoming deck's head vocal mask for one config.

    :param timeline: The incoming deck's validated FireRed timeline.
    :param config: Hysteresis/padding thresholds for the mask.
    :param incoming: The incoming deck, for its BPM.
    """
    return build_vocal_windows(
        timeline.probabilities,
        timeline.frame_duration,
        0.0,
        float(SMART_CROSSFADE_DURATION),
        beat_duration=60.0 / incoming.bpm,
        config=config,
    )


def choose_tier(
    outgoing: Deck,
    incoming: Deck,
    tier_anchor: float,
) -> tuple[bool, TransitionTier]:
    """Pick the transition tier; anything that casts doubt on a long blend picks a shorter one."""
    cross_meter = outgoing.beats_per_bar != incoming.beats_per_bar
    if cross_meter:
        # no shared bar grid to beatmatch or blend across
        return cross_meter, TransitionTier.QUICK_FADE
    anchored_downbeats = outgoing.downbeats[outgoing.downbeats <= tier_anchor]
    if not _tail_is_blendable(anchored_downbeats):
        return cross_meter, TransitionTier.QUICK_FADE
    if _bpm_diff_percent(outgoing.bpm, incoming.bpm) > time_stretch_bpm_percentage_threshold:
        return cross_meter, TransitionTier.QUICK_FADE
    out_a, in_a = outgoing.analysis, incoming.analysis
    # the 16-bar tier is earned by a verifiable energy anchor: without RMS data
    # the blend could land on a mastered fade-out unnoticed
    if out_a.rms_energy is not None and keys_compatible(out_a.key, out_a.mode, in_a.key, in_a.mode):
        # a non-4/4 meter has no corpus evidence to support a 16-bar blend
        if outgoing.beats_per_bar != 4:
            return cross_meter, TransitionTier.TEMPO_BLEND
        return cross_meter, TransitionTier.FULL_BLEND
    return cross_meter, TransitionTier.TEMPO_BLEND


def _tail_is_blendable(downbeats: npt.NDArray[np.float32]) -> bool:
    """Return True when the anchored tail has enough regular downbeats for a blend."""
    import numpy as np  # noqa: PLC0415

    if len(downbeats) < 8:
        return False
    # metronomic grid: research measured 74% of library tails under 0.1s interval std
    return float(np.std(np.diff(downbeats))) < 0.1


def _bpm_diff_percent(outgoing_bpm: float, incoming_bpm: float) -> float:
    """Tempo difference between the two decks as a percentage."""
    return abs(1.0 - incoming_bpm / outgoing_bpm) * 100


def _detect_coda_zone(
    outgoing: Deck,
    outgoing_profile: BandProfile | None,
    vocal_out_placement: VocalMask | None,
    fade_onset_media: float | None,
    buffer_offset: float,
    tier_anchor: float,
    duration: float,
) -> CodaZone | None:
    """Detect a validated outro/coda zone over the buffered tail, if any."""
    if outgoing_profile is None:
        return None
    media_out = _vocal_out_media_mask(vocal_out_placement, buffer_offset)
    tail_start = buffer_offset
    out_saturated = _mask_saturated(media_out, max(0.001, duration - tail_start))
    if out_saturated:
        # a saturated (near-continuous vocal) outro supplies no fine structure
        # to distinguish a coda from the rest of the track
        return None
    earliest = _coda_earliest(outgoing, outgoing_profile, media_out, buffer_offset, tier_anchor)
    return detect_coda_zone(
        outgoing_profile,
        media_out,
        earliest,
        fade_onset_media,
        tail_start,
        duration,
        total_floor=coda_total_floor,
        min_seconds=coda_min_seconds,
        min_bars=coda_min_bars,
        level_hold=coda_level_hold,
    )


def _coda_earliest(
    outgoing: Deck,
    outgoing_profile: BandProfile | None,
    media_out: VocalMask,
    buffer_offset: float,
    tier_anchor: float,
) -> float:
    """Media time before which no coda bar may start: past A's vocal and its kick."""
    lead_end = media_out.last_end()
    kick_end: float | None = None
    if _is_low_timing_eligible(outgoing_profile):
        assert outgoing_profile is not None  # narrowed by the eligibility check
        kick_end = detect_low_mix_out(outgoing_profile, low_anchor_bar_fraction)
        if kick_end is not None:
            # detect_low_mix_out returns the last kick bar's START; the coda
            # begins after that bar rings out
            kick_end += outgoing.beats_per_bar * 60.0 / outgoing.bpm
    if kick_end is None:
        kick_end = buffer_offset + tier_anchor
    return max(lead_end, kick_end)


def _vocal_out_media_mask(vocal_out_placement: VocalMask | None, buffer_offset: float) -> VocalMask:
    """Return the outgoing vocal mask shifted from buffer-local back to media time (coda scope)."""
    if vocal_out_placement is None:
        return VocalMask(windows=[])
    return VocalMask(
        windows=[
            (left + buffer_offset, right + buffer_offset)
            for left, right in vocal_out_placement.windows
        ]
    )


def _mask_saturated(mask: VocalMask, span: float) -> bool:
    """Whether a mask's windows cover >=90% of a span (near-continuous vocal, no fine structure)."""
    covered = sum(right - left for left, right in mask.windows)
    return covered >= 0.9 * max(0.001, span)
