"""Tests for evicting owner-bound (guest) Sendspin pairings."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from aiosendspin.noise.keys import generate_psk, psk_id_for
from aiosendspin.noise.trust_store import InMemoryServerPairingStore, ServerPairingRecord
from aiosendspin.server import ClientDisconnectedEvent
from music_assistant_models.auth import User, UserRole

import music_assistant.providers.sendspin.provider as provider_module
from music_assistant.helpers.guest_access import credential_owners_for_user_id
from music_assistant.providers.sendspin.provider import (
    SendspinProvider,
    _evict_session_pairing_task_id,
    _evict_stale_pairings,
)

from .test_pin_session import _FakeMass, _timers

if TYPE_CHECKING:
    import pytest
    from aiosendspin.server import SendspinServer
    from aiosendspin.server.client import SendspinClient

    from music_assistant.controllers.webserver.auth import AuthenticationManager
    from music_assistant.mass import MusicAssistant


class _EvictionServerApi:
    """Server stand-in with the pairing store and unpair surface eviction uses."""

    def __init__(self) -> None:
        self.pairing_store = InMemoryServerPairingStore()
        self.clients: dict[str, SendspinClient] = {}
        self.unpaired: list[str] = []

    def add_client(self, client_id: str, *, connected: bool) -> None:
        self.clients[client_id] = cast("SendspinClient", SimpleNamespace(is_connected=connected))

    def get_client(self, client_id: str) -> SendspinClient | None:
        return self.clients.get(client_id)

    async def unpair(self, client_id: str) -> None:
        client = self.clients.get(client_id)
        if client is None or not client.is_connected:
            # Mirrors aiosendspin, which resolves the connection before unpairing.
            raise ValueError(f"client {client_id} is not connected")
        await self.pairing_store.remove_record(client_id)
        self.unpaired.append(client_id)


class _FakeAuth:
    """Auth stand-in resolving only the account lookup the reconciliation needs."""

    def __init__(self, *user_ids: str) -> None:
        self._user_ids = set(user_ids)

    async def get_user(self, user_id: str) -> User | None:
        # mirrors the real lookup, which answers None for a disabled account too
        if user_id not in self._user_ids:
            return None
        return User(user_id=user_id, username=user_id, role=UserRole.USER)


def _record(client_id: str, owner: str | None = None) -> ServerPairingRecord:
    psk = generate_psk()
    return ServerPairingRecord(
        psk_id=psk_id_for(psk), psk=psk, client_id=client_id, pair_methods=[], owner=owner
    )


def _make_provider(
    api: _EvictionServerApi, monkeypatch: pytest.MonkeyPatch
) -> tuple[SendspinProvider, list[str]]:
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = cast("MusicAssistant", _FakeMass(asyncio.get_running_loop()))
    provider.server_api = cast("SendspinServer", api)
    provider.logger = logging.getLogger("test.sendspin.eviction")
    provider._unloading = False
    provider._pending_pairing_evictions = set()
    refreshed: list[str] = []

    async def _record_refresh(client_id: str) -> None:
        refreshed.append(client_id)

    monkeypatch.setattr(provider, "_refresh_player", _record_refresh)
    return provider, refreshed


async def test_a_disconnect_schedules_the_eviction_with_a_grace_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnect defers the eviction, so a network blip can reconnect onto the record."""
    api = _EvictionServerApi()
    record = _record("c1", owner="guest-g1")
    await api.pairing_store.store_record(record)
    provider, _refreshed = _make_provider(api, monkeypatch)
    provider.event_cb(
        cast("SendspinServer", api),
        ClientDisconnectedEvent(client_id="c1", goodbye_reason=None),
    )
    assert _evict_session_pairing_task_id("c1") in _timers(provider)
    assert await api.pairing_store.record_by_client_id("c1") == record


async def test_the_delayed_eviction_removes_the_session_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the grace period lapses without a reconnect, the session-scoped record is removed."""
    monkeypatch.setattr(provider_module, "SESSION_PAIRING_EVICTION_GRACE", 0.0)
    api = _EvictionServerApi()
    await api.pairing_store.store_record(_record("c1", owner="guest-g1"))
    provider, refreshed = _make_provider(api, monkeypatch)
    provider.event_cb(
        cast("SendspinServer", api),
        ClientDisconnectedEvent(client_id="c1", goodbye_reason=None),
    )
    await asyncio.sleep(0.05)
    assert await api.pairing_store.record_by_client_id("c1") is None
    assert refreshed == ["c1"]


async def test_a_disconnect_keeps_durable_pairings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standalone and account-bound records survive their client disconnecting."""
    api = _EvictionServerApi()
    standalone = _record("c1")
    account_bound = _record("c2", owner="user-u1")
    await api.pairing_store.store_record(standalone)
    await api.pairing_store.store_record(account_bound)
    provider, refreshed = _make_provider(api, monkeypatch)
    await provider._evict_session_pairing("c1")
    await provider._evict_session_pairing("c2")
    assert await api.pairing_store.record_by_client_id("c1") == standalone
    assert await api.pairing_store.record_by_client_id("c2") == account_bound
    assert refreshed == []


