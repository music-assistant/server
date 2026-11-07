"""Tests for the Sendspin WebSocket proxy handler."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import ClientConnectorError, web
from aiohttp.test_utils import TestServer, make_mocked_request
from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.webserver import sendspin_proxy
from music_assistant.controllers.webserver.sendspin_proxy import SendspinProxyHandler

_USER = User(user_id="user", username="user", role=UserRole.USER)
# the Home Assistant user header the Ingress proxy sets on every request it relays
_INGRESS_HEADERS = {"X-Remote-User-ID": "ha-user"}
_HELLO = '{"type": "client/hello", "payload": {"client_id": "web-1"}}'
# sent by the fake internal Sendspin server as soon as the proxy connects to it
_INTERNAL_GREETING = '{"type": "internal/connected"}'


@dataclass
class _ProxyServer:
    """A Sendspin proxy in front of a fake internal Sendspin server, both on real sockets."""

    url: str
    webserver: MagicMock
    session: aiohttp.ClientSession
    # messages the internal Sendspin server received through the proxy
    received: asyncio.Queue[aiohttp.WSMessage]


@pytest.fixture
async def proxy_server() -> AsyncGenerator[_ProxyServer]:
    """Run the Sendspin proxy against a fake internal Sendspin server."""
    received: asyncio.Queue[aiohttp.WSMessage] = asyncio.Queue()

    async def internal_sendspin(request: web.Request) -> web.WebSocketResponse:
        wsock = web.WebSocketResponse()
        await wsock.prepare(request)
        await wsock.send_str(_INTERNAL_GREETING)
        async for msg in wsock:
            received.put_nowait(msg)
        return wsock

    internal_app = web.Application()
    internal_app.router.add_get("/sendspin", internal_sendspin)
    async with TestServer(internal_app) as internal, aiohttp.ClientSession() as session:
        webserver = MagicMock()
        webserver.internal_sendspin_url = str(internal.make_url("/sendspin"))
        webserver.mass.http_session = session
        webserver.auth.authenticate_with_token = AsyncMock(return_value=_USER)
        proxy_app = web.Application()
        proxy_app.router.add_get("/sendspin", SendspinProxyHandler(webserver).handle_sendspin_proxy)
        async with TestServer(proxy_app) as proxy:
            yield _ProxyServer(str(proxy.make_url("/sendspin")), webserver, session, received)


@pytest.fixture
def ingress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat every proxy request as relayed by Home Assistant Ingress for a signed-in user."""
    monkeypatch.setattr(sendspin_proxy, "is_request_from_ingress_proxy", lambda _request: True)
    monkeypatch.setattr(sendspin_proxy, "get_authenticated_user", AsyncMock(return_value=_USER))


@pytest.fixture
def mock_webserver() -> MagicMock:
    """Create a mock webserver controller."""
    webserver = MagicMock()
    webserver.mass = MagicMock()
    webserver.mass.http_session = MagicMock()
    return webserver


@pytest.fixture
def handler(mock_webserver: MagicMock) -> SendspinProxyHandler:
    """Create a SendspinProxyHandler with mocked dependencies."""
    return SendspinProxyHandler(mock_webserver)


def _make_connector_error() -> ClientConnectorError:
    """Create a realistic ClientConnectorError for testing."""
    connection_key = MagicMock()
    return ClientConnectorError(connection_key, OSError(111, "Connection refused"))


