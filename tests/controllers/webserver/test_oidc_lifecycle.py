"""OIDC logout delivery, durable session association and role synchronization."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import jwt
import pytest
from aiohttp import web
from music_assistant_models.auth import Scope, UserRole
from music_assistant_models.errors import AuthenticationFailed

from music_assistant.controllers.webserver.helpers.oidc import BACKCHANNEL_LOGOUT_EVENT
from music_assistant.controllers.webserver.helpers.oidc_provider import (
    CONF_OIDC_BACKCHANNEL_LOGOUT,
    CONF_OIDC_GROUPS_CLAIM,
    CONF_OIDC_ROLE_MAPPING,
    OIDCLoginProvider,
    parse_role_mapping,
)
from music_assistant.helpers.datetime import utc

from .test_oidc import CLIENT_ID, IdentityProvider, auth_manager, client, idp, provider

__all__ = ["auth_manager", "client", "idp", "provider"]

if TYPE_CHECKING:
    from aiohttp.pytest_plugin import AiohttpClient
    from music_assistant_models.auth import User

    from music_assistant.controllers.webserver.helpers.oidc import OIDCClient
    from music_assistant.controllers.webserver.helpers.oidc_sessions import OIDCSession


def configure(
    provider: OIDCLoginProvider, mapping: str = "", claim: str = "groups"
) -> OIDCLoginProvider:
    """Reload the provider through its normal configuration path."""
    auth = provider.auth_manager
    values: dict[str, str | bool] = {
        CONF_OIDC_BACKCHANNEL_LOGOUT: True,
        CONF_OIDC_ROLE_MAPPING: mapping,
        CONF_OIDC_GROUPS_CLAIM: claim,
    }
    for key, value in values.items():
        auth.webserver.config.values[key].value = value
    auth._setup_oidc_provider()
    result = auth.login_providers["oidc"]
    assert isinstance(result, OIDCLoginProvider)
    return result


async def sign_in(
    provider: OIDCLoginProvider, idp: IdentityProvider, sid: str = "session-one"
) -> tuple[User, str, OIDCSession]:
    """Complete a signed code exchange and issue a tracked application session."""
    idp.overrides.update({"sid": sid, "iat": int(utc().timestamp()) - 1})
    user = await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    session = idp.pending.oidc_session
    assert session is not None
    token = await provider.auth_manager.oidc_sessions.create_token(user, "test browser", session)
    return user, token, session


def logout_token(idp: IdentityProvider, **overrides: Any) -> str:
    """Sign a standards-compliant logout event, with optional fault injection."""
    now = int(utc().timestamp())
    return jwt.encode(
        {
            "iss": idp.issuer,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "jti": "logout-one",
            "sid": "session-one",
            "events": {BACKCHANNEL_LOGOUT_EVENT: {}},
            **overrides,
        },
        idp.key,
        algorithm="RS256",
        headers={"kid": idp.kid, "typ": "logout+jwt"},
    )


async def token_exists(provider: OIDCLoginProvider, token: str) -> bool:
    """Check persistent revocation state without logging token contents."""
    auth = provider.auth_manager
    return bool(
        await auth.database.get_row(
            "auth_tokens", {"token_id": auth.jwt_helper.decode_token(token)["jti"]}
        )
    )


async def test_logout_scopes_sessions_and_preserves_other_login_methods(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """A session logout closes all its tabs, preserving other devices and local/API tokens."""
    provider = configure(provider)
    user, first, session = await sign_in(provider, idp)
    _, second, _ = await sign_in(provider, idp)
    newer = await provider.auth_manager.oidc_sessions.create_token(
        user, "overlapping exchange", replace(session, issued_at=utc().timestamp() + 1)
    )
    _, other_session, _ = await sign_in(provider, idp, "session-two")
    local = await provider.auth_manager.create_token(user, "password login")
    api = await provider.auth_manager.create_token(user, "API", is_long_lived=True)
    with patch.object(
        provider.auth_manager.webserver, "disconnect_websockets_for_token"
    ) as disconnect:
        await provider.handle_backchannel_logout(logout_token(idp))
        assert disconnect.call_count == 3
    assert not await token_exists(provider, first)
    assert not await token_exists(provider, second)
    assert not await token_exists(provider, newer)
    for token in (other_session, local, api):
        assert await token_exists(provider, token)


async def test_subject_logout_is_bound_to_issuer_client_and_subject(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """Subject logout reaches every matching OIDC session and no unrelated identity/client."""
    provider = configure(provider)
    user, first, session = await sign_in(provider, idp)
    _, second, _ = await sign_in(provider, idp, "session-two")
    others = []
    for sibling in (
        replace(session, issuer="https://other.example/"),
        replace(session, client_id="other-client"),
        replace(session, subject="other-subject"),
        replace(session, issued_at=utc().timestamp() + 1),
    ):
        others.append(
            await provider.auth_manager.oidc_sessions.create_token(user, "other", sibling)
        )
    claims = jwt.decode(logout_token(idp), options={"verify_signature": False})
    claims.pop("sid")
    claims["sub"] = session.subject
    token = jwt.encode(claims, idp.key, algorithm="RS256", headers={"kid": idp.kid})
    await provider.handle_backchannel_logout(token)
    assert not await token_exists(provider, first)
    assert not await token_exists(provider, second)
    assert all([await token_exists(provider, token) for token in others])
    newer = replace(session, issued_at=utc().timestamp() + 1)
    assert await provider.auth_manager.oidc_sessions.create_token(user, "new sign-in", newer)


async def test_logout_replay_and_inflight_callback_remain_safe_after_restart(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """Restart retains session links and replay state, and an old callback cannot recreate access."""
    provider = configure(provider)
    user, token, session = await sign_in(provider, idp)
    auth = provider.auth_manager
    await auth.close()
    await auth.setup()
    provider = configure(provider)
    signed = logout_token(idp)
    await provider.handle_backchannel_logout(signed)
    assert not await token_exists(provider, token)
    await auth.close()
    await auth.setup()
    provider = configure(provider)
    with pytest.raises(AuthenticationFailed, match="session has ended"):
        await auth.oidc_sessions.create_token(user, "late callback", session)
    newer = replace(session, issued_at=int(utc().timestamp()) + 1)
    with pytest.raises(AuthenticationFailed, match="session has ended"):
        await auth.oidc_sessions.validate_session(newer)
    with pytest.raises(AuthenticationFailed, match="session has ended"):
        await auth.oidc_sessions.create_token(user, "late callback with newer ID token", newer)
    new_session = replace(newer, session_id="session-two")
    new_token = await auth.oidc_sessions.create_token(user, "new sign-in", new_session)
    await provider.handle_backchannel_logout(signed)
    assert await token_exists(provider, new_token)


async def test_logout_retry_finishes_interrupted_revocation(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """A persisted replay marker cannot hide a token deletion that previously failed."""
    provider = configure(provider)
    _, token, _ = await sign_in(provider, idp)
    signed = logout_token(idp)
    database = provider.auth_manager.database
    execute_write = database.execute_write

    async def fail_bulk_delete(query: str, values: dict[str, Any] | None = None) -> None:
        if query.startswith("DELETE FROM auth_tokens"):
            raise RuntimeError("storage error")
        await execute_write(query, values)

    with (
        patch.object(database, "execute_write", side_effect=fail_bulk_delete),
        patch.object(
            provider.auth_manager.webserver, "disconnect_websockets_for_token"
        ) as disconnect,
        pytest.raises(RuntimeError, match="storage error"),
    ):
        await provider.handle_backchannel_logout(signed)
    disconnect.assert_not_called()
    assert await token_exists(provider, token)
    await provider.handle_backchannel_logout(signed)
    assert not await token_exists(provider, token)


@pytest.mark.parametrize("trigger", ["logout", "mapping_denial"])
async def test_matching_tokens_are_revoked_with_one_bulk_write(
    provider: OIDCLoginProvider, idp: IdentityProvider, trigger: str
) -> None:
    """Logout and mapping denial delete every matching token before disconnecting its clients."""
    provider = configure(provider, '{"members":"user"}')
    idp.overrides["groups"] = ["members"]
    user, first, session = await sign_in(provider, idp)
    auth = provider.auth_manager
    matching = [first]
    for index in range(11):
        matching.append(await auth.oidc_sessions.create_token(user, f"tab {index}", session))
    local = await auth.create_token(user, "local login")
    unrelated = await auth.oidc_sessions.create_token(
        user, "other identity", replace(session, subject="other", session_id="other")
    )
    with (
        patch.object(auth.database, "execute_write", wraps=auth.database.execute_write) as write,
        patch.object(auth.webserver, "disconnect_websockets_for_token") as disconnect,
    ):
        if trigger == "logout":
            await provider.handle_backchannel_logout(logout_token(idp))
        else:
            idp.overrides["groups"] = []
            with pytest.raises(AuthenticationFailed):
                await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    deletes = [
        call for call in write.call_args_list if call.args[0].startswith("DELETE FROM auth_tokens")
    ]
    assert len(deletes) == 1
    assert {call.args[0] for call in disconnect.call_args_list} == {
        auth.jwt_helper.decode_token(token)["jti"] for token in matching
    }
    for token in matching:
        assert not await token_exists(provider, token)
    assert await token_exists(provider, local)
    assert await token_exists(provider, unrelated)


async def test_cancelled_bulk_revocation_finishes_websocket_disconnection(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """Cancellation after deletion cannot leave connected clients using revoked tokens."""
    provider = configure(provider)
    _, token, _ = await sign_in(provider, idp)
    auth = provider.auth_manager
    deleted = asyncio.Event()
    finish = asyncio.Event()
    execute_write = auth.database.execute_write

    async def delayed_delete(query: str, values: dict[str, Any] | None = None) -> None:
        await execute_write(query, values)
        if query.startswith("DELETE FROM auth_tokens"):
            deleted.set()
            await finish.wait()

    with (
        patch.object(auth.database, "execute_write", side_effect=delayed_delete),
        patch.object(auth.webserver, "disconnect_websockets_for_token") as disconnect,
    ):
        task = asyncio.create_task(provider.handle_backchannel_logout(logout_token(idp)))
        try:
            await asyncio.wait_for(deleted.wait(), timeout=5)
            assert not await token_exists(provider, token)
            task.cancel()
            await asyncio.wait({task}, timeout=0.02)
            assert not task.done()
            task.cancel()
            await asyncio.wait({task}, timeout=0.02)
            assert not task.done()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            finish.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        disconnect.assert_called_once_with(auth.jwt_helper.decode_token(token)["jti"])


async def test_concurrent_logout_and_token_creation_cannot_leave_live_session(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """Whichever operation gets the store lock first, the revoked identity stays logged out."""
    provider = configure(provider)
    user, _, session = await sign_in(provider, idp)
    results = await asyncio.gather(
        provider.handle_backchannel_logout(logout_token(idp)),
        provider.auth_manager.oidc_sessions.create_token(user, "racing callback", session),
        return_exceptions=True,
    )
    assert results[0] is None
    if isinstance(results[1], str):
        assert not await token_exists(provider, results[1])
    else:
        assert isinstance(results[1], AuthenticationFailed)


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://other.example/"},
        {"aud": "other-client"},
        {"iat": 1},
        {"iat": True},
        {"iat": 999999999999},
        {"exp": 1},
        {"exp": True},
        {"nonce": "not-a-logout"},
        {"events": {}},
        {"events": {BACKCHANNEL_LOGOUT_EVENT: []}},
        {"sid": ""},
        {"sid": None},
        {"sid": 123},
        {"sid": "s" * 256},
        {"jti": ""},
        {"jti": []},
        {"sub": []},
    ],
)
async def test_invalid_logout_claims_do_not_revoke_sessions(
    provider: OIDCLoginProvider, idp: IdentityProvider, changes: dict[str, Any]
) -> None:
    """Only a signed, current logout event for this client can end an application session."""
    provider = configure(provider)
    _, token, _ = await sign_in(provider, idp)
    with pytest.raises(AuthenticationFailed):
        await provider.handle_backchannel_logout(logout_token(idp, **changes))
    assert await token_exists(provider, token)


@pytest.mark.parametrize("missing", ["iss", "aud", "iat", "exp", "jti", "events", "sid"])
async def test_required_logout_claims(
    client: OIDCClient, idp: IdentityProvider, missing: str
) -> None:
    """An otherwise signed token cannot omit required logout claims or its session identity."""
    claims = jwt.decode(logout_token(idp), options={"verify_signature": False})
    claims.pop(missing)
    signed = jwt.encode(claims, idp.key, algorithm="RS256", headers={"kid": idp.kid})
    with pytest.raises((ValueError, jwt.InvalidTokenError)):
        await client.validate_logout_token(signed)


async def test_http_logout_endpoint_is_opt_in_and_validates_form(
    provider: OIDCLoginProvider, idp: IdentityProvider, aiohttp_client: AiohttpClient
) -> None:
    """A real form POST needs no browser cookies; malformed and oversized requests fail closed."""
    app = web.Application()
    app.router.add_post(
        "/auth/oidc/backchannel-logout",
        provider.auth_manager.webserver._handle_oidc_backchannel_logout,
    )
    http = await aiohttp_client(app)
    signed = logout_token(idp)
    path = "/auth/oidc/backchannel-logout"
    assert (await http.post(path, data={"logout_token": signed})).status == 404
    provider = configure(provider)
    _, token, _ = await sign_in(provider, idp)
    for data, content_type in (
        ("{}", "application/json"),
        ("logout_token=x&logout_token=y", "application/x-www-form-urlencoded"),
        ("logout_token=" + "x" * 17000, "application/x-www-form-urlencoded"),
    ):
        result = await http.post(path, data=data, headers={"Content-Type": content_type})
        assert result.status == 400
        assert result.headers["Cache-Control"] == "no-store"
        assert await token_exists(provider, token)
    result = await http.post(path, data={"logout_token": signed})
    assert result.status == 200
    assert not await token_exists(provider, token)


async def test_group_mapping_grants_and_removes_admin_without_touching_local_admin(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """Explicit ordered mappings synchronize roles on each sign-in and preserve recovery access."""
    provider = configure(provider, '{"music-admins":"admin","music-users":"user"}')
    idp.overrides["groups"] = ["music-users", "music-admins"]
    user, token, _ = await sign_in(provider, idp)
    assert user.role == UserRole.ADMIN
    idp.overrides["groups"] = ["music-users"]
    with patch.object(
        provider.auth_manager.webserver, "disconnect_websockets_for_user"
    ) as disconnect:
        demoted = await provider.complete_authorization(idp.pending, {"code": "valid-code"})
        disconnect.assert_called_once_with(user.user_id)
    assert demoted.role == UserRole.USER
    assert demoted.user_id == user.user_id
    assert await token_exists(provider, token)
    refreshed = await provider.auth_manager.authenticate_with_token(token)
    assert refreshed is not None
    assert refreshed.role == UserRole.USER
    admin = await provider.auth_manager.get_user_by_username("admin")
    assert admin is not None
    assert admin.role == UserRole.ADMIN
    assert (
        await provider.auth_manager.authenticate_with_credentials(
            "builtin", {"username": "admin", "password": "local-test-password"}
        )
    ).success


@pytest.mark.parametrize("groups", [None, [], "music-admins", [1], ["unmapped"]])
async def test_missing_or_unmapped_groups_deny_and_revoke_existing_oidc_sessions(
    provider: OIDCLoginProvider, idp: IdentityProvider, groups: Any
) -> None:
    """Removing mapped access never silently retains administrator OIDC sessions."""
    provider = configure(provider, '{"music-admins":"admin"}')
    idp.overrides["groups"] = ["music-admins"]
    _, token, _ = await sign_in(provider, idp)
    idp.overrides["groups"] = groups
    with pytest.raises(AuthenticationFailed, match="role mapping denied"):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    assert not await token_exists(provider, token)


async def test_group_denial_revokes_sessions_with_provider_clock_ahead(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """Clock skew must not preserve an administrator session after mapped access is removed."""
    provider = configure(provider, '{"music-admins":"admin"}')
    idp.overrides["groups"] = ["music-admins"]
    user, _, session = await sign_in(provider, idp)
    token = await provider.auth_manager.oidc_sessions.create_token(
        user, "ahead clock", replace(session, issued_at=utc().timestamp() + 20)
    )
    idp.overrides["groups"] = []
    with pytest.raises(AuthenticationFailed, match="role mapping denied"):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    assert not await token_exists(provider, token)


@pytest.mark.parametrize("newer_token", [False, True])
async def test_logged_out_callback_cannot_change_mapped_role(
    provider: OIDCLoginProvider, idp: IdentityProvider, newer_token: bool
) -> None:
    """A late callback cannot promote an account after the IdP session has ended."""
    provider = configure(provider, '{"music-admins":"admin","music-users":"user"}')
    idp.overrides["groups"] = ["music-users"]
    user, _, _ = await sign_in(provider, idp)
    await provider.handle_backchannel_logout(logout_token(idp))
    idp.overrides["groups"] = ["music-admins"]
    if newer_token:
        idp.overrides["iat"] = int(utc().timestamp()) + 1
    with pytest.raises(AuthenticationFailed, match="session has ended"):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    current = await provider.auth_manager.get_user(user.user_id)
    assert current is not None
    assert current.role == UserRole.USER


async def test_group_claim_from_userinfo_requires_matching_subject(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """A custom group claim may come from subject-bound UserInfo even with an ID-token username."""
    provider = configure(provider, '{"listeners":"user"}', "music_groups")
    idp.userinfo_overrides["music_groups"] = ["listeners"]
    user, _, _ = await sign_in(provider, idp)
    assert user.role == UserRole.USER
    idp.userinfo_subject = "different-subject"
    with pytest.raises(AuthenticationFailed):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})


@pytest.mark.parametrize(
    "mapping", ["[]", '{"users":"service"}', '{"users":true}', '{"":"user"}', "not json"]
)
def test_invalid_role_mapping_is_rejected(mapping: str) -> None:
    """Malformed configuration cannot enable implicit grants or map the HA-only Service role."""
    with pytest.raises(ValueError, match="OpenID Connect"):
        parse_role_mapping(mapping)


async def test_unknown_mapped_role_denies_new_account(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """A typo or deleted role cannot create an account with unintended permissions."""
    provider = configure(provider, '{"listeners":"missing-role"}')
    idp.overrides["groups"] = ["listeners"]
    with pytest.raises(AuthenticationFailed):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    assert len(await provider.auth_manager.database.get_rows("users")) == 1


async def test_additive_tables_preserve_existing_users_passwords_and_tokens(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """Upgrading a database without OIDC session tables is repeatable and preserves its data."""
    auth = provider.auth_manager
    user = await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    token = await auth.create_token(user, "pre-upgrade session")
    await auth.database.execute_write("DROP TABLE oidc_sessions")
    await auth.database.execute_write("DROP TABLE oidc_logouts")
    await auth.close()
    await auth.setup()
    await auth.oidc_sessions.setup()
    assert await token_exists(provider, token)
    assert (await auth.get_user(user.user_id)) is not None
    assert not await auth.database.get_rows("oidc_sessions")
    assert (
        await auth.authenticate_with_credentials(
            "builtin", {"username": "admin", "password": "local-test-password"}
        )
    ).success


async def test_mapping_supports_custom_roles(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """Configured custom role IDs use the same registered scopes as locally assigned roles."""
    role = await provider.auth_manager.create_role("Own music", [Scope.CONFIG_PROVIDERS_OWN])
    provider = configure(provider, '{"listeners":"' + role.role_id + '"}')
    idp.overrides["groups"] = ["listeners"]
    user, _, _ = await sign_in(provider, idp)
    assert user.role == role.role_id


async def test_last_administrator_cannot_be_demoted_by_group_mapping(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """A group change cannot silently remove the last administrator or leave its OIDC token live."""
    provider = configure(provider, '{"admins":"admin","listeners":"user"}')
    idp.overrides["groups"] = ["admins"]
    user, token, _ = await sign_in(provider, idp)
    local = await provider.auth_manager.get_user_by_username("admin")
    assert local is not None
    await provider.auth_manager.database.update(
        "users", {"user_id": local.user_id}, {"enabled": False}
    )
    idp.overrides["groups"] = ["listeners"]
    with pytest.raises(AuthenticationFailed, match="role mapping denied"):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    stored = await provider.auth_manager.get_user(user.user_id)
    assert stored is not None
    assert stored.role == UserRole.ADMIN
    assert not await token_exists(provider, token)


async def test_source_ownership_conflict_denies_access_without_destroying_sources(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """A forbidden downgrade fails closed for OIDC while retaining existing ownership data."""
    provider = configure(provider, '{"listeners":"user","visitors":"guest"}')
    idp.overrides["groups"] = ["listeners"]
    user, token, _ = await sign_in(provider, idp)
    idp.overrides["groups"] = ["visitors"]
    with (
        patch(
            "music_assistant.controllers.webserver.auth.own_music_sources",
            return_value=["personal-music"],
        ),
        pytest.raises(AuthenticationFailed, match="role mapping denied"),
    ):
        await provider.complete_authorization(idp.pending, {"code": "valid-code"})
    stored = await provider.auth_manager.get_user(user.user_id)
    assert stored is not None
    assert stored.role == UserRole.USER
    assert not await token_exists(provider, token)


async def test_invalid_mapping_configuration_preserves_local_login(
    provider: OIDCLoginProvider,
) -> None:
    """An invalid optional mapping disables only OIDC, leaving recovery login available."""
    auth = provider.auth_manager
    auth.webserver.config.values[CONF_OIDC_ROLE_MAPPING].value = "invalid json"
    auth._setup_oidc_provider()
    assert "oidc" not in auth.login_providers
    assert (
        await auth.authenticate_with_credentials(
            "builtin", {"username": "admin", "password": "local-test-password"}
        )
    ).success


async def test_unsigned_logout_and_id_token_cannot_log_a_user_out(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """ID tokens and unsigned copies of valid logout payloads are never accepted as logout."""
    provider = configure(provider)
    _, token, _ = await sign_in(provider, idp)
    claims = jwt.decode(logout_token(idp), options={"verify_signature": False})
    for invalid in (idp.token(), jwt.encode(claims, "", algorithm="none")):
        with pytest.raises(AuthenticationFailed, match="logout token"):
            await provider.handle_backchannel_logout(invalid)
        assert await token_exists(provider, token)
