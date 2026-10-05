"""Tests for the GDM advertiser of the Plex Connect plugin."""

from __future__ import annotations

import socket
import threading
from unittest.mock import MagicMock, patch

import pytest

from music_assistant.providers.plex_connect.gdm import (
    GDM_BROADCAST_ADDR,
    GDM_BROADCAST_PORT,
    GDM_CLIENT_REGISTER_PORT,
    GDM_LISTEN_PORT,
    GDM_MULTICAST_ADDR,
    PlexGDMAdvertiser,
)


@pytest.fixture
def advertiser() -> PlexGDMAdvertiser:
    """Return a GDM advertiser for testing."""
    return PlexGDMAdvertiser(
        instance_id="client-id-1",
        port=32500,
        publish_ip="192.168.1.10",
        name="Living Room",
        version="2.10.0",
    )


def test_hello_message_contents(advertiser: PlexGDMAdvertiser) -> None:
    """The HELLO message carries the full player identity."""
    message = advertiser._hello_message.decode()
    assert message.startswith("HELLO * HTTP/1.0\r\n")
    assert "Name: Living Room" in message
    assert "Port: 32500" in message
    assert "Resource-Identifier: client-id-1" in message
    assert "Device-Class: speaker" in message
    assert "Provides: client,player,pubsub-player" in message


def test_response_message_contents(advertiser: PlexGDMAdvertiser) -> None:
    """The M-SEARCH response carries the same identity as the HELLO."""
    message = advertiser._response_message.decode()
    assert message.startswith("HTTP/1.0 200 OK\r\n")
    assert message.split("\r\n")[1:] == advertiser._hello_message.decode().split("\r\n")[1:]


def test_bye_message_contents(advertiser: PlexGDMAdvertiser) -> None:
    """The BYE message carries the same identity as the HELLO."""
    message = advertiser._bye_message.decode()
    assert message.startswith("BYE * HTTP/1.0\r\n")
    assert message.split("\r\n")[1:] == advertiser._hello_message.decode().split("\r\n")[1:]


def test_create_listen_socket_options(advertiser: PlexGDMAdvertiser) -> None:
    """The listen socket binds the GDM port and joins the multicast group."""
    sock = MagicMock()
    with patch("socket.socket", return_value=sock):
        assert advertiser._create_listen_socket() is sock

    sock.bind.assert_called_once_with(("0.0.0.0", GDM_LISTEN_PORT))
    setsockopt_calls = [call.args for call in sock.setsockopt.call_args_list]
    assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in setsockopt_calls
    mreq = socket.inet_aton(GDM_MULTICAST_ADDR) + socket.inet_aton("0.0.0.0")
    assert (socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq) in setsockopt_calls
    sock.settimeout.assert_called_once_with(1.0)


def test_discovery_response_sent_from_listen_socket(advertiser: PlexGDMAdvertiser) -> None:
    """M-SEARCH replies are sent from the listen socket (source port 32412)."""
    listen_socket = MagicMock()
    advertiser._listen_socket = listen_socket

    advertiser._send_discovery_response(("192.168.1.20", 54321))

    listen_socket.sendto.assert_called_once_with(
        advertiser._response_message, ("192.168.1.20", 54321)
    )


def test_hello_sent_to_multicast_and_broadcast(advertiser: PlexGDMAdvertiser) -> None:
    """HELLO announcements target the client register group plus legacy broadcast."""
    broadcast_socket = MagicMock()
    advertiser._broadcast_socket = broadcast_socket

    advertiser._send_udp()

    targets = [call.args[1] for call in broadcast_socket.sendto.call_args_list]
    assert (GDM_MULTICAST_ADDR, GDM_CLIENT_REGISTER_PORT) in targets
    assert (GDM_BROADCAST_ADDR, GDM_BROADCAST_PORT) in targets
    assert all(
        call.args[0] == advertiser._hello_message for call in broadcast_socket.sendto.call_args_list
    )


def test_bye_sent_on_stop(advertiser: PlexGDMAdvertiser) -> None:
    """Stopping the advertiser sends a BYE to the client register group."""
    broadcast_socket = MagicMock()
    advertiser._broadcast_socket = broadcast_socket

    advertiser._send_bye()

    messages = [call.args[0] for call in broadcast_socket.sendto.call_args_list]
    assert messages
    assert all(message == advertiser._bye_message for message in messages)


@pytest.mark.parametrize("fail_send", [False, True])
async def test_stop_sends_bye_off_loop_before_closing_sockets(
    advertiser: PlexGDMAdvertiser, fail_send: bool
) -> None:
    """Shutdown sends BYE off-loop and closes sockets even when the send fails."""
    broadcast_socket = MagicMock()
    listen_socket = MagicMock()
    sends: list[tuple[int, bool]] = []
    event_loop_thread = threading.get_ident()

    def sendto(_message: bytes, _target: tuple[str, int]) -> None:
        sends.append((threading.get_ident(), broadcast_socket.close.called))
        if fail_send:
            raise OSError("Network unavailable")

    broadcast_socket.sendto.side_effect = sendto
    with (
        patch.object(advertiser, "_broadcast_socket", broadcast_socket),
        patch.object(advertiser, "_listen_socket", listen_socket),
    ):
        advertiser._running = True

        await advertiser.stop()

        assert len(sends) == 2
        assert all(thread != event_loop_thread and not closed for thread, closed in sends)
        broadcast_socket.close.assert_called_once()
        listen_socket.close.assert_called_once()
        assert advertiser._broadcast_socket is None
        assert advertiser._listen_socket is None
        assert not advertiser._running
