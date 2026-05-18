"""Loopback tests for the WLED V2 UDP transport."""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

from music_assistant.providers.wled_audiosync.constants import WLED_V2_PACKET_SIZE
from music_assistant.providers.wled_audiosync.wled_audiosync_bridge.transport import (
    DestinationKind,
    WledV2Transport,
    classify_destination,
)

# 44-byte sentinel payload — fixed bytes so the assertions are unambiguous.
SAMPLE_PACKET = b"00002\x00\x00\x00" + bytes(range(36))


class _Listener:
    """Tiny asyncio UDP listener that records every datagram received."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self._event = asyncio.Event()
        self._transport: asyncio.DatagramTransport | None = None
        self.host: str = ""
        self.port: int = 0

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
        """Bind and start receiving. Returns the bound (host, port)."""
        loop = asyncio.get_running_loop()
        transport, _proto = await loop.create_datagram_endpoint(
            lambda: self._Protocol(self),
            local_addr=(host, port),
        )
        self._transport = transport
        sock = transport.get_extra_info("socket")
        bound_host, bound_port = sock.getsockname()[:2]
        self.host = bound_host
        self.port = bound_port
        return bound_host, bound_port

    async def wait_for(self, count: int, timeout: float = 1.0) -> None:
        """Wait until at least `count` packets have been received."""
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.received) < count:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                msg = f"only received {len(self.received)}/{count} packets in {timeout}s"
                raise AssertionError(msg)
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=remaining)
            except TimeoutError:
                continue

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    class _Protocol(asyncio.DatagramProtocol):
        def __init__(self, owner: _Listener) -> None:
            self._owner = owner

        def datagram_received(self, data: bytes, _addr: tuple[str, int]) -> None:
            self._owner.received.append(data)
            self._owner._event.set()


@pytest_asyncio.fixture
async def listener() -> AsyncIterator[_Listener]:
    """Provide a bound loopback UDP listener that's torn down after the test."""
    lis = _Listener()
    await lis.start()
    try:
        yield lis
    finally:
        lis.close()


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("192.168.1.42", DestinationKind.UNICAST),
        ("10.0.0.5", DestinationKind.UNICAST),
        ("239.0.0.1", DestinationKind.MULTICAST),
        ("224.0.0.251", DestinationKind.MULTICAST),
        ("255.255.255.255", DestinationKind.BROADCAST),
        ("192.168.1.255", DestinationKind.BROADCAST),
        ("not-an-ip", DestinationKind.UNICAST),
    ],
)
def test_classify_destination(address: str, expected: DestinationKind) -> None:
    """Address-kind classifier picks the right bucket for typical inputs."""
    assert classify_destination(address) is expected


async def test_unicast_send_roundtrip(listener: _Listener) -> None:
    """A single packet sent to a loopback listener is received intact."""
    host, port = listener.host, listener.port
    transport = WledV2Transport(host, port, duplicate_transmit=False)
    try:
        await transport.send(SAMPLE_PACKET)
        await listener.wait_for(1)
    finally:
        await transport.close()
    assert listener.received == [SAMPLE_PACKET]
    assert len(SAMPLE_PACKET) == WLED_V2_PACKET_SIZE


async def test_duplicate_transmit_sends_two_packets(listener: _Listener) -> None:
    """With duplicate_transmit=True, each send() produces two identical datagrams."""
    host, port = listener.host, listener.port
    transport = WledV2Transport(host, port, duplicate_transmit=True)
    try:
        await transport.send(SAMPLE_PACKET)
        await listener.wait_for(2)
    finally:
        await transport.close()
    assert listener.received == [SAMPLE_PACKET, SAMPLE_PACKET]


async def test_transport_reopens_after_close(listener: _Listener) -> None:
    """A transport whose endpoint was closed reopens on the next send()."""
    host, port = listener.host, listener.port
    transport = WledV2Transport(host, port, duplicate_transmit=False)
    try:
        await transport.send(SAMPLE_PACKET)
        await listener.wait_for(1)
        await transport.close()
        await transport.send(SAMPLE_PACKET)
        await listener.wait_for(2)
    finally:
        await transport.close()
    assert listener.received == [SAMPLE_PACKET, SAMPLE_PACKET]


