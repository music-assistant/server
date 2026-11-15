"""Tests for the check of a Cast device that the players controller asks for."""

from __future__ import annotations

import asyncio
import contextlib
import selectors
import socket
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pychromecast.const import CAST_TYPE_AUDIO, CAST_TYPE_GROUP
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
    # pychromecast keeps a socket only while it is connected (or completing a connect)
    client.socket_client.socket = MagicMock() if connected else None
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


def _real_socket_client(protocol: ChromecastPlayer, client: MagicMock, *, connected: bool) -> Any:
    """
    Give a fake client a real pychromecast socket client, connected or not to a closed port.

    :param protocol: The protocol, whose status listener follows the socket client.
    :param client: The fake client of the protocol.
    :param connected: Whether the socket client reads connected, on a socket of its own.
    """
    old_port = _closed_port()
    socket_client = SocketClient(
        cast_type=CAST_TYPE_AUDIO,
        tries=1,
        timeout=1,
        retry_wait=1,
        services={HostServiceInfo("127.0.0.1", old_port)},
        zconf=None,
    )
    socket_client.host, socket_client.port = "127.0.0.1", old_port
    socket_client.connecting = not connected
    if connected:
        # the socket of its connection, which a reconnect replaces
        old_connection, old_peer = socket.socketpair()
        old_peer.close()
        socket_client.socket = cast("Any", old_connection)
        socket_client.selector.register(old_connection, selectors.EVENT_READ)
    socket_client.register_connection_listener(cast("Any", protocol.status_listener))
    client.socket_client = socket_client
    mass: Any = protocol.mass
    mass.closing = False
    mass.loop = asyncio.get_running_loop()
    return socket_client


async def _reconnect_by_itself(socket_client: Any, port: int) -> None:
    """
    Let pychromecast's own connect find the device at another port, in a worker thread.

    Only TLS and the first messages are left out: the TCP connection is real.

    :param socket_client: The real socket client.
    :param port: The port the device answers on now.
    """
    socket_client.services = {HostServiceInfo("127.0.0.1", port)}
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


@pytest.mark.parametrize("connected", [True, False])
async def test_a_probe_made_before_the_client_reconnected_by_itself_changes_nothing(
    connected: bool,
) -> None:
    """
    A probe that failed counts for nothing once the client reconnected by itself meanwhile.

    The client reconnects under the same status listener, on a new socket at another address,
    with pychromecast's own connect in its worker thread. The device reads as the client says,
    and the client is kept.
    """
    protocol, client = await _attached(connected=connected, answers=None)
    socket_client = _real_socket_client(protocol, client, connected=connected)
    probed = asyncio.Event()
    deliver = asyncio.Event()

    async def _probe_then_wait(host: str, port: int) -> bool:
        answer = await _port_answers(host, port)
        probed.set()
        await deliver.wait()
        return answer

    async with _listening() as port:
        try:
            with patch.object(cast_player_module, "_port_answers", _probe_then_wait):
                checking = asyncio.create_task(protocol.check_device())
                await probed.wait()
                await _reconnect_by_itself(socket_client, port)
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


async def test_a_check_while_the_client_completes_a_connect_changes_nothing() -> None:
    """A probe made while the client is completing its own connect does not replace it."""
    protocol, client = await _attached(connected=False, answers=True)
    # its new connection has a socket, and is not reported as connected yet
    client.socket_client.socket = MagicMock()

    answered, new_client = await _check(protocol)

    assert (answered, new_client) == (False, None)
    client.disconnect.assert_not_called()


async def test_a_check_of_a_client_that_stopped_meanwhile_changes_nothing() -> None:
    """A failed probe counts for nothing once the client stopped during it, on the same socket."""
    protocol, client = await _attached(connected=True, answers=None)

    async def _client_stops(*_args: Any) -> bool:
        client.socket_client.is_connected = False
        return False

    protocol._client_endpoint_answers = AsyncMock(side_effect=_client_stops)  # type: ignore[method-assign]

    answered, new_client = await _check(protocol)

    assert (answered, new_client, protocol.available) == (False, None, True)
    cast("MagicMock", protocol.update_state).assert_not_called()
