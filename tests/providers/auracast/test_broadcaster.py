"""Tests for Auracast broadcaster transport and PCM handling."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from bumble import core as bumble_core
from bumble import device as bumble_device
from bumble import hci as bumble_hci
from bumble import transport as bumble_transport
from lc3 import Encoder

from music_assistant.providers.auracast.broadcaster import (
    AuracastBroadcaster,
    BroadcastConfig,
    parse_manufacturer_data,
    probe_controller,
)


class FakeEncoder:
    """Encode marker bytes while recording the PCM input size."""

    def __init__(self) -> None:
        """Initialize the encoder call log."""
        self.calls: list[tuple[bytes, int, int]] = []

    def encode(self, pcm: bytes, num_bytes: int, bit_depth: int) -> bytes:
        """Return the requested number of marker bytes."""
        self.calls.append((pcm, num_bytes, bit_depth))
        return bytes(range(num_bytes))


class FakeIsoStream:
    """Collect packets submitted to one BIS."""

    def __init__(self) -> None:
        """Initialize the packet log."""
        self.packets: list[bytes] = []
        self.data_packet_queue = MagicMock(pending=0)

    async def write(self, packet: bytes) -> None:
        """Record one ISO SDU."""
        self.packets.append(packet)


def create_bluetooth_device(
    missing_feature: bumble_hci.LeFeatureMask | None = None,
    missing_command: int | None = None,
) -> MagicMock:
    """Build a Bumble device stub supporting everything except the given capabilities."""
    bluetooth_device = MagicMock()
    bluetooth_device.supports_le_features.side_effect = lambda feature: feature != missing_feature
    bluetooth_device.host.supports_command.side_effect = lambda op_code: op_code != missing_command
    bluetooth_device.power_on = AsyncMock()
    bluetooth_device.power_off = AsyncMock()
    bluetooth_device.host.local_version = None
    return bluetooth_device


@pytest.mark.asyncio
async def test_pcm_is_accumulated_encoded_and_split_per_bis() -> None:
    """Buffer partial PCM frames and split encoded output across BIS streams."""
    broadcaster = AuracastBroadcaster(
        BroadcastConfig(name="Test", transport_spec="serial:/dev/test,1000000")
    )
    encoder = FakeEncoder()
    streams = [FakeIsoStream(), FakeIsoStream()]
    broadcaster_stub = cast("Any", broadcaster)
    broadcaster_stub._encoder = encoder
    broadcaster_stub._big = MagicMock()
    broadcaster_stub._streams = streams
    broadcaster_stub._frame_bytes = 200
    broadcaster_stub._bytes_per_bis = 100
    broadcaster_stub._samples_per_frame = 480

    pcm = bytes(480 * 2 * 2)
    await broadcaster.send_pcm(pcm[:1000])
    assert encoder.calls == []
    await broadcaster.send_pcm(pcm[1000:])

    assert encoder.calls == [(pcm, 200, 16)]
    assert streams[0].packets == [bytes(range(100))]
    assert streams[1].packets == [bytes(range(100, 200))]


def test_liblc3_encodes_stereo_frame_from_source_built_extension() -> None:
    """Encode one stereo frame using the native liblc3 extension."""
    encoder = Encoder(frame_duration_us=10_000, sample_rate_hz=48_000, num_channels=2)
    frame_bytes = encoder.get_frame_bytes(160_000)

    encoded = encoder.encode(bytes(480 * 2 * 2), num_bytes=frame_bytes, bit_depth=16)

    assert frame_bytes == 200
    assert len(encoded) == 200


def test_manufacturer_data_parses_decimal_and_hex_company_ids() -> None:
    """Parse the reference format, including multiple optional entries."""
    assert parse_manufacturer_data(None) is None
    assert parse_manufacturer_data("") is None
    assert parse_manufacturer_data("1234:abcd;0x59:0102") == {
        1234: bytes.fromhex("abcd"),
        0x59: bytes.fromhex("0102"),
    }


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("not-a-company:abcd", "Invalid manufacturer company ID"),
        ("65536:abcd", "company ID must be between"),
        ("87:xyz", "valid hexadecimal data"),
        ("87:abc", "valid hexadecimal data"),
        ("87:", "COMPANY_ID:HEX_DATA"),
        ("87:01;", "empty entry"),
    ],
)
def test_manufacturer_data_rejects_invalid_entries(value: str, error: str) -> None:
    """Reject invalid company identifiers and payloads with a useful error."""
    with pytest.raises(ValueError, match=error):
        parse_manufacturer_data(value)


@pytest.mark.asyncio
async def test_start_uses_serial_transport_and_stop_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open the requested UART transport and close it when stopped."""
    hci_source = object()
    hci_sink = object()
    hci_transport = MagicMock()
    hci_transport.__iter__.return_value = (hci_source, hci_sink)
    hci_transport.close = AsyncMock()
    bluetooth_device = create_bluetooth_device()
    bluetooth_device.host.local_version = (
        bumble_hci.HCI_Read_Local_Version_Information_ReturnParameters(
            status=bumble_hci.HCI_ErrorCode.SUCCESS,
            hci_version=bumble_hci.HCI_VERSION_BLUETOOTH_CORE_5_2,
            hci_subversion=0x1234,
            lmp_version=bumble_hci.HCI_VERSION_BLUETOOTH_CORE_5_2,
            company_identifier=87,
            lmp_subversion=0x5678,
        )
    )

    monkeypatch.setattr(bumble_transport, "open_transport", AsyncMock(return_value=hci_transport))
    monkeypatch.setattr(
        bumble_device.Device,
        "from_config_with_hci",
        MagicMock(return_value=bluetooth_device),
    )
    monkeypatch.setattr(AuracastBroadcaster, "_create_broadcast", AsyncMock())
    broadcaster = AuracastBroadcaster(
        BroadcastConfig(name="Test", transport_spec="serial:/dev/cu.test,1000000")
    )

    await broadcaster.start()
    await broadcaster.stop()

    cast("Any", bumble_transport.open_transport).assert_awaited_once_with(
        "serial:/dev/cu.test,1000000"
    )
    bluetooth_device.power_on.assert_awaited_once()
    assert broadcaster.controller_stack == (
        "Bluetooth Core 5.2; company ID 0x0057; HCI revision 0x1234; "
        "LMP version 11; firmware revision 0x5678"
    )
    bluetooth_device.power_off.assert_awaited_once()
    hci_transport.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("missing_feature", "missing_command", "expected"),
    [
        (bumble_hci.LeFeatureMask.LE_PERIODIC_ADVERTISING, None, "LE Periodic Advertising"),
        (bumble_hci.LeFeatureMask.ISOCHRONOUS_BROADCASTER, None, "Isochronous Broadcaster"),
        (None, bumble_hci.HCI_LE_CREATE_BIG_COMMAND, "LE Create BIG"),
        (None, bumble_hci.HCI_LE_SETUP_ISO_DATA_PATH_COMMAND, "LE Setup ISO Data Path"),
    ],
)
async def test_start_rejects_controller_without_broadcast_support(
    monkeypatch: pytest.MonkeyPatch,
    missing_feature: bumble_hci.LeFeatureMask | None,
    missing_command: int | None,
    expected: str,
) -> None:
    """Reject controllers that cannot broadcast and release their opened transport."""
    hci_transport = MagicMock()
    hci_transport.__iter__.return_value = (object(), object())
    hci_transport.close = AsyncMock()
    bluetooth_device = create_bluetooth_device(missing_feature, missing_command)
    monkeypatch.setattr(bumble_transport, "open_transport", AsyncMock(return_value=hci_transport))
    monkeypatch.setattr(
        bumble_device.Device,
        "from_config_with_hci",
        MagicMock(return_value=bluetooth_device),
    )
    broadcaster = AuracastBroadcaster(
        BroadcastConfig(name="Test", transport_spec="serial:/dev/cu.test,1000000")
    )

    with pytest.raises(bumble_core.NotSupportedError, match=expected):
        await broadcaster.start()

    bluetooth_device.power_off.assert_awaited_once()
    hci_transport.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [True, False])
