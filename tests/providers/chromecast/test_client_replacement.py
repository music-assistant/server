"""Tests for a Cast device whose client is stopped and replaced by a new one."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from pychromecast.const import CAST_TYPE_AUDIO
from pychromecast.error import ChromecastConnectionError
from pychromecast.models import HostServiceInfo
from pychromecast.socket_client import SocketClient

from music_assistant.providers.chromecast.player import ChromecastPlayer
from tests.providers.chromecast.test_lifecycle import (
    _cast_info,
    _cast_player,
    _connect_client,
    _mocked_provider,
)


async def _attached() -> tuple[ChromecastPlayer, MagicMock]:
    """Attach the protocol of a Cast device to a fake client that is not connected."""
    protocol = _cast_player(_mocked_provider(), _cast_info())
    protocol.set_player(MagicMock())
    with patch("pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client):
        await protocol.on_attach(MagicMock())
    client = cast("MagicMock", protocol.cc)
    client.socket_client.is_connected = False
    client.socket_client.socket = None
    protocol._attr_available = False
    protocol.update_state = MagicMock()  # type: ignore[misc, method-assign]
    return protocol, client


def _replace_client(protocol: ChromecastPlayer) -> MagicMock:
    """Replace the client of a protocol, and return the new one."""
    with patch(
        "pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client
    ) as connect:
        protocol._replace_client()
    connect.assert_called_once()
    return cast("MagicMock", protocol.cc)


def _closed_port() -> int:
    """Return a loopback port that nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _real_socket_client(protocol: ChromecastPlayer, client: MagicMock, port: int) -> Any:
    """
    Give a fake client a real pychromecast socket client that is not connected.

    :param protocol: The protocol, whose status listener follows the socket client.
    :param client: The fake client of the protocol.
    :param port: The loopback port of the one service it tries.
    """
    socket_client = SocketClient(
        cast_type=CAST_TYPE_AUDIO,
        tries=1,
        timeout=1,
        retry_wait=1,
        services={HostServiceInfo("127.0.0.1", port)},
        zconf=None,
    )
    socket_client.register_connection_listener(cast("Any", protocol.status_listener))
    client.socket_client = socket_client
    mass: Any = protocol.mass
    mass.closing = False
    mass.loop = asyncio.get_running_loop()
    return socket_client


def _close_socket_client(socket_client: Any) -> None:
    """Close what a real socket client holds."""
    socket_client.stop.set()
    if socket_client.socket is not None:
        socket_client.socket.close()
    for end in socket_client.socketpair:
        end.close()
    socket_client.selector.close()


async def test_a_replaced_client_is_stopped_and_a_new_one_started() -> None:
    """The old client is stopped at once, and a new client connects to the device."""
    protocol, client = await _attached()

    new_client = _replace_client(protocol)

    client.disconnect.assert_called_once_with(0)
    assert new_client is not client
    new_client.start.assert_called_once_with()
    # it reads available once the new client reports the connection
    cast("MagicMock", protocol.update_state).assert_not_called()


async def test_a_handler_of_another_part_moves_to_the_new_client() -> None:
    """A controller registered on the client of the device is registered on its new client."""
    protocol, client = await _attached()
    handler = MagicMock()
    protocol.register_client_handler(handler)
    client.register_handler.assert_called_with(handler)

    new_client = _replace_client(protocol)

    client.unregister_handler.assert_any_call(handler)
    new_client.register_handler.assert_any_call(handler)
    protocol.unregister_client_handler(handler)
    new_client.unregister_handler.assert_called_with(handler)
    assert protocol._client_handlers == []


async def test_a_client_that_is_replaced_logs_no_error(caplog: pytest.LogCaptureFixture) -> None:
    """
    The worker of a client that is replaced ends its connect without an error in the log.

    pychromecast logs the end of a connect that was stopped as an error.
    """
    protocol, client = await _attached()
    socket_client = _real_socket_client(protocol, client, _closed_port())
    try:
        with caplog.at_level(logging.DEBUG, logger="pychromecast"):
            _replace_client(protocol)
            # what its worker does once the client is stopped
            socket_client.stop.set()
            with pytest.raises(ChromecastConnectionError):
                await asyncio.to_thread(socket_client.initialize_connection)
    finally:
        _close_socket_client(socket_client)

    assert [record.getMessage() for record in caplog.records] == []
