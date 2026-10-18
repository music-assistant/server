"""Regression coverage for concurrent administration, browser origins and OIDC errors."""

from __future__ import annotations

import asyncio
import ssl
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientConnectionError
from aiohttp.test_utils import make_mocked_request
from jwt import InvalidTokenError
from music_assistant_models.auth import UserRole
from music_assistant_models.errors import AuthenticationFailed, InvalidDataError
from yarl import URL

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.oidc import OIDCClient, OIDCProtocolError
from music_assistant.controllers.webserver.helpers.oidc_provider import (
    CONF_OIDC_SCOPES,
    OIDCLoginProvider,
)
from music_assistant.helpers.json import json_loads

from .test_oidc import MA_URL, IdentityProvider, auth_manager, idp, provider
from .test_oidc_lifecycle import configure, sign_in

__all__ = ["auth_manager", "idp", "provider"]

if TYPE_CHECKING:
    from collections.abc import Mapping


@pytest.mark.parametrize("first_actor", ["oidc", "manual"])
@pytest.mark.parametrize("manual_action", ["demote", "disable", "delete"])
async def test_concurrent_admin_removals_preserve_an_enabled_administrator(
    provider: OIDCLoginProvider,
    idp: IdentityProvider,
    monkeypatch: pytest.MonkeyPatch,
    first_actor: str,
    manual_action: str,
) -> None:
    """OIDC demotion and manual administration cannot both remove the remaining admins."""
    provider = configure(provider, '{"admins":"admin","listeners":"user"}')
    idp.overrides["groups"] = ["admins"]
    oidc_admin, _, _ = await sign_in(provider, idp)
    auth = provider.auth_manager
    local_admin = await auth.get_user_by_username("admin")
    assert local_admin is not None
    monkeypatch.setattr(
        provider.mass,
        "music",
        SimpleNamespace(database=AsyncMock(), playlists=AsyncMock(), favorites=AsyncMock()),
        raising=False,
    )
    first_checked = asyncio.Event()
    finish_first = asyncio.Event()
    original_check = auth._ensure_not_last_admin
    checks = 0

    async def pause_first_check(row: Mapping[str, Any]) -> None:
        nonlocal checks
        await original_check(row)
        checks += 1
        if checks == 1:
            first_checked.set()
            await finish_first.wait()

    async def remove_admin(actor: str) -> Any:
        if actor == "oidc":
            return await provider.complete_authorization(idp.pending, {"code": "valid-code"})
        if manual_action == "demote":
            return await auth.update_user_role(local_admin.user_id, UserRole.USER, oidc_admin)
        if manual_action == "disable":
            return await auth.disable_user(local_admin.user_id)
        return await auth.delete_user(local_admin.user_id)

    idp.overrides["groups"] = ["listeners"]
    with (
        patch.object(auth, "_ensure_not_last_admin", side_effect=pause_first_check),
        patch(
            "music_assistant.controllers.webserver.auth.get_current_user", return_value=oidc_admin
        ),
    ):
        first = asyncio.create_task(remove_admin(first_actor))
        second = None
        try:
            await asyncio.wait_for(first_checked.wait(), timeout=5)
            second = asyncio.create_task(
                remove_admin("manual" if first_actor == "oidc" else "oidc")
            )
            await asyncio.wait({second}, timeout=0.2)
        finally:
            finish_first.set()
            results = await asyncio.gather(
                first, *([second] if second is not None else []), return_exceptions=True
            )
    assert len(results) == 2
    errors = [result for result in results if isinstance(result, BaseException)]
    assert len(errors) == 1
    assert isinstance(errors[0], (AuthenticationFailed, InvalidDataError))
    rows = await auth.database.get_rows("users")
    assert sum(row["role"] == UserRole.ADMIN and row["enabled"] for row in rows) == 1


