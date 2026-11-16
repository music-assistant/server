"""Tests for the check of a Cast device that the players controller asks for."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pychromecast.const import CAST_TYPE_AUDIO, CAST_TYPE_GROUP
from pychromecast.error import ChromecastConnectionError
from pychromecast.models import HostServiceInfo
from pychromecast.socket_client import SocketClient

import music_assistant.providers.chromecast.player as cast_player_module
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
    client.socket_client.socket = MagicMock() if connected else None
    protocol._attr_available = connected
    if answers is not None:
        protocol._services_answer = AsyncMock(  # type: ignore[method-assign]
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
    assert connect.call_count <= 1
    return answered, cast("MagicMock", protocol.cc) if connect.called else None


async def test_a_connected_client_is_not_checked() -> None:
    """A connected client tells itself when its device is gone: nothing is probed or changed."""
    protocol, client = await _attached(connected=True, answers=None)
    probe = AsyncMock(return_value=False)
    protocol._services_answer = probe  # type: ignore[method-assign]

    answered, new_client = await _check(protocol)

    assert (answered, new_client, protocol.available) == (None, None, True)
    probe.assert_not_awaited()
    client.disconnect.assert_not_called()
    cast("MagicMock", protocol.update_state).assert_not_called()


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

    assert (answered, new_client, protocol.available) == (False, None, False)
    client.disconnect.assert_not_called()
    cast("MagicMock", protocol.update_state).assert_not_called()


async def test_a_client_in_its_retry_sleep_with_a_socket_is_connected_to_at_once() -> None:
    """A client that keeps the socket of a failed attempt while it waits is replaced too."""
    protocol, client = await _attached(connected=False, answers=True)
    # pychromecast keeps the socket of an attempt whose first message failed until its next try
    client.socket_client.socket = MagicMock()

    answered, new_client = await _check(protocol)

    assert answered is True
    client.disconnect.assert_called_once_with(0)
    assert new_client is not None


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
    protocol, client = await _attached(connected=False, answers=True, cast_type=CAST_TYPE_GROUP)

    answered, new_client = await _check(protocol)

    assert (answered, new_client) == (None, None)
    client.disconnect.assert_not_called()


@pytest.mark.parametrize(
    "order", ["detach only", "taken off its player", "taken off its player, then detached"]
)
async def test_a_check_that_resumes_after_a_detach_changes_nothing(order: str) -> None:
    """A check whose probe comes back after the protocol was detached starts no new client."""
    # a disconnected client whose device answers: a check that acted would replace it
    protocol, client = await _attached(connected=False, answers=None)
    client.socket_client.services = {HostServiceInfo("192.0.2.1", 8009)}
    client.socket_client.zconf = None
    probing = asyncio.Event()
    answer = asyncio.Event()

    async def _open_connection(*_args: Any, **_kwargs: Any) -> tuple[MagicMock, MagicMock]:
        probing.set()
        await answer.wait()
        return MagicMock(), MagicMock(wait_closed=AsyncMock())

    with patch("asyncio.open_connection", _open_connection):
        checking = asyncio.create_task(protocol.check_device())
        await probing.wait()
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


async def _connect_by_itself(socket_client: Any) -> None:
    """
    Let pychromecast's own connect reach the device, in a worker thread.

    Only TLS and the first messages are left out: the TCP connection is real.

    :param socket_client: The real socket client.
    """
    tls = MagicMock()
    tls.wrap_socket.side_effect = lambda raw_socket: raw_socket
    with (
        patch("pychromecast.socket_client.ssl.SSLContext", return_value=tls),
        patch.object(socket_client.receiver_controller, "update_status"),
        patch.object(socket_client.heartbeat_controller, "ping"),
    ):
        await asyncio.to_thread(socket_client.initialize_connection)
    await asyncio.sleep(0)


def _close_socket_client(socket_client: Any) -> None:
    """Close what a real socket client holds."""
    socket_client.stop.set()
    if socket_client.socket is not None:
        socket_client.socket.close()
    for end in socket_client.socketpair:
        end.close()
    socket_client.selector.close()


async def test_a_client_that_connected_by_itself_during_the_probe_is_kept() -> None:
    """
    A client that connected by itself while its device was probed is not replaced.

    The device answers the probe, and before the check acts on that, pychromecast's own
    connect in its worker thread reached the device.
    """
    async with _listening() as port:
        protocol, client = await _attached(connected=False, answers=None)
        socket_client = _real_socket_client(protocol, client, port)
        probed = asyncio.Event()
        deliver = asyncio.Event()

        async def _probe_then_wait(host: str, port: int) -> bool:
            answer = await _port_answers(host, port)
            probed.set()
            await deliver.wait()
            return answer

        try:
            with patch.object(cast_player_module, "_port_answers", _probe_then_wait):
                checking = asyncio.create_task(protocol.check_device())
                await probed.wait()
                await _connect_by_itself(socket_client)
                assert (socket_client.is_connected, protocol.available) == (True, True)
                with patch(
                    "pychromecast.get_chromecast_from_cast_info", side_effect=_connect_client
                ) as connect:
                    deliver.set()
                    answered = await checking
        finally:
            _close_socket_client(socket_client)

    assert (protocol.available, client.disconnect.call_count, connect.call_count) == (True, 0, 0)
    assert answered is True


async def test_a_client_that_is_replaced_logs_no_error(caplog: pytest.LogCaptureFixture) -> None:
    """
    The worker of a client that is replaced ends its connect without an error in the log.

    pychromecast logs the end of a connect that was stopped as an error.
    """
    protocol, client = await _attached(connected=False, answers=True)
    socket_client = _real_socket_client(protocol, client, _closed_port())
    try:
        with caplog.at_level(logging.DEBUG, logger="pychromecast"):
            _, new_client = await _check(protocol)
            # what its worker does once the client is stopped
            socket_client.stop.set()
            with pytest.raises(ChromecastConnectionError):
                await asyncio.to_thread(socket_client.initialize_connection)
    finally:
        _close_socket_client(socket_client)

    assert new_client is not None
    assert [record.getMessage() for record in caplog.records] == []
