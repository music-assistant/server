"""Tests for the DirectConnectionHandler (direct connection mode)."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

if TYPE_CHECKING:
    from aiohttp import web

from music_assistant.providers.yandex_smarthome.constants import (
    CONNECTION_TYPE_DIRECT,
    DIRECT_API_BASE_PATH,
    DIRECT_AUTH_BASE_PATH,
    DIRECT_HEALTH_RESPONSE,
    DIRECT_OAUTH_CLIENT_ID,
    OAUTH_CODE_EXPIRY,
)
from music_assistant.providers.yandex_smarthome.direct import DirectConnectionHandler
from music_assistant.providers.yandex_smarthome.plugin import YandexSmartHomePlugin


def _body_json(resp: web.Response) -> Any:
    """Decode the JSON body of a response."""
    assert isinstance(resp.body, bytes | bytearray)
    return json.loads(resp.body)


TEST_CLIENT_SECRET = "test-client-secret-abc123"

# token_store lists shared between fixtures and tests
_handler_tokens: list[str] = []
_handler_no_token_tokens: list[str] = []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_mass() -> MagicMock:
    """Return a mock MusicAssistant with a webserver stub."""
    mass = MagicMock()
    mass.webserver.base_url = "https://my-ma.example.com"
    mass.webserver.register_dynamic_route = MagicMock(return_value=MagicMock())
    mass.players = []
    mass.http_session = MagicMock()
    # No setup_data persisted on the mock config store, so get_setup_value falls through
    # to the config entry value/default via get_config_value (the provided ProviderConfig).
    mass.config.get = MagicMock(return_value=None)
    mass.config.get_raw_provider_config_value = MagicMock(return_value=None)
    return mass


@pytest.fixture
def handler(mock_mass: MagicMock) -> DirectConnectionHandler:
    """Return a DirectConnectionHandler with a known token."""
    _handler_tokens.clear()

    def on_token(t: str) -> None:
        _handler_tokens.append(t)

    return DirectConnectionHandler(
        mass=mock_mass,
        user_id="test_user",
        access_token="test-token-abc",
        client_secret=TEST_CLIENT_SECRET,
        exposed_ids=None,
        on_token_created=on_token,
    )


@pytest.fixture
def handler_no_token(mock_mass: MagicMock) -> DirectConnectionHandler:
    """Return a handler with no initial access token (first-time OAuth flow)."""
    _handler_no_token_tokens.clear()

    def on_token(t: str) -> None:
        _handler_no_token_tokens.append(t)

    return DirectConnectionHandler(
        mass=mock_mass,
        user_id="test_user",
        access_token="",
        client_secret=TEST_CLIENT_SECRET,
        exposed_ids=None,
        on_token_created=on_token,
    )


def _make_request(
    method: str = "GET",
    path: str = "/",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    post_data: dict[str, str] | None = None,
) -> web.Request:
    """Build a mock aiohttp Request."""
    req = make_mocked_request(
        method,
        path,
        headers=headers or {},
    )
    if query:
        object.__setattr__(req, "_rel_url", req._rel_url.with_query(query))
    if payload is not None:
        object.__setattr__(req, "_payload_writer", None)

        async def _json(**_kwargs: Any) -> dict[str, Any]:
            return payload

        object.__setattr__(req, "json", _json)
    if post_data is not None:

        async def _post() -> dict[str, str]:
            return post_data

        object.__setattr__(req, "post", _post)
    return req


def _get_pending_code(h: DirectConnectionHandler) -> str:
    """Return the first pending authorization code from a handler."""
    return str(next(iter(h._pending_codes.keys())))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_connection_type_direct() -> None:
    """CONNECTION_TYPE_DIRECT should be 'direct'."""
    assert CONNECTION_TYPE_DIRECT == "direct"


def test_api_base_path() -> None:
    """DIRECT_API_BASE_PATH should be under /api/yandex_smarthome."""
    assert DIRECT_API_BASE_PATH.startswith("/api/yandex_smarthome")


def test_auth_base_path() -> None:
    """DIRECT_AUTH_BASE_PATH should be under /api/yandex_smarthome."""
    assert DIRECT_AUTH_BASE_PATH.startswith("/api/yandex_smarthome")


def test_health_response() -> None:
    """DIRECT_HEALTH_RESPONSE should be non-empty."""
    assert len(DIRECT_HEALTH_RESPONSE) > 0


def test_oauth_constants() -> None:
    """OAuth constants should match Yandex Smart Home spec."""
    assert DIRECT_OAUTH_CLIENT_ID == "https://social.yandex.net/"
    assert OAUTH_CODE_EXPIRY == 300


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def test_register_routes(handler: DirectConnectionHandler, mock_mass: MagicMock) -> None:
    """register_routes should register all 10 HTTP routes."""
    handler.register_routes()
    assert mock_mass.webserver.register_dynamic_route.call_count == 10


def test_unregister_routes(handler: DirectConnectionHandler) -> None:
    """unregister_routes should call all stored callbacks and clear."""
    handler.register_routes()
    handler.unregister_routes()
    assert len(handler._unregister_callbacks) == 0


def test_register_routes_rolls_back_on_failure(
    handler: DirectConnectionHandler, mock_mass: MagicMock
) -> None:
    """register_routes must unregister partial routes and re-raise on RuntimeError."""
    unregister_cb = MagicMock()
    call_count = {"n": 0}

    def register(_path: str, _handler_fn: Any, _method: str) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("already registered")
        return unregister_cb

    mock_mass.webserver.register_dynamic_route.side_effect = register

    with pytest.raises(RuntimeError):
        handler.register_routes()

    # 2 successful registrations must be rolled back via unregister_cb
    assert unregister_cb.call_count == 2
    assert handler._unregister_callbacks == []


# ---------------------------------------------------------------------------
# Auth validation
# ---------------------------------------------------------------------------


def test_auth_valid(handler: DirectConnectionHandler) -> None:
    """Valid Bearer token should pass validation."""
    req = _make_request(headers={"Authorization": "Bearer test-token-abc"})
    assert handler._validate_auth(req) is True


def test_auth_invalid(handler: DirectConnectionHandler) -> None:
    """Wrong Bearer token should fail validation."""
    req = _make_request(headers={"Authorization": "Bearer wrong-token"})
    assert handler._validate_auth(req) is False


def test_auth_missing(handler: DirectConnectionHandler) -> None:
    """Missing Authorization header should fail validation."""
    req = _make_request()
    assert handler._validate_auth(req) is False


def test_auth_non_bearer(handler: DirectConnectionHandler) -> None:
    """Non-Bearer auth scheme should fail validation."""
    req = _make_request(headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert handler._validate_auth(req) is False


def test_auth_empty_token_rejects(handler_no_token: DirectConnectionHandler) -> None:
    """Handler with no access token should reject any Bearer token."""
    req = _make_request(headers={"Authorization": "Bearer anything"})
    assert handler_no_token._validate_auth(req) is False


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_get(handler: DirectConnectionHandler) -> None:
    """GET health check should return 200 with health text."""
    req = _make_request(method="GET", path="/v1.0")
    resp = await handler._handle_health(req)
    assert resp.status == 200
    assert resp.text == DIRECT_HEALTH_RESPONSE


@pytest.mark.asyncio
async def test_health_head(handler: DirectConnectionHandler) -> None:
    """HEAD health check should return 200."""
    req = _make_request(method="HEAD", path="/v1.0")
    resp = await handler._handle_health(req)
    assert resp.status == 200


# ---------------------------------------------------------------------------
# API auth rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_devices_unauthorized(handler: DirectConnectionHandler) -> None:
    """POST /user/devices with bad token should return 401."""
    req = _make_request(method="POST", headers={"Authorization": "Bearer bad"})
    resp = await handler._handle_devices(req)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_query_unauthorized(handler: DirectConnectionHandler) -> None:
    """POST /user/devices/query with bad token should return 401."""
    req = _make_request(method="POST", headers={"Authorization": "Bearer bad"})
    resp = await handler._handle_query(req)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_action_unauthorized(handler: DirectConnectionHandler) -> None:
    """POST /user/devices/action with bad token should return 401."""
    req = _make_request(method="POST", headers={"Authorization": "Bearer bad"})
    resp = await handler._handle_action(req)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_unlink_unauthorized(handler: DirectConnectionHandler) -> None:
    """POST /user/unlink with bad token should return 401."""
    req = _make_request(method="POST", headers={"Authorization": "Bearer bad"})
    resp = await handler._handle_unlink(req)
    assert resp.status == 401


# ---------------------------------------------------------------------------
# API authorized calls
# ---------------------------------------------------------------------------

_AUTH_HEADERS = {"Authorization": "Bearer test-token-abc"}


@pytest.mark.asyncio
async def test_devices_success(handler: DirectConnectionHandler) -> None:
    """Authorized /user/devices should return 200 with device list."""
    req = _make_request(
        method="POST",
        headers={**_AUTH_HEADERS, "X-Request-Id": "req-1"},
    )
    mock_result = MagicMock()
    resp_payload = {"request_id": "req-1", "payload": {"devices": []}}
    mock_hdl = patch(
        "music_assistant.providers.yandex_smarthome.direct.handle_device_list",
        new_callable=AsyncMock,
        return_value=mock_result,
    )
    with (
        mock_hdl,
        patch(
            "music_assistant.providers.yandex_smarthome.direct.asdict", return_value={"devices": []}
        ),
        patch(
            "music_assistant.providers.yandex_smarthome.direct.build_response",
            return_value=resp_payload,
        ),
    ):
        resp = await handler._handle_devices(req)
        assert resp.status == 200
        body = _body_json(resp)
        assert body["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_query_success(handler: DirectConnectionHandler) -> None:
    """Authorized /user/devices/query should return 200."""
    req = _make_request(
        method="POST",
        headers={**_AUTH_HEADERS, "X-Request-Id": "req-2"},
        payload={"devices": [{"id": "player1"}]},
    )
    mock_result = MagicMock()
    resp_payload = {"request_id": "req-2", "payload": {"devices": []}}
    mock_query = patch(
        "music_assistant.providers.yandex_smarthome.direct.handle_devices_query",
        new_callable=AsyncMock,
        return_value=mock_result,
    )
    with (
        mock_query,
        patch(
            "music_assistant.providers.yandex_smarthome.direct.asdict", return_value={"devices": []}
        ),
        patch(
            "music_assistant.providers.yandex_smarthome.direct.build_response",
            return_value=resp_payload,
        ),
    ):
        resp = await handler._handle_query(req)
        assert resp.status == 200


@pytest.mark.asyncio
async def test_action_success(handler: DirectConnectionHandler) -> None:
    """Authorized /user/devices/action should return 200."""
    req = _make_request(
        method="POST",
        headers={**_AUTH_HEADERS, "X-Request-Id": "req-3"},
        payload={"payload": {"devices": []}},
    )
    resp_payload = {"request_id": "req-3", "payload": {"devices": []}}
    mock_action = patch(
        "music_assistant.providers.yandex_smarthome.direct.handle_devices_action",
        new_callable=AsyncMock,
        return_value=MagicMock(),
    )
    with (
        patch(
            "music_assistant.providers.yandex_smarthome.direct.parse_action_payload",
            return_value=MagicMock(),
        ),
        mock_action,
        patch(
            "music_assistant.providers.yandex_smarthome.direct.asdict", return_value={"devices": []}
        ),
        patch(
            "music_assistant.providers.yandex_smarthome.direct.build_response",
            return_value=resp_payload,
        ),
    ):
        resp = await handler._handle_action(req)
        assert resp.status == 200


@pytest.mark.asyncio
async def test_unlink_success(handler: DirectConnectionHandler) -> None:
    """Authorized /user/unlink should return 200."""
    req = _make_request(
        method="POST",
        headers={**_AUTH_HEADERS, "X-Request-Id": "req-4"},
    )
    resp_payload = {"request_id": "req-4"}
    with (
        patch(
            "music_assistant.providers.yandex_smarthome.direct.handle_user_unlink",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "music_assistant.providers.yandex_smarthome.direct.build_response",
            return_value=resp_payload,
        ),
    ):
        resp = await handler._handle_unlink(req)
        assert resp.status == 200


# ---------------------------------------------------------------------------
# OAuth authorize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorize_returns_html(handler: DirectConnectionHandler) -> None:
    """GET /auth/authorize should return HTML with link button."""
    req = _make_request(
        method="GET",
        path="/auth/authorize",
        query={
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "redirect_uri": "https://social.yandex.net/broker/redirect",
            "state": "abc123",
            "response_type": "code",
        },
    )
    resp = await handler._handle_oauth_authorize(req)
    assert resp.status == 200
    assert resp.content_type == "text/html"
    assert resp.text is not None
    assert "Music Assistant" in resp.text
    assert "abc123" in resp.text


@pytest.mark.asyncio
async def test_authorize_missing_redirect_uri(handler: DirectConnectionHandler) -> None:
    """GET /auth/authorize without redirect_uri should return 400."""
    req = _make_request(method="GET", path="/auth/authorize", query={})
    resp = await handler._handle_oauth_authorize(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_authorize_creates_pending_code(handler: DirectConnectionHandler) -> None:
    """GET /auth/authorize should create a pending authorization code."""
    req = _make_request(
        method="GET",
        path="/auth/authorize",
        query={
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "redirect_uri": "https://social.yandex.net/broker/redirect",
            "state": "s1",
            "response_type": "code",
        },
    )
    assert len(handler._pending_codes) == 0
    await handler._handle_oauth_authorize(req)
    assert len(handler._pending_codes) == 1


@pytest.mark.asyncio
async def test_authorize_invalid_client_id(handler: DirectConnectionHandler) -> None:
    """GET /auth/authorize with wrong client_id should return 400."""
    req = _make_request(
        method="GET",
        path="/auth/authorize",
        query={
            "client_id": "wrong-client-id",
            "redirect_uri": "https://social.yandex.net/broker/redirect",
            "state": "s1",
            "response_type": "code",
        },
    )
    resp = await handler._handle_oauth_authorize(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_authorize_invalid_response_type(handler: DirectConnectionHandler) -> None:
    """GET /auth/authorize with wrong response_type should return 400."""
    req = _make_request(
        method="GET",
        path="/auth/authorize",
        query={
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "redirect_uri": "https://social.yandex.net/broker/redirect",
            "state": "s1",
            "response_type": "token",
        },
    )
    resp = await handler._handle_oauth_authorize(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_authorize_invalid_redirect_uri_domain(handler: DirectConnectionHandler) -> None:
    """GET /auth/authorize with non-Yandex redirect_uri should return 400."""
    req = _make_request(
        method="GET",
        path="/auth/authorize",
        query={
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "redirect_uri": "https://evil.example.com/steal",
            "state": "s1",
            "response_type": "code",
        },
    )
    resp = await handler._handle_oauth_authorize(req)
    assert resp.status == 400


# ---------------------------------------------------------------------------
# OAuth token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_exchange_valid_code(handler: DirectConnectionHandler) -> None:
    """Token exchange with valid code should return access_token."""
    req_auth = _make_request(
        method="GET",
        path="/auth/authorize",
        query={
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "redirect_uri": "https://social.yandex.net/broker/redirect",
            "state": "s1",
            "response_type": "code",
        },
    )
    await handler._handle_oauth_authorize(req_auth)
    code = _get_pending_code(handler)

    req_token = _make_request(
        method="POST",
        path="/auth/token",
        post_data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "client_secret": TEST_CLIENT_SECRET,
        },
    )
    resp = await handler._handle_oauth_token(req_token)
    assert resp.status == 200
    body = _body_json(resp)
    assert body["access_token"] == "test-token-abc"
    assert body["token_type"] == "bearer"
    assert "refresh_token" in body


@pytest.mark.asyncio
async def test_token_exchange_generates_new_token(
    handler_no_token: DirectConnectionHandler,
) -> None:
    """When no token exists, OAuth should generate a new one."""
    req_auth = _make_request(
        method="GET",
        path="/auth/authorize",
        query={
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "redirect_uri": "https://social.yandex.net/broker/redirect",
            "state": "s1",
            "response_type": "code",
        },
    )
    await handler_no_token._handle_oauth_authorize(req_auth)
    code = _get_pending_code(handler_no_token)

    req_token = _make_request(
        method="POST",
        path="/auth/token",
        post_data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "client_secret": TEST_CLIENT_SECRET,
        },
    )
    resp = await handler_no_token._handle_oauth_token(req_token)
    assert resp.status == 200
    body = _body_json(resp)
    assert body["access_token"]
    assert len(body["access_token"]) == 32  # uuid4().hex
    assert len(_handler_no_token_tokens) == 1
    assert _handler_no_token_tokens[0] == body["access_token"]


@pytest.mark.asyncio
async def test_token_exchange_invalid_client_secret(handler: DirectConnectionHandler) -> None:
    """Token exchange with wrong client_secret should return 401."""
    req = _make_request(
        method="POST",
        path="/auth/token",
        post_data={
            "grant_type": "authorization_code",
            "code": "any",
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "client_secret": "wrong-secret",
        },
    )
    resp = await handler._handle_oauth_token(req)
    assert resp.status == 401
    body = _body_json(resp)
    assert body["error"] == "invalid_client"


@pytest.mark.asyncio
async def test_token_exchange_invalid_client_id(handler: DirectConnectionHandler) -> None:
    """Token exchange with wrong client_id should return 401."""
    req = _make_request(
        method="POST",
        path="/auth/token",
        post_data={
            "grant_type": "authorization_code",
            "code": "any",
            "client_id": "wrong-client-id",
            "client_secret": TEST_CLIENT_SECRET,
        },
    )
    resp = await handler._handle_oauth_token(req)
    assert resp.status == 401
    body = _body_json(resp)
    assert body["error"] == "invalid_client"


@pytest.mark.asyncio
async def test_token_exchange_invalid_code(handler: DirectConnectionHandler) -> None:
    """Token exchange with invalid code should return 400."""
    req = _make_request(
        method="POST",
        path="/auth/token",
        post_data={
            "grant_type": "authorization_code",
            "code": "nonexistent",
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "client_secret": TEST_CLIENT_SECRET,
        },
    )
    resp = await handler._handle_oauth_token(req)
    assert resp.status == 400
    body = _body_json(resp)
    assert body["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_token_exchange_expired_code(handler: DirectConnectionHandler) -> None:
    """Expired authorization codes should be rejected."""
    handler._pending_codes["expired-code"] = time.time() - 10
    req = _make_request(
        method="POST",
        path="/auth/token",
        post_data={
            "grant_type": "authorization_code",
            "code": "expired-code",
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "client_secret": TEST_CLIENT_SECRET,
        },
    )
    resp = await handler._handle_oauth_token(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_refresh_token_valid(handler: DirectConnectionHandler) -> None:
    """Refresh token with correct token should return 200."""
    req = _make_request(
        method="POST",
        path="/auth/token",
        post_data={
            "grant_type": "refresh_token",
            "refresh_token": "test-token-abc",
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "client_secret": TEST_CLIENT_SECRET,
        },
    )
    resp = await handler._handle_oauth_token(req)
    assert resp.status == 200
    body = _body_json(resp)
    assert body["access_token"] == "test-token-abc"


@pytest.mark.asyncio
async def test_refresh_token_invalid(handler: DirectConnectionHandler) -> None:
    """Refresh token with wrong token should return 400."""
    req = _make_request(
        method="POST",
        path="/auth/token",
        post_data={
            "grant_type": "refresh_token",
            "refresh_token": "wrong",
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "client_secret": TEST_CLIENT_SECRET,
        },
    )
    resp = await handler._handle_oauth_token(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_unsupported_grant_type(handler: DirectConnectionHandler) -> None:
    """Unsupported grant_type should return 400."""
    req = _make_request(
        method="POST",
        path="/auth/token",
        post_data={
            "grant_type": "client_credentials",
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "client_secret": TEST_CLIENT_SECRET,
        },
    )
    resp = await handler._handle_oauth_token(req)
    assert resp.status == 400
    body = _body_json(resp)
    assert body["error"] == "unsupported_grant_type"


@pytest.mark.asyncio
async def test_code_consumed_after_use(handler: DirectConnectionHandler) -> None:
    """Authorization codes should be single-use."""
    req_auth = _make_request(
        method="GET",
        path="/auth/authorize",
        query={
            "client_id": DIRECT_OAUTH_CLIENT_ID,
            "redirect_uri": "https://social.yandex.net/broker/redirect",
            "state": "s1",
            "response_type": "code",
        },
    )
    await handler._handle_oauth_authorize(req_auth)
    code = _get_pending_code(handler)

    token_post = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": DIRECT_OAUTH_CLIENT_ID,
        "client_secret": TEST_CLIENT_SECRET,
    }

    # First exchange — success
    req1 = _make_request(method="POST", path="/auth/token", post_data=token_post)
    resp1 = await handler._handle_oauth_token(req1)
    assert resp1.status == 200

    # Second exchange — code consumed, should fail
    req2 = _make_request(method="POST", path="/auth/token", post_data=token_post)
    resp2 = await handler._handle_oauth_token(req2)
    assert resp2.status == 400


# ---------------------------------------------------------------------------
# Plugin integration
# ---------------------------------------------------------------------------


def _make_direct_config(**overrides: Any) -> MagicMock:
    """Create a mock config for direct mode with sensible defaults."""
    defaults: dict[str, Any] = {
        "instance_name": "TestMA",
        "connection_type": CONNECTION_TYPE_DIRECT,
        "skill_id": "test-skill-id",
        "skill_token": "test-skill-token",
        "direct_access_token": "existing-token",
        "direct_client_secret": TEST_CLIENT_SECRET,
        "exposed_players": None,
        "cloud_instance_id": "",
        "cloud_instance_password": "",
        "cloud_connection_token": "",
        "log_level": "GLOBAL",
    }
    defaults.update(overrides)
    config = MagicMock()
    # accept the optional default arg too: get_setup_value falls through via
    # get_config_value(key, default), a two-argument call
    config.get_value = MagicMock(side_effect=lambda key, *_: defaults.get(key, ""))
    return config


@pytest.mark.asyncio
async def test_start_direct_mode_registers_routes(mock_mass: MagicMock) -> None:
    """_start_direct_mode should create handler and register routes."""
    config = _make_direct_config()
    plugin = YandexSmartHomePlugin(
        mass=mock_mass,
        manifest=MagicMock(domain="yandex_smarthome"),
        config=config,
        supported_features=set(),
    )
    await plugin.handle_async_init()
    await plugin.loaded_in_mass()

    assert plugin._direct_handler is not None
    assert mock_mass.webserver.register_dynamic_route.call_count == 10
    assert plugin._state_notifier is not None


@pytest.mark.asyncio
async def test_start_direct_mode_missing_skill_id(mock_mass: MagicMock) -> None:
    """
    Direct mode still registers HTTP routes when skill_id is missing.

    HTTP routes need to be live so Yandex's backend validation during
    auto-create can succeed; the state notifier is skipped because
    there is no skill to push state to yet.
    """
    config = _make_direct_config(skill_id="")
    plugin = YandexSmartHomePlugin(
        mass=mock_mass,
        manifest=MagicMock(domain="yandex_smarthome"),
        config=config,
        supported_features=set(),
    )
    plugin.logger = MagicMock()
    await plugin.handle_async_init()
    await plugin.loaded_in_mass()

    assert plugin._direct_handler is not None
    assert mock_mass.webserver.register_dynamic_route.call_count == 10
    assert plugin._state_notifier is None
    plugin.logger.info.assert_called()


@pytest.mark.asyncio
async def test_unload_cleans_up_direct(mock_mass: MagicMock) -> None:
    """unload() should unregister routes and stop notifier."""
    config = _make_direct_config()
    plugin = YandexSmartHomePlugin(
        mass=mock_mass,
        manifest=MagicMock(domain="yandex_smarthome"),
        config=config,
        supported_features=set(),
    )
    await plugin.handle_async_init()
    await plugin.loaded_in_mass()
    await plugin.unload()

    assert plugin._direct_handler is None
    assert plugin._state_notifier is None
