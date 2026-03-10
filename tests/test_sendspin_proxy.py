"""Tests for the Sendspin WebSocket proxy handler."""

from unittest.mock import AsyncMock, MagicMock, patch

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
