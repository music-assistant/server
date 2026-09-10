"""Tests for pairing the built-in web player from its own pairing token."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiosendspin.models.types import PairMethod
from aiosendspin.noise.keys import PSK_SIZE, b64url_encode
from aiosendspin.noise.pairing import PairingError
from aiosendspin.noise.pairing_token import PSKPairingToken, encode_token
from aiosendspin.noise.trust_store import PskCategory
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import InvalidCommand

import music_assistant.providers.sendspin.provider as provider_module
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.providers.sendspin.player import SendspinBasePlayer

from .test_pin_session import _FakeServerApi, _make_provider

if TYPE_CHECKING:
    from aiosendspin.server.client import SendspinClient

    from music_assistant.providers.sendspin.provider import SendspinProvider

_CLIENT_ID = b64url_encode(bytes(range(32)))
_PAIRING_PSK = bytes(range(100, 100 + PSK_SIZE))
_TOKEN = encode_token(PSKPairingToken(client_id=_CLIENT_ID, pairing_psk=_PAIRING_PSK))


class _FakePairingStore:
    """Pairing store stand-in answering only the pairing-record lookup."""

    def __init__(self, *, paired: bool, record_owner: str | None = None) -> None:
        self._record = SimpleNamespace(owner=record_owner) if paired else None

    async def record_by_client_id(self, client_id: str) -> Any:
        return self._record


def _user_lookup(*, found: bool) -> Any:
    """Return an auth get_user stand-in answering as if the account exists or is gone."""

    async def _get_user(user_id: str) -> Any:
        return SimpleNamespace(user_id=user_id) if found else None

    return _get_user


class _WebPlayerServerApi(_FakeServerApi):
    """_FakeServerApi with the pairing surface the web-player entry point uses."""

    def __init__(
        self, *, connected: bool = True, paired: bool = False, record_owner: str | None = None
    ) -> None:
        super().__init__([], await_pin=False, connected=connected)
        self.pairing_store = _FakePairingStore(paired=paired, record_owner=record_owner)


def _make_web_provider(
    api: _WebPlayerServerApi,
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_web_player: bool = True,
    registered: bool = True,
    initialized: bool = True,
    psk_category: PskCategory | None = PskCategory.SENTINEL,
    caller_has_access: bool = True,
) -> tuple[SendspinProvider, list[str]]:
    provider, refreshed = _make_provider(api, monkeypatch)
    cast("Any", provider.mass).webserver = SimpleNamespace(
        auth=SimpleNamespace(get_user=_user_lookup(found=caller_has_access))
    )
    # the provider resolves its translation namespace through the manifest
    cast("Any", provider).manifest = SimpleNamespace(domain="sendspin")
    player = SendspinBasePlayer.__new__(SendspinBasePlayer)
    player.is_web_player = is_web_player
    security = None if psk_category is None else SimpleNamespace(psk_category=psk_category)
    player.api = cast("SendspinClient", SimpleNamespace(connection_security=security))
    # initialized is a read-only property over a private Event, so seed it directly
    player._Player__initialized = asyncio.Event()  # type: ignore[attr-defined]
    if initialized:
        player.set_initialized()
    cast("Any", provider.mass).players = SimpleNamespace(
        get_player=lambda _client_id: player if registered else None
    )
    monkeypatch.setattr(provider_module, "WEB_PLAYER_CONNECT_TIMEOUT", 0.3)
    return provider, refreshed


async def test_pair_web_player_pairs_with_the_token_psk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token's PSK is handed to a pairing attempt for the client the token names."""
    api = _WebPlayerServerApi()
    provider, refreshed = _make_web_provider(api, monkeypatch)
    await provider.pair_web_player(_TOKEN)
    assert [(a.method, a.pairing_psk) for a in api.attempts] == [
        (PairMethod.PAIRING_PSK, _PAIRING_PSK)
    ]
    assert refreshed == [_CLIENT_ID]


