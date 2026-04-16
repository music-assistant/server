"""Tests for Hue Entertainment data models."""

from __future__ import annotations

from music_assistant.providers.hue_entertainment.hue_sendspin_bridge.models import (
    EntertainmentArea,
    LightChannel,
    LightColorCommand,
)


class TestLightColorCommand:
    """Tests for LightColorCommand."""

    def test_defaults(self) -> None:
        """Test default color values are zero."""
        cmd = LightColorCommand(channel_id=0)
        assert cmd.red == 0
        assert cmd.green == 0
        assert cmd.blue == 0

    def test_custom_values(self) -> None:
        """Test custom color values."""
        cmd = LightColorCommand(channel_id=5, red=65535, green=32768, blue=16384)
        assert cmd.channel_id == 5
        assert cmd.red == 65535
        assert cmd.green == 32768
        assert cmd.blue == 16384


class TestLightChannel:
    """Tests for LightChannel."""

    def test_defaults(self) -> None:
        """Test default position."""
        ch = LightChannel(channel_id=0, service_id="svc_1", name="Light 1")
        assert ch.position == (0.0, 0.0, 0.0)

    def test_custom_position(self) -> None:
        """Test custom position."""
        ch = LightChannel(
            channel_id=1,
            service_id="svc_2",
            name="Light 2",
            position=(-0.5, 0.8, 0.0),
        )
        assert ch.position == (-0.5, 0.8, 0.0)


class TestEntertainmentArea:
    """Tests for EntertainmentArea."""

    def test_defaults(self) -> None:
        """Test default channels list."""
        area = EntertainmentArea(id="area-1", name="Test Area")
        assert area.channels == []

    def test_with_channels(self) -> None:
        """Test area with channels."""
        channels = [
            LightChannel(channel_id=0, service_id="s1", name="L1"),
            LightChannel(channel_id=1, service_id="s2", name="L2"),
        ]
        area = EntertainmentArea(id="area-2", name="Room", channels=channels)
        assert len(area.channels) == 2
        assert area.channels[0].channel_id == 0
