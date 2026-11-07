"""
Serial port handling for the HCI controller used by the Auracast provider.

Enumerates supported controllers and opens them as a Bumble transport through serialx, so the
provider owns port selection, exclusive access and error handling.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import serialx
from bumble.transport.common import StreamPacketSink, StreamPacketSource, Transport

from .constants import SUPPORTED_USB_IDS

if TYPE_CHECKING:
    from collections.abc import Iterable


class SerialHciSource(StreamPacketSource):
    """Feed HCI packets read from a serial transport into Bumble."""

    def connection_lost(self, exc: Exception | None) -> None:
        """Report a lost serial connection to the Bumble host."""
        self.on_transport_lost()


def list_controller_ports(
    current_port: str = "", usb_ids: Iterable[tuple[int, int]] = SUPPORTED_USB_IDS
) -> dict[str, str]:
    """
    Return the supported controllers' serial ports, keyed by stable device path.

    Only USB devices whose vendor and product ID are in `usb_ids` are listed, so other serial
    devices (e.g. Zigbee or Z-Wave sticks) are never offered, nor opened. serialx reports the
    `/dev/serial/by-id` path where the platform provides one, as it survives re-plugging.

    :param current_port: The port of an already configured controller, always included.
    :param usb_ids: The (vendor ID, product ID) pairs of the supported controllers.
    """
    allowed = frozenset(usb_ids)
    ports = {
        port.device: f"{port.product or port.description} ({port.resolved_device})"
        for port in sorted(serialx.list_serial_ports(), key=lambda port: port.device)
        if (port.vid, port.pid) in allowed
    }
    if current_port and current_port not in ports:
        ports[current_port] = current_port
    return ports


async def open_hci_transport(port: str, baudrate: int) -> Transport:
    """
    Open a serial port as a Bumble HCI transport with exclusive access.

    Raises `serialx.SerialException` or `OSError` when the port cannot be opened, for example
    because another process holds it.

    :param port: Path to the controller's UART device.
    :param baudrate: UART baud rate.
    """
    serial_transport, source = await serialx.create_serial_connection(
        asyncio.get_running_loop(),
        SerialHciSource,
        port,
        baudrate=baudrate,
        exclusive=True,
    )
    try:
        # Zephyr's HCI UART sample, like Bumble's own serial transport, expects DTR asserted
        await serial_transport.set_modem_pins(dtr=True)
    except serialx.UnsupportedSetting:
        pass
    except BaseException:
        serial_transport.close()
        raise
    return Transport(cast("SerialHciSource", source), StreamPacketSink(serial_transport))
