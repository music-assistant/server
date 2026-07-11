"""Tests for external API command authorization."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from music_assistant_models.auth import Scope, User, UserRole
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.constants import GUEST_ACCESS_RESTRICTED_PLAYER_ID
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    check_command_permission,
)
from music_assistant.helpers.api import APICommandHandler

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


async def _noop_command() -> None:
    """Provide a no-op API command target."""


def _handler(command: str, scope: Scope | None) -> APICommandHandler:
    """Create an API command handler for authorization tests."""
    return APICommandHandler.parse(command, _noop_command, required_scope=scope)


def _user(
    role: UserRole = UserRole.GUEST,
    player_filter: list[str] | None = None,
) -> User:
    """Create an authenticated user for authorization tests."""
    if player_filter is None:
        return User(
            user_id=f"{role.value}_id",
            username=f"{role.value}_user",
            role=role,
        )
    return User(
        user_id=f"{role.value}_id",
        username=f"{role.value}_user",
        role=role,
        player_filter=player_filter,
    )


@pytest.mark.parametrize("command", ["players/cmd/test", "player_queues/test"])
@pytest.mark.parametrize("scope", [Scope.PLAYERS_CONTROL, Scope.QUEUES_CONTROL])
def test_managed_guest_is_denied_every_core_control_prefix_and_scope(
    command: str,
    scope: Scope,
) -> None:
    """The receive-only boundary covers both core prefixes and control scopes."""
    with pytest.raises(InsufficientPermissions, match="receive-only"):
        check_command_permission(
            _user(player_filter=[GUEST_ACCESS_RESTRICTED_PLAYER_ID]),
            _handler(command, scope),
        )


@pytest.mark.parametrize(
    ("command", "scope"),
    [
        ("players/all", Scope.PLAYERS_READ),
        ("player_queues/all", Scope.QUEUES_READ),
        ("music_quiz/listen_in", Scope.PLAYERS_CONTROL),
        ("music_quiz/stop_listen_in", Scope.PLAYERS_CONTROL),
        ("music_quiz/can_listen_in", Scope.PLAYERS_CONTROL),
        ("music_quiz/submit_answer", None),
        ("music_quiz/ready", None),
    ],
)
def test_managed_guest_keeps_read_and_provider_command_access(
    command: str,
    scope: Scope | None,
) -> None:
    """Read commands and provider-owned controls remain available."""
    check_command_permission(
        _user(player_filter=[GUEST_ACCESS_RESTRICTED_PLAYER_ID]),
        _handler(command, scope),
    )


@pytest.mark.parametrize(
    "role",
    [UserRole.GUEST, UserRole.USER, UserRole.ADMIN, UserRole.SERVICE],
)
def test_users_without_receive_only_sentinel_keep_control_access(role: UserRole) -> None:
    """Built-in roles retain their existing core control permissions."""
    check_command_permission(_user(role), _handler("players/cmd/play", Scope.PLAYERS_CONTROL))


def test_party_guest_without_sentinel_keeps_control_access() -> None:
    """Party-style guests without a managed filter retain existing behavior."""
    check_command_permission(
        _user(player_filter=[]),
        _handler("player_queues/play", Scope.QUEUES_CONTROL),
    )


CORE_CONTROL_COMMANDS = (
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
)


def _create_jsonrpc_controller(
    handler: APICommandHandler,
    target: AsyncMock,
) -> WebserverController:
    """Create a minimally wired JSON-RPC controller."""
    controller = WebserverController.__new__(WebserverController)
    controller.auth = MagicMock(has_users=True)
    controller.logger = MagicMock()
    controller.mass = MagicMock()
    controller.mass.command_handlers = {handler.command: handler}
    controller.mass.translations.ensure_locale_loaded = AsyncMock()
    handler.target = target
    return controller


def _create_jsonrpc_request(command: str, args: dict[str, object] | None = None) -> MagicMock:
    """Create a JSON-RPC request carrying one API command."""
    request = MagicMock(spec=web.Request)
    request.can_read_body = True
    request.read = AsyncMock(
        return_value=json.dumps(
            {"message_id": "1", "command": command, "args": args or {}}
        ).encode()
    )
    request.headers = {}
    return request


@pytest.mark.parametrize(("command", "scope", "args"), CORE_CONTROL_COMMANDS)
async def test_jsonrpc_managed_guest_core_control_is_denied_before_target(
    command: str,
    scope: Scope,
    args: dict[str, object],
) -> None:
    """JSON-RPC rejects receive-only controls before invoking their target."""
    handler = _handler(command, scope)
    target = AsyncMock()
    controller = _create_jsonrpc_controller(handler, target)
    request = _create_jsonrpc_request(command, args)

    with patch(
        "music_assistant.controllers.webserver.controller.get_authenticated_user",
        new=AsyncMock(return_value=_user(player_filter=[GUEST_ACCESS_RESTRICTED_PLAYER_ID])),
    ):
        response = await controller._handle_jsonrpc_api_command(request)

    assert response.status == 403
    assert "receive-only" in (response.text or "")
    target.assert_not_awaited()


@pytest.mark.parametrize(
    ("command", "scope"),
    [
        ("music_quiz/listen_in", Scope.PLAYERS_CONTROL),
        ("music_quiz/ready", None),
    ],
)
async def test_jsonrpc_managed_guest_provider_command_reaches_target(
    command: str,
    scope: Scope | None,
) -> None:
    """JSON-RPC continues to invoke provider-owned guest commands."""
    handler = _handler(command, scope)
    target = AsyncMock(return_value=None)
    controller = _create_jsonrpc_controller(handler, target)
    request = _create_jsonrpc_request(command)

    with patch(
        "music_assistant.controllers.webserver.controller.get_authenticated_user",
        new=AsyncMock(return_value=_user(player_filter=[GUEST_ACCESS_RESTRICTED_PLAYER_ID])),
    ):
        response = await controller._handle_jsonrpc_api_command(request)

    assert response.status == 200
    target.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("prefix", "control_scope", "non_control_commands"),
    [
        (
            "players/",
            Scope.PLAYERS_CONTROL,
            frozenset(
                {
                    "players/all",
                    "players/get",
                    "players/get_by_name",
                    "players/player_controls",
                    "players/player_control",
                    "players/sleep_timer/get",
                    "players/create_group_player",
                    "players/remove_group_player",
                    "players/add_currently_playing_to_favorites",
                    "players/remove",
                }
            ),
        ),
        (
            "player_queues/",
            Scope.QUEUES_CONTROL,
            frozenset(
                {
                    "player_queues/all",
                    "player_queues/get",
                    "player_queues/items",
                    "player_queues/get_active_queue",
                    "player_queues/save_as_playlist",
                }
            ),
        ),
    ],
)
async def test_registered_core_api_handlers_cannot_bypass_receive_only_boundary(
    mass: MusicAssistant,
    prefix: str,
    control_scope: Scope,
    non_control_commands: frozenset[str],
) -> None:
    """Every registered core command is explicitly classified as control or non-control."""
    handlers = {
        command: handler
        for command, handler in mass.command_handlers.items()
        if command.startswith(prefix)
    }
    assert non_control_commands <= handlers.keys()
    if prefix == "player_queues/":
        assert handlers["player_queues/dont_stop_the_music"].alias is True
    managed_guest = _user(player_filter=[GUEST_ACCESS_RESTRICTED_PLAYER_ID])

    for command, handler in handlers.items():
        if command in non_control_commands:
            assert handler.required_scope != control_scope
            continue
        assert handler.required_scope == control_scope, (
            f"{command} must use {control_scope} or be explicitly classified as non-control"
        )
        with pytest.raises(InsufficientPermissions, match="receive-only"):
            check_command_permission(managed_guest, handler)


def test_webrtc_session_user_lookup_requires_authenticated_match() -> None:
    """WebRTC session lookup never returns an unauthenticated connection."""
    controller = WebserverController.__new__(WebserverController)
    authenticated_user = _user(UserRole.USER)
    unauthenticated_client = MagicMock(_webrtc_session_id="missing-user", _authenticated_user=None)
    authenticated_client = MagicMock(
        _webrtc_session_id="authenticated",
        _authenticated_user=authenticated_user,
    )
    controller.clients = {unauthenticated_client, authenticated_client}

    assert (
        controller.get_authenticated_user_for_webrtc_session("authenticated") is authenticated_user
    )
    assert controller.get_authenticated_user_for_webrtc_session("missing-user") is None
    assert controller.get_authenticated_user_for_webrtc_session("unknown") is None
