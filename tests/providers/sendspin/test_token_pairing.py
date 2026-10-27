"""Tests for pairing any Sendspin device from its pairing token through the API."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aiosendspin.models.types import PairMethod
from aiosendspin.noise.keys import b64url_encode
from aiosendspin.noise.pairing import PairingError
from aiosendspin.noise.pairing_token import PSKPairingToken, encode_psk_token
from aiosendspin.noise.trust_store import PskCategory
from music_assistant_models.auth import Scope, User, UserRole
from music_assistant_models.errors import InvalidCommand

import music_assistant.providers.sendspin.provider as provider_module
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user

from .test_server_start import _make_provider as _make_loadable_provider
from .test_web_player_pairing import (
    _CLIENT_ID,
    _PAIRING_PSK,
    _TOKEN,
    _make_web_provider,
    _WebPlayerServerApi,
)


async def test_pair_with_token_pairs_any_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device that is not a web player is paired with the token's PSK."""
    api = _WebPlayerServerApi()
    provider, refreshed = _make_web_provider(api, monkeypatch, is_web_player=False)
    await provider.pair_device_with_token(_TOKEN)
    assert [(a.method, a.client_id, a.pairing_psk) for a in api.attempts] == [
        (PairMethod.PAIRING_PSK, _CLIENT_ID, _PAIRING_PSK)
    ]
    assert refreshed == [_CLIENT_ID]


async def test_pair_with_token_is_standalone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pairing belongs to the device, not to the account that made the call."""
    api = _WebPlayerServerApi()
    provider, _refreshed = _make_web_provider(api, monkeypatch, is_web_player=False)
    set_current_user(User(user_id="u1", username="homeassistant", role=UserRole.ADMIN))
    try:
        await provider.pair_device_with_token(_TOKEN)
    finally:
        set_current_user(None)
    assert [a.owner for a in api.attempts] == [None]


async def test_pair_with_token_rejects_a_malformed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token that does not decode is refused before any lookup or attempt."""
    api = _WebPlayerServerApi()
    provider, refreshed = _make_web_provider(api, monkeypatch, is_web_player=False)
    with pytest.raises(InvalidCommand) as excinfo:
        await provider.pair_device_with_token("SP:0NOTATOKEN")
    assert excinfo.value.translation_key == "pairing_error_token_invalid"
    assert excinfo.value.translation_owner == "provider.sendspin"
    assert api.attempts == []
    assert refreshed == []


async def test_pair_with_token_gives_up_on_a_device_that_never_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device that never connects fails with a translated error after a bounded wait."""
    api = _WebPlayerServerApi(connected=False)
    provider, refreshed = _make_web_provider(api, monkeypatch, is_web_player=False)
    with pytest.raises(InvalidCommand) as excinfo:
        await provider.pair_device_with_token(_TOKEN)
    assert excinfo.value.translation_key == "pairing_error_not_connected"
    assert api.attempts == []
    assert refreshed == []


async def test_pair_with_token_gives_up_on_an_unknown_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token naming a client this server has never seen is refused the same way."""
    api = _WebPlayerServerApi()
    provider, refreshed = _make_web_provider(api, monkeypatch, is_web_player=False)
    unknown = encode_psk_token(
        PSKPairingToken(client_id=b64url_encode(bytes(32)), pairing_psk=_PAIRING_PSK)
    )
    monkeypatch.setattr(
        api, "get_client", lambda client_id: None if client_id != _CLIENT_ID else api._client
    )
    with pytest.raises(InvalidCommand) as excinfo:
        await provider.pair_device_with_token(unknown)
    assert excinfo.value.translation_key == "pairing_error_not_connected"
    assert api.attempts == []
    assert refreshed == []


async def test_pair_with_token_is_a_no_op_when_already_paired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device re-sending its token on every start does not re-handshake its pairing."""
    api = _WebPlayerServerApi(paired=True)
    provider, refreshed = _make_web_provider(
        api, monkeypatch, is_web_player=False, psk_category=PskCategory.LONG_TERM
    )
    await provider.pair_device_with_token(_TOKEN)
    assert api.attempts == []
    assert refreshed == []


async def test_pair_with_token_repairs_a_device_that_lost_its_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record the device can no longer authenticate is re-paired."""
    api = _WebPlayerServerApi(paired=True)
    provider, refreshed = _make_web_provider(
        api, monkeypatch, is_web_player=False, psk_category=PskCategory.SENTINEL
    )
    await provider.pair_device_with_token(_TOKEN)
    assert [a.pairing_psk for a in api.attempts] == [_PAIRING_PSK]
    assert refreshed == [_CLIENT_ID]


