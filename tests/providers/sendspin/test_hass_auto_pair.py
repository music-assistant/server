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
from aiosendspin.server import ClientConnectedEvent

from music_assistant.providers.sendspin.hass_auto_pair import HassAutoPairer

from .test_pin_session import _FakeServerApi, _make_provider, _offer
from .test_refresh_player import _make_provider as _make_event_provider
from .test_refresh_player import _mock_player
from .test_web_player_pairing import _FakePairingStore

if TYPE_CHECKING:
    from aiosendspin.models.core import SupportedPairMethods
    from aiosendspin.server import SendspinServer

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
    if not connected:
        cast("Any", api._client).connection = None
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


def _announce(pairer: HassAutoPairer, client_ids: list[str]) -> None:
    """Deliver a snapshot for the live subscription."""
    pairer._on_clients(pairer._generation, client_ids)


async def _settle(pairer: HassAutoPairer) -> None:
    """Let the scheduled pairing attempts, and any follow-ups they start, finish."""
    while pairer._tasks:
        await asyncio.gather(*pairer._tasks.values())
        await asyncio.sleep(0)


async def _subscribe(pairer: HassAutoPairer) -> None:
    """Subscribe and let the subscription request complete."""
    pairer.subscribe()
    await asyncio.sleep(0)


async def test_pairs_with_the_token_from_home_assistant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token Home Assistant fetched is turned into a pairing PSK attempt."""
    hass = _hass()
    pairer, api, refreshed = _make_pairer(monkeypatch, hass)
    await pairer._attempt(CLIENT_ID)
    hass.get_sendspin_pairing_token.assert_awaited_once_with(CLIENT_ID)
    assert len(api.attempts) == 1
    assert api.attempts[0].method is PairMethod.PAIRING_PSK
    assert api.attempts[0].pairing_psk == PAIRING_PSK
    assert refreshed == [CLIENT_ID]


async def test_pairs_before_the_initial_state_arrived(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client whose hello is in but whose first state is not yet is paired anyway."""
    hass = _hass()
    pairer, api, _ = _make_pairer(monkeypatch, hass)
    cast("Any", api._client).is_connected = False
    await pairer._attempt(CLIENT_ID)
    assert len(api.attempts) == 1


async def test_no_token_leaves_the_client_for_the_next_announcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a token nothing is retried; the next snapshot from Home Assistant asks again."""
    hass = _hass(None)
    pairer, api, _ = _make_pairer(monkeypatch, hass)
    await pairer._attempt(CLIENT_ID)
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
    await pairer._attempt(CLIENT_ID)
    hass.get_sendspin_pairing_token.assert_not_awaited()
    assert api.attempts == []


async def test_a_pairing_the_user_started_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running manual pairing attempt is not interrupted."""
    hass = _hass()
    pairer, api, _ = _make_pairer(monkeypatch, hass)
    pairer.provider._pin_sessions[CLIENT_ID] = cast("Any", SimpleNamespace(attempt_running=True))
    await pairer._attempt(CLIENT_ID)
    hass.get_sendspin_pairing_token.assert_not_awaited()
    assert api.attempts == []


@pytest.mark.parametrize("hass", [None, _hass(available=False)])
async def test_requires_an_available_home_assistant_provider(
    monkeypatch: pytest.MonkeyPatch, hass: SimpleNamespace | None
) -> None:
    """Without a connected Home Assistant provider there is nowhere to get the token."""
    pairer, api, _ = _make_pairer(monkeypatch, hass)
    await pairer._attempt(CLIENT_ID)
    assert api.attempts == []


