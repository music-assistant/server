"""Tests for HueDtlsStreamer (protocol message building)."""

from __future__ import annotations

import struct
import uuid

import pytest

from music_assistant.providers.hue_entertainment.hue_sendspin_bridge.constants import (
    COLOR_SPACE_RGB,
    HUESTREAM_HEADER,
    HUESTREAM_VERSION,
)
from music_assistant.providers.hue_entertainment.hue_sendspin_bridge.dtls import HueDtlsStreamer
from music_assistant.providers.hue_entertainment.hue_sendspin_bridge.models import LightColorCommand


class TestHueStreamMessage:
    """Tests for HueStream v2.0 message building."""

    @pytest.fixture
    def streamer(self) -> HueDtlsStreamer:
        """Return a streamer with a known area UUID."""
        s = HueDtlsStreamer()
        s._area_uuid_bytes = uuid.UUID("12345678-1234-5678-1234-567812345678").bytes
        return s

    def test_message_header(self, streamer: HueDtlsStreamer) -> None:
        """Test that the message starts with the correct HueStream header."""
        msg = streamer._build_huestream_message([])
        assert msg[:9] == HUESTREAM_HEADER
        assert msg[9:11] == HUESTREAM_VERSION
        assert msg[14] == COLOR_SPACE_RGB

    def test_message_area_uuid(self, streamer: HueDtlsStreamer) -> None:
        """Test that the area UUID is correctly encoded."""
        msg = streamer._build_huestream_message([])
        expected_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678").bytes
        assert msg[16:32] == expected_uuid

    def test_message_sequence_increments(self, streamer: HueDtlsStreamer) -> None:
        """Test that sequence number increments per message."""
        msg1 = streamer._build_huestream_message([])
        msg2 = streamer._build_huestream_message([])
        assert msg1[11] == 0
        assert msg2[11] == 1

    def test_message_sequence_wraps(self, streamer: HueDtlsStreamer) -> None:
        """Test that sequence number wraps at 255."""
        streamer._sequence = 255
        msg = streamer._build_huestream_message([])
        assert msg[11] == 255
        msg2 = streamer._build_huestream_message([])
        assert msg2[11] == 0

    def test_empty_commands_header_only(self, streamer: HueDtlsStreamer) -> None:
        """Test that empty commands produce a header-only message."""
        msg = streamer._build_huestream_message([])
        # Header (16 bytes) + UUID (16 bytes) = 32 bytes
        assert len(msg) == 32

    def test_single_channel_message(self, streamer: HueDtlsStreamer) -> None:
        """Test message with a single light channel."""
        cmd = LightColorCommand(channel_id=0, red=65535, green=32768, blue=0)
        msg = streamer._build_huestream_message([cmd])
        # 32 header + 7 per channel = 39
        assert len(msg) == 39

        # Parse channel data
        channel_data = msg[32:]
        ch_id, r, g, b = struct.unpack(">BHHH", channel_data)
        assert ch_id == 0
        assert r == 65535
        assert g == 32768
        assert b == 0

    def test_multiple_channels_message(self, streamer: HueDtlsStreamer) -> None:
        """Test message with multiple light channels."""
        commands = [
            LightColorCommand(channel_id=0, red=65535, green=0, blue=0),
            LightColorCommand(channel_id=1, red=0, green=65535, blue=0),
            LightColorCommand(channel_id=2, red=0, green=0, blue=65535),
        ]
        msg = streamer._build_huestream_message(commands)
        # 32 header + 3 * 7 = 53
        assert len(msg) == 53

        # Verify each channel
        for i, cmd in enumerate(commands):
            offset = 32 + i * 7
            ch_id, r, g, b = struct.unpack(">BHHH", msg[offset : offset + 7])
            assert ch_id == cmd.channel_id
            assert r == cmd.red
            assert g == cmd.green
            assert b == cmd.blue

    def test_channel_id_byte_mask(self, streamer: HueDtlsStreamer) -> None:
        """Test that channel ID is masked to single byte."""
        cmd = LightColorCommand(channel_id=256, red=0, green=0, blue=0)
        msg = streamer._build_huestream_message([cmd])
        ch_id = msg[32]
        assert ch_id == 0  # 256 & 0xFF == 0

    def test_color_value_clamping(self, streamer: HueDtlsStreamer) -> None:
        """Test that color values are masked to 16 bits."""
        cmd = LightColorCommand(channel_id=1, red=70000, green=0, blue=0)
        msg = streamer._build_huestream_message([cmd])
        _, r, _, _ = struct.unpack(">BHHH", msg[32:39])
        assert r == 70000 & 0xFFFF


class TestHueDtlsStreamerState:
    """Tests for streamer state management."""

    def test_initial_state(self) -> None:
        """Test that streamer starts disconnected."""
        streamer = HueDtlsStreamer()
        assert not streamer.is_connected

    def test_send_when_disconnected_is_noop(self) -> None:
        """Test that send_colors does nothing when not connected."""
        streamer = HueDtlsStreamer()
        cmd = LightColorCommand(channel_id=0, red=65535, green=0, blue=0)
        # Should not raise
        streamer.send_colors([cmd])

    def test_send_queues_message_when_connected(self) -> None:
        """Test that send_colors queues messages when connected."""
        streamer = HueDtlsStreamer()
        streamer._connected = True
        streamer._area_uuid_bytes = b"\x00" * 16
        cmd = LightColorCommand(channel_id=0, red=100, green=200, blue=300)
        streamer.send_colors([cmd])
        assert not streamer._send_queue.empty()
        msg = streamer._send_queue.get_nowait()
        assert isinstance(msg, bytes)
        assert msg[:9] == HUESTREAM_HEADER

    def test_disconnect_when_not_connected(self) -> None:
        """Test that disconnect is safe when not connected."""
        streamer = HueDtlsStreamer()
        streamer.disconnect()  # Should not raise