@pytest.mark.parametrize("return_url", [None, "musicassistant://auth"])
@pytest.mark.parametrize(
    ("request_host", "forwarded_host", "expected_base"),
    [
        ("music.example", None, MA_URL),
        ("ma-internal:8095", "music.example", MA_URL),
        ("ma-lan.local:8095", None, "http://ma-lan.local:8095"),
    ],
)
async def test_http_authorization_selects_the_browser_facing_oidc_origin(
    provider: OIDCLoginProvider,
    monkeypatch: pytest.MonkeyPatch,
    return_url: str | None,
    request_host: str,
    forwarded_host: str | None,
    expected_base: str,
) -> None:
    """Public and proxied authorization use the public entry point while LAN access stays local."""
    monkeypatch.setattr(
        WebserverController, "base_url", property(lambda _: "http://ma-lan.local:8095")
    )
    monkeypatch.setattr(WebserverController, "external_url", property(lambda _: MA_URL))
    headers = {"Host": request_host}
    if forwarded_host:
        headers["X-Forwarded-Host"] = forwarded_host
    query = {"provider_id": "oidc"}
    if return_url:
        query["return_url"] = return_url
    auth = provider.auth_manager
    response = await auth.webserver._handle_auth_authorize(
        make_mocked_request("GET", str(URL("/auth/authorize").with_query(query)), headers=headers)
    )
    assert response.status == 200
    assert response.text is not None
    authorization_url = URL(json_loads(response.text)["authorization_url"])
    assert str(authorization_url.origin()) == expected_base
    assert authorization_url.path == "/auth/oidc/start"
    assert authorization_url.query.get("return_url") == return_url
    assert not response.cookies
    assert not auth.pending_logins._pending


@pytest.mark.parametrize(
    ("tls", "forwarded_scheme", "sets_cookie"),
    [(False, None, False), (True, None, True), (False, "https", True), (False, "http", False)],
)
async def test_oidc_start_uses_the_browser_facing_scheme(
    provider: OIDCLoginProvider,
    tls: bool,
    forwarded_scheme: str | None,
    sets_cookie: bool,
) -> None:
    """HTTP entry redirects to HTTPS before binding state; HTTPS proxy entry sets the cookie."""
    headers = {"Host": "music.example"}
    if forwarded_scheme:
        headers.update(
            {
                "Host": "ma-internal:8095",
                "X-Forwarded-Host": "music.example",
                "X-Forwarded-Proto": forwarded_scheme,
            }
        )
    response = await provider.auth_manager.webserver._handle_oidc_start(
        make_mocked_request(
            "GET",
            "/auth/oidc/start",
            headers=headers,
            sslcontext=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER) if tls else None,
        )
    )
    assert response.status == 302
    if sets_cookie:
        assert "state" in URL(response.headers["Location"]).query
        assert response.cookies
        assert all(cookie["secure"] for cookie in response.cookies.values())
    else:
        assert response.headers["Location"] == f"{MA_URL}/auth/oidc/start"
        assert not response.cookies
        assert not provider.auth_manager.pending_logins._pending


