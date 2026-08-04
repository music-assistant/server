"""Tests for the addresses the Sendspin server advertises on mDNS."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.sendspin.provider import SendspinProvider


async def _advertised_addresses(publish_addresses: list[str]) -> list[str] | None:
    """
    Return the addresses a loaded provider hands to the Sendspin server.

    :param publish_addresses: Addresses the streamserver publishes Music Assistant on.
    """
    start_server = AsyncMock()
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = MagicMock()
    provider.mass.streams.publish_addresses = publish_addresses
    provider.server_api = MagicMock(start_server=start_server)
    provider._manual_ip_config = ()
    await provider.loaded_in_mass()
    assert start_server.await_args is not None
    advertised: list[str] | None = start_server.await_args.kwargs["advertise_addresses"]
    return advertised


async def test_advertises_only_ipv4_publish_addresses() -> None:
    """IPv6 publish addresses are dropped and the IPv4 ones keep their original order."""
    assert await _advertised_addresses(["fd00::10", "192.168.1.10", "10.0.0.5"]) == [
        "192.168.1.10",
        "10.0.0.5",
    ]


async def test_advertises_none_without_ipv4_publish_addresses() -> None:
    """None (not []) is passed without IPv4, as [] makes aiosendspin skip mDNS entirely."""
    assert await _advertised_addresses(["fd00::10"]) is None
