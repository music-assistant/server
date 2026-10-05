"""
Beat-driven audio analyzer for Hue Entertainment.

Cycles palette colors smoothly between beats. The schedule of upcoming beats
arrives from the server in advance, so each segment between two beats can be
rendered as a hold + cross-fade window centered on the beat boundary. Each
beat also produces a short brightness pulse (downbeats hit harder in some modes).
"""

from __future__ import annotations

import colorsys
import itertools
import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hue_entertainment import LightColorCommand

if TYPE_CHECKING:
    from aiosendspin.models.visualizer import BeatTiming
    from hue_entertainment import LightChannel

# color@v1 fields harvested for the cycling palette. Order here is only the
# collection order — the rendering palette is reordered for vibrancy + maximal
# perceptual contrast between consecutive beats.
_PALETTE_SOURCE_FIELDS: tuple[str, ...] = (
    "primary",
    "accent",
    "on_dark",
    "on_light",
    "background_dark",
    "background_light",
)

# When two consecutive palette colors land closer than this Euclidean distance
# in normalised RGB, the second one is dimmed so beat-to-beat contrast still
# comes through as a brightness step instead of a near-identical color repeat.
_PALETTE_NEIGHBOUR_DISTANCE_THRESHOLD = 0.4
_PALETTE_DIM_FACTOR = 0.45

# After max-channel equalize, palette entries that land within this RGB
# distance of the previous one are dropped so consecutive beats never share
# a visually identical color.
_PALETTE_DEDUP_DISTANCE = 0.25

# Server colors whose brightest channel falls below this threshold are dropped
# entirely (otherwise they'd appear "off").
_BLACK_DROP_THRESHOLD = 0.05

# Pool entries with HSV saturation below this are treated as achromatic
# (white/grey/black) and excluded from hue selection. Saturation, not absolute
# RGB spread, so a dark-but-colored entry (brown, maroon, navy) still counts as
# a hue instead of washing into the neutral path.
_PALETTE_MIN_SAT = 0.25

# Mild saturation boost applied to every server color so washed-out album
# palettes still feel saturated on Hue lamps. Hue + value preserved.
_PALETTE_SATURATION_BOOST = 1.0

# Two chromatic colors whose HSV hue differs by more than this (fraction of the
# wheel, ~29°) count as separate hue families. Server palettes derive most
# fields from one primary, so several entries usually share a hue; this gates
# whether we cycle the album's real colors or synthesize a same-family gradient.
_HUE_DISTINCT = 0.08

# Saturation floor when expanding a single seed hue into a shade gradient, so a
# dark/muted primary still reads as a color rather than washing out.
_SHADE_MIN_SAT = 0.6
# Saturation multipliers + analogous hue offsets for the same-family gradient.
# _equalize_palette flattens value, so variety must come from saturation/hue.
_SHADE_STEPS: tuple[tuple[float, float], ...] = (
    (-0.06, 1.00),
    (0.00, 1.00),
    (0.00, 0.70),
    (0.06, 0.85),
    (0.00, 0.45),
)

# Subtle warm↔cool tint sweep used both for monochrome covers (no chromatic
# server colors) and as the default before any color@v1 update has arrived.
# Reads as tinted whites on the lamps rather than flashing vibrant defaults.
_NEUTRAL_GRADIENT: list[tuple[float, float, float]] = [
    (1.0, 0.93, 0.86),  # warm white
    (1.0, 1.0, 1.0),  # neutral white
    (0.86, 0.93, 1.0),  # cool white
    (0.90, 0.90, 0.92),  # soft grey
]


@dataclass(frozen=True, slots=True)
class _ModePreset:
    """Bundle of tunable knobs that define a visualization mode."""

    pulse_peak: float
    # Separate pulse target on downbeats (equals pulse_peak when not differentiated).
    downbeat_pulse_peak: float
    beat_animation_fraction: float
    channel_transient_scale: float
    color_gradient_spread: float
    # How many palette slots the cycle advances per beat. <1 slows hue
    # progression so the palette only drifts across multiple bars.
    palette_advance: float
    # How many palette slots the spatial gradient drifts per second. Lets
    # the gradient flow across the room over time independent of beats.
    spatial_drift_per_second: float
    # Per-channel brightness multiplier range driven by the spectrum transient.
    # Setting `floor == max` disables the visible swing (constant brightness),
    # which is what `ambient` mode uses.
    channel_floor: float
    channel_max: float
    # Scales the onset (peak) brightness boost: 0.0 disables onset flashes
    # entirely (ambient), 1.0 is the full boost.
    onset_boost: float = 1.0
    # Scales the bass-driven saturation swing: 1.0 is the full swing, 0.0 holds
    # saturation at its floor (no bass reactivity).
    bass_saturation_scale: float = 1.0


