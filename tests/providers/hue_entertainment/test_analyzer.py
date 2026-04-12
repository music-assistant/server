"""Tests for HueAudioAnalyzer."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from music_assistant.providers.hue_entertainment.hue_sendspin_bridge.analyzer import (
    HueAudioAnalyzer,
    _hue_to_rgb,
)
from music_assistant.providers.hue_entertainment.hue_sendspin_bridge.models import (
    LightChannel,
    LightColorCommand,
)


def _make_channels(count: int) -> list[LightChannel]:
    """Create a list of test light channels."""
    return [
        LightChannel(channel_id=i, service_id=f"svc_{i}", name=f"Light {i}") for i in range(count)
    ]


def _make_frame(
    loudness: int = 32000,
    f_peak: int = 1000,
    spectrum: list[int] | None = None,
) -> MagicMock:
    """Create a mock ExtractedFrame."""
    frame = MagicMock()
    frame.loudness = loudness
    frame.f_peak = f_peak
    frame.spectrum = np.array(spectrum, dtype=np.uint16) if spectrum else None
    return frame


class TestHueAudioAnalyzer:
    """Tests for the HueAudioAnalyzer class."""

    def test_init_defaults(self) -> None:
        """Test analyzer initialization with default settings."""
        channels = _make_channels(3)
        analyzer = HueAudioAnalyzer(channels)
        assert len(analyzer._smoothed) == 3
        assert analyzer._color_mode == "spectrum"
        assert analyzer._brightness == 1.0

    def test_init_custom_settings(self) -> None:
        """Test analyzer initialization with custom settings."""
        channels = _make_channels(2)
        analyzer = HueAudioAnalyzer(channels, color_mode="bass_boost", brightness=50, intensity=90)
        assert analyzer._color_mode == "bass_boost"
        assert analyzer._brightness == pytest.approx(0.5)

    def test_brightness_clamping(self) -> None:
        """Test that brightness is clamped to 0-100."""
        channels = _make_channels(1)
        analyzer = HueAudioAnalyzer(channels, brightness=150)
        assert analyzer._brightness == 1.0
        analyzer2 = HueAudioAnalyzer(channels, brightness=-10)
        assert analyzer2._brightness == 0.0

    def test_update_settings(self) -> None:
        """Test live settings update."""
        channels = _make_channels(2)
        analyzer = HueAudioAnalyzer(channels)
        analyzer.update_settings(color_mode="ambient", brightness=75, intensity=50)
        assert analyzer._color_mode == "ambient"
        assert analyzer._brightness == pytest.approx(0.75)

    def test_update_settings_partial(self) -> None:
        """Test partial settings update."""
        channels = _make_channels(1)
        analyzer = HueAudioAnalyzer(channels, color_mode="spectrum", brightness=80)
        analyzer.update_settings(brightness=60)
        assert analyzer._color_mode == "spectrum"
        assert analyzer._brightness == pytest.approx(0.6)

    def test_process_spectrum_mode(self) -> None:
        """Test spectrum mode produces correct number of commands."""
        channels = _make_channels(4)
        analyzer = HueAudioAnalyzer(channels, color_mode="spectrum")
        frame = _make_frame(loudness=50000, spectrum=[60000, 40000, 20000, 10000])
        commands = analyzer.process_frame(frame)
        assert len(commands) == 4
        assert all(isinstance(c, LightColorCommand) for c in commands)

    def test_process_bass_boost_mode(self) -> None:
        """Test bass boost mode produces commands for all channels."""
        channels = _make_channels(3)
        analyzer = HueAudioAnalyzer(channels, color_mode="bass_boost")
        frame = _make_frame(loudness=40000, spectrum=[65000, 50000, 10000, 5000])
        commands = analyzer.process_frame(frame)
        assert len(commands) == 3

    def test_process_ambient_mode(self) -> None:
        """Test ambient mode produces smooth output."""
        channels = _make_channels(2)
        analyzer = HueAudioAnalyzer(channels, color_mode="ambient")
        frame = _make_frame(loudness=30000, f_peak=500)
        commands = analyzer.process_frame(frame)
        assert len(commands) == 2

    def test_zero_loudness_produces_dim_output(self) -> None:
        """Test that zero loudness produces near-zero color values."""
        channels = _make_channels(1)
        analyzer = HueAudioAnalyzer(channels, color_mode="spectrum")
        frame = _make_frame(loudness=0, spectrum=[0])
        commands = analyzer.process_frame(frame)
        assert commands[0].red < 5000
        assert commands[0].green < 5000
        assert commands[0].blue < 5000

    def test_max_loudness_produces_bright_output(self) -> None:
        """Test that max loudness with high spectrum produces bright output."""
        channels = _make_channels(1)
        # Use intensity=100 and no smoothing interference
        analyzer = HueAudioAnalyzer(channels, color_mode="spectrum", intensity=100)
        frame = _make_frame(loudness=65535, spectrum=[65535])
        commands = analyzer.process_frame(frame)
        # At least one color channel should be significant
        assert max(commands[0].red, commands[0].green, commands[0].blue) > 10000

    def test_color_values_in_range(self) -> None:
        """Test that all color values are within 0-65535."""
        channels = _make_channels(6)
        analyzer = HueAudioAnalyzer(channels, color_mode="spectrum", intensity=100)
        frame = _make_frame(
            loudness=65535,
            spectrum=[65535, 65535, 65535, 65535, 65535, 65535, 65535, 65535],
        )
        commands = analyzer.process_frame(frame)
        for cmd in commands:
            assert 0 <= cmd.red <= 65535
            assert 0 <= cmd.green <= 65535
            assert 0 <= cmd.blue <= 65535

    def test_no_channels_returns_empty(self) -> None:
        """Test that analyzer with no channels returns empty commands."""
        analyzer = HueAudioAnalyzer([], color_mode="spectrum")
        frame = _make_frame()
        commands = analyzer.process_frame(frame)
        assert commands == []

    def test_smoothing_dampens_changes(self) -> None:
        """Test that smoothing makes output change gradually."""
        channels = _make_channels(1)
        analyzer = HueAudioAnalyzer(channels, color_mode="spectrum", intensity=30)

        # First frame: high energy
        frame1 = _make_frame(loudness=65535, spectrum=[65535])
        analyzer.process_frame(frame1)

        # Second frame: zero energy
        frame2 = _make_frame(loudness=0, spectrum=[0])
        cmd2 = analyzer.process_frame(frame2)

        # Due to smoothing, cmd2 should not be zero yet
        total = cmd2[0].red + cmd2[0].green + cmd2[0].blue
        assert total > 0, "Smoothing should prevent instant drop to zero"

    def test_fallback_when_no_spectrum(self) -> None:
        """Test that analyzer works with loudness only (no spectrum)."""
        channels = _make_channels(2)
        analyzer = HueAudioAnalyzer(channels, color_mode="spectrum")
        frame = _make_frame(loudness=40000, spectrum=None)
        commands = analyzer.process_frame(frame)
        assert len(commands) == 2


class TestHueToRgb:
    """Tests for the _hue_to_rgb helper function."""

    def test_hue_zero_is_red(self) -> None:
        """Test that hue=0 produces red."""
        r, _g, b = _hue_to_rgb(0.0)
        assert r == pytest.approx(1.0)
        assert b == pytest.approx(0.0)

    def test_hue_third_is_green(self) -> None:
        """Test that hue=0.33 produces green."""
        _r, g, _b = _hue_to_rgb(1.0 / 3.0)
        assert g == pytest.approx(1.0)

    def test_hue_twothirds_is_blue(self) -> None:
        """Test that hue=0.67 produces blue."""
        _r, _g, b = _hue_to_rgb(2.0 / 3.0)
        assert b == pytest.approx(1.0)

    @pytest.mark.parametrize("hue", [0.0, 0.1, 0.25, 0.5, 0.75, 0.99])
    def test_all_hues_in_range(self, hue: float) -> None:
        """Test that all hues produce values in [0, 1]."""
        r, g, b = _hue_to_rgb(hue)
        assert 0.0 <= r <= 1.0
        assert 0.0 <= g <= 1.0
        assert 0.0 <= b <= 1.0