async def test_broadcast_socket_sets_so_broadcast_flag() -> None:
    """A broadcast-kind transport opens with SO_BROADCAST enabled on its socket."""
    transport = WledV2Transport("192.168.255.255", 0, duplicate_transmit=False)
    assert transport.kind is DestinationKind.BROADCAST
    try:
        # Trigger the lazy open via a send. sendto() may fail because we're
        # not on a network that can route 192.168.255.255 — that's fine, we
        # only care about the socket options the open path applied.
        await transport.send(b"\x00" * WLED_V2_PACKET_SIZE)
        sock = transport.socket
        if sock is None:
            pytest.skip("could not open broadcast socket in this environment")
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST) == 1
    finally:
        await transport.close()


async def test_multicast_socket_sets_ip_multicast_ttl() -> None:
    """A multicast-kind transport applies the configured IP_MULTICAST_TTL."""
    ttl = 7
    transport = WledV2Transport("239.0.0.1", 0, duplicate_transmit=False, multicast_ttl=ttl)
    assert transport.kind is DestinationKind.MULTICAST
    try:
        await transport.send(b"\x00" * WLED_V2_PACKET_SIZE)
        sock = transport.socket
        if sock is None:
            pytest.skip("could not open multicast socket in this environment")
        actual = sock.getsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL)
        assert actual == ttl
    finally:
        await transport.close()


async def test_multicast_transport_joins_group_for_igmp_keepalive() -> None:
    """Multicast destinations must IP_ADD_MEMBERSHIP so IGMP-snooping switches keep forwarding."""
    group = "239.0.0.7"
    expected_mreq = struct.pack("4sl", socket.inet_aton(group), socket.INADDR_ANY)
    original_setsockopt = socket.socket.setsockopt
    membership_calls: list[bytes] = []

    def _spy(self_: socket.socket, level: int, optname: int, value: Any) -> None:
        if level == socket.IPPROTO_IP and optname == socket.IP_ADD_MEMBERSHIP:
            assert isinstance(value, bytes)
            membership_calls.append(value)
        original_setsockopt(self_, level, optname, value)

    transport = WledV2Transport(group, 0, duplicate_transmit=False)
    try:
        with patch.object(socket.socket, "setsockopt", _spy):
            await transport.send(b"\x00" * WLED_V2_PACKET_SIZE)
        assert membership_calls, (
            "expected IP_ADD_MEMBERSHIP to be applied for multicast destinations"
        )
        assert expected_mreq in membership_calls
    finally:
        await transport.close()


async def test_unicast_transport_does_not_join_any_multicast_group() -> None:
    """IP_ADD_MEMBERSHIP must NOT be applied for plain unicast destinations."""
    original_setsockopt = socket.socket.setsockopt
    membership_calls: list[bytes] = []

    def _spy(self_: socket.socket, level: int, optname: int, value: Any) -> None:
        if level == socket.IPPROTO_IP and optname == socket.IP_ADD_MEMBERSHIP:
            assert isinstance(value, bytes)
            membership_calls.append(value)
        original_setsockopt(self_, level, optname, value)

    transport = WledV2Transport("127.0.0.1", 0, duplicate_transmit=False)
    try:
        with patch.object(socket.socket, "setsockopt", _spy):
            await transport.send(b"\x00" * WLED_V2_PACKET_SIZE)
        assert membership_calls == [], (
            f"unicast must not join any multicast group, but joined {membership_calls!r}"
        )
    finally:
        await transport.close()


# --- Robustness: error counting + log throttling + auto-reset ---


async def _force_sendto_to_fail(transport: WledV2Transport) -> None:
    """Open the transport, then replace its sendto with one that raises OSError."""
    await transport.send(SAMPLE_PACKET)  # forces lazy _open()
    inner = transport._transport
    assert inner is not None, "expected the loopback open to succeed"
    inner.sendto = MagicMock(  # type: ignore[method-assign]
        side_effect=OSError("simulated send failure")
    )


async def test_successful_send_resets_error_counter(listener: _Listener) -> None:
    """A successful send returns the consecutive_errors counter to 0."""
    transport = WledV2Transport(listener.host, listener.port, duplicate_transmit=False)
    try:
        await transport.send(SAMPLE_PACKET)
        await listener.wait_for(1)
        assert transport.consecutive_errors == 0
        assert transport.packets_sent == 1
    finally:
        await transport.close()


