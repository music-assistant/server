"""Cancellation must finish accepted revocations and cleanup of unissued credentials."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from music_assistant_models.errors import AuthenticationFailed

from music_assistant.controllers.webserver.helpers.oidc_provider import OIDCLoginProvider

from .conftest import _create_ws_client
from .test_oidc import IdentityProvider, auth_manager, idp, provider
from .test_oidc_lifecycle import configure, logout_token, sign_in, token_exists

__all__ = ["auth_manager", "idp", "provider"]

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.helpers.database import DatabaseConnection


@pytest.mark.parametrize(
    "phase", ["cleanup", "receipt_read", "receipt_write", "selection", "delete"]
)
@pytest.mark.parametrize("after", [False, True])
async def test_denied_mapping_finishes_revocation_after_deadline_at_every_database_stage(
    provider: OIDCLoginProvider, idp: IdentityProvider, phase: str, after: bool
) -> None:
    """A deadline at an earlier logout await cannot preserve existing tokens or sockets."""
    provider = configure(provider, '{"admins":"admin"}')
    user, token, _ = await _admin_sign_in(provider, idp)
    auth = provider.auth_manager
    local = await auth.create_token(user, "local")
    client = _create_ws_client(
        auth.webserver,
        user=user,
        token_id=auth.jwt_helper.decode_token(token)["jti"],
        with_handle_task=True,
    )
    handle_task = client._handle_task
    assert handle_task is not None
    active = asyncio.Event()
    revoke = auth.oidc_sessions.revoke_identity

    async def observed_revoke(*args: Any) -> None:
        active.set()
        await revoke(*args)

    idp.overrides["groups"] = []
    idp.pending.expires_at = time.monotonic() + 1
    with (
        _pause_revocation(auth.database, phase, after, active) as (reached, finish),
        patch.object(auth.oidc_sessions, "revoke_identity", side_effect=observed_revoke),
    ):
        task = asyncio.create_task(
            provider.complete_authorization(idp.pending, {"code": "valid-code"})
        )
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            await asyncio.wait({task}, timeout=1.1)
            assert not task.done()
            assert auth.user_link_lock.locked()
            assert auth.oidc_sessions._lock.locked()
            finish.set()
            with pytest.raises(AuthenticationFailed, match="expired"):
                await asyncio.wait_for(task, timeout=5)
            assert not await token_exists(provider, token)
            assert await token_exists(provider, local)
            assert client._handle_task is not None
            assert client._handle_task.cancelled()
        finally:
            finish.set()
            task.cancel()
            client.cancel()
            await asyncio.gather(task, handle_task, return_exceptions=True)


@pytest.mark.parametrize("lock_name", ["user_link_lock", "session_lock"])
async def test_verified_logout_finishes_after_cancellation_while_waiting_for_a_lock(
    provider: OIDCLoginProvider, idp: IdentityProvider, lock_name: str
) -> None:
    """Once a signed logout is verified, neither account nor session lock waits can drop it."""
    provider = configure(provider)
    _, token, _ = await sign_in(provider, idp)
    auth = provider.auth_manager
    lock = auth.user_link_lock if lock_name == "user_link_lock" else auth.oidc_sessions._lock
    verified = asyncio.Event()
    validate = provider.client.validate_logout_token

    async def observed_validate(*args: Any) -> dict[str, Any]:
        claims = await validate(*args)
        verified.set()
        return claims

    await lock.acquire()
    locked = True
    with patch.object(provider.client, "validate_logout_token", side_effect=observed_validate):
        task = asyncio.create_task(provider.handle_backchannel_logout(logout_token(idp)))
        try:
            await asyncio.wait_for(verified.wait(), timeout=5)
            for _ in range(2):
                task.cancel()
                await asyncio.wait({task}, timeout=0.02)
                assert not task.done()
            lock.release()
            locked = False
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        finally:
            if locked:
                lock.release()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not await token_exists(provider, token)


@pytest.mark.parametrize("table", ["auth_tokens", "oidc_sessions"])
@pytest.mark.parametrize("after", [False, True])
async def test_cancelled_token_issuance_leaves_no_unissued_credentials(
    provider: OIDCLoginProvider, idp: IdentityProvider, table: str, after: bool
) -> None:
    """Cancellation around either persisted token row removes all new credentials."""
    provider = configure(provider)
    user, _, session = await sign_in(provider, idp)
    database = provider.auth_manager.database
    original = {name: await database.get_rows(name) for name in ("auth_tokens", "oidc_sessions")}
    with _pause_call(database, "insert", lambda args: args[0] == table, after) as (reached, finish):
        task = asyncio.create_task(
            provider.auth_manager.oidc_sessions.create_token(user, "unissued", session)
        )
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            task.cancel()
            await asyncio.wait({task}, timeout=0.02)
            task.cancel()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        finally:
            finish.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    for name, rows in original.items():
        assert await database.get_rows(name) == rows


@pytest.mark.parametrize("table", ["auth_tokens", "oidc_sessions"])
@pytest.mark.parametrize("after", [False, True])
async def test_cancellation_during_expired_token_cleanup_finishes_both_deletions(
    provider: OIDCLoginProvider, idp: IdentityProvider, table: str, after: bool
) -> None:
    """An expired issuance cannot lose its cleanup on a second cancellation."""
    provider = configure(provider)
    user, _, session = await sign_in(provider, idp)
    database = provider.auth_manager.database
    original = {name: await database.get_rows(name) for name in ("auth_tokens", "oidc_sessions")}
    now = [time.monotonic()]
    insert = database.insert

    async def expire_after_link(*args: Any, **kwargs: Any) -> int:
        result = await insert(*args, **kwargs)
        if args[0] == "oidc_sessions":
            now[0] = session.login_expires_at + 1
        return result

    with (
        patch(
            "music_assistant.controllers.webserver.helpers.oidc_sessions.monotonic",
            side_effect=lambda: now[0],
        ),
        patch.object(database, "insert", side_effect=expire_after_link),
        _pause_call(database, "delete", lambda args: args[0] == table, after) as (reached, finish),
    ):
        task = asyncio.create_task(
            provider.auth_manager.oidc_sessions.create_token(user, "expired", session)
        )
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            for _ in range(2):
                task.cancel()
                await asyncio.wait({task}, timeout=0.02)
                assert not task.done()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        finally:
            finish.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    for name, rows in original.items():
        assert await database.get_rows(name) == rows


async def test_mapping_denial_waits_for_session_lock_after_deadline(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> None:
    """A known denial must finish revocation even if its first lock wait outlives the login."""
    provider = configure(provider, '{"admins":"admin"}')
    _, token, _ = await _admin_sign_in(provider, idp)
    auth = provider.auth_manager
    store = auth.oidc_sessions
    reached = asyncio.Event()
    revoke = store.revoke_identity
    locked = False

    async def blocked_revoke(*args: Any) -> None:
        nonlocal locked
        await store._lock.acquire()
        locked = True
        reached.set()
        await revoke(*args)

    idp.overrides["groups"] = []
    idp.pending.expires_at = time.monotonic() + 1
    with patch.object(store, "revoke_identity", side_effect=blocked_revoke):
        task = asyncio.create_task(
            provider.complete_authorization(idp.pending, {"code": "valid-code"})
        )
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            await asyncio.wait({task}, timeout=1.1)
            assert not task.done()
            assert auth.user_link_lock.locked()
            store._lock.release()
            locked = False
            with pytest.raises(AuthenticationFailed, match="expired"):
                await asyncio.wait_for(task, timeout=5)
        finally:
            if locked:
                store._lock.release()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not await token_exists(provider, token)


@pytest.mark.parametrize("table", ["auth_tokens", "oidc_sessions"])
async def test_failed_token_write_removes_a_row_that_already_reached_sqlite(
    provider: OIDCLoginProvider, idp: IdentityProvider, table: str
) -> None:
    """A failure reported after persistence still removes the unissued credential and link."""
    provider = configure(provider)
    user, _, session = await sign_in(provider, idp)
    database = provider.auth_manager.database
    original = {name: await database.get_rows(name) for name in ("auth_tokens", "oidc_sessions")}
    insert = database.insert

    async def fail_after_insert(*args: Any, **kwargs: Any) -> int:
        result = await insert(*args, **kwargs)
        if args[0] == table:
            raise RuntimeError("write failed after persistence")
        return result

    with (
        patch.object(database, "insert", side_effect=fail_after_insert),
        pytest.raises(RuntimeError, match="write failed after persistence"),
    ):
        await provider.auth_manager.oidc_sessions.create_token(user, "unissued", session)
    for name, rows in original.items():
        assert await database.get_rows(name) == rows


async def _admin_sign_in(provider: OIDCLoginProvider, idp: IdentityProvider) -> Any:
    idp.overrides["groups"] = ["admins"]
    return await sign_in(provider, idp)


@contextmanager
def _pause_revocation(
    database: DatabaseConnection, phase: str, after: bool, active: asyncio.Event
) -> Iterator[tuple[asyncio.Event, asyncio.Event]]:
    stages: dict[str, tuple[str, Callable[[tuple[Any, ...]], bool]]] = {
        "cleanup": ("execute_write", lambda args: args[0].startswith("DELETE FROM oidc_sessions")),
        "receipt_read": ("get_row", lambda args: args[0] == "oidc_logouts"),
        "receipt_write": ("insert", lambda args: args[0] == "oidc_logouts"),
        "selection": (
            "get_rows_from_query",
            lambda args: args[0].startswith("SELECT token_id FROM oidc_sessions"),
        ),
        "delete": ("execute_write", lambda args: args[0].startswith("DELETE FROM auth_tokens")),
    }
    method, predicate = stages[phase]
    with _pause_call(
        database, method, lambda args: active.is_set() and predicate(args), after
    ) as events:
        yield events


@contextmanager
def _pause_call(
    database: DatabaseConnection,
    method: str,
    predicate: Callable[[tuple[Any, ...]], bool],
    after: bool,
) -> Iterator[tuple[asyncio.Event, asyncio.Event]]:
    reached = asyncio.Event()
    finish = asyncio.Event()
    original = getattr(database, method)

    async def delayed(*args: Any, **kwargs: Any) -> Any:
        if predicate(args) and not reached.is_set():
            result = await original(*args, **kwargs) if after else None
            reached.set()
            await finish.wait()
            return result if after else await original(*args, **kwargs)
        return await original(*args, **kwargs)

    with patch.object(database, method, side_effect=delayed):
        yield reached, finish