async def test_proxy_dials_the_webserver_url(
    handler: SendspinProxyHandler, mock_webserver: MagicMock
) -> None:
    """Verify the proxy connects to the URL resolved by the webserver controller."""
    internal_url = "ws://127.0.0.1:8927/sendspin"
    mock_ws_response = AsyncMock(spec=web.WebSocketResponse)
    mock_ws_response.closed = False
    mock_webserver.internal_sendspin_url = internal_url

    mock_internal_ws = AsyncMock()
    mock_internal_ws.closed = False
    mock_ws_connect = AsyncMock(return_value=mock_internal_ws)

    with (
        patch.object(handler, "_authenticate", return_value=MagicMock()),
        patch.object(handler, "_proxy_messages", new_callable=AsyncMock),
        patch("aiohttp.web.WebSocketResponse", return_value=mock_ws_response),
        patch.object(handler.mass, "http_session", create=True) as mock_session,
        patch(
            "music_assistant.controllers.webserver.sendspin_proxy.is_request_from_ingress_proxy",
            return_value=False,
        ),
    ):
        mock_session.ws_connect = mock_ws_connect
        request = make_mocked_request("GET", "/sendspin")
        await handler.handle_sendspin_proxy(request)

    mock_ws_connect.assert_awaited_once_with(internal_url)


