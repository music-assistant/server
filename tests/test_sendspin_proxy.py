"""Tests for the Sendspin WebSocket proxy handler."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import ClientConnectorError, web
from aiohttp.test_utils import make_mocked_request

from music_assistant.controllers.webserver.sendspin_proxy import SendspinProxyHandler


@pytest.fixture
def mock_webserver() -> MagicMock:
    """Create a mock webserver controller."""
    webserver = MagicMock()
    webserver.mass = MagicMock()
    webserver.mass.streams.bind_ip = "0.0.0.0"
    webserver.mass.http_session = MagicMock()
    webserver.base_url = "http://192.0.2.10:8095"
    return webserver


@pytest.fixture
def handler(mock_webserver: MagicMock) -> SendspinProxyHandler:
    """Create a SendspinProxyHandler with mocked dependencies."""
    return SendspinProxyHandler(mock_webserver)


def _make_connector_error() -> ClientConnectorError:
    """Create a realistic ClientConnectorError for testing."""
    connection_key = MagicMock()
    return ClientConnectorError(connection_key, OSError(111, "Connection refused"))


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
                "music_assistant.controllers.webserver.sendspin_proxy.is_request_from_ingress",
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
                "music_assistant.controllers.webserver.sendspin_proxy.is_request_from_ingress",
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
                "music_assistant.controllers.webserver.sendspin_proxy.is_request_from_ingress",
                return_value=False,
            ),
        ):
            mock_session.ws_connect = mock_ws_connect
            request = make_mocked_request("GET", "/sendspin")
            result = await handler.handle_sendspin_proxy(request)

        assert mock_ws_connect.call_count == 1
        mock_ws_response.close.assert_called_once_with(code=1011, message=b"Internal server error")
        assert result is mock_ws_response


class TestSendspinCastProxy:
    """Tests for the capability-protected Cast proxy endpoint."""

    def test_cast_base_url_requires_https(
        self, handler: SendspinProxyHandler, mock_webserver: MagicMock
    ) -> None:
        """The Cast proxy must never advertise its bearer token over plain HTTP."""
        assert handler.cast_base_url is None

        mock_webserver.base_url = "https://music.example.test/ma"
        secure_base_url = SendspinProxyHandler(mock_webserver).cast_base_url
        assert secure_base_url is not None
        assert secure_base_url.startswith("https://music.example.test/ma/sendspin-cast/")

    async def test_invalid_cast_capability_is_rejected(self, handler: SendspinProxyHandler) -> None:
        """An invalid capability token must be rejected before WebSocket upgrade."""
        request = MagicMock()
        request.match_info = {"access_token": "invalid"}

        with pytest.raises(web.HTTPNotFound):
            await handler.handle_cast_sendspin_proxy(request)

    async def test_valid_cast_capability_connects_proxy(
        self, handler: SendspinProxyHandler, mock_webserver: MagicMock
    ) -> None:
        """A valid capability token may connect without interactive user auth."""
        mock_webserver.base_url = "https://music.example.test"
        cast_base_url = handler.cast_base_url
        assert cast_base_url is not None
        access_token = cast_base_url.rsplit("/", 1)[-1]
        request = MagicMock()
        request.match_info = {"access_token": access_token}
        request.remote = "192.0.2.20"
        mock_ws_response = AsyncMock(spec=web.WebSocketResponse)

        with (
            patch("aiohttp.web.WebSocketResponse", return_value=mock_ws_response),
            patch.object(
                handler,
                "_connect_and_proxy",
                new_callable=AsyncMock,
                return_value=mock_ws_response,
            ) as mock_connect,
        ):
            result = await handler.handle_cast_sendspin_proxy(request)

        mock_ws_response.prepare.assert_awaited_once_with(request)
        mock_connect.assert_awaited_once_with(request, mock_ws_response)
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
