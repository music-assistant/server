"""Tests for HueDtlsStreamer (protocol message building)."""

from __future__ import annotations

import uuid

import pytest

from music_assistant.providers.hue_entertainment.hue_sendspin_bridge.constants import (
    COLOR_SPACE_RGB,
    HUESTREAM_HEADER,
    HUESTREAM_VERSION,
)
from music_assistant.providers.hue_entertainment.hue_sendspin_bridge.dtls import HueDtlsStreamer
from music_assistant.providers.hue_entertainment.hue_sendspin_bridge.models import LightColorCommand

# Header layout:
#   [0:9]   "HueStream"  (9 bytes)
#   [9:11]  version 2.0  (2 bytes)
#   [11]    sequence      (1 byte)
#   [12:14] reserved      (2 bytes)
#   [14]    color space   (1 byte)
#   [15]    reserved      (1 byte)
#   [16:52] area UUID     (36-byte ASCII with dashes)
# Total header = 52 bytes
#
# Per channel (7 bytes): channel_id(1) + R(1)R(1) + G(1)G(1) + B(1)B(1)
# Color bytes are 0-255, duplicated per Q42.HueApi convention.

HEADER_SIZE = 16
UUID_SIZE = 36
FULL_HEADER_SIZE = HEADER_SIZE + UUID_SIZE  # 52
CHANNEL_SIZE = 7


class TestHueStreamMessage:
    """Tests for HueStream v2.0 message building."""

    @pytest.fixture
    def streamer(self) -> HueDtlsStreamer:
        """Return a streamer with a known area UUID (36-byte ASCII)."""
        s = HueDtlsStreamer()
        area_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        s._area_uuid_bytes = str(area_uuid).encode("ascii")
        return s

    def test_message_header(self, streamer: HueDtlsStreamer) -> None:
        """Test that the message starts with the correct HueStream header."""
        msg = streamer._build_huestream_message([])
        assert msg[:9] == HUESTREAM_HEADER
        assert msg[9:11] == HUESTREAM_VERSION
        assert msg[14] == COLOR_SPACE_RGB

    def test_message_area_uuid(self, streamer: HueDtlsStreamer) -> None:
        """Test that the area UUID is correctly encoded as 36-byte ASCII."""
        msg = streamer._build_huestream_message([])
        expected = b"12345678-1234-5678-1234-567812345678"
        assert msg[16:52] == expected

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
        assert len(msg) == FULL_HEADER_SIZE

    def test_single_channel_message(self, streamer: HueDtlsStreamer) -> None:
        """Test message with a single light channel (duplicated-byte color format)."""
        # 65535 >> 8 = 255, 32768 >> 8 = 128, 0 stays 0
        cmd = LightColorCommand(channel_id=0, red=65535, green=32768, blue=0)
        msg = streamer._build_huestream_message([cmd])
        assert len(msg) == FULL_HEADER_SIZE + CHANNEL_SIZE

        channel_data = msg[FULL_HEADER_SIZE:]
        assert channel_data[0] == 0  # channel_id
        assert channel_data[1] == 255  # R
        assert channel_data[2] == 255  # R (duplicated)
        assert channel_data[3] == 128  # G
        assert channel_data[4] == 128  # G (duplicated)
        assert channel_data[5] == 0  # B
        assert channel_data[6] == 0  # B (duplicated)

    def test_multiple_channels_message(self, streamer: HueDtlsStreamer) -> None:
        """Test message with multiple light channels."""
        commands = [
            LightColorCommand(channel_id=0, red=65535, green=0, blue=0),
            LightColorCommand(channel_id=1, red=0, green=65535, blue=0),
            LightColorCommand(channel_id=2, red=0, green=0, blue=65535),
        ]
        msg = streamer._build_huestream_message(commands)
        assert len(msg) == FULL_HEADER_SIZE + 3 * CHANNEL_SIZE

        for i, cmd in enumerate(commands):
            offset = FULL_HEADER_SIZE + i * CHANNEL_SIZE
            assert msg[offset] == cmd.channel_id
            expected_r = min(255, cmd.red >> 8) if cmd.red > 255 else cmd.red
            expected_g = min(255, cmd.green >> 8) if cmd.green > 255 else cmd.green
            expected_b = min(255, cmd.blue >> 8) if cmd.blue > 255 else cmd.blue
            assert msg[offset + 1] == expected_r
            assert msg[offset + 2] == expected_r  # duplicated
            assert msg[offset + 3] == expected_g
            assert msg[offset + 4] == expected_g  # duplicated
            assert msg[offset + 5] == expected_b
            assert msg[offset + 6] == expected_b  # duplicated

    def test_channel_id_byte_mask(self, streamer: HueDtlsStreamer) -> None:
        """Test that channel ID is masked to single byte."""
        cmd = LightColorCommand(channel_id=256, red=0, green=0, blue=0)
        msg = streamer._build_huestream_message([cmd])
        assert msg[FULL_HEADER_SIZE] == 0  # 256 & 0xFF == 0

    def test_color_value_clamping(self, streamer: HueDtlsStreamer) -> None:
        """Test that color values >255 are right-shifted and clamped to 8-bit."""
        # 70000 >> 8 = 273, clamped to min(255, 273) = 255
        cmd = LightColorCommand(channel_id=1, red=70000, green=0, blue=0)
        msg = streamer._build_huestream_message([cmd])
        r = msg[FULL_HEADER_SIZE + 1]
        assert r == 255

    def test_small_color_values_not_shifted(self, streamer: HueDtlsStreamer) -> None:
        """Test that color values <=255 are used directly (not shifted)."""
        cmd = LightColorCommand(channel_id=0, red=100, green=200, blue=50)
        msg = streamer._build_huestream_message([cmd])
        assert msg[FULL_HEADER_SIZE + 1] == 100  # R
        assert msg[FULL_HEADER_SIZE + 3] == 200  # G
        assert msg[FULL_HEADER_SIZE + 5] == 50  # B


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
        streamer.send_colors([cmd])

    def test_send_queues_message_when_connected(self) -> None:
        """Test that send_colors queues messages when connected."""
        streamer = HueDtlsStreamer()
        streamer._connected = True
        streamer._area_uuid_bytes = str(uuid.UUID(int=0)).encode("ascii")
        cmd = LightColorCommand(channel_id=0, red=100, green=200, blue=300)
        streamer.send_colors([cmd])
        assert not streamer._send_queue.empty()
        msg = streamer._send_queue.get_nowait()
        assert isinstance(msg, bytes)
        assert msg[:9] == HUESTREAM_HEADER

    def test_disconnect_when_not_connected(self) -> None:
        """Test that disconnect is safe when not connected."""
        streamer = HueDtlsStreamer()
        streamer.disconnect()
