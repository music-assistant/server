"""Tests for pairing clients with the pairing token Home Assistant reads from their device."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiosendspin.models.types import PairMethod
from aiosendspin.noise.keys import PSK_SIZE, b64url_encode
from aiosendspin.noise.pairing import PairingError
from aiosendspin.noise.pairing_token import PSKPairingToken, encode_psk_token
from aiosendspin.noise.trust_store import PskCategory

from music_assistant.providers.sendspin.hass_auto_pair import HassAutoPairer

from .test_pin_session import _FakeServerApi, _make_provider, _offer
from .test_web_player_pairing import _FakePairingStore

if TYPE_CHECKING:
    from aiosendspin.models.core import SupportedPairMethods

CLIENT_ID = b64url_encode(bytes(range(32)))
PAIRING_PSK = bytes(range(100, 100 + PSK_SIZE))
TOKEN = encode_psk_token(PSKPairingToken(client_id=CLIENT_ID, pairing_psk=PAIRING_PSK))
PSK_METHOD = _offer(PairMethod.PAIRING_PSK)


def _hass(token: str | None = TOKEN, *, available: bool = True) -> SimpleNamespace:
    """Home Assistant provider stand-in answering the token lookup."""
    return SimpleNamespace(
        available=available,
        get_sendspin_pairing_token=AsyncMock(return_value=token),
        subscribe_sendspin_clients=AsyncMock(return_value=Mock()),
    )


def _make_pairer(
    monkeypatch: pytest.MonkeyPatch,
    hass: SimpleNamespace | None,
    *,
    methods: SupportedPairMethods = PSK_METHOD,
    psk_category: PskCategory | None = PskCategory.SENTINEL,
    paired: bool = False,
    connected: bool = True,
    announced: bool = True,
) -> tuple[HassAutoPairer, _FakeServerApi, list[str]]:
    api = _FakeServerApi(methods, await_pin=False, connected=connected)
    cast("Any", api).pairing_store = _FakePairingStore(paired=paired)
    cast("Any", api._client).connection_security = (
        None if psk_category is None else SimpleNamespace(psk_category=psk_category)
    )
    provider, refreshed = _make_provider(api, monkeypatch)
    cast("Any", provider.mass).get_provider = lambda _domain: hass
    pairer = HassAutoPairer(provider)
    if announced:
        pairer._announced = {CLIENT_ID}
    return pairer, api, refreshed


async def _settle(pairer: HassAutoPairer) -> None:
    """Let the scheduled pairing attempts finish."""
    await asyncio.gather(*pairer._tasks.values())
    await asyncio.sleep(0)


async def test_pairs_with_the_token_from_home_assistant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token Home Assistant fetched is turned into a pairing PSK attempt."""
    hass = _hass()
    pairer, api, refreshed = _make_pairer(monkeypatch, hass)
    await pairer._pair(CLIENT_ID)
    hass.get_sendspin_pairing_token.assert_awaited_once_with(CLIENT_ID)
    assert len(api.attempts) == 1
    assert api.attempts[0].method is PairMethod.PAIRING_PSK
    assert api.attempts[0].pairing_psk == PAIRING_PSK
    assert refreshed == [CLIENT_ID]


async def test_no_token_leaves_the_client_for_the_next_announcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a token nothing is retried; the next snapshot from Home Assistant asks again."""
    hass = _hass(None)
    pairer, api, _ = _make_pairer(monkeypatch, hass)
    await pairer._pair(CLIENT_ID)
    hass.get_sendspin_pairing_token.assert_awaited_once_with(CLIENT_ID)
    assert api.attempts == []


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"psk_category": PskCategory.LONG_TERM, "paired": True}, id="paired"),
        pytest.param({"psk_category": None}, id="unencrypted"),
        pytest.param({"connected": False}, id="disconnected"),
        pytest.param(
            {"methods": _offer(PairMethod.DYNAMIC_PAIRING_CODE)},
            id="no_psk_method",
        ),
    ],
)
async def test_clients_with_nothing_to_pair_are_left_alone(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    """Home Assistant is not even asked when the client cannot or need not be paired."""
    hass = _hass()
    pairer, api, _ = _make_pairer(monkeypatch, hass, **kwargs)
    await pairer._pair(CLIENT_ID)
    hass.get_sendspin_pairing_token.assert_not_awaited()
    assert api.attempts == []


async def test_a_pairing_the_user_started_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running manual pairing attempt is not interrupted."""
    hass = _hass()
    pairer, api, _ = _make_pairer(monkeypatch, hass)
    pairer.provider._pin_sessions[CLIENT_ID] = cast("Any", SimpleNamespace(attempt_running=True))
    await pairer._pair(CLIENT_ID)
    hass.get_sendspin_pairing_token.assert_not_awaited()
    assert api.attempts == []


