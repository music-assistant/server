"""Tests for pairing any Sendspin device from its pairing token through the API."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiosendspin.models.types import PairMethod
from aiosendspin.noise.keys import b64url_encode
from aiosendspin.noise.pairing import PairingError
from aiosendspin.noise.pairing_token import PSKPairingToken, encode_psk_token
from aiosendspin.noise.trust_store import PskCategory
from music_assistant_models.auth import Scope, User, UserRole
from music_assistant_models.errors import InvalidCommand

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
    await provider.unload()