class TestSendspinProxyRetry:
    """Tests for the retry logic when connecting to the internal Sendspin server."""

    async def test_retries_on_connection_refused(self, handler: SendspinProxyHandler) -> None:
        """Verify the proxy retries on ClientConnectorError before giving up."""
        mock_ws_response = AsyncMock(spec=web.WebSocketResponse)
        mock_ws_response.closed = False

        connector_error = _make_connector_error()

        mock_internal_ws = AsyncMock()
        mock_internal_ws.closed = False
        mock_ws_connect = AsyncMock(
            side_effect=[connector_error, connector_error, mock_internal_ws]
        )

        with (
            patch.object(handler, "_authenticate", return_value=MagicMock()),
            patch.object(handler, "_proxy_messages", new_callable=AsyncMock),
            patch("aiohttp.web.WebSocketResponse", return_value=mock_ws_response),
            patch.object(handler.mass, "http_session", create=True) as mock_session,
            patch(
                "music_assistant.controllers.webserver.sendspin_proxy.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
            patch(
                "music_assistant.controllers.webserver.sendspin_proxy.is_request_from_ingress_proxy",
                return_value=False,
            ),
        ):
            mock_session.ws_connect = mock_ws_connect
            request = make_mocked_request("GET", "/sendspin")
            await handler.handle_sendspin_proxy(request)

        assert mock_ws_connect.call_count == 3
        assert mock_sleep.call_count == 2
        # Verify backoff: 0.5s, then 1.0s
        mock_sleep.assert_any_call(0.5)
        mock_sleep.assert_any_call(1.0)

    async def test_gives_up_after_max_retries(self, handler: SendspinProxyHandler) -> None:
        """Verify the proxy closes the client websocket after exhausting retries."""
        mock_ws_response = AsyncMock(spec=web.WebSocketResponse)
        mock_ws_response.closed = False

        connector_error = _make_connector_error()
        mock_ws_connect = AsyncMock(side_effect=connector_error)

        with (
            patch.object(handler, "_authenticate", return_value=MagicMock()),
            patch("aiohttp.web.WebSocketResponse", return_value=mock_ws_response),
            patch.object(handler.mass, "http_session", create=True) as mock_session,
            patch(
                "music_assistant.controllers.webserver.sendspin_proxy.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch(
                "music_assistant.controllers.webserver.sendspin_proxy.is_request_from_ingress_proxy",
                return_value=False,
            ),
        ):
            mock_session.ws_connect = mock_ws_connect
            request = make_mocked_request("GET", "/sendspin")
            result = await handler.handle_sendspin_proxy(request)

        assert mock_ws_connect.call_count == 5
        mock_ws_response.close.assert_called_once_with(code=1011, message=b"Internal server error")
        assert result is mock_ws_response

    async def test_does_not_retry_on_other_exceptions(self, handler: SendspinProxyHandler) -> None:
        """Verify non-connection errors are not retried and websocket is closed cleanly."""
        mock_ws_response = AsyncMock(spec=web.WebSocketResponse)
        mock_ws_response.closed = False

        mock_ws_connect = AsyncMock(side_effect=TypeError("unexpected error"))

        with (
            patch.object(handler, "_authenticate", return_value=MagicMock()),
            patch("aiohttp.web.WebSocketResponse", return_value=mock_ws_response),
            patch.object(handler.mass, "http_session", create=True) as mock_session,
            patch(
                "music_assistant.controllers.webserver.sendspin_proxy.is_request_from_ingress_proxy",
                return_value=False,
            ),
        ):
            mock_session.ws_connect = mock_ws_connect
            request = make_mocked_request("GET", "/sendspin")
            result = await handler.handle_sendspin_proxy(request)

        assert mock_ws_connect.call_count == 1
        mock_ws_response.close.assert_called_once_with(code=1011, message=b"Internal server error")
        assert result is mock_ws_response


class TestSendspinProxyMessages:
    """Tests for bidirectional proxy task handling."""

    async def test_expected_disconnect_is_consumed(self, handler: SendspinProxyHandler) -> None:
        """Verify normal client disconnects do not leak as unretrieved task exceptions."""

        async def raise_disconnect(*_: object) -> None:
            raise ConnectionError("Connection lost")

        async def wait_forever(*_: object) -> None:
            await asyncio.Event().wait()

        with (
            patch.object(handler, "_forward_internal_to_client", new=raise_disconnect),
            patch.object(handler, "_forward_client_to_internal", new=wait_forever),
        ):
            await handler._proxy_messages(MagicMock(), MagicMock())

    async def test_unexpected_forwarding_error_is_raised(
        self, handler: SendspinProxyHandler
    ) -> None:
        """Verify unexpected proxy task errors are still surfaced."""

        async def raise_unexpected(*_: object) -> None:
            raise RuntimeError("boom")

        async def wait_forever(*_: object) -> None:
            await asyncio.Event().wait()

        with (
            patch.object(handler, "_forward_internal_to_client", new=raise_unexpected),
            patch.object(handler, "_forward_client_to_internal", new=wait_forever),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await handler._proxy_messages(MagicMock(), MagicMock())

    async def test_primary_error_is_not_masked_by_peer_cleanup_failure(
        self, handler: SendspinProxyHandler
    ) -> None:
        """The first real failure must survive even if the peer task also errors."""

        async def raise_primary(*_: object) -> None:
            raise RuntimeError("primary failure")

        async def raise_on_cancel(*_: object) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("secondary cleanup failure") from None

        with (
            patch.object(handler, "_forward_internal_to_client", new=raise_primary),
            patch.object(handler, "_forward_client_to_internal", new=raise_on_cancel),
            pytest.raises(RuntimeError, match="primary failure"),
        ):
            await handler._proxy_messages(MagicMock(), MagicMock())

    @pytest.mark.parametrize(
        "exc",
        [
            aiohttp.ClientError("handshake failed"),
            asyncio.IncompleteReadError(b"", 1),
        ],
    )
    async def test_expected_transport_errors_are_consumed(
        self, handler: SendspinProxyHandler, exc: BaseException
    ) -> None:
        """Normal aiohttp/stream disconnects must not bubble up as 500s."""

        async def raise_disconnect(*_: object) -> None:
            raise exc

        async def wait_forever(*_: object) -> None:
            await asyncio.Event().wait()

        with (
            patch.object(handler, "_forward_internal_to_client", new=raise_disconnect),
            patch.object(handler, "_forward_client_to_internal", new=wait_forever),
        ):
            await handler._proxy_messages(MagicMock(), MagicMock())


class TestSendspinProxyAuth:
    """Tests for the auth message that announces a web player."""

    async def test_token_auth_binds_the_web_player(self, proxy_server: _ProxyServer) -> None:
        """A direct client authenticates with its token and announces its web player."""
        async with proxy_server.session.ws_connect(proxy_server.url) as client:
            await client.send_json({"type": "auth", "token": "token-a", "client_id": "web-1"})
            assert await client.receive_json(timeout=5) == {"type": "auth_ok"}
            assert await client.receive_str(timeout=5) == _INTERNAL_GREETING
            await client.send_str(_HELLO)
            forwarded = await asyncio.wait_for(proxy_server.received.get(), 5)

        assert forwarded.data == _HELLO
        webserver = proxy_server.webserver
        webserver.set_sendspin_player_for_token.assert_called_once_with("token-a", "web-1")
        webserver.set_sendspin_player_for_ingress_client.assert_not_called()

    @pytest.mark.usefixtures("ingress")
    async def test_ingress_auth_binds_the_web_player_to_the_browser(
        self, proxy_server: _ProxyServer
    ) -> None:
        """An Ingress client announces its web player without a token, and is not forwarded."""
        async with proxy_server.session.ws_connect(
            proxy_server.url, headers=_INGRESS_HEADERS
        ) as client:
            await client.send_json(
                {"type": "auth", "client_id": "web-1", "ingress_client_key": "key-a"}
            )
            assert await client.receive_json(timeout=5) == {"type": "auth_ok"}
            assert await client.receive_str(timeout=5) == _INTERNAL_GREETING
            await client.send_str(_HELLO)
            forwarded = await asyncio.wait_for(proxy_server.received.get(), 5)

        assert forwarded.data == _HELLO
        proxy_server.webserver.set_sendspin_player_for_ingress_client.assert_called_once_with(
            "ha-user", "key-a", "web-1"
        )

    @pytest.mark.usefixtures("ingress")
    async def test_ingress_auth_without_a_client_key_binds_nothing(
        self, proxy_server: _ProxyServer
    ) -> None:
        """Without the key of its browser a web player can not be linked to that browser."""
        async with proxy_server.session.ws_connect(
            proxy_server.url, headers=_INGRESS_HEADERS
        ) as client:
            await client.send_json({"type": "auth", "client_id": "web-1"})
            assert await client.receive_json(timeout=5) == {"type": "auth_ok"}
            assert await client.receive_str(timeout=5) == _INTERNAL_GREETING

        proxy_server.webserver.set_sendspin_player_for_ingress_client.assert_not_called()

    @pytest.mark.usefixtures("ingress")
    async def test_silent_ingress_client_is_still_proxied(
        self, proxy_server: _ProxyServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An Ingress client that never sends an auth message is proxied once the wait is over."""
        monkeypatch.setattr(sendspin_proxy, "AUTH_TIMEOUT", 0.01)
        async with proxy_server.session.ws_connect(
            proxy_server.url, headers=_INGRESS_HEADERS
        ) as client:
            assert await client.receive_str(timeout=5) == _INTERNAL_GREETING
            await client.send_str(_HELLO)
            forwarded = await asyncio.wait_for(proxy_server.received.get(), 5)

        assert forwarded.data == _HELLO
        proxy_server.webserver.set_sendspin_player_for_ingress_client.assert_not_called()

    @pytest.mark.usefixtures("ingress")
    @pytest.mark.parametrize("first_msg", [_HELLO, "not json", b"\x00\x01"])
    async def test_first_ingress_message_that_is_not_auth_is_forwarded(
        self, proxy_server: _ProxyServer, first_msg: str | bytes
    ) -> None:
        """A client that starts with a Sendspin message right away does not lose it."""
        async with proxy_server.session.ws_connect(
            proxy_server.url, headers=_INGRESS_HEADERS
        ) as client:
            if isinstance(first_msg, bytes):
                await client.send_bytes(first_msg)
            else:
                await client.send_str(first_msg)
            forwarded = await asyncio.wait_for(proxy_server.received.get(), 5)
            # no auth_ok: the first message the client gets comes from the Sendspin server
            assert await client.receive_str(timeout=5) == _INTERNAL_GREETING

        assert forwarded.data == first_msg
        proxy_server.webserver.set_sendspin_player_for_ingress_client.assert_not_called()