async def test_controller_probe_returns_support_and_releases_uart(
    monkeypatch: pytest.MonkeyPatch, supported: bool
) -> None:
    """Probe broadcast support and release the serial transport."""
    hci_transport = MagicMock()
    hci_transport.__iter__.return_value = (object(), object())
    hci_transport.close = AsyncMock()
    bluetooth_device = create_bluetooth_device(
        None if supported else bumble_hci.LeFeatureMask.ISOCHRONOUS_BROADCASTER
    )
    monkeypatch.setattr(bumble_transport, "open_transport", AsyncMock(return_value=hci_transport))
    monkeypatch.setattr(
        bumble_device.Device,
        "from_config_with_hci",
        MagicMock(return_value=bluetooth_device),
    )

    result = await probe_controller("/dev/cu.test", 1_000_000)
    assert result.supports_auracast is supported
    assert result.missing_support == (() if supported else ("Isochronous Broadcaster",))

    bluetooth_device.power_on.assert_awaited_once()
    bluetooth_device.power_off.assert_awaited_once()
    hci_transport.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_controller_probe_propagates_open_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report a serial port that cannot be opened instead of treating it as incompatible."""
    monkeypatch.setattr(
        bumble_transport, "open_transport", AsyncMock(side_effect=OSError("port busy"))
    )

    with pytest.raises(OSError, match="port busy"):
        await probe_controller("/dev/cu.test", 1_000_000)


@pytest.mark.asyncio
async def test_drain_pads_partial_frame_and_waits_for_transmission() -> None:
    """Flush the buffered tail as a silent-padded frame before waiting for the queue."""
    broadcaster = AuracastBroadcaster(
        BroadcastConfig(name="Test", transport_spec="serial:/dev/test,1000000")
    )
    encoder = FakeEncoder()
    streams = [FakeIsoStream(), FakeIsoStream()]
    link = MagicMock()
    link.drain = AsyncMock()
    big = MagicMock()
    big.bis_links = [link]
    broadcaster_stub = cast("Any", broadcaster)
    broadcaster_stub._encoder = encoder
    broadcaster_stub._big = big
    broadcaster_stub._streams = streams
    broadcaster_stub._frame_bytes = 200
    broadcaster_stub._bytes_per_bis = 100
    broadcaster_stub._samples_per_frame = 480

    await broadcaster.send_pcm(b"\x01" * 100)
    await broadcaster.drain()

    assert encoder.calls == [(b"\x01" * 100 + bytes(480 * 2 * 2 - 100), 200, 16)]
    link.drain.assert_awaited_once()


def test_queued_seconds_reports_pending_frames() -> None:
    """Convert pending SDUs across all BIS streams into seconds of queued audio."""
    broadcaster = AuracastBroadcaster(
        BroadcastConfig(name="Test", transport_spec="serial:/dev/test,1000000")
    )
    streams = [FakeIsoStream(), FakeIsoStream()]
    streams[0].data_packet_queue.pending = 128
    cast("Any", broadcaster)._streams = streams

    assert broadcaster.queued_seconds == pytest.approx(0.64)


@pytest.mark.asyncio
async def test_manufacturer_data_is_added_to_extended_advertising() -> None:
    """Add company ID and payload bytes to the advertising data structure."""
    payload = bytes.fromhex("00000000000000000000000000000000dffd")
    broadcaster = AuracastBroadcaster(
        BroadcastConfig(
            name="Test",
            transport_spec="serial:/dev/test,1000000",
            manufacturer_data={87: payload},
        )
    )
    advertising_set = MagicMock()
    advertising_set.start_periodic = AsyncMock()
    big = MagicMock()
    big.bis_links = []
    bluetooth_device = MagicMock()
    bluetooth_device.create_advertising_set = AsyncMock(return_value=advertising_set)
    bluetooth_device.create_big = AsyncMock(return_value=big)
    broadcaster._device = bluetooth_device
    broadcaster._bytes_per_bis = 100

    await broadcaster._create_broadcast()

    data = bluetooth_device.create_advertising_set.await_args.kwargs["advertising_data"]
    manufacturer_structure = bytes([3 + len(payload), 0xFF, 0x57, 0x00]) + payload
    assert manufacturer_structure in data
