"""Role writes must close sessions with cached privileges before cancellation escapes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.webserver.helpers.login_flow import AuthTransport, PendingLogin
from music_assistant.controllers.webserver.helpers.oidc_provider import OIDCLoginProvider
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler

from .conftest import _create_ws_client
from .test_oidc import IdentityProvider, auth_manager, idp, provider
from .test_oidc_lifecycle import configure, sign_in

__all__ = ["auth_manager", "idp", "provider"]

if TYPE_CHECKING:
    from music_assistant.controllers.webserver.auth import AuthenticationManager

type AdminClients = tuple[OIDCLoginProvider, User, list[WebsocketClientHandler]]


@pytest.fixture
async def admin_clients(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> AsyncIterator[AdminClients]:
    """Keep two real client handlers with a cached admin and an unrelated recovery client."""
    provider = configure(provider, '{"admins":"admin","members":"user"}')
    idp.overrides["groups"] = ["admins"]
    user, token, _ = await sign_in(provider, idp)
    auth = provider.auth_manager
    recovery = await auth.get_user_by_username("admin")
    assert recovery is not None
    token_id = auth.jwt_helper.decode_token(token)["jti"]
    clients = [
        _create_ws_client(auth.webserver, user=user, token_id=token_id, with_handle_task=True),
        _create_ws_client(auth.webserver, user=user, with_handle_task=True),
        _create_ws_client(auth.webserver, user=recovery, with_handle_task=True),
    ]
    idp.overrides["groups"] = ["members"]
    try:
        yield provider, user, clients
    finally:
        for client in clients:
            client.cancel()
        await asyncio.gather(
            *(client._handle_task for client in clients if client._handle_task),
            return_exceptions=True,
        )


@pytest.mark.parametrize("phase", ["sql", "commit"])
async def test_deadline_during_demotion_closes_cached_admin_sessions(
    admin_clients: AdminClients, idp: IdentityProvider, phase: str
) -> None:
    """A deadline after SQLite applies the role waits for commit and disconnection."""
    provider, user, _ = admin_clients
    auth = provider.auth_manager
    pending = _start_pending(provider, idp)
    pending.expires_at = time.monotonic() + 1
    disconnect = auth.webserver.disconnect_websockets_for_user

    def disconnect_under_lock(user_id: str) -> None:
        assert auth._user_role_lock.locked()
        disconnect(user_id)

    with (
        _paused_role_write(auth, user.user_id, phase) as (reached, finish),
        patch.object(
            auth.webserver, "disconnect_websockets_for_user", side_effect=disconnect_under_lock
        ) as close,
    ):
        callback = asyncio.create_task(
            auth.handle_oauth_callback("oidc", "valid-code", pending.state)
        )
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            row = await auth.database.get_row("users", {"user_id": user.user_id})
            assert row is not None
            assert row["role"] == UserRole.USER
            await asyncio.wait({callback}, timeout=1.1)
            assert not callback.done()
            assert auth._user_role_lock.locked()
            finish.set()
            result = await asyncio.wait_for(callback, timeout=5)
        finally:
            finish.set()
            callback.cancel()
            await asyncio.gather(callback, return_exceptions=True)
        assert not result.success
        assert result.error is not None
        assert "expired" in result.error
        assert pending.oidc_session is None
        await _assert_clients_reconnect_with_demoted_role(admin_clients)

        _start_pending(provider, idp)
        retried = await provider.complete_authorization(idp.pending, {"code": "valid-code"})
        assert retried.role == UserRole.USER
        close.assert_called_once_with(user.user_id)


@pytest.mark.parametrize("actor", ["oidc", "manual"])
@pytest.mark.parametrize("phase", ["sql", "commit"])
async def test_repeated_cancellation_retains_role_lock_through_disconnection(
    admin_clients: AdminClients, idp: IdentityProvider, actor: str, phase: str
) -> None:
    """Repeated cancellation cannot release the role lock while stale sessions survive."""
    provider, user, _ = admin_clients
    auth = provider.auth_manager
    recovery = await auth.get_user_by_username("admin")
    assert recovery is not None
    acquired = asyncio.Event()

    async def competing_role_operation() -> None:
        async with auth._user_role_lock:
            acquired.set()

    with _paused_role_write(auth, user.user_id, phase) as (reached, finish):
        change = asyncio.create_task(
            provider.complete_authorization(idp.pending, {"code": "valid-code"})
            if actor == "oidc"
            else auth.update_user_role(user.user_id, UserRole.USER, recovery)
        )
        competitor = None
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            for _ in range(2):
                change.cancel()
                await asyncio.wait({change}, timeout=0.02)
                assert not change.done()
            competitor = asyncio.create_task(competing_role_operation())
            await asyncio.wait({competitor}, timeout=0.02)
            assert not acquired.is_set()
            assert auth._user_role_lock.locked()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await change
            await asyncio.wait_for(competitor, timeout=5)
        finally:
            finish.set()
            change.cancel()
            await asyncio.gather(change, return_exceptions=True)
            if competitor is not None:
                await asyncio.gather(competitor, return_exceptions=True)
    assert acquired.is_set()
    await _assert_clients_reconnect_with_demoted_role(admin_clients)


async def _assert_clients_reconnect_with_demoted_role(admin_clients: AdminClients) -> None:
    provider, user, clients = admin_clients
    persisted = await provider.auth_manager.get_user(user.user_id)
    assert persisted is not None
    assert persisted.role == UserRole.USER
    for client in clients[:2]:
        assert client.authenticated_user is not None
        assert client.authenticated_user.role == UserRole.ADMIN
        assert client._handle_task is not None
        assert client._handle_task.cancelled()
    assert clients[2]._handle_task is not None
    assert not clients[2]._handle_task.done()


def _start_pending(provider: OIDCLoginProvider, idp: IdentityProvider) -> PendingLogin:
    pending = provider.auth_manager.pending_logins.start(
        "oidc", AuthTransport.DIRECT, idp.pending.redirect_uri, idp.pending.idp_code_verifier
    )
    idp.pending = pending
    return pending


@contextmanager
def _paused_role_write(
    auth: AuthenticationManager, user_id: str, phase: str
) -> Iterator[tuple[asyncio.Event, asyncio.Event]]:
    """Pause after the real SQLite update or its commit, before the caller resumes."""
    reached = asyncio.Event()
    finish = asyncio.Event()
    method = "execute" if phase == "sql" else "update"
    original = getattr(auth.database, method)

    async def delayed(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        matching = (
            args[0].startswith("UPDATE users SET role=")
            if phase == "sql"
            else args[0] == "users" and "role" in args[2]
        )
        if matching and args[1].get("user_id") == user_id:
            reached.set()
            await finish.wait()
        return result

    with patch.object(auth.database, method, side_effect=delayed):
        yield reached, finish
