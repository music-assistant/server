"""Tests for pairing players on the Home Assistant host with the token they serve locally."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast
from unittest.mock import Mock

import pytest
from aiohttp import ClientConnectionError
from aiosendspin.models.types import PairMethod
from aiosendspin.noise.keys import PSK_SIZE, b64url_encode
from aiosendspin.noise.pairing import PairingError
from aiosendspin.noise.pairing_token import PSKPairingToken, encode_psk_token
from aiosendspin.noise.trust_store import PskCategory
from aiosendspin.server import ClientConnectedEvent

from music_assistant.providers.sendspin.constants import CONF_LOCAL_PAIRING
from music_assistant.providers.sendspin.local_pair import (
    LOCAL_PAIRING_MAX_BODY,
    LOCAL_PAIRING_SUPPORT_KEY,
    LocalAutoPairer,
)

from .test_pin_session import _FakeServerApi, _make_provider, _offer
from .test_refresh_player import _make_provider as _make_event_provider
from .test_refresh_player import _mock_player
from .test_web_player_pairing import _FakePairingStore

if TYPE_CHECKING:
    from aiosendspin.models.core import SupportedPairMethods
    from aiosendspin.server import SendspinServer

CLIENT_ID = b64url_encode(bytes(range(32)))
OTHER_CLIENT_ID = b64url_encode(bytes(range(1, 33)))
PAIRING_PSK = bytes(range(100, 100 + PSK_SIZE))
TOKEN = encode_psk_token(PSKPairingToken(client_id=CLIENT_ID, pairing_psk=PAIRING_PSK))
OTHER_TOKEN = encode_psk_token(PSKPairingToken(client_id=OTHER_CLIENT_ID, pairing_psk=PAIRING_PSK))
PSK_METHOD = _offer(PairMethod.PAIRING_PSK)
PORT = 8928


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, n: int) -> bytes:
        # hand out one byte at a time, like a stream split across reads
        chunk, self._body = self._body[: min(n, 1)], self._body[min(n, 1) :]
        return chunk


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content = _FakeContent(body)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeHttpSession:
    """Answers every GET with one scripted response, or raises a scripted error."""

    def __init__(
        self, body: bytes = TOKEN.encode(), status: int = 200, error: BaseException | None = None
    ) -> None:
        self.body = body
        self.status = status
        self.error = error
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.requests.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.status, self.body)


def _make_pairer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    http: _FakeHttpSession | None = None,
    support: Any = None,
    methods: SupportedPairMethods = PSK_METHOD,
    psk_category: PskCategory | None = PskCategory.SENTINEL,
    paired: bool = False,
    connected: bool = True,
    addon: bool = True,
    enabled: bool = True,
) -> tuple[LocalAutoPairer, _FakeServerApi, _FakeHttpSession]:
    api = _FakeServerApi(methods, await_pin=False, connected=connected)
    client = cast("Any", api._client)
    if not connected:
        client.connection = None
    cast("Any", api).pairing_store = _FakePairingStore(paired=paired)
    client.connection_security = (
        None if psk_category is None else SimpleNamespace(psk_category=psk_category)
    )
    client.info_or_none.application_objects = {
        LOCAL_PAIRING_SUPPORT_KEY: {"port": PORT} if support is None else support
    }
    provider, _ = _make_provider(api, monkeypatch)
    http = http or _FakeHttpSession()
    mass = cast("Any", provider.mass)
    mass.http_session = http
    mass.running_as_hass_addon = addon
    provider.config = cast(
        "Any",
        SimpleNamespace(get_value=lambda key, _default=None: {CONF_LOCAL_PAIRING: enabled}[key]),
    )
    return LocalAutoPairer(provider), api, http


async def _settle(pairer: LocalAutoPairer) -> None:
    """Let the scheduled pairing attempts, and any follow-ups they start, finish."""
    while pairer._tasks:
        await asyncio.gather(*pairer._tasks.values())
        await asyncio.sleep(0)


async def test_pairs_with_the_locally_served_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token from the hassio host at the announced port becomes a pairing PSK attempt."""
    pairer, api, http = _make_pairer(monkeypatch)
    pairer.on_client_connected(CLIENT_ID)
    await _settle(pairer)
    assert len(http.requests) == 1
    url, kwargs = http.requests[0]
    assert url == f"http://172.30.32.1:{PORT}/"
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"].total == 5
    assert len(api.attempts) == 1
    assert api.attempts[0].method is PairMethod.PAIRING_PSK
    assert api.attempts[0].pairing_psk == PAIRING_PSK


