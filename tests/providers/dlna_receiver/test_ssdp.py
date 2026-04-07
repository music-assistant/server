"""Tests for SSDP advertiser."""

from __future__ import annotations

from provider.ssdp import SSDPAdvertiser


def test_ssdp_advertiser_init() -> None:
    adv = SSDPAdvertiser(
        udn="uuid:test-1234",
        description_url="http://192.168.1.100:8298/description.xml",
        bind_ip="192.168.1.100",
    )
    assert adv.udn == "uuid:test-1234"
    assert adv.bind_ip == "192.168.1.100"
    assert "8298" in adv.description_url


def test_handle_search_ignores_non_matching() -> None:
    adv = SSDPAdvertiser(
        udn="uuid:test-1234",
        description_url="http://192.168.1.100:8298/description.xml",
        bind_ip="192.168.1.100",
    )
    # Non-M-SEARCH data should be silently ignored (no exception)
    adv.handle_search(b"NOTIFY * HTTP/1.1\r\n", ("192.168.1.1", 1900))