async def test_a_reconnected_client_keeps_its_session_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that reconnected before the eviction ran keeps its pairing."""
    api = _EvictionServerApi()
    record = _record("c1", owner="guest-g1")
    await api.pairing_store.store_record(record)
    api.add_client("c1", connected=True)
    provider, refreshed = _make_provider(api, monkeypatch)
    await provider._evict_session_pairing("c1")
    assert await api.pairing_store.record_by_client_id("c1") == record
    assert refreshed == []


async def test_revoking_an_owner_evicts_only_their_pairings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Withdrawing a user's access drops both its owner forms, sparing everyone else's."""
    api = _EvictionServerApi()
    # Stamped with the same owner formats pair_web_player mints, so this also pins
    # that the revocation hook and the minting side cannot drift apart.
    guest_owner, user_owner = credential_owners_for_user_id("g1")
    await api.pairing_store.store_record(_record("connected", owner=guest_owner))
    await api.pairing_store.store_record(_record("offline", owner=guest_owner))
    await api.pairing_store.store_record(_record("account", owner=user_owner))
    other = _record("other", owner=credential_owners_for_user_id("g2")[0])
    durable = _record("durable")
    await api.pairing_store.store_record(other)
    await api.pairing_store.store_record(durable)
    api.add_client("connected", connected=True)
    provider, refreshed = _make_provider(api, monkeypatch)

    provider._on_user_access_revoked(
        User(user_id="g1", username="party_guest", role=UserRole.GUEST)
    )
    for _ in range(5):
        await asyncio.sleep(0)

    assert await api.pairing_store.record_by_client_id("connected") is None
    assert await api.pairing_store.record_by_client_id("offline") is None
    assert await api.pairing_store.record_by_client_id("account") is None
    assert await api.pairing_store.record_by_client_id("other") == other
    assert await api.pairing_store.record_by_client_id("durable") == durable
    # Only the connected client had a live session to notify.
    assert api.unpaired == ["connected"]
    assert sorted(refreshed) == ["account", "connected", "offline"]


async def test_the_startup_eviction_clears_session_pairings() -> None:
    """Leftover session-scoped records go, while records of live accounts stay."""
    store = InMemoryServerPairingStore()
    standalone = _record("standalone")
    account_bound = _record("account", owner="user-u1")
    await store.store_record(standalone)
    await store.store_record(account_bound)
    await store.store_record(_record("guest-a", owner="guest-g1"))
    await store.store_record(_record("guest-b", owner="guest-g2"))

    auth = cast("AuthenticationManager", _FakeAuth("u1"))
    assert await _evict_stale_pairings(store, auth) == (2, 0)
    assert list(await store.list_records()) == [standalone, account_bound]


async def test_the_startup_eviction_clears_pairings_of_gone_accounts() -> None:
    """A record survives a restart only while its account does, so a revocation cannot be missed."""
    store = InMemoryServerPairingStore()
    standalone = _record("standalone")
    live = _record("live", owner="user-u1")
    await store.store_record(standalone)
    await store.store_record(live)
    # deleted or disabled since this record was written (get_user answers None for both)
    await store.store_record(_record("gone", owner="user-u2"))

    auth = cast("AuthenticationManager", _FakeAuth("u1"))
    assert await _evict_stale_pairings(store, auth) == (0, 1)
    assert list(await store.list_records()) == [standalone, live]


async def test_revoking_skips_a_pairing_that_changed_hands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record re-paired since the listing belongs to its new owner, so it is left alone."""
    api = _EvictionServerApi()
    restamped = _record("c1", owner="user-u2")
    await api.pairing_store.store_record(restamped)
    api.add_client("c1", connected=True)
    provider, refreshed = _make_provider(api, monkeypatch)

    async def _stale_listing(_owner: str) -> list[ServerPairingRecord]:
        return [_record("c1", owner="guest-g1")]

    monkeypatch.setattr(api.pairing_store, "records_by_owner", _stale_listing)
    await provider._evict_pairings_for_owner("guest-g1")

    assert await api.pairing_store.record_by_client_id("c1") == restamped
    assert api.unpaired == []
    assert refreshed == []
