"""Tests for HueAudioAnalyzer (beat-driven palette cycling)."""

from __future__ import annotations

import colorsys

import pytest
from aiosendspin.models.visualizer import BeatTiming
from hue_entertainment import LightChannel, LightColorCommand

from music_assistant.providers.hue_entertainment.analyzer import (
    _NEUTRAL_GRADIENT,
    DEFAULT_MODE,
    HueAudioAnalyzer,
    _distinct_hue_count,
    _ScheduledBeat,
)


def _make_channels(count: int) -> list[LightChannel]:
    """Create a list of test light channels."""
    return [
        LightChannel(channel_id=i, service_id=f"svc_{i}", name=f"Light {i}") for i in range(count)
    ]


def _beat(ts_us: int, *, downbeat: bool = False) -> BeatTiming:
    """Shortcut to build a BeatTiming."""
    return BeatTiming(timestamp_us=ts_us, is_downbeat=downbeat)


class TestSettings:
    """Tests for the analyzer's settings handling."""

    def test_init_defaults(self) -> None:
        """Test analyzer initialization with default settings."""
        analyzer = HueAudioAnalyzer(_make_channels(3))
        assert analyzer._brightness == 1.0
        assert analyzer._color_mode == DEFAULT_MODE

    def test_brightness_clamping(self) -> None:
        """Test that brightness is clamped to 0-100."""
        assert HueAudioAnalyzer(_make_channels(1), brightness=150)._brightness == 1.0
        assert HueAudioAnalyzer(_make_channels(1), brightness=-10)._brightness == 0.0

    def test_update_settings(self) -> None:
        """Test live settings update."""
        analyzer = HueAudioAnalyzer(_make_channels(2))
        analyzer.update_settings(brightness=75)
        assert analyzer._brightness == pytest.approx(0.75)


class TestBeatScheduling:
    """Tests for beat queue management and per-bar palette indexing."""

    def test_push_beats_resolves_bar_positions(self) -> None:
        """The beat counter increments continuously across the schedule."""
        analyzer = HueAudioAnalyzer(_make_channels(1))
        analyzer.push_beats(
            [
                _beat(1_000_000, downbeat=True),
                _beat(1_500_000),
                _beat(2_000_000),
                _beat(2_500_000),
                _beat(3_000_000, downbeat=True),
                _beat(3_500_000),
            ]
        )
        positions = [b.beat_in_bar for b in analyzer._beats]
        assert positions == [0, 1, 2, 3, 4, 5]

    def test_clear_beats_resets_counter(self) -> None:
        """clear_beats also resets the per-bar counter for the next schedule."""
        analyzer = HueAudioAnalyzer(_make_channels(1))
        analyzer.push_beats([_beat(0), _beat(500_000), _beat(1_000_000)])
        analyzer.clear_beats()
        analyzer.push_beats([_beat(2_000_000)])
        assert analyzer._beats[0].beat_in_bar == 0

    def test_render_prunes_old_beats(self) -> None:
        """Past beats older than the most recent one are pruned during render."""
        analyzer = HueAudioAnalyzer(_make_channels(1))
        analyzer.push_beats([_beat(i * 1_000_000) for i in range(5)])
        analyzer.render(now_us=3_500_000)
        # Only the most recent past beat is kept ahead of the cursor.
        assert analyzer._beats[0].timestamp_us == 3_000_000


