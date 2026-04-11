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

        # Filters tuned for ~10Hz frame rate (100ms between frames)
        self._bass_filter = _ExpFilter(alpha_rise=0.95, alpha_decay=0.3)
        self._energy_filter = _ExpFilter(alpha_rise=0.9, alpha_decay=0.4)
        self._beat_flash = 0.0  # decays each frame

        # Beat detection — tuned for 10Hz: lower thresholds, shorter history
        self._loudness_history: list[float] = []
        self._last_beat_time = 0.0
        self._min_beat_interval = 0.2  # 300 BPM max at 10Hz
        self._prev_loudness = 0.0  # for delta-based detection

        # Color cycling + channel rotation
        self._color_index = 0
        self._channel_offset = 0  # rotates which light gets which color
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
        # Use bass energy for beat detection — aligns with drum hits,
        # not vocals or general loudness
        beat = self._detect_beat(bass_energy)

        # Color cycling speed depends on energy — faster when music is intense
        self._base_hue_offset += 0.003 + 0.01 * loudness

        # High energy peak → full white strobe flash
        if loudness > 0.7 and beat:
            self._beat_flash = 1.5  # extra bright white strobe
            self._color_index = (self._color_index + 2) % len(_COLOR_PALETTE)
            self._channel_offset = (self._channel_offset + 1) % max(1, num_channels)
        elif beat:
            self._beat_flash = max(self._beat_flash, 0.8)
            self._color_index = (self._color_index + 1) % len(_COLOR_PALETTE)
            self._channel_offset = (self._channel_offset + 1) % max(1, num_channels)

        # Decay beat flash
        self._beat_flash *= 0.4

        # Filtered bass for brightness modulation
        filtered_bass = self._bass_filter.update(bass_energy)
        filtered_energy = self._energy_filter.update(loudness)

        # Low floor so quiet parts are dim — makes beats pop
        base_brightness = 0.05 + 0.95 * filtered_energy
        brightness = min(1.0, base_brightness * self._brightness)

        num_bins = len(spectrum)
        for i, channel in enumerate(self._channels):
            # Map channel to spectrum bin
            bin_idx = min(i * num_bins // num_channels, num_bins - 1) if num_bins > 0 else 0
            energy = spectrum[bin_idx] if bin_idx < num_bins else 0.0

            # Get color — rotate channel assignment on beats so all lights change
            palette_idx = (self._color_index + i + self._channel_offset) % len(_COLOR_PALETTE)
            base_r, base_g, base_b = _COLOR_PALETTE[palette_idx]

            # Scale by energy and overall brightness
            scale = max(0.05, energy * brightness)
            r = base_r * scale
            g = base_g * scale
            b = base_b * scale

            # Beat flash — white overlay, overrides color on strong hits
            if self._beat_flash > 0.05:
                flash = min(1.0, self._beat_flash * self._intensity)
                # Blend toward white: the stronger the flash, the more white
                r = r * (1.0 - flash) + flash
                g = g * (1.0 - flash) + flash
                b = b * (1.0 - flash) + flash

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
        beat = self._detect_beat(bass_energy)

        if loudness > 0.7 and beat:
            self._beat_flash = 1.5  # white strobe on big hits
            self._color_index = (self._color_index + 2) % len(_COLOR_PALETTE)
        elif beat:
            self._beat_flash = max(self._beat_flash, 0.8)
            self._color_index = (self._color_index + 1) % len(_COLOR_PALETTE)

        self._beat_flash *= 0.35

        filtered_bass = self._bass_filter.update(bass_energy)
        brightness = min(1.0, (0.05 + 0.95 * filtered_bass) * self._brightness)

        for i, channel in enumerate(self._channels):
            # Warm bass color
            r = filtered_bass * brightness
            g = filtered_bass * 0.3 * brightness
            b = filtered_bass * 0.05 * brightness

            # Beat flash — white strobe on peaks, palette color on normal beats
            if self._beat_flash > 0.05:
                flash = min(1.0, self._beat_flash * self._intensity)
                if self._beat_flash > 1.0:
                    # High energy peak — white strobe
                    r = r * (1.0 - flash) + flash
                    g = g * (1.0 - flash) + flash
                    b = b * (1.0 - flash) + flash
                else:
                    # Normal beat — palette color flash
                    flash_color = _COLOR_PALETTE[self._color_index]
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
        """Beat detection tuned for 10Hz frame rate.

        Uses both frame-to-frame delta AND average comparison for
        reliable detection at low sample rates.
        """
        now = time.monotonic()
        delta = loudness - self._prev_loudness
        self._prev_loudness = loudness

        # Minimum interval between beats
        if now - self._last_beat_time < self._min_beat_interval:
            self._loudness_history.append(loudness)
            if len(self._loudness_history) > 10:
                self._loudness_history.pop(0)
            return False

        self._loudness_history.append(loudness)
        if len(self._loudness_history) > 10:
            self._loudness_history.pop(0)

        if len(self._loudness_history) < 3:
            return False

        avg = sum(self._loudness_history) / len(self._loudness_history)

        # Beat detected if:
        # - Positive delta (sudden loudness increase), OR
        # - Current loudness above recent average
        is_beat = (delta > 0.08 and loudness > 0.15) or (
            avg > 0.01 and loudness > avg * 1.15 and loudness > 0.15
        )
        if is_beat:
            self._last_beat_time = now
            return True
        return False

    def _smooth(self, idx: int, r: float, g: float, b: float) -> tuple[float, float, float]:
        """Apply minimal smoothing — at 10Hz each frame counts."""
        prev_r, prev_g, prev_b = self._smoothed[idx]
        s = 0.15  # 15% of previous, 85% new — very responsive at 10Hz
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