# Modes selectable from the UI. ``DEFAULT_MODE`` is used when the configured
# mode string is unknown or missing.
DEFAULT_MODE = "smooth"
_MODES: dict[str, _ModePreset] = {
    "smooth": _ModePreset(
        # Default: gentle spectrum brightness swing (25%) over a slowly drifting palette.
        pulse_peak=1.0,
        downbeat_pulse_peak=1.0,
        beat_animation_fraction=0.0,
        channel_transient_scale=8.0,
        color_gradient_spread=1.0,
        palette_advance=0.125,
        spatial_drift_per_second=0.1,
        channel_floor=0.60,
        channel_max=0.85,
    ),
    "ambient": _ModePreset(
        # Pure beat-driven colour cycling, no brightness or time drift.
        # Hold colour 90% of the segment, crossfade the last 10%.
        pulse_peak=1.0,
        downbeat_pulse_peak=1.0,
        beat_animation_fraction=0.1,
        channel_transient_scale=0.0,
        color_gradient_spread=0.7,
        palette_advance=0.125,
        spatial_drift_per_second=0.0,
        channel_floor=0.80,
        channel_max=0.80,
        onset_boost=0.0,
    ),
    "flashing": _ModePreset(
        # Strong on-beat brightness pulse, mild spectrum reaction. Downbeats hit harder.
        pulse_peak=1.8,
        downbeat_pulse_peak=2.2,
        beat_animation_fraction=0.0,
        channel_transient_scale=2.0,
        color_gradient_spread=0.7,
        palette_advance=0.25,
        spatial_drift_per_second=0.05,
        channel_floor=0.45,
        channel_max=0.80,
    ),
    "energetic": _ModePreset(
        # Maximum swing (70%) with fast palette movement and quick hue rotation.
        pulse_peak=1.2,
        downbeat_pulse_peak=1.2,
        beat_animation_fraction=0.2,
        channel_transient_scale=8.0,
        color_gradient_spread=1.0,
        palette_advance=0.5,
        spatial_drift_per_second=0.15,
        channel_floor=0.30,
        channel_max=1.00,
    ),
}

# Brightness pulse on the beat. Triangle window with this half-width fraction
# of the surrounding beat interval. ~8% of segment ≈ ±40 ms at 120 BPM. The
# peak magnitude is mode-driven (see `_MODES`).
_PULSE_HALF_FRACTION = 0.08

# Per-channel spectrum split. The fast filter tracks the band's current
# energy; the baseline filter is slow (~5 s at 50 Hz) so its lag from the
# fast value is essentially the band's transient onset. Sustained bass keeps
# the baseline near the fast value → transient ≈ 0 → light sits at the mode's
# floor. A new kick / hit briefly raises the fast value above the baseline →
# transient spikes → light flashes up toward the mode's max.
# Per-mode channel_floor/channel_max define the actual range (see _MODES).
# Cap the highest spectrum bin used for the z-axis mapping. With 17 mel bins
# (20-20000 Hz), bin 10 ends at ~3.5 kHz which covers vocal fundamentals,
# presence, and lead-instrument clarity. Higher bins (~5-20 kHz) are pure
# cymbal/air and excluded to keep top lights musical, not hissy.
_CHANNEL_BIN_MAX = 10
_SPECTRUM_GAMMA = 0.5
_CHANNEL_RISE = 0.75
# Server already EMA-smooths spectrum bins. A heavy bridge decay on top stacks
# the smoothing chain and reads as sluggish tails on snare/hi-hat.
_CHANNEL_DECAY = 0.10
_CHANNEL_BASELINE_RISE = 0.013
_CHANNEL_BASELINE_DECAY = 0.013
# Transient scaling, beat animation fraction, pulse peak and color gradient
# spread are all selected by the active mode (see _MODES). _SPECTRUM_BIN_NOISE_GATE
# stays as a flat threshold across modes. Raised above the server's soft-floor
# emission band (db -60..-54 maps to ~0.025) so quiet passages stay quiet.
_SPECTRUM_BIN_NOISE_GATE = 0.02

# Lights need at least this much spread (in the bridge's normalised position
# units, typically [-1, 1]) along an axis for spatial effects to be enabled.
_SPATIAL_SPREAD_THRESHOLD = 0.3

# Hard floor on the per-channel multiplier so no individual light ever
# vanishes while the stream is active.
_PER_CHANNEL_MIN_MULT = 0.3

# Bass-band → saturation modulation. Sustained bass keeps the slow baseline
# tracking the current level, so only TRANSIENTS (kicks above the baseline)
# push saturation toward the max — quiet music sits at the min.
# Bins fed into the bass-saturation transient. Bins 0-1 cover sub-bass +
# kick body (20-130 Hz), so any drum hit drives the saturation, not just
# sub-bass-heavy genres.
_BASS_SAT_BINS = 2
_BASS_SAT_MIN = 0.5
_BASS_SAT_MAX = 0.85
_BASS_BASELINE_SMOOTHING = 0.005  # ~10 s baseline at 20 Hz — lags more
_BASS_TRANSIENT_SCALE = 10.0
_BASS_SAT_RISE = 0.8
# Server EMA already softens the saturation drop; lower bridge decay to avoid
# saturation crawling back to floor too slowly.
_BASS_SAT_DECAY = 0.08

# Peak (onset) flash: a `peak` binary fires when the server's onset detector
# trips. Map strength (0-255) to a brightness boost added to every channel,
# decaying linearly over this window.
_PEAK_BOOST_DECAY_US = 250_000
_PEAK_BOOST_SCALE = 0.3
# Ignore onsets below this normalized strength so low-confidence peaks don't
# scroll the palette or lift brightness. Tunable.
_PEAK_MIN_STRENGTH = 0.1


@dataclass(frozen=True, slots=True)
class _ScheduledBeat:
    """A beat in the analyzer's queue with its resolved palette position."""

    timestamp_us: int
    beat_in_bar: int
    is_downbeat: bool


