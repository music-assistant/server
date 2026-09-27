"""Helper to wake a sleeping device on the local network via Wake-on-LAN."""

from __future__ import annotations

import asyncio
import socket

from music_assistant.helpers.util import is_valid_mac_address

_SYNC_STREAM = b"\xff" * 6
_MAC_REPEAT_COUNT = 16
DEFAULT_WOL_PORT = 9
DEFAULT_SEND_COUNT = 3


async def send_magic_packet(
    mac_address: str,
    broadcast_ip: str = "255.255.255.255",
    port: int = DEFAULT_WOL_PORT,
    repeat: int = DEFAULT_SEND_COUNT,
) -> None:
    """
    Wake a device on the local network with a Wake-on-LAN magic packet.

    :param mac_address: MAC address of the device to wake, colon- or dash-separated.
    :param broadcast_ip: Broadcast address to send the packet to.
    :param port: UDP port to send the packet to.
    :param repeat: Number of times to send the packet, guarding against a dropped frame.
    """
    if not is_valid_mac_address(mac_address):
        msg = f"Invalid MAC address: {mac_address}"
        raise ValueError(msg)
    mac_bytes = bytes.fromhex(mac_address.replace(":", "").replace("-", ""))
    packet = _SYNC_STREAM + mac_bytes * _MAC_REPEAT_COUNT
    await asyncio.to_thread(_send_magic_packet, packet, broadcast_ip, port, repeat)


def _send_magic_packet(packet: bytes, broadcast_ip: str, port: int, repeat: int) -> None:
    """Send the magic packet as a UDP broadcast (blocking)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(repeat):
            sock.sendto(packet, (broadcast_ip, port))