class TestRenderColors:
    """Tests for color rendering across the beat schedule."""

    def test_render_with_no_beats_falls_back_to_peak_walker(self) -> None:
        """No beats scheduled → renderer delegates to the peak-driven walker."""
        analyzer = HueAudioAnalyzer(_make_channels(2))
        commands = analyzer.render(now_us=0)
        assert len(commands) == 2
        assert _channel_sum(commands[0]) > 0

    def test_render_no_channels_returns_empty(self) -> None:
        """Empty channel list yields no commands."""
        analyzer = HueAudioAnalyzer([])
        analyzer.push_beats([_beat(0), _beat(500_000)])
        assert analyzer.render(now_us=250_000) == []

    def test_brightness_scales_output(self) -> None:
        """Halving brightness halves the rendered channel values."""
        full = HueAudioAnalyzer(_make_channels(1), brightness=100).render(now_us=0)[0]
        half = HueAudioAnalyzer(_make_channels(1), brightness=50).render(now_us=0)[0]
        assert half.green == pytest.approx(full.green // 2, abs=1)

    def test_pulse_boosts_above_steady_level(self) -> None:
        """In a pulsed mode, output on the beat exceeds the steady (off-beat) level."""
        analyzer = HueAudioAnalyzer(_make_channels(1), color_mode="flashing")
        analyzer.push_beats([_beat(0, downbeat=True), _beat(1_000_000), _beat(2_000_000)])
        on_beat = analyzer.render(now_us=1_000_000)[0]
        steady = analyzer.render(now_us=1_500_000)[0]
        assert _channel_sum(on_beat) > _channel_sum(steady)

    def test_pulse_higher_on_downbeat_than_regular(self) -> None:
        """Identical timing but a downbeat flag produces a stronger pulse."""

        def render_at_beat(*, downbeat: bool) -> LightColorCommand:
            analyzer = HueAudioAnalyzer(_make_channels(1), color_mode="flashing")
            analyzer.push_beats(
                [_beat(0, downbeat=True), _beat(500_000, downbeat=downbeat), _beat(1_000_000)]
            )
            return analyzer.render(now_us=500_000)[0]

        regular = render_at_beat(downbeat=False)
        downbeat = render_at_beat(downbeat=True)
        assert _channel_sum(downbeat) > _channel_sum(regular)


class TestPalette:
    """Tests for color@v1 palette selection."""

    def test_no_color_uses_neutral_fallback(self) -> None:
        """With no color@v1 update, the neutral tint gradient is used."""
        raw, synthesized = HueAudioAnalyzer(_make_channels(1))._gather_raw_palette()
        assert raw == _NEUTRAL_GRADIENT
        assert synthesized is True

    def test_two_distinct_hues_cycle_the_album_colors(self) -> None:
        """Two hue-distant colors are used as-is, not synthesized."""
        analyzer = HueAudioAnalyzer(_make_channels(1))
        analyzer.apply_color_palette({"primary": (200, 0, 0), "accent": (0, 0, 200)})
        raw, synthesized = analyzer._gather_raw_palette()
        assert synthesized is False
        assert _distinct_hue_count(raw) >= 2

    def test_single_hue_expands_into_same_family(self) -> None:
        """A single hue plus near-whites yields a gradient that stays in that hue."""
        analyzer = HueAudioAnalyzer(_make_channels(1))
        analyzer.apply_color_palette(
            {
                "primary": (20, 90, 30),
                "background_light": (235, 245, 238),
                "on_dark": (250, 250, 250),
            }
        )
        palette = analyzer._active_palette()
        # Every entry keeps green as its dominant channel (no wash to white).
        assert all(g >= r and g >= b for r, g, b in palette)

    def test_single_hue_palette_does_not_collapse(self) -> None:
        """The single-hue gradient keeps more than one distinct color."""
        analyzer = HueAudioAnalyzer(_make_channels(1))
        analyzer.apply_color_palette({"primary": (20, 90, 30), "on_dark": (250, 250, 250)})
        palette = analyzer._active_palette()
        assert len({tuple(round(c, 3) for c in entry) for entry in palette}) > 1

    def test_dark_saturated_color_is_not_achromatic(self) -> None:
        """A dark-but-colored seed (brown) keeps its hue instead of washing neutral."""
        analyzer = HueAudioAnalyzer(_make_channels(1))
        analyzer.apply_color_palette({"primary": (60, 40, 25), "on_dark": (250, 250, 250)})
        raw, synthesized = analyzer._gather_raw_palette()
        assert synthesized is True
        assert raw != _NEUTRAL_GRADIENT

    def test_achromatic_uses_neutral_gradient(self) -> None:
        """An all-grey palette produces the subtle neutral tint gradient."""
        analyzer = HueAudioAnalyzer(_make_channels(1))
        analyzer.apply_color_palette(
            {
                "primary": (128, 128, 128),
                "background_light": (240, 240, 240),
                "on_dark": (255, 255, 255),
            }
        )
        raw, synthesized = analyzer._gather_raw_palette()
        assert synthesized is True
        assert raw == _NEUTRAL_GRADIENT

    def test_undefined_palette_field_keeps_prior_value(self) -> None:
        """apply_color_palette merges, leaving fields not in the update untouched."""
        analyzer = HueAudioAnalyzer(_make_channels(1))
        analyzer.apply_color_palette({"primary": (10, 20, 30), "accent": (40, 50, 60)})
        analyzer.apply_color_palette({"primary": (100, 100, 100)})
        assert analyzer._server_palette["primary"] == (100, 100, 100)
        assert analyzer._server_palette["accent"] == (40, 50, 60)


class TestDistinctHueCount:
    """Tests for the hue-family grouping helper."""

    def test_shades_of_one_hue_count_as_one(self) -> None:
        """Dark and light shades of the same hue collapse to a single family."""
        dark = colorsys.hsv_to_rgb(0.39, 0.8, 0.3)
        light = colorsys.hsv_to_rgb(0.39, 0.5, 0.9)
        assert _distinct_hue_count([dark, light]) == 1

    def test_opposite_hues_count_separately(self) -> None:
        """Hues far apart on the wheel count as separate families."""
        red = colorsys.hsv_to_rgb(0.0, 0.9, 0.8)
        cyan = colorsys.hsv_to_rgb(0.5, 0.9, 0.8)
        assert _distinct_hue_count([red, cyan]) == 2


class TestSpectrumScheduling:
    """Tests for timestamped spectrum drain inside render()."""

    def test_spectrum_not_applied_before_timestamp(self) -> None:
        """A spectrum frame stays pending until render time reaches its ts."""
        analyzer = HueAudioAnalyzer(_make_channels(1))
        analyzer.apply_spectrum([60000] * 4, timestamp_us=1_000_000)
        analyzer.render(now_us=500_000)
        assert analyzer._spectrum == []

    def test_spectrum_applied_once_due(self) -> None:
        """Once now_us reaches the ts, the frame is promoted to active spectrum."""
        analyzer = HueAudioAnalyzer(_make_channels(1))
        analyzer.apply_spectrum([60000] * 4, timestamp_us=1_000_000)
        analyzer.render(now_us=1_000_000)
        assert len(analyzer._spectrum) == 4


class TestPeakWalkFallback:
    """Tests for the peak-driven palette walker used when no beat schedule exists."""

    def test_peaks_advance_palette_when_no_beats(self) -> None:
        """Each consumed peak bumps _peak_palette_position by mode.palette_advance."""
        analyzer = HueAudioAnalyzer(_make_channels(1), color_mode="ambient")
        step = analyzer._mode.palette_advance
        analyzer.apply_peak(strength=200, timestamp_us=1_000_000)
        analyzer.apply_peak(strength=200, timestamp_us=2_000_000)
        analyzer.render(now_us=2_500_000)
        assert analyzer._peak_palette_position == pytest.approx(step * 2)

    def test_pending_peak_does_not_advance_before_due(self) -> None:
        """Peaks scheduled in the future leave the walker untouched until promoted."""
        analyzer = HueAudioAnalyzer(_make_channels(1), color_mode="ambient")
        analyzer.apply_peak(strength=200, timestamp_us=5_000_000)
        analyzer.render(now_us=1_000_000)
        assert analyzer._peak_palette_position == 0.0

    def test_no_beats_no_peaks_holds_first_palette_slot(self) -> None:
        """Without any beats or peaks, the walker stays at slot 0."""
        analyzer = HueAudioAnalyzer(_make_channels(2))
        analyzer.render(now_us=0)
        assert analyzer._peak_palette_position == 0.0

    def test_clear_beats_resets_peak_walker(self) -> None:
        """clear_beats wipes the peak-driven position so the next track starts fresh."""
        analyzer = HueAudioAnalyzer(_make_channels(1), color_mode="ambient")
        analyzer.apply_peak(strength=255, timestamp_us=500_000)
        analyzer.render(now_us=500_000)
        assert analyzer._peak_palette_position > 0.0
        analyzer.clear_beats()
        assert analyzer._peak_palette_position == 0.0


class TestScheduledBeatModel:
    """Sanity tests for the internal scheduled-beat dataclass."""

    def test_scheduled_beat_is_frozen(self) -> None:
        """The dataclass should be frozen so the queue is immutable per-entry."""
        sb = _ScheduledBeat(timestamp_us=0, beat_in_bar=0, is_downbeat=True)
        with pytest.raises(AttributeError):
            sb.timestamp_us = 1  # type: ignore[misc]


def _channel_sum(command: LightColorCommand) -> int:
    """Total 16-bit energy across a command's RGB channels."""
    return int(command.red + command.green + command.blue)
