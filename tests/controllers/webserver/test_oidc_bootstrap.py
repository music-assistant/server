"""Deployment-managed OIDC bootstrap on fresh and existing installations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import AuthProviderType, UserRole
from music_assistant_models.errors import AuthenticationFailed
from yarl import URL

from music_assistant.constants import HOMEASSISTANT_SYSTEM_USER
from music_assistant.controllers.webserver.controller import WebserverController, _oidc_cookie_name
from music_assistant.controllers.webserver.helpers.auth_providers import BuiltinLoginProvider
from music_assistant.controllers.webserver.helpers.oidc_deployment import (
    OIDC_CONFIG_FILE_ENV,
    load_oidc_deployment,
)
from music_assistant.controllers.webserver.helpers.oidc_provider import OIDCLoginProvider

from .test_oidc import CLIENT_ID, CLIENT_SECRET, MA_URL, IdentityProvider, auth_manager, idp
from .test_setup import ACCOUNT, _post_setup, _served_app, _start_webserver

__all__ = ["auth_manager", "idp"]

if TYPE_CHECKING:
    from music_assistant.controllers.webserver.auth import AuthenticationManager
    from music_assistant.mass import MusicAssistant


def deployment_settings(issuer: str) -> dict[str, Any]:
    """Return a confidential client with explicit bootstrap administrator membership."""
    return {
        "base_url": MA_URL,
        "issuer": issuer,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "name": "Authentik",
        "bootstrap_admin": True,
        "allow_self_registration": True,
        "backchannel_logout": True,
        "role_mapping": {"music-admins": "admin", "music-users": "user"},
    }


@pytest.fixture
def deployment_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idp: IdentityProvider) -> Path:
    """Mount deployment settings independently of writable server storage."""
    path = tmp_path / "oidc.json"
    path.write_text(json.dumps(deployment_settings(idp.issuer)))
    monkeypatch.setenv(OIDC_CONFIG_FILE_ENV, str(path))
    return path


@pytest.fixture
async def bootstrap_server(
    mass_minimal: MusicAssistant,
    idp: IdentityProvider,
    deployment_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[WebserverController]:
    """Start authentication with no pre-created users or recovery password."""
    assert deployment_file.is_file()
    monkeypatch.setattr(mass_minimal, "_http_session", idp.session)
    server = await _start_webserver(mass_minimal)
    try:
        yield server
    finally:
        await server.auth.close()


@pytest.mark.parametrize("entry", ["/", "/login", "/setup"])
async def test_fresh_browser_bootstraps_a_mapped_administrator(
    bootstrap_server: WebserverController, idp: IdentityProvider, entry: str
) -> None:
    """All initial entry points use bound OIDC and create an admin without a password."""
    server = bootstrap_server
    assert not bool(server.auth.has_users)
    handler = {
        "/": server._handle_index,
        "/login": server._handle_login_page,
        "/setup": server._handle_setup_page,
    }[entry]
    response = await handler(
        make_mocked_request(
            "GET", entry, headers={"Host": "music.example", "X-Forwarded-Proto": "https"}
        )
    )
    assert response.status == 302
    state = URL(response.headers["Location"]).query["state"]
    pending = server.auth.pending_logins.get(state)
    assert pending is not None
    assert pending.redirect_uri == f"{MA_URL}/auth/callback?provider_id=oidc"
    idp.pending = pending
    idp.overrides["groups"] = ["music-admins"]
    cookie_name = _oidc_cookie_name(state)
    callback_url = str(
        URL("/auth/callback").with_query(provider_id="oidc", code="valid-code", state=state)
    )
    assert (
        await server._handle_auth_callback(make_mocked_request("GET", callback_url))
    ).status == 400
    assert not bool(server.auth.has_users)
    request = make_mocked_request(
        "GET",
        callback_url,
        headers={"Cookie": f"{cookie_name}={response.cookies[cookie_name].value}"},
    )
    assert (await server._handle_auth_callback(request)).status == 200
    assert (await server._handle_auth_callback(request)).status == 400
    users = await server.auth.database.get_rows("users")
    assert len(users) == 1
    assert users[0]["role"] == "admin"
    links = await server.auth.database.get_rows("user_auth_providers")
    assert len(links) == 1
    assert links[0]["provider_type"] == "oidc"
    assert not await server.auth.database.get_rows(
        "user_auth_providers", {"provider_type": "builtin"}
    )
    assert server.auth.has_users
    assert not server.auth.oidc_bootstrap_required
    assert not server.mass.config.onboard_done
    with patch.object(server._server, "serve_static", _served_app()) as serve:
        assert (await server._handle_index(make_mocked_request("GET", "/"))).status == 200
        serve.assert_awaited_once()


@pytest.mark.parametrize("groups", [None, [], ["unmapped"], ["music-users"], "music-admins"])
async def test_non_administrators_cannot_claim_initial_setup(
    bootstrap_server: WebserverController, idp: IdentityProvider, groups: Any
) -> None:
    """A valid identity cannot bootstrap unless its verified groups map to admin."""
    idp.overrides["groups"] = groups
    provider = bootstrap_server.auth.login_providers["oidc"]
    with pytest.raises(AuthenticationFailed):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    assert not bootstrap_server.auth.has_users
    assert not await bootstrap_server.auth.database.get_rows("users")
    assert not await bootstrap_server.auth.database.get_rows("auth_tokens")


async def test_local_and_other_provider_creation_cannot_bypass_bootstrap(
    bootstrap_server: WebserverController,
) -> None:
    """The setup endpoint and common user creation path enforce the deployment policy."""
    assert (await _post_setup(bootstrap_server, ACCOUNT)).status == 403
    builtin = bootstrap_server.auth.login_providers["builtin"]
    assert isinstance(builtin, BuiltinLoginProvider)
    with pytest.raises(AuthenticationFailed):
        await builtin.create_user_with_password("visitor", "long-password", role=UserRole.ADMIN)
    for username, role in (
        ("ha-admin", UserRole.ADMIN),
        (HOMEASSISTANT_SYSTEM_USER, UserRole.ADMIN),
        ("other-service", UserRole.SERVICE),
    ):
        with pytest.raises(AuthenticationFailed):
            await bootstrap_server.auth.create_user(username, role=role)
    assert not await bootstrap_server.auth.database.get_rows("users")


async def test_addon_system_account_does_not_claim_oidc_bootstrap(
    bootstrap_server: WebserverController, idp: IdentityProvider
) -> None:
    """Add-on discovery can obtain a Service token while first-admin setup stays protected."""
    auth = bootstrap_server.auth
    token = await auth.get_homeassistant_system_user_token()
    assert await auth.get_homeassistant_system_user_token() == token
    system_user = await auth.get_homeassistant_system_user()
    assert system_user.username == HOMEASSISTANT_SYSTEM_USER
    assert system_user.role == UserRole.SERVICE
    assert not bool(auth.has_users)
    assert bool(auth.oidc_bootstrap_required)
    token_row = await auth.database.get_row(
        "auth_tokens", {"token_id": auth.jwt_helper.decode_token(token)["jti"]}
    )
    assert token_row is not None
    assert token_row["user_id"] == system_user.user_id
    provider = auth.login_providers["oidc"]
    idp.overrides["groups"] = ["music-users"]
    with pytest.raises(AuthenticationFailed):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    idp.overrides["groups"] = ["music-admins"]
    admin = await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    assert admin.role == UserRole.ADMIN
    assert admin.user_id != system_user.user_id
    assert auth.has_users
    assert not auth.oidc_bootstrap_required
    assert len(await auth.database.get_rows("users")) == 2


async def test_deployment_scopes_allow_scope_gated_admin_bootstrap(
    bootstrap_server: WebserverController, deployment_file: Path, idp: IdentityProvider
) -> None:
    """Mounted scopes can release group claims needed for the first mapped administrator."""
    settings = json.loads(deployment_file.read_text())
    settings["scopes"] = "profile groups"
    deployment_file.write_text(json.dumps(settings))
    auth = bootstrap_server.auth
    await auth.close()
    await auth.setup()
    provider = auth.login_providers["oidc"]
    idp.groups_scope = "groups"
    idp.userinfo_overrides["groups"] = ["music-admins"]
    url = await provider.build_authorization_url(idp.pending)
    assert url is not None
    assert URL(url).query["scope"] == "openid profile groups"
    async with idp.session.get(url) as response:
        assert response.status == 204
    admin = await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    assert admin.role == UserRole.ADMIN
    assert auth.has_users


async def test_bootstrap_rejects_untrusted_return_urls(
    bootstrap_server: WebserverController, idp: IdentityProvider
) -> None:
    """Initial sign-in cannot hand its administrator token to an untrusted destination."""
    response = await bootstrap_server._handle_index(
        make_mocked_request(
            "GET", "/?return_url=https://untrusted.example/", headers={"Host": "music.example"}
        )
    )
    assert response.status == 400
    assert idp.discovery_requests == 0
    assert not bootstrap_server.auth.pending_logins._pending


async def test_bootstrap_is_explicitly_opt_in(
    deployment_file: Path,
    mass_minimal: MusicAssistant,
    idp: IdentityProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mounted provider alone preserves the existing first-user setup behavior."""
    data = json.loads(deployment_file.read_text())
    data.pop("bootstrap_admin")
    deployment_file.write_text(json.dumps(data))
    monkeypatch.setattr(mass_minimal, "_http_session", idp.session)
    server = await _start_webserver(mass_minimal)
    try:
        assert not server.auth.oidc_bootstrap_required
        assert (await _post_setup(server, ACCOUNT)).status == 200
        assert (await server.auth.authenticate_with_credentials("builtin", ACCOUNT)).success
    finally:
        await server.auth.close()


