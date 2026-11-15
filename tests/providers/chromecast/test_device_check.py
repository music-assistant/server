"""Tests for the check of a Cast device that the players controller asks for."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.chromecast.player import ChromecastPlayer
from tests.providers.chromecast.test_lifecycle import (
    _cast_info,
    _cast_player,
    _connect_client,
    _mocked_provider,
)


async def _attached(*, connected: bool, answers: bool) -> tuple[ChromecastPlayer, MagicMock]:
    """
    Attach the protocol of a Cast device to a fake client, and probe its cast port as told.

    :param connected: Whether the client is connected to the device.
    :param answers: Whether the cast port of the device accepts a connection.
    :return: The protocol and its client.
    """
    protocol = _cast_player(_mocked_provider(), _cast_info())
    with patch("pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client):
        await protocol.on_attach(MagicMock())
    client = cast("MagicMock", protocol.cc)
    client.socket_client.is_connected = connected
    protocol._attr_available = connected
    protocol._cast_port_answers = AsyncMock(return_value=answers)  # type: ignore[method-assign]
    protocol.update_state = MagicMock()  # type: ignore[misc, method-assign]
    return protocol, client


async def _check(protocol: ChromecastPlayer) -> tuple[bool | None, MagicMock | None]:
    """Run a device check; return its answer and the client that replaced the old one, if any."""
    with patch(
        "pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client
    ) as connect:
        answered = await protocol.check_device()
    return answered, cast("MagicMock", protocol.cc) if connect.called else None


async def test_a_connected_device_that_answers_keeps_its_client() -> None:
    """A device that answers on its cast port while the client is connected is left as it is."""
    protocol, client = await _attached(connected=True, answers=True)

    answered, new_client = await _check(protocol)

    assert answered is True
    assert new_client is None
    assert protocol.available is True
    client.disconnect.assert_not_called()


async def test_a_connected_device_that_does_not_answer_reads_unavailable_at_once() -> None:
    """A device that does not answer reads unavailable, and a new client waits for it."""
    protocol, client = await _attached(connected=True, answers=False)
    listener = protocol.status_listener
    assert listener is not None

    answered, new_client = await _check(protocol)

    assert answered is False
    assert protocol.available is False
    cast("MagicMock", protocol.update_state).assert_called_once_with()
    # the old client goes without waiting for its thread, and reports nothing any more
    client.disconnect.assert_called_once_with(0)
    assert listener._valid is False
    assert new_client is not None
    new_client.start.assert_called_once_with()


async def test_a_device_that_answers_again_is_connected_to_at_once() -> None:
    """A device that answers while the client is disconnected gets a client that connects now."""
    protocol, client = await _attached(connected=False, answers=True)

    answered, new_client = await _check(protocol)

    assert answered is True
    client.disconnect.assert_called_once_with(0)
    assert new_client is not None
    new_client.start.assert_called_once_with()
    # it reads available once the new client reports the connection
    cast("MagicMock", protocol.update_state).assert_not_called()


async def test_a_device_that_is_not_back_keeps_its_client() -> None:
    """A device that does not answer while the client is disconnected is left to its client."""
    protocol, client = await _attached(connected=False, answers=False)

    answered, new_client = await _check(protocol)

    assert answered is False
    assert new_client is None
    client.disconnect.assert_not_called()


async def test_a_handler_of_another_part_moves_to_the_new_client() -> None:
    """A controller registered on the client of the device is registered on its new client."""
    protocol, client = await _attached(connected=False, answers=True)
    handler = MagicMock()
    protocol.register_client_handler(handler)
    client.register_handler.assert_called_with(handler)

    _, new_client = await _check(protocol)

    assert new_client is not None
    client.unregister_handler.assert_any_call(handler)
    new_client.register_handler.assert_any_call(handler)
    protocol.unregister_client_handler(handler)
    new_client.unregister_handler.assert_called_with(handler)
    assert protocol._client_handlers == []


async def test_the_cast_port_is_probed_with_a_connection() -> None:
    """The probe tells a port that accepts a connection from one that does not."""

    async def _accept(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(_accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    protocol: Any = _cast_player(MagicMock(), _cast_info())
    protocol.cast_info.host = "127.0.0.1"
    protocol.cast_info.port = port
    try:
        assert await protocol._cast_port_answers() is True
    finally:
        server.close()
        await server.wait_closed()

    assert await protocol._cast_port_answers() is False
