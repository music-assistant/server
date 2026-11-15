"""Tests for the check of a Cast device that the players controller asks for."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pychromecast.const import CAST_TYPE_GROUP
from pychromecast.models import HostServiceInfo

from music_assistant.providers.chromecast.player import ChromecastPlayer, _port_answers
from tests.providers.chromecast.test_lifecycle import (
    _cast_info,
    _cast_player,
    _connect_client,
    _mocked_provider,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def _attached(
    *, connected: bool, answers: bool | None, cast_type: str | None = None
) -> tuple[ChromecastPlayer, MagicMock]:
    """
    Attach the protocol of a Cast device to a fake client, and probe its cast port as told.

    :param connected: Whether the client is connected to the device.
    :param answers: Whether the cast port of the device accepts a connection; None to probe
        for real.
    :param cast_type: The cast type of the device, an audio device when omitted.
    :return: The protocol and its client.
    """
    info = _cast_info(cast_type=cast_type) if cast_type else _cast_info()
    protocol = _cast_player(_mocked_provider(), info)
    protocol.set_player(MagicMock())
    with patch("pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client):
        await protocol.on_attach(MagicMock())
    client = cast("MagicMock", protocol.cc)
    client.socket_client.is_connected = connected
    protocol._attr_available = connected
    if answers is not None:
        protocol._client_endpoint_answers = AsyncMock(  # type: ignore[method-assign]
            return_value=answers
        )
    protocol.update_state = MagicMock()  # type: ignore[misc, method-assign]
    return protocol, client


@contextlib.asynccontextmanager
async def _listening() -> AsyncIterator[int]:
    """Accept connections on a loopback port, and return the port."""

    async def _accept(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(_accept, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def _closed_port() -> int:
    """Return a loopback port that nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
    async with _listening() as port:
        assert await _port_answers("127.0.0.1", port) is True

    assert await _port_answers("127.0.0.1", _closed_port()) is False


async def test_a_connected_client_is_probed_where_it_is_connected() -> None:
    """The device is probed on the endpoint its client talks to, not on a stale discovery record."""
    async with _listening() as port:
        protocol, _ = await _attached(connected=True, answers=None)
        protocol.cast_info.host, protocol.cast_info.port = "127.0.0.1", _closed_port()
        socket_client = protocol.cc.socket_client
        socket_client.host, socket_client.port = "127.0.0.1", port

        answered, new_client = await _check(protocol)

    assert (answered, new_client, protocol.available) == (True, None, True)


async def test_a_disconnected_client_is_probed_on_the_services_it_tries() -> None:
    """A device is probed where the services of its client resolve to, as the client does."""
    async with _listening() as port:
        protocol, client = await _attached(connected=False, answers=None)
        protocol.cast_info.host, protocol.cast_info.port = "127.0.0.1", _closed_port()
        client.socket_client.services = {HostServiceInfo("127.0.0.1", port)}
        client.socket_client.zconf = None

        answered, new_client = await _check(protocol)

    assert answered is True
    assert new_client is not None
    client.disconnect.assert_called_once_with(0)


async def test_a_cast_group_is_not_checked() -> None:
    """A cast group is no device of its own behind a power control: it can not be checked."""
    protocol, client = await _attached(connected=True, answers=False, cast_type=CAST_TYPE_GROUP)

    answered, new_client = await _check(protocol)

    assert (answered, new_client, protocol.available) == (None, None, True)
    client.disconnect.assert_not_called()


@pytest.mark.parametrize(
    "order", ["detach only", "taken off its player", "taken off its player, then detached"]
)
async def test_a_check_that_resumes_after_a_detach_changes_nothing(order: str) -> None:
    """A check whose probe comes back after the protocol was detached starts no new client."""
    protocol, client = await _attached(connected=True, answers=None)
    probing = asyncio.Event()
    answer = asyncio.Event()

    async def _open_connection(*_args: Any, **_kwargs: Any) -> tuple[MagicMock, MagicMock]:
        probing.set()
        await answer.wait()
        return MagicMock(), MagicMock(wait_closed=AsyncMock())

    with patch("asyncio.open_connection", _open_connection):
        checking = asyncio.create_task(protocol.check_device())
        await probing.wait()
        # the client lost its connection meanwhile: a check that acted would replace it
        client.socket_client.is_connected = False
        if order != "detach only":
            protocol.set_player(None)
        if order != "taken off its player":
            await protocol.on_detach()
        with patch(
            "pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client
        ) as connect:
            answer.set()
            await checking

    connect.assert_not_called()
    assert protocol.cc is client
    cast("MagicMock", protocol.update_state).assert_not_called()
