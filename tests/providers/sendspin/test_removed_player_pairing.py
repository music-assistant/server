"""Tests for dropping a Sendspin device's pairing when the user removes its player."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiosendspin.noise.trust_store import TrustedUnpairedClient

from .test_pairing_eviction import _EvictionServerApi, _make_provider, _record

if TYPE_CHECKING:
    import pytest


async def _store_pairing_and_approval(api: _EvictionServerApi, client_id: str) -> None:
    await api.pairing_store.store_record(_record(client_id))
    await api.pairing_store.add_trusted_unpaired(TrustedUnpairedClient(client_id=client_id))


async def test_removing_a_connected_device_unpairs_both_sides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connected device is told to drop its own half of the pairing too."""
    api = _EvictionServerApi()
    await _store_pairing_and_approval(api, "c1")
    api.add_client("c1", connected=True)
    provider, _refreshed = _make_provider(api, monkeypatch)

    await provider.on_protocol_removed("c1")

    assert api.unpaired == ["c1"]
    assert await api.pairing_store.record_by_client_id("c1") is None
    assert await api.pairing_store.trusted_unpaired("c1") is None


async def test_removing_an_offline_device_drops_our_pairing_and_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An offline device can't be told, so only this server's record and approval go."""
    api = _EvictionServerApi()
    await _store_pairing_and_approval(api, "c1")
    provider, _refreshed = _make_provider(api, monkeypatch)

    await provider.on_protocol_removed("c1")

    assert api.unpaired == []
    assert await api.pairing_store.record_by_client_id("c1") is None
    assert await api.pairing_store.trusted_unpaired("c1") is None
