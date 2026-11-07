"""Tests for the Auracast serial HCI helper."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import serialx

from music_assistant.providers.auracast import serial_hci


def port_info(device: str, product: str, vid: int | None, pid: int | None) -> MagicMock:
    """Build a serialx port stub."""
    return MagicMock(
        device=device, resolved_device="/dev/ttyACM0", product=product, vid=vid, pid=pid
    )


def test_list_controller_ports_only_offers_supported_controllers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offer only allowlisted USB controllers and keep the configured port selectable."""
    monkeypatch.setattr(
        serialx,
        "list_serial_ports",
        lambda: [
            port_info("/dev/serial/by-id/usb-ctl", "Controller", 0x1915, 0x521F),
            port_info("/dev/ttyS0", "n/a", None, None),
            port_info("/dev/serial/by-id/usb-zigbee", "Zigbee stick", 0x10C4, 0xEA60),
            port_info("/dev/serial/by-id/usb-nordic", "Other Nordic", 0x1915, 0x0001),
        ],
    )

    assert serial_hci.list_controller_ports("/dev/missing", usb_ids={(0x1915, 0x521F)}) == {
        "/dev/serial/by-id/usb-ctl": "Controller (/dev/ttyACM0)",
        "/dev/missing": "/dev/missing",
    }


@pytest.mark.asyncio
async def test_open_hci_transport_opens_exclusively_and_asserts_dtr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open the port with exclusive access and wire the serial transport as source and sink."""
    serial_transport = MagicMock()
    serial_transport.set_modem_pins = AsyncMock()
    source = serial_hci.SerialHciSource()
    create = AsyncMock(return_value=(serial_transport, source))
    monkeypatch.setattr(serialx, "create_serial_connection", create)

    hci_transport = await serial_hci.open_hci_transport("/dev/test", 1_000_000)

    create.assert_awaited_once_with(
        asyncio.get_running_loop(),
        serial_hci.SerialHciSource,
        "/dev/test",
        baudrate=1_000_000,
        exclusive=True,
    )
    serial_transport.set_modem_pins.assert_awaited_once_with(dtr=True)
    assert hci_transport.source is source
    hci_transport.sink.on_packet(b"\x01")
    serial_transport.write.assert_called_once_with(b"\x01")
    await hci_transport.close()
    serial_transport.close.assert_called_once()


@pytest.mark.asyncio
async def test_open_hci_transport_tolerates_missing_dtr_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the transport usable when the port cannot drive modem pins."""
    serial_transport = MagicMock()
    serial_transport.set_modem_pins = AsyncMock(side_effect=serialx.UnsupportedSetting("dtr"))
    create: Any = AsyncMock(return_value=(serial_transport, serial_hci.SerialHciSource()))
    monkeypatch.setattr(serialx, "create_serial_connection", create)

    assert await serial_hci.open_hci_transport("/dev/test", 115200)


@pytest.mark.asyncio
async def test_open_hci_transport_closes_port_when_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release the port instead of leaking it when the controller cannot be configured."""
    serial_transport = MagicMock()
    serial_transport.set_modem_pins = AsyncMock(side_effect=serialx.SerialException("lost"))
    create: Any = AsyncMock(return_value=(serial_transport, serial_hci.SerialHciSource()))
    monkeypatch.setattr(serialx, "create_serial_connection", create)

    with pytest.raises(serialx.SerialException):
        await serial_hci.open_hci_transport("/dev/test", 115200)

    serial_transport.close.assert_called_once()


@pytest.mark.asyncio
async def test_source_reports_lost_connection() -> None:
    """Terminate the Bumble source when the serial connection drops."""
    source = serial_hci.SerialHciSource()

    source.connection_lost(None)

    assert source.terminated.done()