@pytest.mark.parametrize("hass", [None, _hass(available=False)])
async def test_requires_an_available_home_assistant_provider(
    monkeypatch: pytest.MonkeyPatch, hass: SimpleNamespace | None
) -> None:
    """Without a connected Home Assistant provider there is nowhere to get the token."""
    pairer, api, _ = _make_pairer(monkeypatch, hass)
    await pairer._pair(CLIENT_ID)
    assert api.attempts == []


async def test_a_failed_pairing_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected token is reported without the token itself."""
    hass = _hass()
    pairer, api, _ = _make_pairer(monkeypatch, hass)
    api.outcomes.append(PairingError("rejected"))
    with caplog.at_level(logging.WARNING, logger="test.sendspin.pin"):
        await pairer._pair(CLIENT_ID)
    assert len(api.attempts) == 1
    assert "rejected" in caplog.text
    assert TOKEN not in caplog.text


async def test_only_announced_clients_are_paired_on_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connecting client is paired only once Home Assistant announced its id, and once."""
    hass = _hass()
    pairer, api, _ = _make_pairer(monkeypatch, hass, announced=False)
    pairer.on_client_connected(CLIENT_ID)
    assert pairer._tasks == {}

    pairer._announced = {CLIENT_ID}
    pairer.on_client_connected(CLIENT_ID)
    pairer.on_client_connected(CLIENT_ID)
    assert list(pairer._tasks) == [CLIENT_ID]
    await _settle(pairer)
    assert len(api.attempts) == 1
    assert pairer._tasks == {}


async def test_newly_announced_clients_are_paired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each snapshot from Home Assistant pairs the clients it did not list before."""
    hass = _hass()
    pairer, api, _ = _make_pairer(monkeypatch, hass, announced=False)
    pairer._on_clients(["other", CLIENT_ID])
    assert pairer._announced == {"other", CLIENT_ID}
    assert set(pairer._tasks) == {"other", CLIENT_ID}
    await _settle(pairer)
    asked = {call.args[0] for call in hass.get_sendspin_pairing_token.await_args_list}
    assert asked == {"other", CLIENT_ID}
    assert len(api.attempts) == 1

    # the same snapshot again changes nothing; a device that drops off and comes
    # back is announced anew and paired again if it still needs it
    hass.get_sendspin_pairing_token.reset_mock()
    pairer._on_clients([CLIENT_ID, "other"])
    assert pairer._tasks == {}
    pairer._on_clients(["other"])
    pairer._on_clients(["other", CLIENT_ID])
    assert list(pairer._tasks) == [CLIENT_ID]
    await _settle(pairer)
    hass.get_sendspin_pairing_token.assert_awaited_once_with(CLIENT_ID)


async def test_follows_home_assistant_while_it_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subscription lives as long as the Home Assistant provider is connected."""
    hass = _hass()
    pairer, _, _ = _make_pairer(monkeypatch, hass, announced=False)
    await pairer.subscribe()
    hass.subscribe_sendspin_clients.assert_awaited_once_with(pairer._on_clients)
    unsubscribe = hass.subscribe_sendspin_clients.return_value
    assert pairer._unsubscribe is unsubscribe

    pairer._on_clients([CLIENT_ID])
    task = pairer._tasks[CLIENT_ID]
    pairer.unsubscribe()
    unsubscribe.assert_called_once_with()
    assert pairer._unsubscribe is None
    assert pairer._announced == set()
    assert task.cancelling()
    assert pairer._tasks == {}


async def test_subscribing_needs_an_available_home_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a connected Home Assistant provider there is nothing to follow."""
    hass = _hass(available=False)
    pairer, _, _ = _make_pairer(monkeypatch, hass, announced=False)
    await pairer.subscribe()
    hass.subscribe_sendspin_clients.assert_not_awaited()
    assert pairer._unsubscribe is None
