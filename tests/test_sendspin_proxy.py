"""Tests for the Sendspin WebSocket proxy handler."""

import asyncio
import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import ClientConnectorError, WSMsgType, web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import UserRole

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
            await handler._proxy_messages(MagicMock(), MagicMock(), MagicMock(role=UserRole.USER))

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
            await handler._proxy_messages(MagicMock(), MagicMock(), MagicMock(role=UserRole.USER))

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
            await handler._proxy_messages(MagicMock(), MagicMock(), MagicMock(role=UserRole.USER))

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
            await handler._proxy_messages(MagicMock(), MagicMock(), MagicMock(role=UserRole.USER))


class _MessageStream:
    """Async iterator over websocket-like messages."""

    def __init__(self, *messages: SimpleNamespace) -> None:
        self._messages = iter(messages)

    def __aiter__(self) -> _MessageStream:
        return self

    async def __anext__(self) -> SimpleNamespace:
        try:
            return next(self._messages)
        except StopIteration:
            raise StopAsyncIteration from None


def _hello(*roles: str, include_player_support: bool = True) -> str:
    """Build a Sendspin client hello with the given roles."""
    payload: dict[str, object] = {
        "client_id": "web-player",
        "name": "Web player",
        "version": 1,
        "supported_roles": list(roles),
    }
    if include_player_support:
        payload["player@v1_support"] = {
            "supported_formats": [],
            "buffer_capacity": 1024,
            "supported_commands": [],
        }
    return json.dumps({"type": "client/hello", "payload": payload}, indent=2)


class TestSendspinGuestRoles:
    """Tests for authenticated role restriction on client-to-server messages."""

    async def test_guest_first_and_repeated_hello_are_player_only(
        self, handler: SendspinProxyHandler
    ) -> None:
        """Every Guest hello is rewritten while non-hello and binary traffic passes through."""
        first_hello = _hello("player@v1", "metadata@v1", "controller@v1")
        second_hello = _hello("metadata@v1")
        non_hello = '{"type":"client/state","payload":{"volume":50}}'
        malformed = "{not-json"
        binary = b"\x00audio"
        client = _MessageStream(
            SimpleNamespace(type=WSMsgType.TEXT, data=first_hello),
            SimpleNamespace(type=WSMsgType.TEXT, data=non_hello),
            SimpleNamespace(type=WSMsgType.TEXT, data=second_hello),
            SimpleNamespace(type=WSMsgType.TEXT, data=malformed),
            SimpleNamespace(type=WSMsgType.BINARY, data=binary),
        )
        internal = AsyncMock()

        await handler._forward_client_to_internal(
            cast("web.WebSocketResponse", client), internal, ("player@v1",)
        )

        forwarded = [call.args[0] for call in internal.send_str.await_args_list]
        assert json.loads(forwarded[0])["payload"]["supported_roles"] == ["player@v1"]
        assert forwarded[1] == non_hello
        assert json.loads(forwarded[2])["payload"]["supported_roles"] == ["player@v1"]
        assert forwarded[3] == malformed
        internal.send_bytes.assert_awaited_once_with(binary)

    @pytest.mark.parametrize("role", [UserRole.USER, UserRole.ADMIN, UserRole.SERVICE])
    async def test_non_guest_hello_is_byte_identical(
        self, handler: SendspinProxyHandler, role: UserRole
    ) -> None:
        """Regular, admin and service users retain exact client traffic."""
        raw_hello = _hello("player@v1", "metadata@v1")
        client = _MessageStream(SimpleNamespace(type=WSMsgType.TEXT, data=raw_hello))
        internal = AsyncMock()

        await handler._forward_client_to_internal(
            cast("web.WebSocketResponse", client), internal, None
        )

        internal.send_str.assert_awaited_once_with(raw_hello)

    @pytest.mark.parametrize(
        "raw_message",
        [
            "{not-json",
            "[]",
            '{"type":"client/hello","payload":[]}',
            '{"type":"client/state","payload":{"supported_roles":["metadata@v1"]}}',
        ],
    )
    def test_malformed_or_non_hello_text_passes_through(
        self, handler: SendspinProxyHandler, raw_message: str
    ) -> None:
        """Messages that are not structurally valid hellos remain parser-visible unchanged."""
        assert handler._restrict_client_hello_roles(raw_message, ("player@v1",)) == raw_message

    def test_rewrite_preserves_player_support_without_synthesizing_it(
        self, handler: SendspinProxyHandler
    ) -> None:
        """Role rewriting preserves support data and leaves missing support for upstream rejection."""
        supported = json.loads(
            handler._restrict_client_hello_roles(
                _hello("metadata@v1", include_player_support=True),
                ("player@v1",),
            )
        )
        missing = json.loads(
            handler._restrict_client_hello_roles(
                _hello("metadata@v1", include_player_support=False),
                ("player@v1",),
            )
        )

        assert supported["payload"]["supported_roles"] == ["player@v1"]
        assert supported["payload"]["player@v1_support"]["buffer_capacity"] == 1024
        assert missing["payload"]["supported_roles"] == ["player@v1"]
        assert "player@v1_support" not in missing["payload"]

    @pytest.mark.parametrize(
        ("role", "allowed_roles"),
        [
            (UserRole.GUEST, ("player@v1",)),
            (UserRole.USER, None),
            (UserRole.ADMIN, None),
            (UserRole.SERVICE, None),
        ],
    )
    async def test_authenticated_role_selects_forwarding_policy(
        self,
        handler: SendspinProxyHandler,
        role: UserRole,
        allowed_roles: tuple[str, ...] | None,
    ) -> None:
        """The forwarding policy depends only on the authenticated user's role."""
        user = MagicMock(role=role)
        client = MagicMock()
        internal = MagicMock()
        with (
            patch.object(
                handler, "_forward_client_to_internal", new_callable=AsyncMock
            ) as forward_client,
            patch.object(handler, "_forward_internal_to_client", new_callable=AsyncMock),
        ):
            await handler._proxy_messages(client, internal, user)

        forward_client.assert_awaited_once_with(client, internal, allowed_roles)

    @pytest.mark.parametrize("ingress", [False, True], ids=["token", "ingress"])
    async def test_guest_auth_paths_reach_proxy_with_authenticated_user(
        self,
        handler: SendspinProxyHandler,
        ingress: bool,
    ) -> None:
        """Token and ingress Guest authentication both apply the same role policy."""
        user = MagicMock(role=UserRole.GUEST, username="guest")
        client_ws = AsyncMock(spec=web.WebSocketResponse)
        client_ws.closed = True
        internal_ws = AsyncMock()
        internal_ws.closed = True
        with (
            patch("aiohttp.web.WebSocketResponse", return_value=client_ws),
            patch(
                "music_assistant.controllers.webserver.sendspin_proxy.is_request_from_ingress",
                return_value=ingress,
            ),
            patch(
                "music_assistant.controllers.webserver.sendspin_proxy.get_authenticated_user",
                new=AsyncMock(return_value=user),
            ),
            patch.object(handler, "_authenticate", new=AsyncMock(return_value=user)),
            patch.object(handler, "_proxy_messages", new_callable=AsyncMock) as proxy,
        ):
            handler.mass.http_session.ws_connect = AsyncMock(  # type: ignore[method-assign]
                return_value=internal_ws
            )
            request = make_mocked_request("GET", "/sendspin")
            await handler.handle_sendspin_proxy(request)

        proxy.assert_awaited_once_with(client_ws, internal_ws, user)