async def test_a_failed_pairing_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected token is reported without the token itself."""
    hass = _hass()
    pairer, api, _ = _make_pairer(monkeypatch, hass)
    api.outcomes.append(PairingError("rejected"))
    with caplog.at_level(logging.WARNING, logger="test.sendspin.pin"):
        await pairer._attempt(CLIENT_ID)
    assert len(api.attempts) == 1
    assert "rejected" in caplog.text
    assert TOKEN not in caplog.text


async def test_only_announced_clients_are_paired_on_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connecting client is paired only once Home Assistant announced its id."""
    hass = _hass()
    pairer, api, _ = _make_pairer(monkeypatch, hass, announced=False)
    pairer.on_client_connected(CLIENT_ID)
    assert pairer._tasks == {}

    pairer._announced = {CLIENT_ID}
    pairer.on_client_connected(CLIENT_ID)
    assert list(pairer._tasks) == [CLIENT_ID]
    await _settle(pairer)
    assert len(api.attempts) == 1
    assert pairer._tasks == {}


async def test_newly_announced_clients_are_paired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each snapshot from Home Assistant pairs the clients it did not list before."""
    hass = _hass()
    pairer, api, _ = _make_pairer(monkeypatch, hass, announced=False)
    _announce(pairer, ["other", CLIENT_ID])
    assert pairer._announced == {"other", CLIENT_ID}
    assert set(pairer._tasks) == {"other", CLIENT_ID}
    await _settle(pairer)
    asked = {call.args[0] for call in hass.get_sendspin_pairing_token.await_args_list}
    assert asked == {"other", CLIENT_ID}
    assert len(api.attempts) == 1

    # the same snapshot again changes nothing; a device that drops off and comes
    # back is announced anew and paired again if it still needs it
    hass.get_sendspin_pairing_token.reset_mock()
    _announce(pairer, [CLIENT_ID, "other"])
    assert pairer._tasks == {}
    _announce(pairer, ["other"])
    _announce(pairer, ["other", CLIENT_ID])
    assert list(pairer._tasks) == [CLIENT_ID]
    await _settle(pairer)
    hass.get_sendspin_pairing_token.assert_awaited_once_with(CLIENT_ID)


async def test_follows_home_assistant_while_it_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subscription lives as long as the Home Assistant provider is connected."""
    hass = _hass()
    pairer, _, _ = _make_pairer(monkeypatch, hass, announced=False)
    await _subscribe(pairer)
    unsubscribe = hass.subscribe_sendspin_clients.return_value
    assert pairer._unsubscribe == unsubscribe
    on_clients = hass.subscribe_sendspin_clients.await_args.args[0]

    on_clients([CLIENT_ID])
    assert pairer._announced == {CLIENT_ID}
    task = pairer._tasks[CLIENT_ID]
    pairer.unsubscribe()
    assert unsubscribe.call_count == 1
    assert pairer._unsubscribe is None
    assert pairer._announced == set()
    # an attempt already talking to the device is left to finish
    assert not task.cancelling()
    await _settle(pairer)

    # a snapshot of the torn down subscription that was still in flight is ignored
    on_clients([CLIENT_ID])
    assert pairer._announced == set()
    assert pairer._tasks == {}


async def test_a_subscription_answered_after_teardown_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsubscribing while the subscribe request is in flight still ends the subscription."""
    hass = _hass()
    pairer, _, _ = _make_pairer(monkeypatch, hass, announced=False)
    unsubscribe = cast("Mock", hass.subscribe_sendspin_clients.return_value)

    async def _unsubscribe_meanwhile(_callback: Any) -> Mock:
        pairer.unsubscribe()
        return unsubscribe

    hass.subscribe_sendspin_clients.side_effect = _unsubscribe_meanwhile
    await _subscribe(pairer)
    assert unsubscribe.call_count == 1
    assert pairer._unsubscribe is None


async def test_the_provider_never_waits_for_home_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscribing returns at once; an answer after teardown ends its own subscription."""
    hass = _hass()
    pairer, _, _ = _make_pairer(monkeypatch, hass, announced=False)
    answered = asyncio.Event()

    async def _answer_when_released(_callback: Any) -> Mock:
        await answered.wait()
        return cast("Mock", hass.subscribe_sendspin_clients.return_value)

    hass.subscribe_sendspin_clients.side_effect = _answer_when_released
    pairer.subscribe()
    await asyncio.sleep(0)
    hass.subscribe_sendspin_clients.assert_awaited_once()

    pairer.unsubscribe()
    answered.set()
    await asyncio.sleep(0)
    assert hass.subscribe_sendspin_clients.return_value.call_count == 1
    assert pairer._unsubscribe is None


