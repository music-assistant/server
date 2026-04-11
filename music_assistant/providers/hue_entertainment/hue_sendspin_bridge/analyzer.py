"""
Audio analyzer for converting visualization data to light colors.

Converts Sendspin VisualizerFrame data (loudness, spectrum, peak frequency)
into RGB color commands for Hue Entertainment light channels.

Uses layered effects inspired by LedFx:
- Beat detection with flash overlay (fast decay)
- Bass energy drives brightness with slow decay
- Color cycling on beats for variety
- Frequency-band mapping for spatial spread across lights
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from .models import LightColorCommand

if TYPE_CHECKING:
    from aiosendspin.models.visualizer import VisualizerFrame

    from .models import LightChannel

# Vibrant color palette — cycles on beats
_COLOR_PALETTE: list[tuple[float, float, float]] = [
    (1.0, 0.0, 0.2),  # hot pink
    (0.0, 0.5, 1.0),  # electric blue
    (1.0, 0.4, 0.0),  # orange
    (0.0, 1.0, 0.4),  # green
    (0.8, 0.0, 1.0),  # purple
    (1.0, 1.0, 0.0),  # yellow
    (0.0, 1.0, 1.0),  # cyan
    (1.0, 0.0, 0.6),  # magenta
]


class _ExpFilter:
    """Exponential smoothing filter with separate attack and decay rates."""

    def __init__(self, alpha_rise: float = 0.5, alpha_decay: float = 0.5) -> None:
        self.alpha_rise = alpha_rise
        self.alpha_decay = alpha_decay
        self.value = 0.0

    def update(self, new_value: float) -> float:
        """Update the filter with a new value."""
        alpha = self.alpha_rise if new_value > self.value else self.alpha_decay
        self.value = alpha * new_value + (1.0 - alpha) * self.value
        return self.value


class HueAudioAnalyzer:
    """
    Converts visualization data into dynamic light color commands.

    Layered effect architecture:
    - Base layer: frequency-band colors spread across channels
    - Beat flash: bright overlay on detected beats (fast decay)
    - Bass pulse: overall brightness modulated by bass energy (slow decay)
    - Color cycling: palette rotates on beats for variety
    """

    def __init__(
        self,
        channels: list[LightChannel],
        color_mode: str = "spectrum",
        brightness: int = 100,
        intensity: int = 70,
    ) -> None:
        """Initialize the analyzer."""
        self._channels = channels
        self._color_mode = color_mode
        self._brightness = max(0, min(100, brightness)) / 100.0
        self._intensity = max(0, min(100, intensity)) / 100.0

        # Filters for smooth dynamics
        self._bass_filter = _ExpFilter(alpha_rise=0.9, alpha_decay=0.15)
        self._energy_filter = _ExpFilter(alpha_rise=0.8, alpha_decay=0.2)
        self._beat_flash = 0.0  # decays each frame

        # Beat detection state
        self._loudness_history: list[float] = []
        self._last_beat_time = 0.0
        self._min_beat_interval = 0.12  # 500 BPM max

        # Color cycling
        self._color_index = 0
        self._base_hue_offset = 0.0

        # Per-channel smoothed output
        self._smoothed: list[tuple[float, float, float]] = [(0.0, 0.0, 0.0) for _ in channels]

    def update_settings(
        self,
        color_mode: str | None = None,
        brightness: int | None = None,
        intensity: int | None = None,
    ) -> None:
        """Update analyzer settings without reset."""
        if color_mode is not None:
            self._color_mode = color_mode
        if brightness is not None:
            self._brightness = max(0, min(100, brightness)) / 100.0
        if intensity is not None:
            self._intensity = max(0, min(100, intensity)) / 100.0

    def process_frame(self, frame: VisualizerFrame) -> list[LightColorCommand]:
        """Convert a visualization frame to light color commands."""
        if self._color_mode == "bass_boost":
            return self._process_bass_boost(frame)
        if self._color_mode == "ambient":
            return self._process_ambient(frame)
        return self._process_spectrum(frame)

    # -- Effect modes --

    def _process_spectrum(self, frame: VisualizerFrame) -> list[LightColorCommand]:
        """
        Spectrum mode: energetic, beat-reactive, color-cycling.

        Each light maps to a frequency band. Colors cycle on beats.
        Bass drives brightness pulse. Beats trigger white flashes.
        """
        commands: list[LightColorCommand] = []
        num_channels = len(self._channels)
        if num_channels == 0:
            return commands

        spectrum = self._get_spectrum_energies(frame)
        bass_energy = self._get_bass_energy(spectrum)
        loudness = self._get_loudness(frame)
        beat = self._detect_beat(loudness)

        # Cycle colors on beat
        if beat:
            self._color_index = (self._color_index + 1) % len(_COLOR_PALETTE)
            self._beat_flash = 1.0  # white flash

        # Decay beat flash
        self._beat_flash *= 0.6

        # Filtered bass for brightness modulation
        filtered_bass = self._bass_filter.update(bass_energy)
        filtered_energy = self._energy_filter.update(loudness)

        # Overall brightness: base + bass pulse + intensity scaling
        base_brightness = 0.3 + 0.7 * filtered_energy
        brightness = min(1.0, base_brightness * self._brightness)

        num_bins = len(spectrum)
        for i, channel in enumerate(self._channels):
            # Map channel to spectrum bin
            bin_idx = min(i * num_bins // num_channels, num_bins - 1) if num_bins > 0 else 0
            energy = spectrum[bin_idx] if bin_idx < num_bins else 0.0

            # Get color from cycling palette — offset per channel for spread
            palette_idx = (self._color_index + i) % len(_COLOR_PALETTE)
            base_r, base_g, base_b = _COLOR_PALETTE[palette_idx]

            # Scale by energy and overall brightness
            scale = max(0.05, energy * brightness)
            r = base_r * scale
            g = base_g * scale
            b = base_b * scale

            # Add beat flash (white overlay)
            if self._beat_flash > 0.05:
                flash = self._beat_flash * self._intensity
                r = min(1.0, r + flash)
                g = min(1.0, g + flash)
                b = min(1.0, b + flash)

            # Add bass pulse (warm tint)
            if filtered_bass > 0.2:
                bass_tint = (filtered_bass - 0.2) * 0.5
                r = min(1.0, r + bass_tint * 0.3)

            r, g, b = self._smooth(i, r, g, b)
            commands.append(self._to_command(channel.channel_id, r, g, b))

        return commands

    def _process_bass_boost(self, frame: VisualizerFrame) -> list[LightColorCommand]:
        """
        Bass boost mode: all lights pulse with bass, flash on beats.

        Warm colors dominated by bass energy. Beats add cool flash.
        """
        commands: list[LightColorCommand] = []
        spectrum = self._get_spectrum_energies(frame)
        bass_energy = self._get_bass_energy(spectrum)
        loudness = self._get_loudness(frame)
        beat = self._detect_beat(loudness)

        if beat:
            self._beat_flash = 1.0
            self._color_index = (self._color_index + 1) % len(_COLOR_PALETTE)

        self._beat_flash *= 0.55

        filtered_bass = self._bass_filter.update(bass_energy)
        brightness = min(1.0, (0.2 + 0.8 * filtered_bass) * self._brightness)

        for i, channel in enumerate(self._channels):
            # Warm bass color
            r = filtered_bass * brightness
            g = filtered_bass * 0.3 * brightness
            b = filtered_bass * 0.05 * brightness

            # Beat flash — cycle through palette colors
            if self._beat_flash > 0.05:
                flash_color = _COLOR_PALETTE[self._color_index]
                flash = self._beat_flash * self._intensity
                r = min(1.0, r + flash_color[0] * flash)
                g = min(1.0, g + flash_color[1] * flash)
                b = min(1.0, b + flash_color[2] * flash)

            r, g, b = self._smooth(i, r, g, b)
            commands.append(self._to_command(channel.channel_id, r, g, b))

        return commands

    def _process_ambient(self, frame: VisualizerFrame) -> list[LightColorCommand]:
        """
        Ambient mode: slow, smooth color transitions. Relaxing.

        Slowly rotating hue, modulated gently by overall energy.
        """
        commands: list[LightColorCommand] = []
        loudness = self._get_loudness(frame)
        f_peak = frame.f_peak if frame.f_peak is not None else 0

        filtered_energy = self._energy_filter.update(loudness)

        # Slow hue rotation based on time + peak frequency influence
        self._base_hue_offset += 0.002
        hue = (self._base_hue_offset + f_peak / 20000.0) % 1.0

        # Gentle brightness from energy
        brightness = (0.3 + 0.4 * filtered_energy) * self._brightness

        r, g, b = _hue_to_rgb(hue)
        r *= brightness
        g *= brightness
        b *= brightness

        # Heavy smoothing for ambient feel
        smoothing = 0.92
        for i, channel in enumerate(self._channels):
            # Slight hue offset per channel for depth
            ch_hue = (hue + i * 0.05) % 1.0
            cr, cg, cb = _hue_to_rgb(ch_hue)
            cr *= brightness
            cg *= brightness
            cb *= brightness

            prev_r, prev_g, prev_b = self._smoothed[i]
            cr = prev_r * smoothing + cr * (1.0 - smoothing)
            cg = prev_g * smoothing + cg * (1.0 - smoothing)
            cb = prev_b * smoothing + cb * (1.0 - smoothing)
            self._smoothed[i] = (cr, cg, cb)
            commands.append(self._to_command(channel.channel_id, cr, cg, cb))

        return commands

    # -- Helpers --

    def _get_spectrum_energies(self, frame: VisualizerFrame) -> list[float]:
        """Extract normalized spectrum energies with perceptual boost."""
        if frame.spectrum is not None and len(frame.spectrum) > 0:
            max_val = 65535.0
            return [min(1.0, (float(v) / max_val) ** 0.5) for v in frame.spectrum]
        loudness = (frame.loudness or 0) / 65535.0
        return [min(1.0, loudness**0.4)]

    def _get_bass_energy(self, spectrum: list[float]) -> float:
        """Extract bass energy from the first ~25% of spectrum bins."""
        if not spectrum:
            return 0.0
        bass_bins = max(1, len(spectrum) // 4)
        return sum(spectrum[:bass_bins]) / bass_bins

    def _get_loudness(self, frame: VisualizerFrame) -> float:
        """Get perceptually-boosted loudness as 0-1."""
        if frame.loudness is not None:
            return float(min(1.0, (frame.loudness / 65535.0) ** 0.3))
        return 0.3

    def _detect_beat(self, loudness: float) -> bool:
        """Simple volume-spike beat detection."""
        now = time.monotonic()

        # Minimum interval between beats
        if now - self._last_beat_time < self._min_beat_interval:
            self._loudness_history.append(loudness)
            if len(self._loudness_history) > 20:
                self._loudness_history.pop(0)
            return False

        self._loudness_history.append(loudness)
        if len(self._loudness_history) > 20:
            self._loudness_history.pop(0)

        if len(self._loudness_history) < 5:
            return False

        avg = sum(self._loudness_history) / len(self._loudness_history)
        # Beat = current loudness significantly above recent average
        if avg > 0.01 and loudness > avg * 1.4 and loudness > 0.25:
            self._last_beat_time = now
            return True
        return False

    def _smooth(self, idx: int, r: float, g: float, b: float) -> tuple[float, float, float]:
        """Apply light smoothing — faster than before for responsiveness."""
        prev_r, prev_g, prev_b = self._smoothed[idx]
        s = 0.3  # 30% of previous, 70% new — responsive
        r_s = prev_r * s + r * (1.0 - s)
        g_s = prev_g * s + g * (1.0 - s)
        b_s = prev_b * s + b * (1.0 - s)
        self._smoothed[idx] = (r_s, g_s, b_s)
        return r_s, g_s, b_s

    @staticmethod
    def _to_command(channel_id: int, r: float, g: float, b: float) -> LightColorCommand:
        """Convert float RGB (0-1) to a 16-bit LightColorCommand."""
        return LightColorCommand(
            channel_id=channel_id,
            red=int(max(0.0, min(1.0, r)) * 65535),
            green=int(max(0.0, min(1.0, g)) * 65535),
            blue=int(max(0.0, min(1.0, b)) * 65535),
        )


def _hue_to_rgb(hue: float) -> tuple[float, float, float]:
    """Convert a hue value (0-1) to RGB."""
    h = hue * 6.0
    c = 1.0
    x = c * (1.0 - abs(math.fmod(h, 2.0) - 1.0))

    if h < 1:
        return (c, x, 0.0)
    if h < 2:
        return (x, c, 0.0)
    if h < 3:
        return (0.0, c, x)
    if h < 4:
        return (0.0, x, c)
    if h < 5:
        return (x, 0.0, c)
    return (c, 0.0, x)
