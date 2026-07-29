"""Tests for SSDP advertiser."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.dlna_receiver.ssdp import (
    _MX_MAX_SECONDS,
    SSDPAdvertiser,
    _parse_mx_delay,
)

if TYPE_CHECKING:
    import pytest


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
