"""Tests for SSDP advertiser."""

from __future__ import annotations

import asyncio
import socket
from typing import Any, cast

import pytest
from music_assistant_models.errors import SetupFailedError

from music_assistant.providers.dlna_receiver.constants import (
    SSDP_MULTICAST_ADDR,
    SSDP_PORT,
    UPNP_DEVICE_TYPE,
    UPNP_SERVICE_AV_TRANSPORT,
    UPNP_SERVICE_CONNECTION_MANAGER,
    UPNP_SERVICE_RENDERING_CONTROL,
)
from music_assistant.providers.dlna_receiver.ssdp import (
    _MX_MAX_SECONDS,
    SSDPAdvertiser,
    _parse_mx_delay,
    _SSDPRecvProtocol,
)


def test_ssdp_advertiser_init() -> None:
    """SSDPAdvertiser stores its configured UDN, bind IP, and description URL."""
    adv = SSDPAdvertiser(
        udn="uuid:test-1234",
        description_url="http://192.168.1.100:8298/description.xml",
        bind_ip="192.168.1.100",
    )
    assert adv.udn == "uuid:test-1234"
    assert adv.bind_ip == "192.168.1.100"
    assert "8298" in adv.description_url


def test_handle_search_ignores_non_matching() -> None:
    """Non-M-SEARCH datagrams are silently dropped without raising."""
    adv = SSDPAdvertiser(
        udn="uuid:test-1234",
        description_url="http://192.168.1.100:8298/description.xml",
        bind_ip="192.168.1.100",
    )
    adv.handle_search(b"NOTIFY * HTTP/1.1\r\n", ("192.168.1.1", 1900))


def test_search_response_includes_required_ext_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """M-SEARCH responses include the required empty EXT header."""
    adv = SSDPAdvertiser(
        udn="uuid:test-1234",
        description_url="http://192.168.1.100:8298/description.xml",
        bind_ip="192.168.1.100",
    )
    responses: list[bytes] = []
    monkeypatch.setattr(
        adv,
        "_send_response",
        lambda response, _addr, _st: responses.append(response),
    )

    adv.handle_search(
        b"M-SEARCH * HTTP/1.1\r\nST: upnp:rootdevice\r\n\r\n",
        ("192.168.1.5", 1900),
    )

    assert len(responses) == 1
    assert b"\r\nEXT:\r\n" in responses[0]


def test_parse_mx_delay_missing_is_zero() -> None:
    """A missing MX header means respond immediately (delay 0)."""
    assert _parse_mx_delay("") == 0.0


def test_parse_mx_delay_non_integer_is_zero() -> None:
    """A malformed MX (non-integer) falls back to immediate response."""
    assert _parse_mx_delay("abc") == 0.0
    assert _parse_mx_delay("3.5") == 0.0


def test_parse_mx_delay_non_positive_is_zero() -> None:
    """Zero or negative MX values fall back to immediate response."""
    assert _parse_mx_delay("0") == 0.0
    assert _parse_mx_delay("-1") == 0.0


def test_parse_mx_delay_within_cap() -> None:
    """For MX ≤ cap, the returned delay is bounded by MX itself."""
    for _ in range(20):
        delay = _parse_mx_delay("3")
        assert 0.0 <= delay < 3.0


def test_parse_mx_delay_caps_large_mx() -> None:
    """Large MX values are clamped to _MX_MAX_SECONDS to keep discovery snappy."""
    for _ in range(20):
        delay = _parse_mx_delay("120")
        assert 0.0 <= delay < _MX_MAX_SECONDS