async def test_surrounding_whitespace_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A trailing newline from the endpoint does not break the token."""
    pairer, api, _ = _make_pairer(monkeypatch, http=_FakeHttpSession(f"{TOKEN}\n".encode()))
    await pairer._attempt(CLIENT_ID)
    assert len(api.attempts) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"addon": False}, id="not_addon"),
        pytest.param({"enabled": False}, id="disabled"),
    ],
)
async def test_only_active_on_the_addon_while_enabled(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    """Outside the add-on there is no hassio network; the hidden flag turns it off there too."""
    pairer, api, http = _make_pairer(monkeypatch, **kwargs)
    pairer.on_client_connected(CLIENT_ID)
    assert pairer._tasks == {}
    assert http.requests == []
    assert api.attempts == []


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"psk_category": PskCategory.LONG_TERM, "paired": True}, id="paired"),
        pytest.param({"psk_category": None}, id="unencrypted"),
        pytest.param({"connected": False}, id="disconnected"),
        pytest.param({"methods": _offer(PairMethod.DYNAMIC_PAIRING_CODE)}, id="no_psk_method"),
    ],
)
async def test_clients_with_nothing_to_pair_are_left_alone(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    """Nothing is fetched when the client cannot or need not be paired."""
    pairer, api, http = _make_pairer(monkeypatch, **kwargs)
    await pairer._attempt(CLIENT_ID)
    assert http.requests == []
    assert api.attempts == []


@pytest.mark.parametrize(
    "support",
    [
        pytest.param({}, id="no_port"),
        pytest.param({"port": "8928"}, id="string"),
        pytest.param({"port": True}, id="bool"),
        pytest.param({"port": 80}, id="privileged"),
        pytest.param({"port": 65536}, id="too_high"),
        pytest.param([PORT], id="not_an_object"),
    ],
)
async def test_an_invalid_port_is_never_asked(
    monkeypatch: pytest.MonkeyPatch, support: Any
) -> None:
    """Only an int port in 1024-65535 is fetched from."""
    pairer, api, http = _make_pairer(monkeypatch, support=support)
    await pairer._attempt(CLIENT_ID)
    assert http.requests == []
    assert api.attempts == []


async def test_an_unannounced_client_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client whose hello does not carry the local pairing role is not fetched for."""
    pairer, api, http = _make_pairer(monkeypatch)
    cast("Any", api._client).info_or_none.application_objects = {}
    await pairer._attempt(CLIENT_ID)
    cast("Any", api._client).info_or_none = None
    await pairer._attempt(CLIENT_ID)
    assert http.requests == []
    assert api.attempts == []


async def test_a_pairing_the_user_started_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running manual pairing attempt is not interrupted."""
    pairer, api, http = _make_pairer(monkeypatch)
    pairer.provider._pin_sessions[CLIENT_ID] = cast("Any", SimpleNamespace(attempt_running=True))
    await pairer._attempt(CLIENT_ID)
    assert http.requests == []
    assert api.attempts == []


async def test_a_token_of_another_client_is_rejected(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A token naming a different client never reaches the handshake, nor the log."""
    pairer, api, _ = _make_pairer(monkeypatch, http=_FakeHttpSession(OTHER_TOKEN.encode()))
    with caplog.at_level(logging.DEBUG, logger="test.sendspin.pin"):
        await pairer._attempt(CLIENT_ID)
    assert api.attempts == []
    assert "pairing_error_token_mismatch" in caplog.text
    assert OTHER_TOKEN not in caplog.text