async def test_pair_with_token_refuses_an_unencrypted_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unencrypted device cannot hold a pairing, which is reported instead of faking success."""
    api = _WebPlayerServerApi()
    provider, refreshed = _make_web_provider(
        api, monkeypatch, is_web_player=False, psk_category=None
    )
    with pytest.raises(InvalidCommand) as excinfo:
        await provider.pair_device_with_token(_TOKEN)
    assert excinfo.value.translation_key == "pairing_error_unencrypted"
    assert api.attempts == []
    assert refreshed == []


async def test_pair_with_token_reports_a_failure_without_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed attempt surfaces as a translated error that leaves the token out."""
    api = _WebPlayerServerApi()
    provider, _refreshed = _make_web_provider(api, monkeypatch, is_web_player=False)
    api.outcomes.append(PairingError("device refused the PSK"))
    with pytest.raises(InvalidCommand) as excinfo:
        await provider.pair_device_with_token(_TOKEN)
    assert excinfo.value.translation_key == "pairing_error_failed"
    assert _TOKEN not in str(excinfo.value)
    assert all(_TOKEN not in str(arg) for arg in excinfo.value.translation_args or ())


async def test_pair_with_token_requires_the_player_config_scope() -> None:
    """Pairing an arbitrary device is a configuration change, so guests cannot call it."""
    provider, mass, _server_api = _make_loadable_provider(AsyncMock())
    await provider.loaded_in_mass()
    scopes = {
        call.args[0]: call.kwargs["required_scope"]
        for call in mass.register_api_command.call_args_list
    }
    assert scopes["sendspin/pair_with_token"] == Scope.CONFIG_PLAYERS_WRITE
    assert scopes["sendspin/is_paired"] == Scope.CONFIG_PLAYERS_READ
    await provider.unload()


def _with_connection_security(
    api: _WebPlayerServerApi, psk_category: PskCategory | None
) -> _WebPlayerServerApi:
    security = None if psk_category is None else SimpleNamespace(psk_category=psk_category)
    cast("Any", api._client).connection_security = security
    return api


async def test_is_paired_for_a_connected_long_term_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored pairing the live connection authenticates with counts as paired."""
    api = _with_connection_security(_WebPlayerServerApi(paired=True), PskCategory.LONG_TERM)
    provider, _refreshed = _make_web_provider(api, monkeypatch)
    assert await provider.is_paired(_CLIENT_ID) is True


async def test_is_paired_rejects_a_client_that_lost_its_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored pairing the connected client no longer holds its half of is not paired."""
    api = _with_connection_security(_WebPlayerServerApi(paired=True), PskCategory.SENTINEL)
    provider, _refreshed = _make_web_provider(api, monkeypatch)
    assert await provider.is_paired(_CLIENT_ID) is False


async def test_is_paired_rejects_an_unencrypted_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client connected without encryption is not paired, whatever the store holds."""
    api = _with_connection_security(_WebPlayerServerApi(paired=True), None)
    provider, _refreshed = _make_web_provider(api, monkeypatch)
    assert await provider.is_paired(_CLIENT_ID) is False


async def test_is_paired_trusts_the_record_of_a_disconnected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that has not connected yet is answered from the store, without waiting."""
    api = _WebPlayerServerApi(paired=True, connected=False)
    provider, _refreshed = _make_web_provider(api, monkeypatch)
    # Any wait for the connection would hit this timeout instead of returning.
    monkeypatch.setattr(provider_module, "WEB_PLAYER_CONNECT_TIMEOUT", 60.0)
    async with asyncio.timeout(1):
        assert await provider.is_paired(_CLIENT_ID) is True


async def test_is_paired_for_an_unknown_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client without a stored pairing is not paired, connected or not."""
    api = _WebPlayerServerApi(paired=False)
    provider, _refreshed = _make_web_provider(api, monkeypatch)
    monkeypatch.setattr(api, "get_client", lambda _client_id: None)
    assert await provider.is_paired(_CLIENT_ID) is False