class _ExpFilter:
    """Exponential smoothing filter with separate attack and decay rates."""

    def __init__(self, alpha_rise: float, alpha_decay: float) -> None:
        self._alpha_rise = alpha_rise
        self._alpha_decay = alpha_decay
        self.value: float = 0.0

    def update(self, new_value: float) -> float:
        """Update the filter with ``new_value`` and return the smoothed result."""
        alpha = self._alpha_rise if new_value > self.value else self._alpha_decay
        self.value = alpha * new_value + (1.0 - alpha) * self.value
        return self.value


class HueAudioAnalyzer:
    """Render Hue colors from a paced beat schedule and a color@v1 palette."""

    def __init__(
        self,
        channels: list[LightChannel],
        color_mode: str = DEFAULT_MODE,
        brightness: int = 100,
    ) -> None:
        """
        Initialize the analyzer.

        ``color_mode`` selects a preset bundle from `_MODES` (defaults to
        `DEFAULT_MODE` when unknown); reactivity is governed by that preset.
        """
        self._channels = channels
        self._color_mode = color_mode
        self._mode = _MODES.get(color_mode, _MODES[DEFAULT_MODE])
        self._brightness = max(0, min(100, brightness)) / 100.0

        self._server_palette: dict[str, tuple[int, int, int] | None] = {}
        self._beats: deque[_ScheduledBeat] = deque()
        # -1 means no beat seen yet; first beat (or first downbeat) anchors it.
        self._last_beat_in_bar = -1

        # Per-channel smoothed band energy (one filter per light).
        self._channel_filters: list[_ExpFilter] = [
            _ExpFilter(_CHANNEL_RISE, _CHANNEL_DECAY) for _ in channels
        ]
        # Slow per-channel baseline; transient = fast - baseline drives the
        # onset-only modulation so sustained bass doesn't peg the multiplier.
        self._channel_baselines: list[_ExpFilter] = [
            _ExpFilter(_CHANNEL_BASELINE_RISE, _CHANNEL_BASELINE_DECAY) for _ in channels
        ]
        # Latest active spectrum (normalized 0-1, perceptually lifted) after
        # draining the pending queue up to the current render time.
        self._spectrum: list[float] = []
        # Spectrum frames keyed by server-clock ts. Drained inside render() so
        # spectrum-driven color reacts on audio time, not network arrival time.
        self._pending_spectrum: deque[tuple[int, list[int]]] = deque()

        # Bass-transient → saturation (1.0 = full color, _BASS_SAT_MIN = dim chroma).
        self._bass_saturation = _BASS_SAT_MIN
        self._bass_baseline = 0.0

        # Onset peak boost: brief brightness lift applied to every channel.
        # Peaks arrive ahead of playback (audio is buffered seconds in advance);
        # they are queued by their server-clock ts and promoted to the active
        # boost when ``render(now_us)`` catches up. Active boost decays linearly
        # from `_peak_boost` over `_PEAK_BOOST_DECAY_US`.
        self._pending_peaks: deque[tuple[int, float]] = deque()
        self._peak_boost: float = 0.0
        self._peak_set_at_us: int | None = None

        # Peak-driven palette walker. Used only when no beat schedule is
        # active; each consumed onset advances the position by one palette
        # advance step so colours scroll on onsets instead of holding static.
        self._peak_palette_position: float = 0.0

        # Spatial layout — cached once because channel positions don't change
        # for the lifetime of an analyzer.
        self._z_valid, self._z_norm, self._z_spread = _normalise_axis(channels, axis_index=2)
        self._x_valid, self._x_norm, self._x_spread = _normalise_axis(channels, axis_index=0)

    def update_settings(
        self,
        color_mode: str | None = None,
        brightness: int | None = None,
    ) -> None:
        """Update settings without reset."""
        if color_mode is not None:
            self._color_mode = color_mode
            self._mode = _MODES.get(color_mode, _MODES[DEFAULT_MODE])
        if brightness is not None:
            self._brightness = max(0, min(100, brightness)) / 100.0

    # -- Input events --

    def apply_color_palette(self, update: dict[str, tuple[int, int, int] | None]) -> None:
        """Merge a color@v1 server/state update into the cached palette."""
        self._server_palette.update(update)

    def apply_spectrum(self, bins: list[int], timestamp_us: int) -> None:
        """
        Queue a spectrum frame for the renderer.

        The frame is keyed on its server-clock ``timestamp_us`` and only
        promoted to the active spectrum once ``now_us`` catches up to it inside
        ``render``. This keeps spectrum-driven color aligned with audio time
        instead of network arrival time.
        """
        self._pending_spectrum.append((timestamp_us, bins))

    def apply_peak(self, strength: int, timestamp_us: int) -> None:
        """
        Queue an onset peak for the renderer.

        ``strength`` is the uint8 value from the visualizer `peak` binary
        (0-255). The peak is held in a pending queue keyed on its server-clock
        ``timestamp_us`` and only contributes to brightness once ``now_us``
        catches up to it inside ``render``. Onsets below ``_PEAK_MIN_STRENGTH``
        are dropped so low-confidence peaks don't scroll the palette.
        """
        normalized = max(0.0, min(1.0, strength / 255.0))
        if normalized < _PEAK_MIN_STRENGTH:
            return
        boost = normalized * _PEAK_BOOST_SCALE
        self._pending_peaks.append((timestamp_us, boost))

    def push_beats(self, beats: list[BeatTiming]) -> None:
        """
        Append new beats to the schedule with a continuous counter.

        The counter just increments per beat — no downbeat reset. With a
        fractional ``palette_advance`` (e.g. 0.25) the walker would otherwise
        be forced to wrap backwards at every bar end, which reads as a flash.
        Keeping a continuous count gives a strictly forward palette sweep.
        """
        for beat in beats:
            bib = 0 if self._last_beat_in_bar < 0 else self._last_beat_in_bar + 1
            self._beats.append(
                _ScheduledBeat(
                    timestamp_us=beat.timestamp_us,
                    beat_in_bar=bib,
                    is_downbeat=beat.is_downbeat,
                )
            )
            self._last_beat_in_bar = bib

    def clear_beat_schedule(self) -> None:
        """Drop only the beat schedule (a track change re-pushes it; spectrum keeps flowing)."""
        self._beats.clear()
        self._last_beat_in_bar = -1

    def clear_beats(self) -> None:
        """Drop the entire beat schedule + pending peaks (seek / stream end)."""
        self.clear_beat_schedule()
        self._pending_peaks.clear()
        self._peak_boost = 0.0
        self._peak_set_at_us = None
        self._peak_palette_position = 0.0
        self._pending_spectrum.clear()
        self._spectrum = []

    # -- Rendering --

    def render(self, now_us: int) -> list[LightColorCommand]:
        """Render every channel at server-clock time ``now_us``."""
        self._prune(now_us)
        self._advance_spectrum(now_us)
        palette = self._active_palette()
        if not self._channels:
            return []
        channel_mults = self._combined_channel_multipliers()
        # `_consume_peaks` always runs (it advances the peak palette walker);
        # `onset_boost` gates only the brightness flash, so ambient still cycles
        # colour on onsets but does not flash.
        peak_factor = self._consume_peaks(now_us) * self._mode.onset_boost
        if peak_factor > 0.0:
            channel_mults = [min(1.0, m + peak_factor) for m in channel_mults]
        base_brightness = self._brightness

        # Determine the segment (prior + next beat) we are inside. Without
        # both a prior and a next beat the beat-driven walker can't
        # interpolate, so fall back to a peak-driven palette scroll.
        prior, next_beat, segment, _prior_bib = self._current_segment(now_us)
        if next_beat is None or segment is None or segment <= 0 or prior is None:
            colors = self._peak_walk_colors(palette, now_us)
            return self._fill_per_channel(colors, base_brightness, channel_mults)

        pulse = self._compute_pulse(now_us, prior, next_beat, segment)
        colors = self._per_channel_colors(palette, prior, next_beat, now_us, segment)
        return self._fill_per_channel(colors, base_brightness * pulse, channel_mults)

    def _advance_spectrum(self, now_us: int) -> None:
        """Drain due spectrum frames and update saturation per drained frame."""
        while self._pending_spectrum and self._pending_spectrum[0][0] <= now_us:
            _, bins = self._pending_spectrum.popleft()
            self._consume_spectrum_frame(bins)

    def _consume_spectrum_frame(self, bins: list[int]) -> None:
        """Normalize ``bins`` into ``self._spectrum`` and step bass saturation."""
        if not bins:
            self._spectrum = []
            return
        spectrum: list[float] = []
        for b in bins:
            norm = max(0, min(65535, b)) / 65535.0
            if norm < _SPECTRUM_BIN_NOISE_GATE:
                spectrum.append(0.0)
                continue
            spectrum.append(min(1.0, norm**_SPECTRUM_GAMMA))
        self._spectrum = spectrum
        # Bass energy → saturation via transient detection. Slow baseline
        # tracks the sustained level, so only bass HITS above the baseline
        # push saturation up.
        bass_count = min(_BASS_SAT_BINS, len(spectrum))
        bass_energy = sum(spectrum[:bass_count]) / bass_count if bass_count else 0.0
        self._bass_baseline += (bass_energy - self._bass_baseline) * _BASS_BASELINE_SMOOTHING
        bass_transient = max(0.0, bass_energy - self._bass_baseline)
        swing = self._mode.bass_saturation_scale * min(1.0, bass_transient * _BASS_TRANSIENT_SCALE)
        sat_target = _BASS_SAT_MIN + (_BASS_SAT_MAX - _BASS_SAT_MIN) * swing
        sat_alpha = _BASS_SAT_RISE if sat_target > self._bass_saturation else _BASS_SAT_DECAY
        self._bass_saturation += (sat_target - self._bass_saturation) * sat_alpha

    def _consume_peaks(self, now_us: int) -> float:
        """
        Drain due peaks and return the decayed peak-boost contribution.

        Side effects: promotes pending peaks (keeping the strongest) and
        advances ``_peak_palette_position`` once per consumed onset so the
        fallback render branch (no beat schedule) scrolls colours on every
        hit. The return value is the active brightness lift after a linear
        fade over ``_PEAK_BOOST_DECAY_US``; peaks scheduled in the future
        contribute nothing yet.
        """
        while self._pending_peaks and self._pending_peaks[0][0] <= now_us:
            ts, boost = self._pending_peaks.popleft()
            if self._peak_set_at_us is None or boost >= self._peak_boost:
                self._peak_boost = boost
                self._peak_set_at_us = ts
            self._peak_palette_position += self._mode.palette_advance
        if self._peak_set_at_us is None:
            return 0.0
        elapsed = now_us - self._peak_set_at_us
        if elapsed < 0:
            return 0.0
        if elapsed >= _PEAK_BOOST_DECAY_US:
            self._peak_set_at_us = None
            self._peak_boost = 0.0
            return 0.0
        return self._peak_boost * (1.0 - elapsed / _PEAK_BOOST_DECAY_US)

    def _current_segment(
        self, now_us: int
    ) -> tuple[_ScheduledBeat | None, _ScheduledBeat | None, int | None, int | None]:
        """Return (prior, next, segment_us, prior_bib) for the current moment."""
        if not self._beats:
            return None, None, None, None
        prior_idx = self._find_prior_index(now_us)
        if prior_idx < 0:
            return None, None, None, self._beats[0].beat_in_bar
        prior = self._beats[prior_idx]
        if prior_idx + 1 >= len(self._beats):
            return prior, None, None, prior.beat_in_bar
        next_beat = self._beats[prior_idx + 1]
        return prior, next_beat, next_beat.timestamp_us - prior.timestamp_us, prior.beat_in_bar

    def _per_channel_colors(
        self,
        palette: list[tuple[float, float, float]],
        prior: _ScheduledBeat,
        next_beat: _ScheduledBeat,
        now_us: int,
        segment: int,
    ) -> list[tuple[float, float, float]]:
        """
        Per-channel colors with beat-aligned changes + spatial gradient.

        Each segment interpolates the palette position from `prior`'s slot
        toward the next beat's slot via the shortest modular path so downbeat-reset
        beat_in_bar values (e.g. 3 → 0) stay continuous regardless of palette length.
        The first ``1 - mode.beat_animation_fraction`` of the segment holds
        the prior color steady; the rest crossfades to land exactly on the
        next beat. ``mode.palette_advance`` scales the slot increment per
        beat — lower values keep hue progression slow even at fast tempos.
        """
        palette_len = len(palette)
        if palette_len == 0:
            return [(0.0, 0.0, 0.0) for _ in self._channels]
        if palette_len == 1:
            return [palette[0] for _ in self._channels]

        anim_fraction = self._mode.beat_animation_fraction
        advance = self._mode.palette_advance
        t_norm = max(0.0, min(1.0, (now_us - prior.timestamp_us) / segment))
        hold_until = 1.0 - anim_fraction
        if t_norm <= hold_until or anim_fraction <= 0.0:
            time_blend = 0.0
        else:
            time_blend = (t_norm - hold_until) / anim_fraction

        prior_slot = (prior.beat_in_bar * advance) % palette_len
        next_slot = (next_beat.beat_in_bar * advance) % palette_len
        # Walk the shortest signed slot distance from prior to next so a downbeat
        # (beat_in_bar resets to 0) keeps the palette moving forward, not backward.
        delta = (next_slot - prior_slot) % palette_len
        if delta > palette_len / 2:
            delta -= palette_len
        beat_position = prior_slot + time_blend * delta
        drift = self._mode.spatial_drift_per_second * (now_us / 1_000_000.0)
        sat_factor = self._bass_saturation
        result: list[tuple[float, float, float]] = []
        for i in range(len(self._channels)):
            spatial = self._spatial_palette_offset(i) + drift
            position = (beat_position + spatial) % palette_len
            low = int(position) % palette_len
            high = (low + 1) % palette_len
            frac = position - int(position)
            color = _lerp(palette[low], palette[high], frac)
            if sat_factor < 0.999:
                color = _scale_saturation(color, sat_factor)
            result.append(color)
        return result

    def _peak_walk_colors(
        self, palette: list[tuple[float, float, float]], now_us: int
    ) -> list[tuple[float, float, float]]:
        """
        Per-channel colors driven by the peak-onset palette walker.

        Used when no beat schedule is active. Each consumed peak has already
        advanced ``_peak_palette_position``; here we just sample the palette
        at that position plus the per-channel spatial offset and time drift,
        matching the beat-path math so the visuals stay coherent.
        """
        palette_len = len(palette)
        if palette_len == 0:
            return [(0.0, 0.0, 0.0) for _ in self._channels]
        if palette_len == 1:
            return [palette[0] for _ in self._channels]
        drift = self._mode.spatial_drift_per_second * (now_us / 1_000_000.0)
        sat_factor = self._bass_saturation
        result: list[tuple[float, float, float]] = []
        for i in range(len(self._channels)):
            position = (
                self._peak_palette_position + self._spatial_palette_offset(i) + drift
            ) % palette_len
            low = int(position) % palette_len
            high = (low + 1) % palette_len
            frac = position - int(position)
            color = _lerp(palette[low], palette[high], frac)
            if sat_factor < 0.999:
                color = _scale_saturation(color, sat_factor)
            result.append(color)
        return result

    def _spatial_palette_offset(self, channel_index: int) -> float:
        """
        Per-channel palette index offset from the lights' spatial layout.

        Picks whichever spatial axis has the larger spread (so e.g. lights
        stretched left↔right take the x-axis even when one of them is slightly
        elevated). Returns 0.0 when neither axis is usable so the room shows a
        single palette slot uniformly. The whole offset is added to a time-
        driven drift in the caller so the gradient flows across the room.
        """
        spread = self._mode.color_gradient_spread
        use_x = self._x_valid and self._x_spread >= self._z_spread
        if use_x:
            return (self._x_norm[channel_index] + 1.0) * 0.5 * spread
        if self._z_valid:
            return self._z_norm[channel_index] * spread
        if self._x_valid:
            return (self._x_norm[channel_index] + 1.0) * 0.5 * spread
        return 0.0

    def _fill_per_channel(
        self,
        colors: list[tuple[float, float, float]],
        brightness: float,
        channel_multipliers: list[float] | None,
    ) -> list[LightColorCommand]:
        """Emit one command per channel with its own color and brightness."""
        commands: list[LightColorCommand] = []
        for i, ch in enumerate(self._channels):
            mult = (
                channel_multipliers[i]
                if channel_multipliers is not None and i < len(channel_multipliers)
                else 1.0
            )
            scale = brightness * mult
            color = colors[i] if i < len(colors) else (0.0, 0.0, 0.0)
            commands.append(
                self._to_command(
                    ch.channel_id, color[0] * scale, color[1] * scale, color[2] * scale
                )
            )
        return commands

    def _combined_channel_multipliers(self) -> list[float]:
        """Per-channel spectrum-band multipliers, floored to _PER_CHANNEL_MIN_MULT."""
        n = len(self._channels)
        if n == 0:
            return []
        band = self._channel_multipliers()
        return [max(_PER_CHANNEL_MIN_MULT, band[i]) for i in range(n)]

    def _channel_multipliers(self) -> list[float]:
        """
        Per-channel brightness driven by per-band onset transients.

        For each light we sample a spectrum band (z-axis position when valid,
        contiguous index otherwise), maintain a fast smoothed value and a
        slow baseline, and modulate brightness by ``max(0, fast - baseline)``
        so sustained energy stops dominating. Constant bass → multiplier at
        floor; new kicks → multiplier flashes up.
        """
        n = len(self._channels)
        if n == 0 or not self._spectrum:
            return [1.0] * n
        bins = self._spectrum
        if n == 1:
            energy = sum(bins) / len(bins)
            return [self._transient_multiplier(0, energy)]
        usable = min(_CHANNEL_BIN_MAX, len(bins) - 1) + 1
        if self._z_valid:
            # Sort channels by z position so the bottom light gets the bottom
            # band, top light gets the top band — independent of channel order
            # in self._channels.
            z_order = sorted(range(n), key=lambda i: self._z_norm[i])
            multipliers = [0.0] * n
            for slot, idx in enumerate(z_order):
                lo = (slot * usable) // n
                hi = max(lo + 1, ((slot + 1) * usable) // n)
                band = bins[lo:hi]
                energy = sum(band) / len(band) if band else 0.0
                multipliers[idx] = self._transient_multiplier(idx, energy)
            return multipliers
        multipliers = []
        for i in range(n):
            lo = (i * usable) // n
            hi = max(lo + 1, ((i + 1) * usable) // n)
            band = bins[lo:hi]
            energy = sum(band) / len(band) if band else 0.0
            multipliers.append(self._transient_multiplier(i, energy))
        return multipliers

    def _transient_multiplier(self, channel_index: int, energy: float) -> float:
        """Update channel filters and return the transient-driven multiplier."""
        floor = self._mode.channel_floor
        ceiling = self._mode.channel_max
        if self._mode.channel_transient_scale <= 0.0:
            # No spectrum-driven swing: hold the floor (which == max for the
            # `ambient` mode, giving fully constant brightness).
            return floor
        fast = self._channel_filters[channel_index].update(energy)
        baseline = self._channel_baselines[channel_index].update(energy)
        transient = max(0.0, fast - baseline)
        modulation = min(1.0, transient * self._mode.channel_transient_scale)
        return floor + (ceiling - floor) * modulation

    # -- Helpers --

    def _prune(self, now_us: int) -> None:
        """
        Keep one past beat so the queue stays bounded.

        The most recent past beat is the start of the current segment used
        by the continuous palette walker.
        """
        past_count = 0
        for beat in self._beats:
            if beat.timestamp_us <= now_us:
                past_count += 1
            else:
                break
        while past_count > 1:
            self._beats.popleft()
            past_count -= 1

    def _find_prior_index(self, now_us: int) -> int:
        """Return the index of the latest beat with ``timestamp_us <= now_us``."""
        prior_idx = -1
        for i, beat in enumerate(self._beats):
            if beat.timestamp_us <= now_us:
                prior_idx = i
            else:
                break
        return prior_idx

    def _compute_pulse(
        self,
        now_us: int,
        prior: _ScheduledBeat,
        next_beat: _ScheduledBeat,
        segment: int,
    ) -> float:
        """
        Triangle-shaped brightness pulse centered on each adjacent beat.

        ``mode.pulse_peak > 1.0`` brightens on the beat; ``< 1.0`` dims on the
        beat; ``== 1.0`` disables the pulse. The largest-magnitude effect from
        either adjacent beat wins so the dip/punch lands cleanly on the beat
        without double-stacking when both windows overlap.
        """
        half_width_us = max(1, int(segment * _PULSE_HALF_FRACTION))
        max_effect = 0.0
        for beat in (prior, next_beat):
            dist = abs(now_us - beat.timestamp_us)
            if dist >= half_width_us:
                continue
            peak = self._mode.downbeat_pulse_peak if beat.is_downbeat else self._mode.pulse_peak
            shape = 1.0 - dist / half_width_us
            effect = (peak - 1.0) * shape
            if abs(effect) > abs(max_effect):
                max_effect = effect
        return 1.0 + max_effect

    def _active_palette(self) -> list[tuple[float, float, float]]:
        """
        Return the cycling palette ordered for vibrancy + maximum contrast.

        Collects every defined color@v1 field, picks the most vibrant one for
        the downbeat slot, then orders the rest greedily so each consecutive
        beat color is as different as possible from the previous one. Falls
        back to the neutral tint gradient when no color@v1 update has arrived.
        Finally every color is scaled to a common max channel so the cycle
        reads as evenly bright while hues stay pure.

        Synthesized gradients (single-hue shades / neutral tints) skip the dedup
        + neighbour-contrast passes: their variation is deliberately subtle and
        those passes would flatten it back into a single color.
        """
        raw, synthesized = self._gather_raw_palette()
        ordered = _order_palette(raw)
        max_pulse = max(self._mode.pulse_peak, self._mode.downbeat_pulse_peak)
        equalized = _equalize_palette(ordered, max_pulse)
        if synthesized:
            return equalized
        deduped = _drop_close_neighbours(equalized, _PALETTE_DEDUP_DISTANCE)
        return _enforce_neighbour_contrast(deduped, deduped)

    def _gather_raw_palette(self) -> tuple[list[tuple[float, float, float]], bool]:
        """
        Collect server colors and synthesise a gradient where needed.

        Returns ``(colors, synthesized)``. The server derives most color@v1
        fields from a single primary, so a colored cover usually yields one hue
        plus several near-whites. We therefore key off how many distinct hue
        families the chromatic colors span:

        - Two or more distinct hues: cycle the album's real colors.
        - A single hue (any number of shades of it): expand the most saturated
          one into a same-family gradient, dropping the washed near-whites.
        - No chromatic color at all: a subtle neutral tint sweep.

        Only when the color role has produced nothing at all do we fall back
        to the neutral gradient (same one used for monochrome covers). The
        boolean flags the synthesized cases so the caller can skip passes
        that would flatten their subtle variation.
        """
        bases: list[tuple[float, float, float]] = []
        seen: set[tuple[int, int, int]] = set()
        for name in _PALETTE_SOURCE_FIELDS:
            value = self._server_palette.get(name)
            if value is None or value in seen:
                continue
            seen.add(value)
            base = (value[0] / 255.0, value[1] / 255.0, value[2] / 255.0)
            if max(base) < _BLACK_DROP_THRESHOLD:
                continue
            bases.append(_boost_saturation(base, _PALETTE_SATURATION_BOOST))
        if not bases:
            return list(_NEUTRAL_GRADIENT), True
        chromatic = [c for c in bases if colorsys.rgb_to_hsv(*c)[1] >= _PALETTE_MIN_SAT]
        if _distinct_hue_count(chromatic) >= 2:
            return chromatic, False
        if chromatic:
            seed = max(chromatic, key=lambda c: colorsys.rgb_to_hsv(*c)[1])
            return _build_shade_palette(seed), True
        return list(_NEUTRAL_GRADIENT), True

    @staticmethod
    def _to_command(channel_id: int, r: float, g: float, b: float) -> LightColorCommand:
        """Convert float RGB (0-1) to a 16-bit LightColorCommand."""
        return LightColorCommand(
            channel_id=channel_id,
            red=int(max(0.0, min(1.0, r)) * 65535),
            green=int(max(0.0, min(1.0, g)) * 65535),
            blue=int(max(0.0, min(1.0, b)) * 65535),
        )


def _lerp(
    a: tuple[float, float, float], b: tuple[float, float, float], t: float
) -> tuple[float, float, float]:
    """Linear interpolation between two RGB triples."""
    return (a[0] * (1.0 - t) + b[0] * t, a[1] * (1.0 - t) + b[1] * t, a[2] * (1.0 - t) + b[2] * t)


def _normalise_axis(
    channels: list[LightChannel], *, axis_index: int
) -> tuple[bool, list[float], float]:
    """
    Return (valid, per-channel normalised position, min-gap) for an axis.

    Valid when the lights span at least ``_SPATIAL_SPREAD_THRESHOLD``. The
    returned score is the minimum gap between adjacent sorted positions — an
    axis where lights are evenly spread scores high, an axis with all-but-one
    light clustered scores ~0. Lets the caller pick the axis where the
    spatial gradient will actually look spatial.
    """
    if not channels:
        return False, [], 0.0
    values = [ch.position[axis_index] for ch in channels]
    lo, hi = min(values), max(values)
    spread = hi - lo
    if spread < _SPATIAL_SPREAD_THRESHOLD:
        return False, [0.0 for _ in channels], 0.0
    sorted_vals = sorted(values)
    gaps = [sorted_vals[i + 1] - sorted_vals[i] for i in range(len(sorted_vals) - 1)]
    min_gap = min(gaps) if gaps else spread
    if axis_index == 0:
        return True, [(v - lo) / spread * 2.0 - 1.0 for v in values], min_gap
    return True, [(v - lo) / spread for v in values], min_gap


def _scale_saturation(rgb: tuple[float, float, float], factor: float) -> tuple[float, float, float]:
    """
    Lerp ``rgb`` toward its grey-axis projection by ``1 - factor``.

    factor=1.0 → original color. factor=0.0 → pure grey at the same channel
    sum. Preserves brightness; only chroma changes.
    """
    r, g, b = rgb
    grey = (r + g + b) / 3.0
    return (
        grey + (r - grey) * factor,
        grey + (g - grey) * factor,
        grey + (b - grey) * factor,
    )


def _boost_saturation(rgb: tuple[float, float, float], factor: float) -> tuple[float, float, float]:
    """Multiply HSV saturation by ``factor`` (clipped to 1.0). Hue+value kept."""
    r, g, b = rgb
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return colorsys.hsv_to_rgb(h, min(1.0, s * factor), v)


def _distinct_hue_count(colors: list[tuple[float, float, float]]) -> int:
    """
    Count how many distinct hue families ``colors`` span.

    Hues within ``_HUE_DISTINCT`` of each other (with wraparound) collapse into
    one family, so several shades of one color count as a single hue.
    """
    if not colors:
        return 0
    hues = sorted(colorsys.rgb_to_hsv(*c)[0] for c in colors)
    families = 1
    for prev, cur in itertools.pairwise(hues):
        if cur - prev > _HUE_DISTINCT:
            families += 1
    if families > 1 and (hues[0] + 1.0 - hues[-1]) <= _HUE_DISTINCT:
        families -= 1
    return families


def _build_shade_palette(
    seed: tuple[float, float, float],
) -> list[tuple[float, float, float]]:
    """
    Expand a single chromatic seed into a same-family gradient.

    Sweeps the seed's saturation and adds small analogous hue shifts so the
    cycle has gentle movement without straying off the album's hue. Value is
    held constant because `_equalize_palette` normalises brightness downstream;
    variety is carried by saturation (vivid → pale) and the hue offsets.
    """
    h, s, v = colorsys.rgb_to_hsv(*seed)
    s = max(s, _SHADE_MIN_SAT)
    v = max(v, _SHADE_MIN_SAT)
    return [
        colorsys.hsv_to_rgb((h + dh) % 1.0, min(1.0, s * sat_mult), v)
        for dh, sat_mult in _SHADE_STEPS
    ]


def _vibrancy(rgb: tuple[float, float, float]) -> float:
    """HSV-chroma vibrancy metric: distance from the grey axis."""
    return max(rgb) - min(rgb)


def _rgb_distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """Euclidean distance between two RGB colors in linearised gamma space."""
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _order_palette(
    colors: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """Order ``colors`` for cycling: vibrancy first, then greedy farthest-next."""
    if len(colors) <= 1:
        return list(colors)
    remaining = list(colors)
    start = max(range(len(remaining)), key=lambda i: _vibrancy(remaining[i]))
    ordered = [remaining.pop(start)]
    while remaining:
        last = ordered[-1]
        farthest_idx = max(range(len(remaining)), key=lambda i: _rgb_distance(remaining[i], last))
        ordered.append(remaining.pop(farthest_idx))
    return ordered


def _equalize_palette(
    colors: list[tuple[float, float, float]], pulse_peak: float
) -> list[tuple[float, float, float]]:
    """
    Scale every color so its brightest channel hits a common target.

    Every lamp is pushed to the same LED peak so the room stays at constant
    output regardless of which palette slot is showing. Hue + saturation are
    preserved. When the pulse brightens on the beat (``pulse_peak > 1``) we
    leave ``1/pulse_peak`` of headroom; when it dims (``pulse_peak <= 1``)
    no headroom is needed and we push to full output.
    """
    # Cap below 1.0 so Hue Color bulbs don't push to the gamut edge where the
    # firmware can fall back to off-color (sometimes green) interpolation.
    cap = 0.85
    target = cap if pulse_peak <= 1.0 else cap / pulse_peak
    return [_scale_to_max_channel(c, target) for c in colors]


def _drop_close_neighbours(
    colors: list[tuple[float, float, float]], threshold: float
) -> list[tuple[float, float, float]]:
    """
    Drop entries that sit within ``threshold`` RGB distance of the previous.

    Keeps the first entry. After max-channel equalize two album reds with
    different original brightness collapse to very similar values; this
    pass removes the duplicate so the cycle steps through visibly distinct
    colors. Always returns at least the first entry.
    """
    if len(colors) <= 1:
        return list(colors)
    result = [colors[0]]
    for c in colors[1:]:
        if _rgb_distance(result[-1], c) >= threshold:
            result.append(c)
    return result


def _enforce_neighbour_contrast(
    ordered_raw: list[tuple[float, float, float]],
    equalized: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """
    Dim consecutive entries whose raw RGB is too similar to the previous one.

    Brightness contrast carries the eye through the cycle when hue contrast
    alone is too weak. Dimming alternates so we never dim two in a row.
    """
    if len(equalized) <= 1:
        return list(equalized)
    result = [equalized[0]]
    last_dimmed = False
    for idx in range(1, len(ordered_raw)):
        too_close = (
            _rgb_distance(ordered_raw[idx - 1], ordered_raw[idx])
            < _PALETTE_NEIGHBOUR_DISTANCE_THRESHOLD
        )
        if too_close and not last_dimmed:
            r, g, b = equalized[idx]
            result.append(
                (r * _PALETTE_DIM_FACTOR, g * _PALETTE_DIM_FACTOR, b * _PALETTE_DIM_FACTOR)
            )
            last_dimmed = True
        else:
            result.append(equalized[idx])
            last_dimmed = False
    return result


def _scale_to_max_channel(
    rgb: tuple[float, float, float], target_max: float
) -> tuple[float, float, float]:
    """
    Scale ``rgb`` so ``max(r, g, b) == target_max`` while preserving hue.

    Black stays black. Tiny rounding overshoots are clipped to 1.0.
    """
    r, g, b = rgb
    max_ch = max(r, g, b)
    if max_ch < 1e-6 or target_max <= 0.0:
        return (0.0, 0.0, 0.0)
    gain = target_max / max_ch
    return (min(1.0, r * gain), min(1.0, g * gain), min(1.0, b * gain))