@pytest.mark.parametrize(
    "http",
    [
        pytest.param(_FakeHttpSession(status=404), id="not_found"),
        pytest.param(_FakeHttpSession(status=302), id="redirect"),
        pytest.param(_FakeHttpSession(error=ClientConnectionError("refused")), id="refused"),
        pytest.param(_FakeHttpSession(error=TimeoutError()), id="timeout"),
        pytest.param(_FakeHttpSession(b"x" * (LOCAL_PAIRING_MAX_BODY + 1)), id="too_long"),
        pytest.param(_FakeHttpSession(b"\xff\xfe"), id="not_text"),
    ],
)
async def test_a_failed_fetch_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, http: _FakeHttpSession
) -> None:
    """Without a usable answer nothing is paired, and one warning says why."""
    pairer, api, _ = _make_pairer(monkeypatch, http=http)
    with caplog.at_level(logging.WARNING, logger="test.sendspin.pin"):
        await pairer._attempt(CLIENT_ID)
    assert api.attempts == []
    assert len(caplog.records) == 1
    assert "Fetching the local token" in caplog.text


async def test_a_failed_pairing_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected token is reported without the token itself."""
    pairer, api, _ = _make_pairer(monkeypatch)
    api.outcomes.append(PairingError("rejected"))
    with caplog.at_level(logging.DEBUG, logger="test.sendspin.pin"):
        await pairer._attempt(CLIENT_ID)
    assert len(api.attempts) == 1
    assert "rejected" in caplog.text
    assert TOKEN not in caplog.text


async def test_a_trigger_during_an_attempt_is_tried_again_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client triggered while its attempt runs gets one more attempt once that one ends."""
    pairer, api, http = _make_pairer(monkeypatch)
    first_asked = asyncio.Event()
    release_first = asyncio.Event()
    fetch = pairer._fetch_token

    async def _token_on_the_second_ask(client_id: str, port: int) -> str | None:
        if first_asked.is_set():
            return await fetch(client_id, port)
        first_asked.set()
        await release_first.wait()
        return None

    monkeypatch.setattr(pairer, "_fetch_token", _token_on_the_second_ask)
    pairer.on_client_connected(CLIENT_ID)
    await first_asked.wait()
    pairer.on_client_connected(CLIENT_ID)
    pairer.on_client_connected(CLIENT_ID)
    assert pairer._pending == {CLIENT_ID}
    release_first.set()
    await _settle(pairer)
    assert len(http.requests) == 1
    assert len(api.attempts) == 1
    assert pairer._pending == set()


async def test_closing_stops_running_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unloading cancels an attempt, nothing is left behind, and later connects start nothing."""
    pairer, api, _ = _make_pairer(monkeypatch)
    asked = asyncio.Event()

    async def _never_answered(_client_id: str, _port: int) -> None:
        asked.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(pairer, "_fetch_token", _never_answered)
    pairer.on_client_connected(CLIENT_ID)
    await asked.wait()
    pairer.on_client_connected(CLIENT_ID)
    task = pairer._tasks[CLIENT_ID]
    pairer.close()
    await asyncio.sleep(0)
    assert task.cancelled()
    assert pairer._tasks == {}
    assert pairer._pending == set()

    pairer.on_client_connected(CLIENT_ID)
    assert pairer._tasks == {}
    assert api.attempts == []


async def test_every_connection_triggers_an_attempt() -> None:
    """A reconnect that reuses the player triggers the check too, once the hello is in."""
    provider = _make_event_provider(_mock_player())
    pairer = Mock()
    provider._local_auto_pairer = pairer
    provider.event_cb(cast("SendspinServer", None), ClientConnectedEvent(CLIENT_ID))
    pairer.on_client_connected.assert_not_called()
    await asyncio.sleep(0)
    pairer.on_client_connected.assert_called_once_with(CLIENT_ID)
