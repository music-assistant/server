"""Tests for websocket API command authorization."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.api import CommandMessage, ErrorResultMessage
from music_assistant_models.auth import Scope, User, UserRole
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.constants import GUEST_ACCESS_RESTRICTED_PLAYER_ID
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler
from music_assistant.helpers.api import APICommandHandler


async def _noop_command() -> None:
    """Test command target."""


def _create_client(
    user_role: UserRole | None,
    handler: APICommandHandler,
    player_filter: list[str] | None = None,
) -> Any:
    """Create a minimally wired websocket client handler for dispatch tests."""
    client: Any = WebsocketClientHandler.__new__(WebsocketClientHandler)
    client._logger = MagicMock()
    client.mass = MagicMock()
    client.mass.command_handlers = {handler.command: handler}
    # close the coroutine passed to create_task to avoid "never awaited" warnings
    client.mass.create_task = MagicMock(side_effect=lambda coro, *_: coro.close())
    if user_role is None:
        client._authenticated_user = None
    elif player_filter is None:
        client._authenticated_user = User(user_id="user_1", username="tester", role=user_role)
    else:
        client._authenticated_user = User(
            user_id="user_1",
            username="tester",
            role=user_role,
            player_filter=player_filter,
        )
    client._current_token = "token" if user_role else None
    client._sendspin_player_id = None
    client._send_message = AsyncMock()
    return client


def _command_handler(
    required_scope: Scope | None = None,
    authenticated: bool = True,
    command: str = "test/protected",
) -> APICommandHandler:
    """Create an API command handler with the given auth requirements."""
    return APICommandHandler.parse(
        command,
        _noop_command,
        authenticated=authenticated,
        required_scope=required_scope,
    )


def _sent_error_code(client: Any) -> str | None:
    """Return the error code of the last sent error message, if any."""
    for call in client._send_message.await_args_list:
        message = call.args[0]
        if isinstance(message, ErrorResultMessage):
            return str(message.error_code)
    return None


PERMISSION_DENIED = str(InsufficientPermissions.error_code)


@pytest.mark.asyncio
async def test_user_scoped_command_rejects_guest() -> None:
    """A guest must not be able to invoke commands gated on a user-only scope."""
    client = _create_client(UserRole.GUEST, _command_handler(required_scope=Scope.USERS_INVITE))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) == PERMISSION_DENIED
    client.mass.create_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.USER, UserRole.ADMIN])
async def test_user_scoped_command_allows_user_and_admin(role: UserRole) -> None:
    """Users and admins hold the USERS_INVITE scope used by host commands."""
    client = _create_client(role, _command_handler(required_scope=Scope.USERS_INVITE))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) is None
    client.mass.create_task.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.USER, UserRole.GUEST])
async def test_admin_scoped_command_rejects_non_admin(role: UserRole) -> None:
    """Only admins hold admin-only scopes such as USERS_MANAGE."""
    client = _create_client(role, _command_handler(required_scope=Scope.USERS_MANAGE))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) == PERMISSION_DENIED
    client.mass.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_authenticated_command_without_scope_allows_guest() -> None:
    """Guests may invoke authenticated commands that require no specific scope."""
    client = _create_client(UserRole.GUEST, _command_handler(required_scope=None))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) is None
    client.mass.create_task.assert_called_once()


@pytest.mark.asyncio
async def test_scoped_command_rejects_unauthenticated_socket() -> None:
    """Without authentication a scoped command must not run at all."""
    client = _create_client(None, _command_handler(required_scope=Scope.USERS_INVITE))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) is not None
    client.mass.create_task.assert_not_called()


@pytest.mark.parametrize(
    ("command", "scope", "args"),
    [
        ("players/cmd/play", Scope.PLAYERS_CONTROL, {"player_id": "host"}),
        (
            "players/cmd/set_members",
            Scope.PLAYERS_CONTROL,
            {"player_id": "host", "player_ids_to_add": ["guest_player"]},
        ),
        ("player_queues/clear", Scope.QUEUES_CONTROL, {"queue_id": "host"}),
        (
            "player_queues/shuffle",
            Scope.QUEUES_CONTROL,
            {"queue_id": "host", "shuffle_enabled": True},
        ),
        (
            "player_queues/overlay",
            Scope.QUEUES_CONTROL,
            {"queue_id": "host", "characteristic": "volume", "value": 50},
        ),
    ],
)
async def test_managed_guest_core_control_is_denied_before_dispatch(
    command: str,
    scope: Scope,
    args: dict[str, Any],
) -> None:
    """Managed guests cannot schedule direct core player or queue controls."""
    client = _create_client(
        UserRole.GUEST,
        _command_handler(required_scope=scope, command=command),
        [GUEST_ACCESS_RESTRICTED_PLAYER_ID],
    )

    await client._handle_command(CommandMessage(message_id="1", command=command, args=args))

    assert _sent_error_code(client) == PERMISSION_DENIED
    client.mass.create_task.assert_not_called()


@pytest.mark.parametrize(
    ("command", "scope"),
    [
        ("music_quiz/listen_in", Scope.PLAYERS_CONTROL),
        ("music_quiz/ready", None),
    ],
)
async def test_managed_guest_provider_commands_remain_dispatchable(
    command: str,
    scope: Scope | None,
) -> None:
    """Managed guests may still invoke provider-owned commands."""
    client = _create_client(
        UserRole.GUEST,
        _command_handler(required_scope=scope, command=command),
        [GUEST_ACCESS_RESTRICTED_PLAYER_ID],
    )

    await client._handle_command(CommandMessage(message_id="1", command=command))

    assert _sent_error_code(client) is None
    client.mass.create_task.assert_called_once()