async def test_configured_scopes_release_scope_gated_groups(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """UI-configured extra scopes enable groups that neither token nor UserInfo supplied before."""
    provider = configure(provider, '{"admins":"admin"}')
    idp.groups_scope = "groups"
    idp.userinfo_overrides["groups"] = ["admins"]
    url = await provider.build_authorization_url(idp.pending)
    assert url is not None
    async with idp.session.get(url) as response:
        assert response.status == 204
    with pytest.raises(AuthenticationFailed, match="role mapping denied"):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    auth = provider.auth_manager
    auth.webserver.config.values[CONF_OIDC_SCOPES].value = "profile groups groups"
    auth._setup_oidc_provider()
    configured = auth.login_providers["oidc"]
    assert isinstance(configured, OIDCLoginProvider)
    url = await configured.build_authorization_url(idp.pending)
    assert url is not None
    assert URL(url).query["scope"] == "openid profile groups"
    async with idp.session.get(url) as response:
        assert response.status == 204
    user = await configured.complete_authorization(idp.pending, {"code": "valid-code"})
    assert user.role == UserRole.ADMIN


@pytest.mark.parametrize(
    "scopes",
    [
        "profile\ngroups",
        "profile\tgroups",
        'profile "groups"',
        "profile \\groups",
        "grüppe",
        "x" * 1025,
    ],
)
def test_invalid_scope_tokens_are_rejected(provider: OIDCLoginProvider, scopes: str) -> None:
    """Invalid scope syntax cannot reach an authorization request."""
    client = provider.client
    with pytest.raises(ValueError, match="scopes"):
        OIDCClient(client.session, client.issuer, client.client_id, "test-secret", scopes=scopes)


@pytest.mark.parametrize(
    "operation", ["authorization_url", "authenticate", "validate_logout_token"]
)
async def test_unexpected_oidc_errors_propagate(
    provider: OIDCLoginProvider, idp: IdentityProvider, operation: str
) -> None:
    """Programming errors retain their diagnostic traceback instead of becoming login failures."""
    provider = configure(provider)
    with (
        patch.object(provider.client, operation, new=AsyncMock(side_effect=RuntimeError("bug"))),
        pytest.raises(RuntimeError, match="bug"),
    ):
        await _invoke_oidc_operation(provider, idp, operation)


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        ("authorization_url", ClientConnectionError("sensitive response")),
        ("authenticate", InvalidTokenError("sensitive token")),
        ("authenticate", OIDCProtocolError("sensitive response")),
        ("validate_logout_token", TimeoutError("sensitive response")),
    ],
)
async def test_expected_oidc_failures_keep_credential_safe_messages(
    provider: OIDCLoginProvider,
    idp: IdentityProvider,
    operation: str,
    error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Expected transport and protocol errors retain generic feedback without logging credentials."""
    provider = configure(provider)
    with (
        patch.object(provider.client, operation, new=AsyncMock(side_effect=error)),
        pytest.raises(AuthenticationFailed) as failure,
    ):
        await _invoke_oidc_operation(provider, idp, operation)
    assert "sensitive" not in str(failure.value)
    assert "sensitive" not in caplog.text


@pytest.mark.parametrize("expected", [False, True])
async def test_oidc_start_only_translates_expected_failures(
    provider: OIDCLoginProvider, expected: bool
) -> None:
    """Unexpected startup errors reach the server error handler, while provider outages use 503."""
    error = AuthenticationFailed("provider unavailable") if expected else RuntimeError("bug")
    auth = provider.auth_manager
    request = make_mocked_request("GET", "/auth/oidc/start", headers={"Host": "music.example"})
    with patch.object(auth, "get_authorization_url", new=AsyncMock(side_effect=error)):
        if expected:
            assert (await auth.webserver._handle_oidc_start(request)).status == 503
        else:
            with pytest.raises(RuntimeError, match="bug"):
                await auth.webserver._handle_oidc_start(request)


@pytest.mark.parametrize(
    "metadata",
    [
        {"userinfo_endpoint": None},
        {"response_types_supported": None},
        {"token_endpoint_auth_methods_supported": None},
        {"code_challenge_methods_supported": None},
        {"id_token_signing_alg_values_supported": [{}]},
    ],
)
async def test_malformed_discovery_is_an_expected_protocol_failure(
    provider: OIDCLoginProvider, idp: IdentityProvider, metadata: dict[str, Any]
) -> None:
    """Untrusted response types cannot turn malformed discovery into an unexpected TypeError."""
    idp.metadata_overrides.update(metadata)
    with pytest.raises(AuthenticationFailed):
        await provider.build_authorization_url(idp.pending)


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null"])
async def test_malformed_token_response_is_an_expected_protocol_failure(
    provider: OIDCLoginProvider, idp: IdentityProvider, body: bytes
) -> None:
    """Invalid JSON or a non-object token response is safely rejected as a protocol failure."""
    idp.token_body = body
    with pytest.raises(AuthenticationFailed):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})


async def _invoke_oidc_operation(
    provider: OIDCLoginProvider, idp: IdentityProvider, operation: str
) -> None:
    if operation == "authorization_url":
        await provider.build_authorization_url(idp.pending)
    elif operation == "authenticate":
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    else:
        await provider.handle_backchannel_logout("test-token")