async def test_empty_configuration_path_is_not_treated_as_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A defined but empty deployment variable must not reopen local setup."""
    monkeypatch.setenv(OIDC_CONFIG_FILE_ENV, "")
    with pytest.raises(ValueError, match="MASS_OIDC_CONFIG_FILE"):
        await load_oidc_deployment()


async def test_failed_exchange_does_not_reopen_local_setup(
    bootstrap_server: WebserverController, idp: IdentityProvider
) -> None:
    """An IdP outage leaves bootstrap reserved for a verified OIDC administrator."""
    idp.token_status = 503
    provider = bootstrap_server.auth.login_providers["oidc"]
    with pytest.raises(AuthenticationFailed):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    assert (await _post_setup(bootstrap_server, ACCOUNT)).status == 403
    assert not bootstrap_server.auth.has_users


async def test_bootstrap_survives_concurrency_and_restart(
    bootstrap_server: WebserverController, idp: IdentityProvider
) -> None:
    """Concurrent verified sign-ins share one durable passwordless administrator."""
    auth = bootstrap_server.auth
    provider = auth.login_providers["oidc"]
    idp.overrides["groups"] = ["music-admins"]
    first, second = await asyncio.gather(
        *(provider.complete_authorization(idp.pending, {"code": "valid-code"}) for _ in range(2))
    )
    assert first.user_id == second.user_id
    token = await auth.create_token(first, "bootstrap browser")
    await auth.close()
    await auth.setup()
    assert not auth.oidc_bootstrap_required
    restored = await auth.authenticate_with_token(token)
    assert restored is not None
    assert restored.user_id == first.user_id
    assert (await _post_setup(bootstrap_server, ACCOUNT)).status == 409
    idp.overrides.update({"sub": "member", "groups": ["music-users"]})
    member = await auth.login_providers["oidc"].complete_authorization(
        idp.pending, {"code": "valid-code"}
    )
    assert member.role == UserRole.USER
    assert not await auth.database.get_rows("user_auth_providers", {"provider_type": "builtin"})


async def test_file_settings_are_authoritative_and_not_stored(
    bootstrap_server: WebserverController, deployment_file: Path
) -> None:
    """UI settings cannot silently replace mounted credentials or the callback origin."""
    server = bootstrap_server
    original = deployment_file.read_bytes()
    server.config.values["base_url"].value = "http://wrong.example"
    server.config.values["oidc_enabled"].value = False
    server.config.values["oidc_client_secret"].value = "wrong-secret"
    server.auth._setup_oidc_provider()
    assert server.base_url == MA_URL
    provider = server.auth.login_providers["oidc"]
    assert isinstance(provider, OIDCLoginProvider)
    assert provider.client.client_id == CLIENT_ID
    entries = {entry.key: entry for entry in await server.get_config_entries()}
    assert not entries["oidc_managed"].hidden
    assert all(
        entry.hidden
        for key, entry in entries.items()
        if key.startswith("oidc_") and key != "oidc_managed"
    )
    assert entries["base_url"].hidden
    await server.mass.config.async_save()
    assert CLIENT_SECRET not in Path(server.mass.config.filename).read_text()
    assert deployment_file.read_bytes() == original


@pytest.mark.parametrize(
    "changes",
    [
        {"bootstrap_admin": "true"},
        {"allow_self_registration": False},
        {"role_mapping": {}},
        {"role_mapping": {"music-admins": "user"}},
        {"role_mapping": []},
        {"base_url": "http://music.example"},
        {"base_url": "https://music.example/path"},
        {"base_url": "https://music.example/?token=secret"},
        {"issuer": "http://issuer.example"},
        {"client_id": ""},
        {"client_secret": ""},
        {"token_auth_method": "unknown"},
        {"groups_claim": ""},
        {"scopes": ["groups"]},
        {"scopes": "profile\ngroups"},
        {"scopes": 'profile "groups"'},
        {"name": 123},
        {"unexpected": True},
    ],
)
async def test_invalid_deployment_is_rejected(
    deployment_file: Path, idp: IdentityProvider, changes: dict[str, Any]
) -> None:
    """Invalid configuration cannot silently enable local setup or weaken role checks."""
    deployment_file.write_text(json.dumps({**deployment_settings(idp.issuer), **changes}))
    with pytest.raises(ValueError, match=r"OIDC|OpenID|MASS_OIDC"):
        await load_oidc_deployment()


@pytest.mark.parametrize("contents", [None, "not-json-secret", "x" * 65537, "[]"])
async def test_unreadable_file_fails_before_authentication_starts(
    deployment_file: Path, mass_minimal: MusicAssistant, contents: str | None
) -> None:
    """An unreadable file must abort startup, without exposing its contents."""
    if contents is None:
        deployment_file.unlink()
    else:
        deployment_file.write_text(contents)
    server = WebserverController(mass_minimal)
    with pytest.raises(ValueError, match=r"OIDC|OpenID|MASS_OIDC") as error:
        await server.auth.setup()
    assert "not-json-secret" not in str(error.value)
    assert not server.auth.login_providers


async def test_existing_accounts_and_tokens_survive_enabling_bootstrap(
    auth_manager: AuthenticationManager,
    tmp_path: Path,
    idp: IdentityProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding deployment configuration leaves existing administrator credentials and sessions intact."""
    admin = await auth_manager.get_user_by_username("admin")
    assert admin is not None
    token = await auth_manager.create_token(admin, "existing session")
    path = tmp_path / "oidc.json"
    path.write_text(json.dumps(deployment_settings(idp.issuer)))
    monkeypatch.setenv(OIDC_CONFIG_FILE_ENV, str(path))
    await auth_manager.close()
    await auth_manager.setup()
    assert not auth_manager.oidc_bootstrap_required
    assert await auth_manager.authenticate_with_token(token)
    assert (
        await auth_manager.authenticate_with_credentials(
            "builtin", {"username": "admin", "password": "local-test-password"}
        )
    ).success
    assert not await auth_manager.database.get_rows(
        "user_auth_providers", {"provider_type": AuthProviderType.OIDC.value}
    )