class TestSendspinProxyAuth:
    """Tests for strict custom proxy authentication payloads."""

    @pytest.mark.parametrize(
        "auth_data",
        [
            [],
            {"type": "auth", "token": ""},
            {"type": "auth", "token": 123},
            {"type": "auth", "token": "token", "client_id": ""},
            {"type": "auth", "token": "token", "client_id": 123},
            {"type": "auth", "token": "token", "client_id": None},
        ],
    )
    async def test_invalid_auth_payload_types_are_rejected(
        self,
        handler: SendspinProxyHandler,
        auth_data: object,
    ) -> None:
        """Auth payloads require a dict and non-empty string identifiers."""
        wsock = AsyncMock()
        wsock.receive.return_value = SimpleNamespace(
            type=WSMsgType.TEXT,
            data=json.dumps(auth_data),
        )
        handler.webserver.auth.authenticate_with_token = AsyncMock()  # type: ignore[method-assign]

        assert await handler._authenticate(wsock) is None

        wsock.close.assert_awaited_once()
        handler.webserver.auth.authenticate_with_token.assert_not_awaited()

    async def test_valid_auth_registers_non_empty_client_id(
        self, handler: SendspinProxyHandler
    ) -> None:
        """A valid token and client id authenticate and register normally."""
        user = MagicMock(user_id="guest-id", username="guest")
        handler.webserver.auth.authenticate_with_token = AsyncMock(  # type: ignore[method-assign]
            return_value=user
        )
        wsock = AsyncMock()
        wsock.receive.return_value = SimpleNamespace(
            type=WSMsgType.TEXT,
            data=json.dumps(
                {
                    "type": "auth",
                    "token": "token",
                    "client_id": "web-player",
                }
            ),
        )

        assert await handler._authenticate(wsock) is user

        handler.webserver.auth.authenticate_with_token.assert_awaited_once_with("token")
        cast("MagicMock", handler.webserver.set_sendspin_player_for_user).assert_called_once_with(
            "guest-id", "web-player"
        )
        wsock.send_str.assert_awaited_once_with('{"type": "auth_ok"}')