async def test_a_trigger_during_an_attempt_is_tried_again_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client triggered while its attempt runs gets one more attempt once that one ends."""
    hass = _hass()
    pairer, api, _ = _make_pairer(monkeypatch, hass)
    first_attempt_asked = asyncio.Event()
    release_first_attempt = asyncio.Event()

    async def _token_on_the_second_ask(_client_id: str) -> str | None:
        if first_attempt_asked.is_set():
            return TOKEN
        first_attempt_asked.set()
        await release_first_attempt.wait()
        return None

    hass.get_sendspin_pairing_token.side_effect = _token_on_the_second_ask
    pairer.on_client_connected(CLIENT_ID)
    await first_attempt_asked.wait()
    pairer.on_client_connected(CLIENT_ID)
    pairer.on_client_connected(CLIENT_ID)
    assert pairer._pending == {CLIENT_ID}
    release_first_attempt.set()
    await _settle(pairer)
    assert hass.get_sendspin_pairing_token.await_count == 2
    assert len(api.attempts) == 1
    assert pairer._pending == set()


async def test_closing_stops_running_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unloading cancels an attempt and nothing is left behind, triggered again or not."""
    hass = _hass()
    pairer, api, _ = _make_pairer(monkeypatch, hass)
    asked = asyncio.Event()

    async def _never_answered(_client_id: str) -> None:
        asked.set()
        await asyncio.Event().wait()

    hass.get_sendspin_pairing_token.side_effect = _never_answered
    pairer.on_client_connected(CLIENT_ID)
    await asked.wait()
    pairer.on_client_connected(CLIENT_ID)
    task = pairer._tasks[CLIENT_ID]
    pairer.close()
    await asyncio.sleep(0)
    assert task.cancelled()
    assert pairer._tasks == {}
    assert pairer._pending == set()
    assert api.attempts == []


async def test_an_attempt_over_before_it_was_stored_does_not_linger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With eagerly started tasks, an announcement before the client connects leaves no trace."""
    hass = _hass()
    pairer, api, _ = _make_pairer(monkeypatch, hass, connected=False, announced=False)
    loop = asyncio.get_running_loop()
    cast("Any", pairer.provider.mass).create_task = lambda coro: asyncio.Task(
        coro, loop=loop, eager_start=True
    )
    _announce(pairer, [CLIENT_ID])
    assert pairer._tasks == {}
    assert pairer._pending == set()

    cast("Any", api._client).connection = api.connection
    pairer.on_client_connected(CLIENT_ID)
    await _settle(pairer)
    assert len(api.attempts) == 1


async def test_a_closed_pairer_does_not_subscribe_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late subscribe after the provider unloaded starts nothing."""
    hass = _hass()
    pairer, _, _ = _make_pairer(monkeypatch, hass, announced=False)
    await _subscribe(pairer)
    unsubscribe = hass.subscribe_sendspin_clients.return_value
    pairer.close()
    assert unsubscribe.call_count == 1

    pairer.subscribe()
    await asyncio.sleep(0)
    hass.subscribe_sendspin_clients.assert_awaited_once()


async def test_subscribing_needs_an_available_home_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a connected Home Assistant provider there is nothing to follow."""
    hass = _hass(available=False)
    pairer, _, _ = _make_pairer(monkeypatch, hass, announced=False)
    pairer.subscribe()
    await asyncio.sleep(0)
    hass.subscribe_sendspin_clients.assert_not_awaited()


async def test_every_connection_triggers_an_attempt() -> None:
    """A reconnect that reuses the player triggers the check too, once the hello is in."""
    provider = _make_event_provider(_mock_player())
    pairer = Mock()
    provider._hass_auto_pairer = pairer
    provider.event_cb(cast("SendspinServer", None), ClientConnectedEvent(CLIENT_ID))
    pairer.on_client_connected.assert_not_called()
    await asyncio.sleep(0)
    pairer.on_client_connected.assert_called_once_with(CLIENT_ID)
