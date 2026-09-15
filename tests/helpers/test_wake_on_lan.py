"""Tests for the Wake-on-LAN helper."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from music_assistant.helpers.wake_on_lan import send_magic_packet


def _mock_broadcast_socket() -> tuple[MagicMock, MagicMock]:
    """Return a (context manager patch target, socket instance) pair for socket.socket."""
    sock = MagicMock()
    socket_cls = MagicMock()
    socket_cls.return_value.__enter__.return_value = sock
    return socket_cls, sock


@pytest.mark.asyncio
@pytest.mark.parametrize("mac_address", ["AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff"])
async def test_magic_packet_layout(mac_address: str) -> None:
    """Test the packet is 102 bytes: 6x0xFF followed by the MAC repeated 16 times."""
    socket_cls, sock = _mock_broadcast_socket()
    with patch("music_assistant.helpers.wake_on_lan.socket.socket", socket_cls):
        await send_magic_packet(mac_address)

    packet, _address = sock.sendto.call_args[0]
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("aabbccddeeff") * 16


@pytest.mark.asyncio
async def test_magic_packet_is_broadcast_to_the_given_address_and_port() -> None:
    """Test the packet is sent as a UDP broadcast to the requested address/port."""
    socket_cls, sock = _mock_broadcast_socket()
    with patch("music_assistant.helpers.wake_on_lan.socket.socket", socket_cls):
        await send_magic_packet("AA:BB:CC:DD:EE:FF", broadcast_ip="192.168.1.255", port=7)

    sock.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    _packet, address = sock.sendto.call_args[0]
    assert address == ("192.168.1.255", 7)


@pytest.mark.asyncio
async def test_magic_packet_is_sent_repeat_times() -> None:
    """Test the packet is (re)sent the requested number of times to guard against a dropped frame."""
    socket_cls, sock = _mock_broadcast_socket()
    with patch("music_assistant.helpers.wake_on_lan.socket.socket", socket_cls):
        await send_magic_packet("AA:BB:CC:DD:EE:FF", repeat=5)

    assert sock.sendto.call_count == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mac_address", ["", "not-a-mac", "AA:BB:CC:DD:EE", "GG:BB:CC:DD:EE:FF", "00:00:00:00:00:00"]
)
async def test_invalid_mac_address_raises(mac_address: str) -> None:
    """Test an invalid (or null) MAC address raises instead of silently sending nothing."""
    with pytest.raises(ValueError, match="Invalid MAC address"):
        await send_magic_packet(mac_address)
