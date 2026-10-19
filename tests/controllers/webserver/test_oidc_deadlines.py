"""Original sign-in deadlines bound OIDC claim application and token issuance."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest
from music_assistant_models.auth import UserRole
from music_assistant_models.errors import AuthenticationFailed

from music_assistant.controllers.webserver.helpers.auth_providers import AuthResult
from music_assistant.controllers.webserver.helpers.login_flow import AuthTransport, PendingLogin
from music_assistant.controllers.webserver.helpers.oidc_provider import OIDCLoginProvider

from .test_oidc import IdentityProvider, auth_manager, idp, provider
from .test_oidc_lifecycle import configure, logout_token, sign_in, token_exists

__all__ = ["auth_manager", "idp", "provider"]


@pytest.mark.parametrize("delay", ["exchange", "user_link_lock"])
@pytest.mark.parametrize("existing_user", [False, True])
async def test_expired_logout_receipt_cannot_admit_a_delayed_callback(
    provider: OIDCLoginProvider, idp: IdentityProvider, delay: str, existing_user: bool
) -> None:
    """A callback cannot create an account or promote it after its logout receipt expires."""
    provider = configure(provider, '{"members":"user","admins":"admin"}')
    idp.overrides.update({"groups": ["members"], "exp": time.time() + 3600, "sid": "session-one"})
    auth = provider.auth_manager
    original_token = None
    if existing_user:
        _, original_token, _ = await sign_in(provider, idp)
    idp.overrides["groups"] = ["admins"]
    pending = _start_pending(provider, idp)
    reached = asyncio.Event()
    finish = asyncio.Event()
    authenticate = provider.client.authenticate

    async def delayed_authenticate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        claims = await authenticate(*args, **kwargs)
        reached.set()
        await finish.wait()
        return claims

    with (
        patch.object(provider.client, "authenticate", side_effect=delayed_authenticate),
        patch.object(provider, "_get_or_create_user", wraps=provider._get_or_create_user) as apply,
    ):
        callback = asyncio.create_task(
            auth.handle_oauth_callback("oidc", "valid-code", pending.state)
        )
        locked = False
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            assert auth.pending_logins.get(pending.state) is None
            await provider.handle_backchannel_logout(logout_token(idp))
            if original_token is not None:
                assert not await token_exists(provider, original_token)
            assert len(await auth.database.get_rows("oidc_logouts")) == 1
            if delay == "user_link_lock":
                await auth.user_link_lock.acquire()
                locked = True
                finish.set()
                await asyncio.wait({callback}, timeout=0.02)
                assert not callback.done()

            await auth.database.execute_write(
                "UPDATE oidc_logouts SET expires_at = :expired", {"expired": time.time() - 1}
            )
            with patch(
                "music_assistant.controllers.webserver.helpers.oidc_sessions.monotonic",
                return_value=pending.expires_at + 1,
            ):
                if locked:
                    auth.user_link_lock.release()
                    locked = False
                finish.set()
                result = await asyncio.wait_for(callback, timeout=5)
        finally:
            if locked:
                auth.user_link_lock.release()
            finish.set()
            callback.cancel()
            await asyncio.gather(callback, return_exceptions=True)

    apply.assert_not_called()
    await _assert_expired_callback(provider, idp, pending, result, existing_user)


@pytest.mark.parametrize("delay", ["exchange", "user_link_lock", "role_lock"])
async def test_original_deadline_cancels_claim_application_waits(
    provider: OIDCLoginProvider, idp: IdentityProvider, delay: str
) -> None:
    """The original deadline includes code exchange and both account mutation locks."""
    provider = configure(provider, '{"members":"user","admins":"admin"}')
    idp.overrides["groups"] = ["members"]
    user, _, _ = await sign_in(provider, idp)
    auth = provider.auth_manager
    pending = _start_pending(provider, idp)
    pending.expires_at = time.monotonic() + 1
    reached = asyncio.Event()
    never_finish = asyncio.Event()
    claims = {
        "sub": "subject-1",
        "sid": "session-one",
        "iat": time.time(),
        "exp": time.time() + 3600,
        "groups": ["admins"],
    }

    async def authenticate(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if delay != "role_lock":
            reached.set()
        if delay == "exchange":
            await never_finish.wait()
        return claims

    sync_role = auth.sync_oidc_user_role

    async def sync(*args: Any, **kwargs: Any) -> Any:
        reached.set()
        return await sync_role(*args, **kwargs)

    lock = auth.user_link_lock if delay == "user_link_lock" else auth._user_role_lock
    if delay != "exchange":
        await lock.acquire()
    with (
        patch.object(provider.client, "authenticate", side_effect=authenticate),
        patch.object(auth, "sync_oidc_user_role", side_effect=sync),
    ):
        callback = asyncio.create_task(
            auth.handle_oauth_callback("oidc", "valid-code", pending.state)
        )
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            result = await asyncio.wait_for(callback, timeout=5)
        finally:
            if delay != "exchange":
                lock.release()
            callback.cancel()
            await asyncio.gather(callback, return_exceptions=True)
    assert not result.success
    assert result.error is not None
    assert "expired" in result.error
    assert pending.oidc_session is None
    persisted = await auth.get_user(user.user_id)
    assert persisted is not None
    assert persisted.role == UserRole.USER


@pytest.mark.parametrize("phase", ["expired", "lock", "validation", "token_write", "session_write"])
async def test_token_issuance_enforces_deadline_and_removes_late_writes(
    provider: OIDCLoginProvider, idp: IdentityProvider, phase: str
) -> None:
    """Expiry while waiting or writing cannot issue a token or remove unrelated sessions."""
    provider = configure(provider)
    user, existing_token, session = await sign_in(provider, idp)
    auth = provider.auth_manager
    local_token = await auth.create_token(user, "local login")
    token_ids = {row["token_id"] for row in await auth.database.get_rows("auth_tokens")}
    session_ids = {row["token_id"] for row in await auth.database.get_rows("oidc_sessions")}
    now = time.monotonic()
    session = replace(session, login_expires_at=now + 600)
    clock = [session.login_expires_at + 1 if phase == "expired" else now]
    insert = auth.database.insert
    query = auth.database.get_rows_from_query
    expire_after_table = {"token_write": "auth_tokens", "session_write": "oidc_sessions"}.get(phase)

    async def delayed_insert(table: str, values: dict[str, Any], **kwargs: Any) -> int:
        result = await insert(table, values, **kwargs)
        if table == expire_after_table:
            clock[0] = session.login_expires_at + 1
        return result

    async def delayed_query(sql: str, *args: Any, **kwargs: Any) -> Any:
        result = await query(sql, *args, **kwargs)
        if phase == "validation" and sql.startswith("SELECT logout_id"):
            clock[0] = session.login_expires_at + 1
        return result

    store = auth.oidc_sessions
    locked = phase == "lock"
    if locked:
        await store._lock.acquire()
    with (
        patch(
            "music_assistant.controllers.webserver.helpers.oidc_sessions.monotonic",
            side_effect=lambda: clock[0],
        ),
        patch.object(auth.database, "insert", side_effect=delayed_insert),
        patch.object(auth.database, "get_rows_from_query", side_effect=delayed_query),
    ):
        task = asyncio.create_task(store.create_token(user, "delayed callback", session))
        try:
            if locked:
                await asyncio.wait({task}, timeout=0.02)
                assert not task.done()
                clock[0] = session.login_expires_at + 1
                store._lock.release()
                locked = False
            with pytest.raises(AuthenticationFailed, match="expired"):
                await asyncio.wait_for(task, timeout=5)
        finally:
            if locked:
                store._lock.release()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert {row["token_id"] for row in await auth.database.get_rows("auth_tokens")} == token_ids
    assert {row["token_id"] for row in await auth.database.get_rows("oidc_sessions")} == session_ids
    assert await token_exists(provider, existing_token)
    assert await token_exists(provider, local_token)


def _start_pending(provider: OIDCLoginProvider, idp: IdentityProvider) -> PendingLogin:
    pending = provider.auth_manager.pending_logins.start(
        "oidc", AuthTransport.DIRECT, idp.pending.redirect_uri, idp.pending.idp_code_verifier
    )
    idp.pending = pending
    return pending


async def _assert_expired_callback(
    provider: OIDCLoginProvider,
    idp: IdentityProvider,
    pending: PendingLogin,
    result: AuthResult,
    existing_user: bool,
) -> None:
    assert not result.success
    assert result.error is not None
    assert "expired" in result.error
    assert pending.oidc_session is None
    auth = provider.auth_manager
    user = await auth.get_user_by_username("listener")
    if existing_user:
        assert user is not None
        assert user.role == UserRole.USER
    else:
        assert user is None
    assert not await auth.database.get_rows("auth_tokens")

    _start_pending(provider, idp)
    fresh_user, fresh_token, _ = await sign_in(provider, idp, "session-two")
    assert fresh_user.role == UserRole.ADMIN
    assert await token_exists(provider, fresh_token)
    assert not await auth.database.get_rows("oidc_logouts")