async def test_pair_web_player_binds_a_guest_pairing_to_the_guest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guest's pairing is bound as session-scoped, so it ends with their session."""
    api = _WebPlayerServerApi()
    provider, _refreshed = _make_web_provider(api, monkeypatch)
    set_current_user(User(user_id="g1", username="party_guest", role=UserRole.GUEST))
    try:
        await provider.pair_web_player(_TOKEN)
    finally:
        set_current_user(None)
    assert [a.owner for a in api.attempts] == ["guest-g1"]


async def test_pair_web_player_binds_a_full_user_pairing_to_their_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A logged-in user's pairing is account-bound, so deleting the account removes it."""
    api = _WebPlayerServerApi()
    provider, _refreshed = _make_web_provider(api, monkeypatch)
    set_current_user(User(user_id="u1", username="maxim", role=UserRole.USER))
    try:
        await provider.pair_web_player(_TOKEN)
    finally:
        set_current_user(None)
    assert [a.owner for a in api.attempts] == ["user-u1"]


async def test_pair_web_player_rejects_a_malformed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token that does not decode names no client, so it is refused before any lookup."""
    api = _WebPlayerServerApi()
    provider, refreshed = _make_web_provider(api, monkeypatch)
    with pytest.raises(InvalidCommand) as excinfo:
        await provider.pair_web_player("SP:0NOTATOKEN")
    assert excinfo.value.translation_key == "pairing_error_token_invalid"
    assert api.attempts == []
    assert refreshed == []


async def test_pair_web_player_gives_up_on_a_client_that_never_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that only left its hello behind is refused rather than pairing blind."""
    api = _WebPlayerServerApi(connected=False)
    provider, refreshed = _make_web_provider(api, monkeypatch)
    with pytest.raises(InvalidCommand, match="did not register"):
        await provider.pair_web_player(_TOKEN)
    assert api.attempts == []
    assert refreshed == []


async def test_pair_web_player_waits_for_a_connection_still_landing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A web player asking before its handshake arrives is paired once the client lands."""
    api = _WebPlayerServerApi(connected=False)
    provider, refreshed = _make_web_provider(api, monkeypatch)
    client = cast("Any", api._client)
    lookups = 0

    def _get_client(_client_id: str) -> Any:
        nonlocal lookups
        lookups += 1
        client.is_connected = lookups > 1
        return client

    monkeypatch.setattr(api, "get_client", _get_client)
    await provider.pair_web_player(_TOKEN)
    assert lookups > 1
    assert [a.pairing_psk for a in api.attempts] == [_PAIRING_PSK]
    assert refreshed == [_CLIENT_ID]


async def test_pair_web_player_refuses_a_non_web_player(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client whose hello does not classify it as a web player keeps the operator gesture."""
    api = _WebPlayerServerApi()
    provider, refreshed = _make_web_provider(api, monkeypatch, is_web_player=False)
    with pytest.raises(InvalidCommand, match="not a built-in web player"):
        await provider.pair_web_player(_TOKEN)
    assert api.attempts == []
    assert refreshed == []


async def test_pair_web_player_refuses_an_unregistered_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connected client with no Sendspin player behind it is never paired."""
    api = _WebPlayerServerApi()
    provider, refreshed = _make_web_provider(api, monkeypatch, registered=False)
    with pytest.raises(InvalidCommand, match="did not register"):
        await provider.pair_web_player(_TOKEN)
    assert api.attempts == []
    assert refreshed == []


async def test_pair_web_player_waits_out_a_half_registered_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A player still registering is not paired, so the pairing refresh cannot be dropped."""
    api = _WebPlayerServerApi()
    provider, refreshed = _make_web_provider(api, monkeypatch, initialized=False)
    with pytest.raises(InvalidCommand, match="did not register"):
        await provider.pair_web_player(_TOKEN)
    assert api.attempts == []
    assert refreshed == []


