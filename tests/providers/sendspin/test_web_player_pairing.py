"""Tests for pairing the built-in web player from its own pairing token."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

import pytest
from aiosendspin.models.types import PairMethod
from aiosendspin.noise.keys import PSK_SIZE, b64url_encode
from aiosendspin.noise.pairing import PairingError
from aiosendspin.noise.pairing_token import PSKPairingToken, encode_token
from music_assistant_models.errors import InvalidCommand

import music_assistant.providers.sendspin.provider as provider_module
from music_assistant.providers.sendspin.player import SendspinBasePlayer

from .test_pin_session import _FakeServerApi, _make_provider

if TYPE_CHECKING:
    from music_assistant.providers.sendspin.provider import SendspinProvider

_CLIENT_ID = b64url_encode(bytes(range(32)))
_PAIRING_PSK = bytes(range(100, 100 + PSK_SIZE))
_TOKEN = encode_token(PSKPairingToken(client_id=_CLIENT_ID, pairing_psk=_PAIRING_PSK))


class _FakePairingStore:
    """Pairing store stand-in answering only the pairing-record lookup."""

    def __init__(self, *, paired: bool) -> None:
        self._record = SimpleNamespace() if paired else None

    async def record_by_client_id(self, client_id: str) -> Any:
        return self._record


class _WebPlayerServerApi(_FakeServerApi):
    """_FakeServerApi with the pairing surface the web-player entry point uses."""

    def __init__(self, *, connected: bool = True, paired: bool = False) -> None:
        super().__init__([], await_pin=False, connected=connected)
        self.pairing_store = _FakePairingStore(paired=paired)


def _make_web_provider(
    api: _WebPlayerServerApi,
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_web_player: bool = True,
    registered: bool = True,
    initialized: bool = True,
) -> tuple[SendspinProvider, list[str]]:
    provider, refreshed = _make_provider(api, monkeypatch)
    # the provider resolves its translation namespace through the manifest
    cast("Any", provider).manifest = SimpleNamespace(domain="sendspin")
    player = Mock(spec=SendspinBasePlayer)
    player.is_web_player = is_web_player
    player.initialized.is_set.return_value = initialized
    cast("Any", provider.mass).players = SimpleNamespace(
        get_player=lambda _client_id: player if registered else None
    )
    monkeypatch.setattr(provider_module, "WEB_PLAYER_CONNECT_TIMEOUT", 0.3)
    return provider, refreshed


async def test_pair_web_player_pairs_with_the_token_psk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token's PSK is handed to the pairing attempt for the session's own client."""
    api = _WebPlayerServerApi()
    provider, refreshed = _make_web_provider(api, monkeypatch)
    await provider.pair_web_player(_TOKEN)
    assert [(a.method, a.pairing_psk) for a in api.attempts] == [
        (PairMethod.PAIRING_PSK, _PAIRING_PSK)
    ]
    assert refreshed == [_CLIENT_ID]


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


async def test_pair_web_player_reports_a_pairing_failure_without_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed attempt surfaces as a typed error, keeping the token out of the error path."""
    api = _WebPlayerServerApi()
    provider, _refreshed = _make_web_provider(api, monkeypatch)
    api.outcomes.append(PairingError("device refused the PSK"))
    with pytest.raises(InvalidCommand, match="pairing_error_failed") as excinfo:
        await provider.pair_web_player(_TOKEN)
    assert _TOKEN not in str(excinfo.value)


async def test_pair_web_player_is_a_no_op_when_already_paired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A web player calling on every mount does not re-handshake an existing pairing."""
    api = _WebPlayerServerApi(paired=True)
    provider, refreshed = _make_web_provider(api, monkeypatch)
    await provider.pair_web_player(_TOKEN)
    assert api.attempts == []
    assert refreshed == []
