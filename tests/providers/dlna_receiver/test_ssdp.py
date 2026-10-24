"""Tests for SSDP advertiser."""

from __future__ import annotations

import socket

import pytest
from music_assistant_models.errors import SetupFailedError

from music_assistant.providers.dlna_receiver.ssdp import (
    _MX_MAX_SECONDS,
    SSDPAdvertiser,
    _parse_mx_delay,
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