async def test_pair_web_player_skips_an_unencrypted_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unencrypted client cannot hold a pairing, so it is left alone instead of failing."""
    api = _WebPlayerServerApi()
    provider, refreshed = _make_web_provider(api, monkeypatch, psk_category=None)
    await provider.pair_web_player(_TOKEN)
    assert api.attempts == []
    assert refreshed == []


async def test_pair_web_player_reports_a_pairing_failure_without_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed attempt surfaces as a localized error, keeping the token out of the message."""
    api = _WebPlayerServerApi()
    provider, _refreshed = _make_web_provider(api, monkeypatch)
    api.outcomes.append(PairingError("device refused the PSK"))
    with pytest.raises(InvalidCommand) as excinfo:
        await provider.pair_web_player(_TOKEN)
    assert excinfo.value.translation_key == "pairing_error_failed"
    assert excinfo.value.translation_owner == "provider.sendspin"
    assert _TOKEN not in str(excinfo.value)


async def test_pair_web_player_is_a_no_op_when_already_paired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A web player calling on every mount does not re-handshake an existing pairing."""
    api = _WebPlayerServerApi(paired=True)
    provider, refreshed = _make_web_provider(api, monkeypatch, psk_category=PskCategory.LONG_TERM)
    await provider.pair_web_player(_TOKEN)
    assert api.attempts == []
    assert refreshed == []


async def test_pair_web_player_restamps_a_pairing_owned_by_another_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser keeps its identity across logins, so the pairing follows the current caller."""
    api = _WebPlayerServerApi(paired=True, record_owner="user-someone-else")
    provider, refreshed = _make_web_provider(api, monkeypatch, psk_category=PskCategory.LONG_TERM)
    set_current_user(User(user_id="g1", username="party_guest", role=UserRole.GUEST))
    try:
        await provider.pair_web_player(_TOKEN)
    finally:
        set_current_user(None)
    assert [a.owner for a in api.attempts] == ["guest-g1"]
    assert refreshed == [_CLIENT_ID]


async def test_pair_web_player_is_a_no_op_for_the_pairing_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller that owns the existing pairing never re-handshakes it."""
    api = _WebPlayerServerApi(paired=True, record_owner="guest-g1")
    provider, refreshed = _make_web_provider(api, monkeypatch, psk_category=PskCategory.LONG_TERM)
    set_current_user(User(user_id="g1", username="party_guest", role=UserRole.GUEST))
    try:
        await provider.pair_web_player(_TOKEN)
    finally:
        set_current_user(None)
    assert api.attempts == []
    assert refreshed == []


async def test_pair_web_player_repairs_a_client_that_lost_its_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record the client can no longer authenticate is re-paired instead of stranding it."""
    api = _WebPlayerServerApi(paired=True)
    provider, refreshed = _make_web_provider(api, monkeypatch, psk_category=PskCategory.SENTINEL)
    await provider.pair_web_player(_TOKEN)
    assert [a.pairing_psk for a in api.attempts] == [_PAIRING_PSK]
    assert refreshed == [_CLIENT_ID]


async def test_pair_web_player_withdraws_a_pairing_the_caller_just_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Access withdrawn while the handshake ran leaves no pairing behind."""
    api = _WebPlayerServerApi()
    provider, _refreshed = _make_web_provider(api, monkeypatch, caller_has_access=False)
    evicted: list[str] = []

    async def _record_eviction(owner: str) -> None:
        evicted.append(owner)

    monkeypatch.setattr(provider, "_evict_pairings_for_owner", _record_eviction)
    set_current_user(User(user_id="u1", username="maxim", role=UserRole.USER))
    try:
        await provider.pair_web_player(_TOKEN)
    finally:
        set_current_user(None)

    assert [a.owner for a in api.attempts] == ["user-u1"]
    assert evicted == ["user-u1"]


async def test_pair_web_player_keeps_the_pairing_of_a_live_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The revalidation after pairing leaves an unaffected caller's pairing in place."""
    api = _WebPlayerServerApi()
    provider, _refreshed = _make_web_provider(api, monkeypatch)
    evicted: list[str] = []

    async def _record_eviction(owner: str) -> None:
        evicted.append(owner)

    monkeypatch.setattr(provider, "_evict_pairings_for_owner", _record_eviction)
    set_current_user(User(user_id="u1", username="maxim", role=UserRole.USER))
    try:
        await provider.pair_web_player(_TOKEN)
    finally:
        set_current_user(None)

    assert [a.owner for a in api.attempts] == ["user-u1"]
    assert evicted == []