async def test_failed_send_increments_consecutive_error_counter(
    listener: _Listener,
) -> None:
    """Each failing sendto bumps the counter while the transport stays alive."""
    transport = WledV2Transport(
        listener.host,
        listener.port,
        duplicate_transmit=False,
        reset_after_consecutive_errors=10,
    )
    try:
        await _force_sendto_to_fail(transport)
        for _ in range(5):
            await transport.send(SAMPLE_PACKET)
        assert transport.consecutive_errors == 5
        assert isinstance(transport.last_error, OSError)
        assert transport.is_open  # still under the reset threshold
    finally:
        await transport.close()


async def test_transport_resets_after_consecutive_error_threshold(
    listener: _Listener,
) -> None:
    """After the configured threshold, the endpoint is torn down for reopen."""
    transport = WledV2Transport(
        listener.host,
        listener.port,
        duplicate_transmit=False,
        reset_after_consecutive_errors=4,
    )
    try:
        await _force_sendto_to_fail(transport)
        for _ in range(4):
            await transport.send(SAMPLE_PACKET)
        # The 4th failure trips the reset and closes the endpoint.
        assert not transport.is_open, "expected the transport to be closed by reset"
        assert transport.consecutive_errors == 4
    finally:
        await transport.close()


async def test_transport_recovers_after_failures_when_send_succeeds(
    listener: _Listener,
) -> None:
    """A successful send after a streak of failures resets the counters."""
    transport = WledV2Transport(
        listener.host,
        listener.port,
        duplicate_transmit=False,
        reset_after_consecutive_errors=100,
    )
    try:
        await _force_sendto_to_fail(transport)
        for _ in range(3):
            await transport.send(SAMPLE_PACKET)
        assert transport.consecutive_errors == 3
        # Restore working sendto so the next call goes through.
        inner = transport._transport
        assert inner is not None
        del inner.sendto
        await transport.send(SAMPLE_PACKET)
        await listener.wait_for(2)
        assert transport.consecutive_errors == 0
        # The initial open-and-send plus the recovery send both counted; the
        # three intermediate failures did not, since sendto raised before
        # _record_success would have run.
        assert transport.packets_sent == 2
    finally:
        await transport.close()


async def test_on_reset_callback_fires_when_threshold_hit(
    listener: _Listener,
) -> None:
    """When auto-reset triggers, the configured on_reset coroutine is scheduled."""
    reset_event = asyncio.Event()

    async def _on_reset() -> None:
        reset_event.set()

    transport = WledV2Transport(
        listener.host,
        listener.port,
        duplicate_transmit=False,
        reset_after_consecutive_errors=3,
        on_reset=_on_reset,
    )
    try:
        await _force_sendto_to_fail(transport)
        for _ in range(3):
            await transport.send(SAMPLE_PACKET)
        await asyncio.wait_for(reset_event.wait(), timeout=1.0)
        assert not transport.is_open
    finally:
        await transport.close()


async def test_on_reset_callback_not_fired_before_threshold(
    listener: _Listener,
) -> None:
    """on_reset must NOT fire if the failure streak stays under the threshold."""
    reset_event = asyncio.Event()

    async def _on_reset() -> None:
        reset_event.set()

    transport = WledV2Transport(
        listener.host,
        listener.port,
        duplicate_transmit=False,
        reset_after_consecutive_errors=10,
        on_reset=_on_reset,
    )
    try:
        await _force_sendto_to_fail(transport)
        for _ in range(5):
            await transport.send(SAMPLE_PACKET)
        # Give any spurious task a chance to run.
        await asyncio.sleep(0.05)
        assert not reset_event.is_set()
        assert transport.is_open
    finally:
        await transport.close()


async def test_error_logging_is_throttled(
    listener: _Listener, caplog: pytest.LogCaptureFixture
) -> None:
    """Sustained errors must not emit a WARNING per send (would flood the log)."""
    transport = WledV2Transport(
        listener.host,
        listener.port,
        duplicate_transmit=False,
        reset_after_consecutive_errors=1000,
        error_log_interval_s=3600.0,  # effectively "never repeat" during the test
    )
    try:
        await _force_sendto_to_fail(transport)
        with caplog.at_level(
            logging.WARNING,
            logger="music_assistant.providers.wled_audiosync.transport",
        ):
            for _ in range(20):
                await transport.send(SAMPLE_PACKET)
        # Exactly one initial "sendto failed" warning despite 20 failed sends.
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING and "sendto" in r.getMessage()
        ]
        assert len(warning_records) == 1, (
            f"expected exactly one throttled WARNING, got {len(warning_records)}: "
            f"{[r.getMessage() for r in warning_records]}"
        )
    finally:
        await transport.close()
