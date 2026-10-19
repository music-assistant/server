"""A WebSocket authentication in flight must not outlive a revocation or access change."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.api import CommandMessage, ErrorResultMessage, SuccessResultMessage
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import InvalidToken

from music_assistant.controllers.webserver.auth import AuthenticationManager
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.controllers.webserver.helpers.auth_providers import BuiltinLoginProvider
from music_assistant.controllers.webserver.helpers.oidc_provider import OIDCLoginProvider
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler

from .test_oidc import IdentityProvider, auth_manager, idp, provider
from .test_oidc_lifecycle import configure, logout_token, sign_in

__all__ = ["auth_manager", "idp", "provider"]


@pytest.mark.parametrize("paused_after", ["auth_tokens", "users"])
@pytest.mark.parametrize("change", ["oidc_logout", "token_revoked", "user_disabled"])
async def test_revocation_during_authentication_rejects_the_socket(
    provider: OIDCLoginProvider, idp: IdentityProvider, change: str, paused_after: str
) -> None:
    """A revocation after validation read the token or user cannot leave a live socket."""
    provider = configure(provider)
    user, token, _ = await sign_in(provider, idp)
    auth = provider.auth_manager
    admin = await _local_user(auth, "admin")

    async def revoke() -> None:
        # Set here, after the auth task copied its context, so only the change runs as admin.
        set_current_user(admin)
        if change == "oidc_logout":
            await provider.handle_backchannel_logout(logout_token(idp))
        elif change == "token_revoked":
            await auth.revoke_token(auth.jwt_helper.decode_token(token)["jti"])
        else:
            await auth.disable_user(user.user_id)

    client, sent = await _authenticate_during(auth, token, paused_after, revoke)

    assert client not in auth.webserver.clients
    assert client.authenticated_user is None
    assert client.token_id is None
    assert isinstance(sent[-1], ErrorResultMessage)
    assert sent[-1].error_code == InvalidToken.error_code


async def test_demotion_during_authentication_registers_the_new_role(
    auth_manager: AuthenticationManager,
) -> None:
    """A socket that missed a demotion's disconnect still ends up holding the demoted role."""
    admin = await _local_user(auth_manager, "admin")
    user = await _create_local_user(auth_manager, "bob", UserRole.ADMIN)
    token = await auth_manager.create_token(user, "browser")

    client, sent = await _authenticate_during(
        auth_manager,
        token,
        "users",
        lambda: auth_manager.update_user_role(user.user_id, UserRole.USER, admin),
    )

    assert client in auth_manager.webserver.clients
    assert client.authenticated_user is not None
    assert client.authenticated_user.role == UserRole.USER
    assert isinstance(sent[-1], SuccessResultMessage)
    assert sent[-1].result["user"]["role"] == UserRole.USER


@pytest.mark.parametrize("filters", [["garage"], ["garage", "attic"]])
async def test_filter_changes_during_authentication_apply_to_the_socket(
    auth_manager: AuthenticationManager, filters: list[str]
) -> None:
    """Player filters saved during validation or re-validation all reach the socket."""
    user = await _create_local_user(auth_manager, "carol", UserRole.USER, ["kitchen"])
    token = await auth_manager.create_token(user, "browser")

    client, _ = await _authenticate_during(
        auth_manager,
        token,
        "users",
        *(_set_filter(auth_manager, user, player_id) for player_id in filters),
    )

    assert client in auth_manager.webserver.clients
    assert client.authenticated_user is not None
    assert client.authenticated_user.player_filter == [filters[-1]]


async def _authenticate_during(
    auth: AuthenticationManager,
    token: str,
    paused_after: str,
    *changes: Callable[[], Awaitable[Any]],
) -> tuple[WebsocketClientHandler, list[Any]]:
    """Run the WebSocket auth command, completing one change after each read of a table."""
    client = WebsocketClientHandler(
        auth.webserver, make_mocked_request("GET", "/ws", app=web.Application())
    )
    sender = AsyncMock()
    task = asyncio.create_task(
        client._handle_auth_command(
            CommandMessage(message_id="1", command="auth", args={"token": token})
        )
    )
    # Revocation cancels a registered socket's handle task, which runs its commands.
    client._handle_task = task
    with (
        patch.object(client, "_send_message", sender),
        _pause_after_reads(auth, task, paused_after, len(changes)) as pauses,
    ):
        try:
            for change, (reached, release) in zip(changes, pauses, strict=True):
                await asyncio.wait_for(reached.wait(), timeout=5)
                await asyncio.wait_for(change(), timeout=5)
                release.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            for _, release in pauses:
                release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            client._handle_task = None
            if client._events_unsub_callback:
                client._events_unsub_callback()
    return client, [call.args[0] for call in sender.await_args_list]


@contextmanager
def _pause_after_reads(
    auth: AuthenticationManager, reader: asyncio.Task[Any], paused_table: str, count: int
) -> Iterator[list[tuple[asyncio.Event, asyncio.Event]]]:
    pauses = [(asyncio.Event(), asyncio.Event()) for _ in range(count)]
    pending = iter(pauses)
    get_row = auth.database.get_row

    async def paused_get_row(table: str, match: dict[str, Any]) -> Any:
        row = await get_row(table, match)
        if (
            table == paused_table
            and asyncio.current_task() is reader
            and (pause := next(pending, None))
        ):
            reached, release = pause
            reached.set()
            await release.wait()
        return row

    with patch.object(auth.database, "get_row", side_effect=paused_get_row):
        yield pauses


def _set_filter(
    auth: AuthenticationManager, user: User, player_id: str
) -> Callable[[], Awaitable[Any]]:
    return lambda: auth.update_user_filters(user, [player_id])


async def _create_local_user(
    auth: AuthenticationManager,
    username: str,
    role: UserRole,
    player_filter: list[str] | None = None,
) -> User:
    builtin = auth.login_providers["builtin"]
    assert isinstance(builtin, BuiltinLoginProvider)
    user = await builtin.create_user_with_password(username, "local-test-password", role=role)
    if player_filter is not None:
        user = await auth.update_user_filters(user, player_filter)
    return user


async def _local_user(auth: AuthenticationManager, username: str) -> User:
    user = await auth.get_user_by_username(username)
    assert user is not None
    return user