async def test_shared_ssdp_port_failure_is_setup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unsupported SSDP port sharing is reported as a provider setup failure."""

    class _Socket:
        def __init__(self, fail_bind: bool) -> None:
            self._fail_bind = fail_bind

        def setsockopt(self, _level: int, option: int, _value: object) -> None:
            if option == socket.SO_REUSEPORT:
                raise OSError("unsupported")

        def setblocking(self, _value: bool) -> None:
            return None

        def bind(self, _address: object) -> None:
            if self._fail_bind:
                error = OSError("address in use")
                error.errno = 98
                raise error

        def close(self) -> None:
            return None

    sockets = iter((_Socket(False), _Socket(True)))
    monkeypatch.setattr(
        "music_assistant.providers.dlna_receiver.ssdp.socket.socket", lambda *_args: next(sockets)
    )

    class _Loop:
        async def create_datagram_endpoint(
            self, _factory: object, **_kwargs: object
        ) -> tuple[object, object]:
            return object(), object()

    monkeypatch.setattr(
        "music_assistant.providers.dlna_receiver.ssdp.asyncio.get_running_loop", lambda: _Loop()
    )
    advertiser = SSDPAdvertiser(
        udn="uuid:test",
        description_url="http://192.0.2.10:8298/description.xml",
        bind_ip="192.0.2.10",
    )

    with pytest.raises(SetupFailedError, match="Unable to bind SSDP"):
        await advertiser.start()


def _advertiser() -> SSDPAdvertiser:
    """Build an advertiser without opening sockets."""
    return SSDPAdvertiser(
        udn="uuid:test-1234",
        description_url="http://192.168.1.100:8298/description.xml",
        bind_ip="192.168.1.100",
    )


async def test_send_alive_announces_device_and_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alive notifications cover root, UDN, device and every service type."""
    advertiser = _advertiser()
    sent: list[str] = []
    monkeypatch.setattr(advertiser, "_send_datagram", sent.append)

    await advertiser._send_alive()

    assert len(sent) == 6
    assert all("NTS: ssdp:alive" in message for message in sent)
    assert all(f"HOST: {SSDP_MULTICAST_ADDR}:{SSDP_PORT}" in message for message in sent)
    assert all("LOCATION: http://192.168.1.100:8298/description.xml" in message for message in sent)
    joined = "\n".join(sent)
    for notification_type in (
        "upnp:rootdevice",
        "uuid:test-1234",
        UPNP_DEVICE_TYPE,
        UPNP_SERVICE_AV_TRANSPORT,
        UPNP_SERVICE_RENDERING_CONTROL,
        UPNP_SERVICE_CONNECTION_MANAGER,
    ):
        assert f"NT: {notification_type}" in joined


async def test_send_byebye_announces_device_and_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Byebye notifications cover root, UDN and the device type."""
    advertiser = _advertiser()
    sent: list[str] = []
    monkeypatch.setattr(advertiser, "_send_datagram", sent.append)

    await advertiser._send_byebye()

    assert len(sent) == 3
    assert all("NTS: ssdp:byebye" in message for message in sent)
    assert all("USN:" in message for message in sent)


def test_send_datagram_requires_an_open_transport() -> None:
    """No datagram is sent before start or after the transport closed."""
    advertiser = _advertiser()
    calls: list[bytes] = []

    class _Transport:
        def __init__(self, closing: bool) -> None:
            self._closing = closing

        def is_closing(self) -> bool:
            return self._closing

        def sendto(self, data: bytes, _addr: object) -> None:
            calls.append(data)

    advertiser._transport = cast("Any", _Transport(True))
    advertiser._send_datagram("NOTIFY")
    assert calls == []

    advertiser._transport = cast("Any", _Transport(False))
    advertiser._send_datagram("NOTIFY")
    assert calls == [b"NOTIFY"]


def test_send_response_requires_an_open_receive_transport() -> None:
    """M-SEARCH responses are dropped when the receive socket is closed."""
    advertiser = _advertiser()
    calls: list[tuple[bytes, object]] = []

    class _Transport:
        def __init__(self, closing: bool) -> None:
            self._closing = closing

        def is_closing(self) -> bool:
            return self._closing

        def sendto(self, data: bytes, addr: object) -> None:
            calls.append((data, addr))

    advertiser._recv_transport = cast("Any", _Transport(True))
    advertiser._send_response(b"response", ("192.168.1.5", 1900), "ssdp:all")
    assert calls == []

    advertiser._recv_transport = cast("Any", _Transport(False))
    advertiser._send_response(b"response", ("192.168.1.5", 1900), "ssdp:all")
    assert calls == [(b"response", ("192.168.1.5", 1900))]


async def test_delayed_response_cancellation_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled MX jitter task must not answer after shutdown."""
    advertiser = _advertiser()
    responses: list[bytes] = []
    monkeypatch.setattr(
        advertiser,
        "_send_response",
        lambda response, _addr, _st: responses.append(response),
    )

    task = asyncio.create_task(
        advertiser._delayed_response(b"response", ("192.168.1.5", 1900), "ssdp:all", 30)
    )
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert responses == []


def test_receive_protocol_routes_datagrams_to_the_advertiser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UDP receive protocol forwards incoming bytes to handle_search."""
    advertiser = _advertiser()
    received: list[tuple[bytes, tuple[str, int]]] = []
    monkeypatch.setattr(
        advertiser,
        "handle_search",
        lambda data, addr: received.append((data, addr)),
    )

    protocol = _SSDPRecvProtocol(advertiser)
    protocol.datagram_received(b"M-SEARCH * HTTP/1.1\r\n\r\n", ("192.168.1.5", 1900))

    assert received == [(b"M-SEARCH * HTTP/1.1\r\n\r\n", ("192.168.1.5", 1900))]
